"""Раздел «Ключи и функции» (T13): токены в Setting, геттер «настройка -> env».

Приёмка: ключ, вписанный в UI, подхватывается без рестарта — тест-кнопка и
поллер идут с ним (мок-транспорт видит значение настройки); в HTML только
маска «••••1234» или «не задан», полного значения нет нигде — ни в ответах,
ни в логах, ни в текстах ошибок (токен телеграма живёт в URL запроса);
приоритет — настройка над env, очистка настройки возвращает env или выключает
интеграцию; пустое поле ввода не затирает сохранённый токен; TELEGRAM_CHAT_ID —
число или @канал с русской ошибкой; форма и тест-кнопки под CSRF.
Живой сети нет: клиенты — httpx.MockTransport, поллер — подмена фабрик.
"""

import httpx
import pytest
from conftest import body_of, login, make_user, telegram_mock

from app.config import reset_settings_cache
from app.sync import (
    EVENT_TELEGRAM_NEW,
    EVENT_USEDESK,
    SETTING_TELEGRAM_BOT_TOKEN,
    SETTING_TELEGRAM_CHAT_ID,
    SETTING_USEDESK_API_TOKEN,
    enqueue,
    poll_once,
    reset_clients,
    set_clients,
    set_setting,
    telegram_bot_token,
    telegram_chat_id,
    usedesk_api_token,
)
from app.telegram import TelegramError
from app.tickets import create_ticket
from app.usedesk import UsedeskClient, UsedeskError

NEW_USEDESK = "TEST-ud-secret-9876"
NEW_TELEGRAM = "TEST-tg-leaky-777"
NEW_CHAT = "-1001234567890"


def as_admin(client, session):
    make_user(session, "TEST-chief", "TEST-pass", role="admin")
    login(client, "TEST-chief", "TEST-pass")


@pytest.fixture()
def no_tokens(db, monkeypatch):
    """Панель без ключей: env-переменные интеграций пусты."""
    monkeypatch.setenv("ADMIN_LOGIN", "TEST-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "TEST-pass")
    monkeypatch.setenv("PANEL_SECRET_KEY", "TEST-secret")
    for name in ("USEDESK_API_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.setenv(name, "")
    reset_settings_cache()
    yield monkeypatch
    reset_settings_cache()
    reset_clients()


def save_tokens(client, **fields) -> object:
    return client.post("/admin/settings/tokens", data=fields, follow_redirects=False)


# --- маска: в HTML только «••••последние4» или «не задан» ---


def test_env_token_shown_masked_never_full(ui, client, session):
    as_admin(client, session)
    page = client.get("/admin")
    assert page.status_code == 200
    assert "••••oken" in page.text, "маска — последние 4 символа (TEST-ud-token)"
    assert "••••TEST" in page.text, "маска — последние 4 символа (-100TEST)"
    for value in ("TEST-ud-token", "TEST-tg-token", "-100TEST"):
        assert value not in page.text, "полное значение не возвращается в HTML"


def test_unset_token_shown_as_missing(client, no_tokens, session):
    as_admin(client, session)
    page = client.get("/admin")
    assert "не задан" in page.text


def test_saved_tokens_masked_full_value_absent_in_html(ui, client, session):
    as_admin(client, session)
    response = save_tokens(
        client,
        usedesk_api_token=NEW_USEDESK,
        telegram_bot_token=NEW_TELEGRAM,
        telegram_chat_id=NEW_CHAT,
    )
    assert response.status_code == 303
    page = client.get("/admin")
    assert "••••9876" in page.text
    assert "••••7890" in page.text
    for value in (NEW_USEDESK, NEW_TELEGRAM, NEW_CHAT):
        assert value not in page.text, "полное значение токена не возвращается в HTML"


# --- write-only: пустое поле не затирает сохранённый токен ---


def test_empty_input_keeps_saved_token(ui, client, session):
    as_admin(client, session)
    assert save_tokens(client, usedesk_api_token=NEW_USEDESK).status_code == 303
    assert save_tokens(client, usedesk_api_token="").status_code == 303
    assert usedesk_api_token() == NEW_USEDESK
    assert save_tokens(client).status_code == 303, "пустая форма тоже ничего не меняет"
    assert usedesk_api_token() == NEW_USEDESK


# --- приоритет: настройка > env; очистка -> env или выкл ---


def test_setting_overrides_env_and_clear_returns_to_env(ui, client, session):
    as_admin(client, session)
    assert save_tokens(client, telegram_bot_token=NEW_TELEGRAM).status_code == 303
    assert telegram_bot_token() == NEW_TELEGRAM, "настройка сильнее env"
    assert save_tokens(client, clear_telegram="on").status_code == 303
    assert telegram_bot_token() == "TEST-tg-token", "очистка вернула env-значение"
    assert save_tokens(client, usedesk_api_token=NEW_USEDESK).status_code == 303
    assert save_tokens(client, clear_usedesk="on").status_code == 303
    assert usedesk_api_token() == "TEST-ud-token"


def test_clear_without_env_turns_integration_off(ui, client, session, monkeypatch):
    as_admin(client, session)
    monkeypatch.setenv("USEDESK_API_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    reset_settings_cache()
    assert save_tokens(client, usedesk_api_token=NEW_USEDESK).status_code == 303
    assert usedesk_api_token() == NEW_USEDESK
    assert save_tokens(client, clear_usedesk="on").status_code == 303
    assert usedesk_api_token() == "", "без env очистка оставляет интеграцию выключенной"


# --- мгновенное применение: тест-кнопка и поллер идут с новым токеном ---


def test_probe_button_uses_saved_token_without_restart(ui, client, session, monkeypatch):
    as_admin(client, session)
    assert save_tokens(client, usedesk_api_token=NEW_USEDESK).status_code == 303
    seen: list[dict] = []

    def factory(token: str) -> UsedeskClient:
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(body_of(request))
            return httpx.Response(200, json=[])

        return UsedeskClient(
            token, "https://usedesk.test", transport=httpx.MockTransport(handler)
        )

    monkeypatch.setattr("app.routes.admin._usedesk_probe_client", factory)
    response = client.post("/admin/diagnostics/test-usedesk", data={})
    assert response.status_code == 200
    assert "Подключение ок" in response.text
    assert seen and seen[0].get("api_token") == NEW_USEDESK, "тест-кнопка с токеном из UI"


def test_poller_uses_saved_token_without_restart(ui, client, session, monkeypatch):
    as_admin(client, session)
    assert save_tokens(client, usedesk_api_token=NEW_USEDESK).status_code == 303
    seen: list[str] = []

    def factory(token: str, base_url: str, **kwargs) -> UsedeskClient:
        seen.append(token)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/create/ticket":
                return httpx.Response(200, json={"status": "success", "ticket_id": 42})
            return httpx.Response(200, json=[])

        return UsedeskClient(
            token, base_url, transport=httpx.MockTransport(handler), min_interval=0
        )

    monkeypatch.setattr("app.sync.UsedeskClient", factory)
    assert client.post(
        "/admin/features/usedesk_enabled", data={"value": "1"}, follow_redirects=False
    ).status_code == 303
    actor = make_user(session, "TEST-op", "TEST-pass")
    ticket = create_ticket("заявка на новом токене", "описание", None, actor)
    enqueue(ticket.id, {EVENT_USEDESK})
    poll_once()
    assert seen == [NEW_USEDESK], "поллер собрал клиент с токеном из настройки"


# --- секрет не утекает: ни в логи, ни в тексты ошибок ---


def leaky_telegram_mock() -> object:
    def handler(_: httpx.Request) -> httpx.Response:
        raise TelegramError(
            f"HTTP 500 на https://api.telegram.org/bot{NEW_TELEGRAM}/sendMessage"
        )

    return telegram_mock(handler)


def test_leaky_probe_error_hides_token_in_response_and_logs(
    ui, client, session, monkeypatch, caplog
):
    as_admin(client, session)
    assert save_tokens(client, telegram_bot_token=NEW_TELEGRAM).status_code == 303
    monkeypatch.setattr("app.routes.admin._telegram_probe_client", lambda token: (
        leaky_telegram_mock()
    ))
    with caplog.at_level("WARNING"):
        response = client.post("/admin/diagnostics/test-telegram", data={})
    assert response.status_code == 200
    assert "Ошибка" in response.text
    assert NEW_TELEGRAM not in response.text, "токен вычищен из текста ошибки"
    assert NEW_TELEGRAM not in caplog.text, "токен не попадает в логи"


def test_leaky_poller_error_hides_token_in_logs(ui, client, session, caplog):
    as_admin(client, session)
    assert save_tokens(client, telegram_bot_token=NEW_TELEGRAM).status_code == 303
    set_clients(
        telegram=leaky_telegram_mock(),
        usedesk=None,
    )
    try:
        assert client.post(
            "/admin/features/telegram_enabled", data={"value": "1"}, follow_redirects=False
        ).status_code == 303
        actor = make_user(session, "TEST-op", "TEST-pass")
        ticket = create_ticket("заявка с утёкшим телеграмом", "описание", None, actor)
        enqueue(ticket.id, {EVENT_TELEGRAM_NEW})
        with caplog.at_level("WARNING"):
            poll_once()
        assert NEW_TELEGRAM not in caplog.text, "токен не попадает в логи поллера"
        assert "не отправлено" in caplog.text, "отказ зафиксирован по-русски, без токена"
    finally:
        reset_clients()


def leaky_usedesk_mock() -> UsedeskClient:
    def handler(_: httpx.Request) -> httpx.Response:
        raise UsedeskError(f"UseDesk: ошибка 110 на /tickets: ключ {NEW_USEDESK}")

    return UsedeskClient(
        "TEST-ud-unused", "https://usedesk.test",
        transport=httpx.MockTransport(handler), min_interval=0,
    )


def test_usedesk_token_absent_from_poller_logs(ui, client, session, caplog):
    as_admin(client, session)
    assert save_tokens(client, usedesk_api_token=NEW_USEDESK).status_code == 303
    set_clients(usedesk=leaky_usedesk_mock(), telegram=None)
    try:
        assert client.post(
            "/admin/features/usedesk_enabled", data={"value": "1"}, follow_redirects=False
        ).status_code == 303
        actor = make_user(session, "TEST-op", "TEST-pass")
        ticket = create_ticket("заявка с утёкшим юздеском", "описание", None, actor)
        enqueue(ticket.id, {EVENT_USEDESK})
        with caplog.at_level("WARNING"):
            poll_once()  # исходящая ветка: «не отправлено»
            poll_once()  # входящая ветка: «выборка не удалась»
        assert NEW_USEDESK not in caplog.text, "usedesk-токен не попадает в логи поллера"
        assert "не отправлено" in caplog.text and "не удалась" in caplog.text
    finally:
        reset_clients()


# --- валидация TELEGRAM_CHAT_ID: число или @канал ---


def test_chat_id_invalid_rejected_in_russian(ui, client, session):
    as_admin(client, session)
    response = client.post("/admin/settings/tokens", data={"telegram_chat_id": "abc!"})
    assert response.status_code == 200
    assert "число" in response.text
    assert "@имя_канала" in response.text
    assert telegram_chat_id() == "-100TEST", "невалидное значение не сохранилось"


def test_chat_id_unicode_digits_rejected(ui, client, session):
    as_admin(client, session)
    response = client.post("/admin/settings/tokens", data={"telegram_chat_id": "١٢٣٤٥"})
    assert response.status_code == 200
    assert "число" in response.text
    assert telegram_chat_id() == "-100TEST", "юникод-цифры не проходят как число группы"


def test_chat_id_group_number_and_channel_accepted(ui, client, session):
    as_admin(client, session)
    assert save_tokens(client, telegram_chat_id=NEW_CHAT).status_code == 303
    assert telegram_chat_id() == NEW_CHAT
    assert save_tokens(client, telegram_chat_id="@my_channel").status_code == 303
    assert telegram_chat_id() == "@my_channel"


# --- граница: маршрут только для админа и строго под CSRF ---


def test_tokens_route_guard_and_csrf(ui, client, session):
    anonymous = client.post(
        "/admin/settings/tokens", data={"usedesk_api_token": NEW_USEDESK},
        follow_redirects=False,
    )
    assert anonymous.status_code == 303
    assert anonymous.headers["location"].startswith("/login"), anonymous.headers["location"]
    assert usedesk_api_token() != NEW_USEDESK, "анонимный POST ничего не сохранил"

    make_user(session, "TEST-op", "TEST-pass")
    login(client, "TEST-op", "TEST-pass")
    assert client.post(
        "/admin/settings/tokens", data={"usedesk_api_token": NEW_USEDESK}
    ).status_code == 403

    as_admin(client, session)
    assert client.post(
        "/admin/settings/tokens", data={"csrf_token": "TEST-bad", "usedesk_api_token": "x"}
    ).status_code == 403, "POST без валидного CSRF отклонён"
    assert usedesk_api_token() != "x"


# --- ключи Setting существуют и читаются геттером ---


def test_setting_keys_round_trip(db, session):
    set_setting(SETTING_USEDESK_API_TOKEN, NEW_USEDESK)
    set_setting(SETTING_TELEGRAM_BOT_TOKEN, NEW_TELEGRAM)
    set_setting(SETTING_TELEGRAM_CHAT_ID, NEW_CHAT)
    assert usedesk_api_token() == NEW_USEDESK
    assert telegram_bot_token() == NEW_TELEGRAM
    assert telegram_chat_id() == NEW_CHAT
