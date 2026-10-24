"""Админка, волна 3: пользователи (активация, роль, правило пароля), шаблоны
(авто-включение, примеры, «Сохранить как шаблон»), состояние синка, меню разделов."""
import httpx
from conftest import login, make_user, usedesk_mock
from sqlalchemy import func, select

from app import sync
from app.models import Template, User
from app.sync import (
    SETTING_SYNC_LAST_ERROR,
    SETTING_SYNC_LAST_POLL,
    SETTING_TEMPLATES_ENABLED,
    SETTING_USEDESK_API_TOKEN,
    SETTING_USEDESK_ENABLED,
    get_setting,
    set_setting,
)


def _admin(client, session) -> User:
    admin = make_user(session, "TEST-chief", "TEST-pass", role="admin")
    login(client, "TEST-chief")
    return admin


# --- меню разделов и чистка ---


def test_admin_has_section_menu_and_no_duplicate_nav(client, ui, session):
    _admin(client, session)
    html = client.get("/admin").text
    for anchor in ("#appearance", "#features", "#usedesk", "#templates", "#export",
                   "#diagnostics"):
        assert f'href="{anchor}"' in html
    assert ">К заявкам<" not in html
    assert "Сводка-счётчики" not in html and "Фильтр по дате" not in html


def test_summary_and_period_filter_always_shown(client, ui, session):
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    html = client.get("/tickets").text
    assert "Открыто:" in html and "Период исполнения" in html


# --- пользователи ---


def test_activate_deactivated_user(client, ui, session):
    _admin(client, session)
    op = make_user(session, "TEST-op", active=False)
    response = client.post(f"/users/{op.id}/activate", follow_redirects=False)
    assert response.status_code == 303
    session.expire_all()
    assert session.get(User, op.id).is_active


def test_change_role_and_guards(client, ui, session):
    admin = _admin(client, session)
    op = make_user(session, "TEST-op")
    client.post(f"/users/{op.id}/role", data={"role": "admin"})
    session.expire_all()
    assert session.get(User, op.id).role == "admin"
    own = client.post(f"/users/{admin.id}/role", data={"role": "operator"})
    assert "Свою роль поменять нельзя" in own.text
    bad = client.post(f"/users/{op.id}/role", data={"role": "root"})
    assert "Неизвестная роль" in bad.text


def test_demote_inactive_admin_allowed(client, ui, session):
    _admin(client, session)
    other = make_user(session, "TEST-other-admin", role="admin", active=False)
    response = client.post(f"/users/{other.id}/role", data={"role": "operator"})
    assert response.status_code in (200, 303)  # неактивного понижать можно
    session.expire_all()
    assert session.get(User, other.id).role == "operator"


def test_admin_set_password_rule_8_chars(client, ui, session):
    _admin(client, session)
    op = make_user(session, "TEST-op")
    short = client.post(f"/users/{op.id}/password", data={"password": "TEST-1"})
    assert "короче 8" in short.text
    created = client.post("/users/create", data={"login": "TEST-new", "password": "TEST-1"})
    assert "короче 8" in created.text


def test_users_page_has_topbar_on_error(client, ui, session):
    _admin(client, session)
    html = client.post("/users/create", data={"login": "", "password": ""}).text
    assert 'class="topbar"' in html


# --- шаблоны ---


def test_first_template_turns_buttons_on(client, ui, session):
    _admin(client, session)
    set_setting(SETTING_TEMPLATES_ENABLED, "0")
    client.post(
        "/admin/templates/create",
        data={"name": "TEST-tpl", "title_tpl": "Обновить {база}", "description_tpl": "d"},
    )
    assert get_setting(SETTING_TEMPLATES_ENABLED) == "1"
    panel = client.get("/tickets/template-panel").text
    assert "TEST-tpl" in panel


def test_example_templates_only_when_empty(client, ui, session):
    _admin(client, session)
    client.post("/admin/templates/examples")
    client.post("/admin/templates/examples")
    count = session.scalar(select(func.count()).select_from(Template))
    assert count == 3, "повторное нажатие не плодит дубли"
    assert "{база}" in client.get("/tickets/template-panel").text


# --- состояние синка ---


def test_poller_records_last_poll_and_sanitized_error(client, ui, session):
    set_setting(SETTING_USEDESK_API_TOKEN, "TEST-secret-token")
    set_setting(SETTING_USEDESK_ENABLED, "1")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom TEST-secret-token")

    sync.set_clients(usedesk=usedesk_mock(handler))
    try:
        sync.poll_once()
        sync.poll_once()
    finally:
        sync.reset_clients()
    assert get_setting(SETTING_SYNC_LAST_POLL)
    last_error = get_setting(SETTING_SYNC_LAST_ERROR)
    assert last_error and "TEST-secret-token" not in last_error

    _admin(client, session)
    html = client.get("/admin").text
    assert "Последний цикл поллера" in html
    assert "Ждут отправки" in html
    assert "TEST-secret-token" not in html
