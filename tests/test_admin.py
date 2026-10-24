"""Раздел «Админ»: тумблеры функций, маппинг статусов, шаблоны, экспорт, диагностика.

Приёмка: каждый тумблер реально управляет своей функцией (телеграм — на моке:
выкл → вызовов нет); маппинг сохраняется и используется поллером; шаблоны —
CRUD плюс панель кнопок у оператора, скрытая при выключенном тумблере; экспорт
CSV (utf-8-sig) и JSON выгружает фильтр целиком; тест-кнопки отвечают по-русски
(«Токен не задан» без токенов); оператор на /admin — 403 «Нет доступа».
Живой сети нет: клиенты — httpx.MockTransport через швы base_url/transport.
"""

import json
import re

import httpx
import pytest
from conftest import body_of, login, make_user, telegram_mock, usedesk_mock

from app.config import reset_settings_cache
from app.models import Template
from app.sync import (
    EVENT_TELEGRAM_NEW,
    EVENT_USEDESK,
    SETTING_SYNC_ERROR_COUNT,
    SETTING_TELEGRAM_ENABLED,
    SETTING_USEDESK_CLOSED_STATUSES,
    SETTING_USEDESK_STATUS_CLOSED,
    enqueue,
    get_setting,
    poll_once,
    reset_clients,
    set_clients,
    set_setting,
)
from app.tickets import close_ticket, create_ticket


def ok_telegram(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


@pytest.fixture()
def no_tokens(db, monkeypatch):
    monkeypatch.setenv("ADMIN_LOGIN", "TEST-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "TEST-pass")
    monkeypatch.setenv("PANEL_SECRET_KEY", "TEST-secret")
    monkeypatch.setenv("USEDESK_API_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    reset_settings_cache()
    yield monkeypatch
    reset_settings_cache()
    reset_clients()


# --- граница прав: каждый /admin-маршрут закрыт require("admin") ---


def test_admin_page_redirects_anonymous_to_login(client, ui):
    response = client.get("/admin", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login"), response.headers["location"]


def test_operator_gets_no_access_on_admin_page(client, ui, session):
    make_user(session, "op-1", "TEST-pass")
    login(client, "op-1", "TEST-pass")
    response = client.get("/admin")
    assert response.status_code == 403
    assert "Нет доступа" in response.text


def test_every_admin_route_requires_admin(client, ui, session):
    make_user(session, "op-2", "TEST-pass")
    login(client, "op-2", "TEST-pass")
    guarded = [
        ("GET", "/admin", None),
        ("POST", "/admin/features/telegram_enabled", {"value": "1"}),
        ("POST", "/admin/settings/mapping", {"usedesk_status_closed": "2",
                                             "usedesk_closed_statuses": "2,3"}),
        ("POST", "/admin/templates/create", {"name": "n", "title_tpl": "t",
                                             "description_tpl": "d"}),
        ("GET", "/admin/templates/1/edit", None),
        ("POST", "/admin/templates/1/edit", {"name": "n", "title_tpl": "t",
                                             "description_tpl": "d"}),
        ("POST", "/admin/templates/1/delete", {}),
        ("GET", "/admin/export.csv", None),
        ("GET", "/admin/export.json", None),
        ("POST", "/admin/diagnostics/reset-errors", {}),
        ("POST", "/admin/diagnostics/test-usedesk", {}),
        ("POST", "/admin/diagnostics/test-telegram", {}),
    ]
    for method, path, data in guarded:
        response = client.request(method, path, data=data, follow_redirects=False)
        assert response.status_code == 403, path


# --- раздел «Функции»: страница, тумблеры, реальное управление ---


def test_admin_page_shows_all_sections(client, ui, session):
    make_user(session, "chief", "TEST-pass", role="admin")
    login(client, "chief", "TEST-pass")
    response = client.get("/admin")
    assert response.status_code == 200
    for heading in ("Функции", "Маппинг статусов", "Шаблоны", "Экспорт", "Диагностика"):
        assert heading in response.text, heading
    assert "Выключена" in response.text


def test_toggle_saves_setting_and_swaps_fragment(client, ui, session):
    make_user(session, "chief", "TEST-pass", role="admin")
    login(client, "chief", "TEST-pass")
    response = client.post(
        "/admin/features/telegram_enabled", data={"value": "1"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert get_setting(SETTING_TELEGRAM_ENABLED, "0") == "1"
    page = client.get("/admin")
    assert "Включена" in page.text
    fragment = client.post(
        "/admin/features/telegram_enabled", data={"value": "0"}, headers={"HX-Request": "true"}
    )
    assert fragment.status_code == 200
    assert "Выключена" in fragment.text
    assert get_setting(SETTING_TELEGRAM_ENABLED, "0") == "0"


def test_toggle_unknown_key_is_404(client, ui, session):
    make_user(session, "chief", "TEST-pass", role="admin")
    login(client, "chief", "TEST-pass")
    known = client.post(
        "/admin/features/telegram_enabled", data={"value": "1"}, follow_redirects=False
    )
    assert known.status_code == 303, "известный ключ тумблера обязан работать"
    unknown = client.post(
        "/admin/features/warp_drive", data={"value": "1"}, follow_redirects=False
    )
    assert unknown.status_code == 404


def test_telegram_toggle_controls_poller_on_mock(ui, client, session):
    sent: list[httpx.Request] = []

    def tg_handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"ok": True})

    set_clients(
        telegram=telegram_mock(tg_handler),
        usedesk=usedesk_mock(lambda _: httpx.Response(200, json=[])),
    )
    try:
        chief = make_user(session, "chief", "TEST-pass", role="admin")
        login(client, "chief", "TEST-pass")
        ticket = create_ticket("заявка с телеграмом", "описание", None, chief)
        enqueue(ticket.id, {EVENT_TELEGRAM_NEW})
        poll_once()
        assert sent == [], "тумблер выкл — вызовов телеграма нет"
        response = client.post(
            "/admin/features/telegram_enabled", data={"value": "1"}, follow_redirects=False
        )
        assert response.status_code == 303
        poll_once()
        assert len(sent) == 1, "тумблер вкл — sendMessage ушёл"
        assert "/sendMessage" in str(sent[0].url)
    finally:
        reset_clients()


def test_usedesk_toggle_gates_poller_on_mock(ui, client, session):
    ud_calls: list[httpx.Request] = []

    def ud_handler(request: httpx.Request) -> httpx.Response:
        ud_calls.append(request)
        if request.url.path == "/create/ticket":
            return httpx.Response(200, json={"status": "success", "ticket_id": 42})
        return httpx.Response(200, json=[])

    set_clients(usedesk=usedesk_mock(ud_handler), telegram=telegram_mock(ok_telegram))
    try:
        chief = make_user(session, "chief", "TEST-pass", role="admin")
        login(client, "chief", "TEST-pass")
        ticket = create_ticket("заявка в usedesk", "описание", None, chief)
        enqueue(ticket.id, {EVENT_USEDESK})
        poll_once()
        assert ud_calls == [], "тумблер выкл — запросов к UseDesk нет"
        response = client.post(
            "/admin/features/usedesk_enabled", data={"value": "1"}, follow_redirects=False
        )
        assert response.status_code == 303
        poll_once()
        assert any(request.url.path == "/create/ticket" for request in ud_calls)
    finally:
        reset_clients()


# --- маппинг статусов: сохранение и использование поллером ---


def test_mapping_save_changes_outgoing_status(ui, client, session):
    updates: list[dict] = []

    def ud_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/update/ticket":
            updates.append(body_of(request))
            return httpx.Response(200, json={"status": "success"})
        if request.url.path == "/create/ticket":
            return httpx.Response(200, json={"status": "success", "ticket_id": 42})
        return httpx.Response(200, json={"status": "success"})

    set_clients(usedesk=usedesk_mock(ud_handler))
    try:
        chief = make_user(session, "chief", "TEST-pass", role="admin")
        login(client, "chief", "TEST-pass")
        response = client.post(
            "/admin/settings/mapping",
            data={"usedesk_status_closed": "7", "usedesk_closed_statuses": "7,9"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert get_setting(SETTING_USEDESK_STATUS_CLOSED, "2") == "7"
        assert get_setting(SETTING_USEDESK_CLOSED_STATUSES, "2,3,4,7,10") == "7,9"
        assert client.post(
            "/admin/features/usedesk_enabled", data={"value": "1"}, follow_redirects=False
        ).status_code == 303
        ticket = create_ticket("закрываемая заявка", "текст", None, chief)
        close_ticket(ticket.id, "готово", chief)
        enqueue(ticket.id, {EVENT_USEDESK})
        poll_once()
        assert updates and updates[0].get("status") == "7", "поллер шлёт статус из маппинга"
    finally:
        reset_clients()


def test_mapping_invalid_value_rejected(ui, client, session):
    make_user(session, "chief", "TEST-pass", role="admin")
    login(client, "chief", "TEST-pass")
    set_setting(SETTING_USEDESK_STATUS_CLOSED, "2")
    set_setting(SETTING_USEDESK_CLOSED_STATUSES, "2,3,4,7,10")
    response = client.post(
        "/admin/settings/mapping",
        data={"usedesk_status_closed": "abc", "usedesk_closed_statuses": "2,3"},
    )
    assert response.status_code == 200
    assert "число" in response.text
    assert get_setting(SETTING_USEDESK_STATUS_CLOSED, "2") == "2"
    response = client.post(
        "/admin/settings/mapping",
        data={"usedesk_status_closed": "2", "usedesk_closed_statuses": "2;x"},
    )
    assert "запятую" in response.text
    assert get_setting(SETTING_USEDESK_CLOSED_STATUSES, "2,3,4,7,10") == "2,3,4,7,10"


# --- шаблоны: CRUD ---


def test_template_crud_flow(client, ui, session):
    make_user(session, "chief", "TEST-pass", role="admin")
    login(client, "chief", "TEST-pass")
    response = client.post(
        "/admin/templates/create",
        data={"name": "Обновление баз", "title_tpl": "Обновление баз [ДАТА]",
              "description_tpl": "Просьба обновить базы."},
        follow_redirects=False,
    )
    assert response.status_code == 303
    row = session.query(Template).one()
    assert row.title_tpl == "Обновление баз [ДАТА]"
    assert "Обновление баз" in client.get("/templates").text
    response = client.post(
        f"/admin/templates/{row.id}/edit",
        data={"name": "Обновление баз v2", "title_tpl": "Обновление баз [ДАТА] v2",
              "description_tpl": "Текст"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    session.expire_all()
    assert session.query(Template).one().name == "Обновление баз v2"
    response = client.post(
        f"/admin/templates/{row.id}/delete", data={}, follow_redirects=False
    )
    assert response.status_code == 303
    assert session.query(Template).count() == 0


def test_template_validation_empty_fields(client, ui, session):
    make_user(session, "chief", "TEST-pass", role="admin")
    login(client, "chief", "TEST-pass")
    response = client.post(
        "/admin/templates/create", data={"name": "", "title_tpl": "", "description_tpl": ""}
    )
    assert response.status_code == 200
    assert "Укажите название шаблона" in response.text
    assert session.query(Template).count() == 0


# --- панель шаблонов у оператора ---


def test_template_panel_hidden_when_disabled_and_fills_when_enabled(ui, client, session):
    make_user(session, "chief", "TEST-pass", role="admin")
    make_user(session, "op", "TEST-pass")
    session.add(
        Template(name="Обновление баз", title_tpl="Обновление баз [ДАТА]",
                 description_tpl="Просьба обновить базы.")
    )
    session.commit()
    set_setting("templates_enabled", "0")
    login(client, "op", "TEST-pass")
    new_page = client.get("/tickets/new")
    assert 'hx-get="/tickets/template-panel"' in new_page.text
    assert client.get("/tickets/template-panel").text == "", "тумблер выкл — панель скрыта"
    login(client, "chief", "TEST-pass")
    assert client.post(
        "/admin/features/templates_enabled", data={"value": "1"}, follow_redirects=False
    ).status_code == 303
    panel = client.get("/tickets/template-panel")
    assert "Обновление баз" in panel.text
    assert 'data-title="Обновление баз [ДАТА]"' in panel.text
    assert 'data-description="Просьба обновить базы."' in panel.text


# --- экспорт: CSV utf-8-sig и JSON, фильтр целиком ---


def test_export_csv_bom_and_filter(ui, client, session):
    actor = make_user(session, "op", "TEST-pass")
    create_ticket("Заявка про енота", "текст один", None, actor)
    create_ticket("Заявка про енота два", "текст два", None, actor)
    other = create_ticket("Совсем другое дело", "текст три", None, actor)
    close_ticket(other.id, None, actor)
    make_user(session, "chief", "TEST-pass", role="admin")
    login(client, "chief", "TEST-pass")
    response = client.get("/admin/export.csv", params={"status": "open", "q": "енот"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers.get("content-disposition", "")
    body = response.content.decode("utf-8")
    assert body.startswith("\ufeff"), "CSV начинается с BOM utf-8-sig"
    assert "Заявка про енота" in body
    assert "текст один" in body
    assert "Совсем другое" not in body, "фильтр статуса отсёк закрытую заявку"


def test_export_json_full_filter(ui, client, session):
    actor = make_user(session, "op", "TEST-pass")
    for index in range(35):
        create_ticket(f"енот bulk {index}", "описание", None, actor)
    create_ticket("прочее", "не про то", None, actor)
    make_user(session, "chief", "TEST-pass", role="admin")
    login(client, "chief", "TEST-pass")
    response = client.get("/admin/export.json", params={"q": "енот"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    rows = json.loads(response.content.decode("utf-8"))
    assert isinstance(rows, list)
    assert len(rows) == 35, "экспорт выгружает фильтр целиком, без пагинации"
    numbers = {row["number"] for row in rows}
    assert len(numbers) == 35
    assert all("енот" in row["title"] for row in rows)


# --- диагностика: счётчик ошибок и тест-кнопки ---


def test_diagnostics_error_counter_visible_and_reset(client, ui, session):
    make_user(session, "chief", "TEST-pass", role="admin")
    login(client, "chief", "TEST-pass")
    set_setting(SETTING_SYNC_ERROR_COUNT, "4")
    page = client.get("/admin")
    shown = re.search(r'id="sync-error-count"[^>]*>(\d+)</span>', page.text)
    assert shown is not None and shown.group(1) == "4"
    response = client.post("/admin/diagnostics/reset-errors", data={}, follow_redirects=False)
    assert response.status_code == 303
    assert get_setting(SETTING_SYNC_ERROR_COUNT, "0") == "0"
    shown_after = re.search(r'id="sync-error-count"[^>]*>(\d+)</span>', client.get("/admin").text)
    assert shown_after is not None and shown_after.group(1) == "0"


def test_probes_without_tokens_say_token_missing(client, no_tokens, session):
    make_user(session, "chief", "TEST-pass", role="admin")
    login(client, "chief", "TEST-pass")
    usedesk = client.post("/admin/diagnostics/test-usedesk", data={})
    assert "Токен не задан" in usedesk.text
    telegram = client.post("/admin/diagnostics/test-telegram", data={})
    assert "Токен не задан" in telegram.text


def test_telegram_probe_without_chat_id_names_the_variable(client, ui, session):
    ui.setenv("TELEGRAM_CHAT_ID", "")
    reset_settings_cache()
    make_user(session, "chief", "TEST-pass", role="admin")
    login(client, "chief", "TEST-pass")
    response = client.post("/admin/diagnostics/test-telegram", data={})
    assert "Не задан chat_id" in response.text
    assert "TELEGRAM_CHAT_ID" in response.text


def test_usedesk_probe_reports_in_russian(client, ui, session, monkeypatch):
    make_user(session, "chief", "TEST-pass", role="admin")
    login(client, "chief", "TEST-pass")
    monkeypatch.setattr(
        "app.routes.admin._usedesk_probe_client",
        lambda token: usedesk_mock(lambda _: httpx.Response(200, json=[])),
    )
    response = client.post("/admin/diagnostics/test-usedesk", data={})
    assert response.status_code == 200
    assert "Подключение ок" in response.text
    monkeypatch.setattr(
        "app.routes.admin._usedesk_probe_client",
        lambda token: usedesk_mock(lambda _: httpx.Response(500, text="boom")),
    )
    response = client.post("/admin/diagnostics/test-usedesk", data={})
    assert "Ошибка" in response.text
    monkeypatch.setattr(
        "app.routes.admin._usedesk_probe_client",
        lambda token: usedesk_mock(lambda _: httpx.Response(200, json=[])),
    )
    fragment = client.post(
        "/admin/diagnostics/test-usedesk", data={}, headers={"HX-Request": "true"}
    )
    assert fragment.status_code == 200
    assert "Подключение ок" in fragment.text


def test_telegram_probe_reports_in_russian(client, ui, session, monkeypatch):
    make_user(session, "chief", "TEST-pass", role="admin")
    login(client, "chief", "TEST-pass")
    monkeypatch.setattr(
        "app.routes.admin._telegram_probe_client", lambda token: telegram_mock(ok_telegram)
    )
    response = client.post("/admin/diagnostics/test-telegram", data={})
    assert response.status_code == 200
    assert "Тестовое сообщение отправлено" in response.text
    monkeypatch.setattr(
        "app.routes.admin._telegram_probe_client",
        lambda token: telegram_mock(lambda _: httpx.Response(500, text="boom")),
    )
    response = client.post("/admin/diagnostics/test-telegram", data={})
    assert "Ошибка" in response.text
    assert "Тестовое сообщение отправлено" not in response.text


def test_removed_toggles_not_accepted(client, ui, session):
    """Тумблеры «Сводка-счётчики» и «Фильтр по дате» сняты (волна 3): блоки видны всегда."""
    make_user(session, "TEST-admin", "TEST-pass", role="admin")
    login(client, "TEST-admin")
    for key in ("summary_enabled", "datefilter_enabled"):
        response = client.post(f"/admin/features/{key}", data={"value": "0"})
        assert response.status_code == 404
    page = client.get("/tickets")
    assert "Открыто:" in page.text and 'name="date_from"' in page.text

