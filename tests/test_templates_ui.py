"""Шаблоны от операторов (волна 4): личные и общие, права, инструкция, миграция колонок."""
from conftest import CsrfClient, login, make_user, seed_ticket
from sqlalchemy import create_engine, inspect, text

from app.db import init_db
from app.main import app
from app.models import Template
from app.sync import SETTING_TEMPLATES_ENABLED, get_setting, set_setting


def _create(client, name: str, shared: bool = False, **extra):
    data = {
        "name": name,
        "title_tpl": extra.get("title", "Обновить {база}"),
        "description_tpl": extra.get("description", "на крайний"),
    }
    if shared:
        data["shared"] = "1"
    return client.post("/templates/create", data=data, follow_redirects=False)


def test_templates_page_for_operator_with_howto(client, ui, session):
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    html = client.get("/templates").text
    assert "Как сделать шаблон" in html
    assert "Показывать всем" in html
    assert 'href="/templates"' in client.get("/tickets").text, "вкладка «Шаблоны» в шапке"


def test_operator_personal_template_visible_only_to_author(client, ui, session):
    make_user(session, "TEST-a")
    make_user(session, "TEST-b")
    login(client, "TEST-a")
    response = _create(client, "Мой личный")
    assert response.status_code == 303
    assert response.headers["location"] == "/templates?done=created"
    assert "Мой личный" in client.get("/tickets/template-panel").text

    other = CsrfClient(app)
    login(other, "TEST-b")
    assert "Мой личный" not in other.get("/tickets/template-panel").text
    assert "Мой личный" not in other.get("/templates").text
    row = session.query(Template).one()
    assert other.get(f"/templates/{row.id}/edit").status_code == 404


def test_shared_template_visible_to_all_but_edited_by_author(client, ui, session):
    make_user(session, "TEST-a")
    make_user(session, "TEST-b")
    login(client, "TEST-a")
    _create(client, "Общий от А", shared=True)
    row = session.query(Template).one()

    other = CsrfClient(app)
    login(other, "TEST-b")
    assert "Общий от А" in other.get("/tickets/template-panel").text
    assert other.get(f"/templates/{row.id}/edit").status_code == 403
    assert other.post(f"/templates/{row.id}/delete").status_code == 403
    assert session.query(Template).count() == 1


def test_admin_edits_shared_but_not_personal(client, ui, session):
    make_user(session, "TEST-op")
    make_user(session, "TEST-chief", role="admin")
    op = CsrfClient(app)
    login(op, "TEST-op")
    _create(op, "Личный оператора")
    _create(op, "Общий оператора", shared=True)
    personal = session.query(Template).filter_by(name="Личный оператора").one()
    shared = session.query(Template).filter_by(name="Общий оператора").one()

    login(client, "TEST-chief")
    assert client.get(f"/templates/{shared.id}/edit").status_code == 200
    assert client.get(f"/templates/{personal.id}/edit").status_code == 404
    assert client.get(f"/admin/templates/{personal.id}/edit").status_code == 404
    response = client.post(
        f"/templates/{shared.id}/edit",
        data={"name": "Общий v2", "title_tpl": "t", "description_tpl": "d"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    session.expire_all()
    edited = session.get(Template, shared.id)
    assert edited.name == "Общий v2" and edited.is_shared, "админ не снимает «для всех» чужого"


def test_author_can_share_and_unshare_and_delete(client, ui, session):
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    _create(client, "Свой")
    row = session.query(Template).one()
    client.post(
        f"/templates/{row.id}/edit",
        data={"name": "Свой", "title_tpl": "t", "description_tpl": "d", "shared": "1"},
    )
    session.expire_all()
    assert session.get(Template, row.id).is_shared
    response = client.post(f"/templates/{row.id}/delete", follow_redirects=False)
    assert response.headers["location"] == "/templates?done=deleted"
    assert session.query(Template).count() == 0


def test_validation_and_auto_enable(client, ui, session):
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    set_setting(SETTING_TEMPLATES_ENABLED, "0")
    empty = {"name": "", "title_tpl": "", "description_tpl": ""}
    bad = client.post("/templates/create", data=empty)
    assert "Укажите название шаблона" in bad.text
    long_name = _create(client, "x" * 200)
    assert "Название длиннее" in long_name.text
    _create(client, "Ок")
    assert get_setting(SETTING_TEMPLATES_ENABLED) == "1", "первый шаблон включает кнопки"


def test_save_as_template_from_ticket_for_operator(client, ui, session):
    make_user(session, "TEST-op")
    ticket = seed_ticket(session, number=5, title="Обновить 5746", description="на крайний")
    login(client, "TEST-op")
    html = client.get(f"/templates?from={ticket.id}").text
    assert 'value="Обновить 5746"' in html
    assert "на крайний</textarea>" in html
    assert f"/templates?from={ticket.id}" in client.get("/tickets").text
    assert client.get("/templates?from=abc").status_code == 200


def test_old_admin_save_link_redirects(client, ui, session):
    make_user(session, "TEST-chief", role="admin")
    login(client, "TEST-chief")
    response = client.get("/admin?tpl_from=7", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/templates?from=7"


def test_panel_shows_hint_when_no_templates(client, ui, session):
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    panel = client.get("/tickets/template-panel").text
    assert "шаблонов пока нет" in panel and 'href="/templates#howto"' in panel


def test_additive_migration_adds_template_columns(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.sqlite'}")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE templates (id INTEGER PRIMARY KEY, name VARCHAR(128), "
            "title_tpl TEXT, description_tpl TEXT)"
        ))
        connection.execute(text(
            "INSERT INTO templates (name, title_tpl, description_tpl) VALUES ('old', 't', 'd')"
        ))
    init_db(engine)
    init_db(engine)  # повторный старт ничего не ломает
    columns = {column["name"] for column in inspect(engine).get_columns("templates")}
    assert {"created_by", "is_shared"} <= columns
    with engine.connect() as connection:
        row = connection.execute(text("SELECT created_by, is_shared FROM templates")).one()
    assert row.created_by is None and row.is_shared == 1, "старые шаблоны остаются общими"
