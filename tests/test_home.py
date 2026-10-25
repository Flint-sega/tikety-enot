"""Главная-меню: «/» — три кнопки-раздела, список — на «/tickets»."""
from conftest import make_user


def test_home_requires_login(client, ui):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_home_menu_buttons(client, ui, session):
    make_user(session, "operator-home", "TEST-pass")
    client.get("/login")
    client.post("/login", data={"login": "operator-home", "password": "TEST-pass"})
    response = client.get("/")
    assert response.status_code == 200
    assert "Сводка смены" in response.text
    assert "Открытые заявки" in response.text
    assert "История закрытых" in response.text
    assert "Создать заявку" in response.text
    assert 'href="/tickets?status=open"' in response.text
    assert 'href="/tickets?status=closed"' in response.text
    assert 'href="/tickets/new"' in response.text


def test_tickets_list_on_own_path(client, ui, session):
    make_user(session, "operator-list", "TEST-pass")
    client.get("/login")
    client.post("/login", data={"login": "operator-list", "password": "TEST-pass"})
    response = client.get("/tickets?status=open")
    assert response.status_code == 200
    assert "Открытые" in response.text
    assert "Сводка смены" not in response.text
    assert "На главную" in response.text
