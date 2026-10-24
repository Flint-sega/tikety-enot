"""Шов управления пользователями: граница прав (require), создание операторов,
смена пароля, деактивация (R05.1, R05.2, R22i).

Тесты идут через HTTP (TestClient) как админ и оператор; пароли — TEST-значения.
"""

from conftest import login, make_user
from sqlalchemy.orm import Session

from app.auth import verify_password
from app.models import Ticket, User


def test_users_page_redirects_anonymous_to_login(client, ui):
    response = client.get("/users", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login"), response.headers["location"]


def test_operator_gets_no_access_screen_on_users(client, ui, session):
    make_user(session, "operator-1", "TEST-pass")
    login(client, "operator-1", "TEST-pass")
    response = client.get("/users")
    assert response.status_code == 403
    assert "Нет доступа" in response.text
    assert "operator-1" not in response.text.split("Нет доступа")[0][-200:]


def test_operator_gets_no_access_on_admin_actions(client, ui, session):
    make_user(session, "operator-2", "TEST-pass")
    login(client, "operator-2", "TEST-pass")
    for path, data in (
        ("/users/create", {"login": "TEST-new", "password": "TEST-p"}),
        ("/users/1/password", {"password": "TEST-p"}),
        ("/users/1/deactivate", {}),
    ):
        response = client.post(path, data=data, follow_redirects=False)
        assert response.status_code == 403, path


def test_admin_sees_users_page_with_list(client, ui, session):
    make_user(session, "chief", "TEST-pass", role="admin")
    make_user(session, "operator-3", "TEST-pass")
    login(client, "chief", "TEST-pass")
    response = client.get("/users")
    assert response.status_code == 200
    assert "chief" in response.text
    assert "operator-3" in response.text


def test_admin_creates_operator(client, ui, session):
    make_user(session, "chief", "TEST-pass", role="admin")
    login(client, "chief", "TEST-pass")
    response = client.post(
        "/users/create", data={"login": "new-op", "password": "TEST-op-pass"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    created = session.query(User).filter(User.login == "new-op").one()
    assert created.role == "operator"
    assert created.is_active is True
    page = client.get("/users")
    assert "new-op" in page.text


def test_duplicate_login_rejected_with_russian_error(client, ui, session):
    make_user(session, "chief", "TEST-pass", role="admin")
    make_user(session, "taken", "TEST-pass")
    login(client, "chief", "TEST-pass")
    response = client.post("/users/create", data={"login": "taken", "password": "TEST-p"})
    assert response.status_code == 200
    assert "Логин уже занят" in response.text
    assert 'value="taken"' in response.text
    assert session.query(User).filter(User.login == "taken").count() == 1


def test_create_with_empty_fields_shows_error(client, ui, session):
    make_user(session, "chief", "TEST-pass", role="admin")
    login(client, "chief", "TEST-pass")
    response = client.post("/users/create", data={"login": "", "password": ""})
    assert response.status_code == 200
    assert "Укажите логин и пароль" in response.text
    assert session.query(User).count() == 1


def test_change_password_switches_credentials(client, ui, db, session):
    make_user(session, "chief", "TEST-pass", role="admin")
    operator = make_user(session, "operator-4", "TEST-old-pass")
    login(client, "chief", "TEST-pass")
    response = client.post(
        f"/users/{operator.id}/password", data={"password": "TEST-new-pass"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(db) as fresh_session:
        fresh = fresh_session.query(User).filter(User.login == "operator-4").one()
        assert verify_password("TEST-new-pass", fresh.password_hash) is True
        assert verify_password("TEST-old-pass", fresh.password_hash) is False


def test_deactivated_operator_cannot_login_but_stays_in_history(client, ui, db, session):
    make_user(session, "chief", "TEST-pass", role="admin")
    operator = make_user(session, "operator-5", "TEST-pass")
    session.add(Ticket(number=1, title="старая заявка", created_by=operator.id))
    session.commit()
    login(client, "chief", "TEST-pass")
    response = client.post(
        f"/users/{operator.id}/deactivate", data={}, follow_redirects=False
    )
    assert response.status_code == 303
    with Session(db) as fresh_session:
        fresh = fresh_session.query(User).filter(User.login == "operator-5").one()
        assert fresh.is_active is False
        assert fresh.id == fresh_session.query(Ticket.created_by).filter(
            Ticket.number == 1
        ).scalar()
    client.post("/logout")
    blocked = client.post(
        "/login", data={"login": "operator-5", "password": "TEST-pass"}
    )
    assert blocked.status_code == 200
    assert "Неверный логин или пароль" in blocked.text


def test_create_with_too_long_password_shows_russian_error(client, ui, session):
    make_user(session, "chief", "TEST-pass", role="admin")
    login(client, "chief", "TEST-pass")
    response = client.post(
        "/users/create", data={"login": "long-op", "password": "TEST-" + "x" * 100}
    )
    assert response.status_code == 200
    assert "72 байт" in response.text
    assert 'value="long-op"' in response.text
    assert session.query(User).filter(User.login == "long-op").count() == 0


def test_create_accepts_password_exactly_72_bytes(client, ui, session):
    make_user(session, "chief", "TEST-pass", role="admin")
    login(client, "chief", "TEST-pass")
    response = client.post(
        "/users/create",
        data={"login": "edge-op", "password": "TEST-" + "x" * 67},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert session.query(User).filter(User.login == "edge-op").count() == 1


def test_change_password_rejects_too_long(client, ui, db, session):
    make_user(session, "chief", "TEST-pass", role="admin")
    operator = make_user(session, "operator-long", "TEST-old-pass")
    login(client, "chief", "TEST-pass")
    response = client.post(
        f"/users/{operator.id}/password", data={"password": "TEST-" + "x" * 100}
    )
    assert response.status_code == 200
    assert "72 байт" in response.text
    with Session(db) as fresh_session:
        fresh = fresh_session.query(User).filter(User.login == "operator-long").one()
        assert verify_password("TEST-old-pass", fresh.password_hash) is True


def test_admin_cannot_deactivate_self(client, ui, session):
    chief = make_user(session, "chief", "TEST-pass", role="admin")
    login(client, "chief", "TEST-pass")
    response = client.post(
        f"/users/{chief.id}/deactivate", data={}, follow_redirects=False
    )
    assert response.status_code == 200
    assert "Нельзя деактивировать себя" in response.text
    fresh = session.query(User).filter(User.login == "chief").one()
    assert fresh.is_active is True
