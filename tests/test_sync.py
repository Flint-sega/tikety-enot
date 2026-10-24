"""Поллер синхронизации: enqueue/poll_once, анти-цикл, LWW, watermark, тумблеры.

Инварианты interfaces.md: поллер — единственный писатель sync_state/watermark,
роуты только enqueue; source=usedesk никогда не порождает исходящие UseDesk-
запросы; конфликт статусов — LWW по updated_at (naive-UTC); пустой токен =
интеграция выкл независимо от тумблера; телеграм — best-effort. Тесты — только
на httpx.MockTransport против base_url-швов; живой сети нет.
"""

import json
import threading
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from conftest import body_of, login, make_user, seed_ticket, telegram_mock, usedesk_mock
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import reset_settings_cache
from app.main import app
from app.models import Ticket, User
from app.sync import (
    EVENT_TELEGRAM_NEW,
    EVENT_USEDESK,
    SETTING_SYNC_ERROR_COUNT,
    SETTING_TELEGRAM_ENABLED,
    SETTING_TEMPLATES_ENABLED,
    SETTING_USEDESK_ARMED,
    SETTING_USEDESK_ARMED_EVER,
    SETTING_USEDESK_CLOSED_STATUSES,
    SETTING_USEDESK_ENABLED,
    SETTING_USEDESK_STATUS_CLOSED,
    SETTING_USEDESK_WATERMARK,
    enqueue,
    ensure_default_settings,
    get_setting,
    pending_events,
    pending_events_key,
    poll_once,
    reset_clients,
    set_clients,
    set_setting,
)
from app.telegram import TelegramClient
from app.tickets import close_ticket, create_ticket, format_msk
from app.usedesk import UsedeskClient, parse_usedesk_datetime

OLD_WATERMARK = "2026-10-12 08:00:00"


# --- помощники: моки клиентов и фикстуры окружения ---


def json_body(request: httpx.Request) -> dict:
    request.read()
    return json.loads(request.content)


def failing_usedesk() -> UsedeskClient:
    return usedesk_mock(lambda request: httpx.Response(500, json={"oops": True}))


def failing_telegram() -> TelegramClient:
    return telegram_mock(lambda request: httpx.Response(500, text="boom"))


@pytest.fixture()
def env(db, monkeypatch):
    monkeypatch.setenv("USEDESK_API_TOKEN", "TEST-ud-token")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TEST-tg-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100TEST")
    reset_settings_cache()
    yield monkeypatch
    reset_settings_cache()
    reset_clients()


@pytest.fixture()
def no_tokens(db, monkeypatch):
    monkeypatch.setenv("USEDESK_API_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    reset_settings_cache()
    yield monkeypatch
    reset_settings_cache()
    reset_clients()


def enable(setting_key: str) -> None:
    ensure_default_settings()
    set_setting(setting_key, "1")


def both_on() -> None:
    enable(SETTING_USEDESK_ENABLED)
    enable(SETTING_TELEGRAM_ENABLED)


def enable_incoming(watermark: str = OLD_WATERMARK) -> None:
    """UseDesk уже включён и поллится: watermark задан (не первое включение)."""
    enable(SETTING_USEDESK_ENABLED)
    set_setting(SETTING_USEDESK_ARMED, "1")
    set_setting(SETTING_USEDESK_ARMED_EVER, "1")
    set_setting(SETTING_USEDESK_WATERMARK, watermark)


def make_useodesk_item(
    usedesk_id: int,
    status: int,
    updated: str,
    subject: str = "Не приходят письма",
) -> dict:
    return {
        "id": usedesk_id,
        "subject": subject,
        "status": status,
        "created_at": updated,
        "last_updated_at": updated,
        "last_comment": {"message": "перестали приходить"},
    }


# --- настройки-дефолты (строки Setting при первом старте) ---


def test_default_settings_created_and_idempotent(db):
    ensure_default_settings()
    assert get_setting(SETTING_USEDESK_ENABLED) == "0"
    assert get_setting(SETTING_TELEGRAM_ENABLED) == "0"
    assert get_setting(SETTING_TEMPLATES_ENABLED) == "1"
    assert get_setting(SETTING_USEDESK_STATUS_CLOSED) == "2"
    assert get_setting(SETTING_USEDESK_CLOSED_STATUSES) == "2,3,4,7,10"
    assert get_setting(SETTING_SYNC_ERROR_COUNT) == "0"
    assert get_setting(SETTING_USEDESK_ARMED) == "0"
    assert parse_usedesk_datetime(get_setting(SETTING_USEDESK_WATERMARK)) is not None
    before = get_setting(SETTING_USEDESK_WATERMARK)
    ensure_default_settings()
    assert get_setting(SETTING_USEDESK_WATERMARK) == before


def test_lifespan_seeds_default_settings(db, ui, monkeypatch, tmp_path):
    monkeypatch.setenv("SEED_PATH", str(tmp_path / "нет-файла.json"))
    reset_settings_cache()
    with TestClient(app) as started:
        assert started.get("/healthz").status_code == 200
    assert get_setting(SETTING_USEDESK_ENABLED) == "0"
    assert get_setting(SETTING_USEDESK_STATUS_CLOSED) == "2"


# --- enqueue: единственный вход роутов в синхронизацию ---


def test_enqueue_marks_ticket_pending_with_events(session):
    operator = make_user(session, "TEST-op")
    ticket = create_ticket("заявка в UseDesk", "описание", None, operator)
    enqueue(ticket.id, {"usedesk", "telegram_new"})
    stored = session.get(Ticket, ticket.id)
    assert stored.sync_state == "pending"
    assert pending_events(ticket.id) == {"usedesk", "telegram_new"}


def test_enqueue_merges_events_of_second_action(session):
    operator = make_user(session, "TEST-op")
    ticket = create_ticket("создали и сразу закрыли", "описание", None, operator)
    enqueue(ticket.id, {"usedesk", "telegram_new"})
    close_ticket(ticket.id, "быстро закрыли", operator)
    enqueue(ticket.id, {"usedesk", "telegram_closed"})
    assert pending_events(ticket.id) == {"usedesk", "telegram_new", "telegram_closed"}
    assert session.get(Ticket, ticket.id).sync_state == "pending"


def test_enqueue_unknown_ticket_raises(session):
    try:
        enqueue(9999, {"usedesk"})
        raise AssertionError("enqueue по отсутствующей заявке прошёл молча")
    except LookupError:
        pass


def test_enqueue_drops_usedesk_event_for_usedesk_source(session):
    ticket = seed_ticket(
        session, number=1, title="из UseDesk", source="usedesk", usedesk_ticket_id="42"
    )
    enqueue(ticket.id, {"usedesk", "telegram_new"})
    assert pending_events(ticket.id) == {"telegram_new"}


def test_pending_events_absent_by_default(session):
    assert pending_events(424242) == set()


# --- исходящие: создание и закрытие уходят в UseDesk и телеграм ---


def test_poll_sends_create_to_usedesk_and_new_to_telegram(db, env, session):
    both_on()
    usedesk_calls: list[tuple[str, dict]] = []
    telegram_calls: list[tuple[str, dict]] = []

    def usedesk_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tickets":
            return httpx.Response(200, json=[])
        usedesk_calls.append((request.url.path, body_of(request)))
        return httpx.Response(200, json={"status": "success", "ticket_id": 501})

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        telegram_calls.append((request.url.path, json_body(request)))
        return httpx.Response(200, json={"ok": True})

    set_clients(usedesk=usedesk_mock(usedesk_handler), telegram=telegram_mock(telegram_handler))
    operator = make_user(session, "TEST-op")
    ticket = create_ticket("Енот обновил базы", "Обновление Клиент-Мед", "завтра в 6", operator)
    enqueue(ticket.id, {"usedesk", "telegram_new"})

    stats = poll_once()

    assert stats.outgoing_sent == 2
    assert stats.outgoing_errors == 0
    stored = session.get(Ticket, ticket.id)
    assert stored.usedesk_ticket_id == "501"
    assert stored.sync_state == "sent"
    assert pending_events(ticket.id) == set()
    assert [path for path, _ in usedesk_calls] == ["/create/ticket"]
    create_body = usedesk_calls[0][1]
    assert create_body["subject"] == "Енот обновил базы"
    assert create_body["message"] == "Обновление Клиент-Мед"
    path, message = telegram_calls[0]
    assert path == "/botTEST-tg-token/sendMessage"
    assert message["chat_id"] == "-100TEST"
    assert "№ 1" in message["text"]
    assert "Енот обновил базы" in message["text"]
    assert "TEST-op" in message["text"]
    assert "завтра в 6" in message["text"]


def test_poll_close_sends_private_comment_and_default_mapped_status(db, env, session):
    both_on()
    usedesk_calls: list[tuple[str, dict]] = []
    telegram_messages: list[dict] = []

    def usedesk_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tickets":
            return httpx.Response(200, json=[])
        usedesk_calls.append((request.url.path, body_of(request)))
        if request.url.path == "/create/comment":
            return httpx.Response(200, json={"status": "success", "comment_id": 5})
        return httpx.Response(200, json={"status": "success"})

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        telegram_messages.append(json_body(request))
        return httpx.Response(200, json={"ok": True})

    set_clients(usedesk=usedesk_mock(usedesk_handler), telegram=telegram_mock(telegram_handler))
    operator = make_user(session, "TEST-op")
    ticket = seed_ticket(
        session, number=3, title="закрываемая", description="описание",
        usedesk_ticket_id="501",
    )
    close_ticket(ticket.id, "базы обновлены", operator)
    enqueue(ticket.id, {"usedesk", "telegram_closed"})

    stats = poll_once()

    assert stats.outgoing_sent == 2
    session.expire_all()
    assert session.get(Ticket, ticket.id).sync_state == "sent"
    assert [path for path, _ in usedesk_calls] == ["/create/comment", "/update/ticket"]
    comment_body = usedesk_calls[0][1]
    update_body = usedesk_calls[1][1]
    assert comment_body["ticket_id"] == "501"
    assert comment_body["type"] == "private"
    assert "базы обновлены" in comment_body["message"]
    assert update_body["ticket_id"] == "501"
    assert update_body["status"] == "2"
    assert len(telegram_messages) == 1
    assert "№ 3" in telegram_messages[0]["text"]
    assert "TEST-op" in telegram_messages[0]["text"]
    assert "базы обновлены" in telegram_messages[0]["text"]


def test_mapping_setting_overrides_closed_status(db, env, session):
    enable(SETTING_USEDESK_ENABLED)
    set_setting(SETTING_USEDESK_STATUS_CLOSED, "3")
    update_bodies: list[dict] = []

    def usedesk_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tickets":
            return httpx.Response(200, json=[])
        if request.url.path == "/create/comment":
            return httpx.Response(200, json={"status": "success", "comment_id": 5})
        update_bodies.append(body_of(request))
        return httpx.Response(200, json={"status": "success"})

    set_clients(usedesk=usedesk_mock(usedesk_handler))
    operator = make_user(session, "TEST-op")
    ticket = seed_ticket(session, number=1, title="под маппинг", usedesk_ticket_id="9")
    close_ticket(ticket.id, "готово", operator)
    enqueue(ticket.id, {"usedesk"})

    poll_once()

    assert update_bodies[0]["status"] == "3"


# --- анти-цикл: source=usedesk не порождает UseDesk-запросы ---


def test_usedesk_source_never_sends_usedesk_requests(db, env, session):
    both_on()
    usedesk_calls: list[str] = []
    telegram_calls: list[str] = []

    def usedesk_handler(request: httpx.Request) -> httpx.Response:
        usedesk_calls.append(request.url.path)
        return httpx.Response(200, json={"status": "success", "ticket_id": 1})

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        telegram_calls.append(request.url.path)
        return httpx.Response(200, json={"ok": True})

    set_clients(usedesk=usedesk_mock(usedesk_handler), telegram=telegram_mock(telegram_handler))
    ticket = seed_ticket(
        session, number=1, title="из UseDesk", source="usedesk", usedesk_ticket_id="42"
    )
    enqueue(ticket.id, {"telegram_new"})
    set_setting(pending_events_key(ticket.id, EVENT_USEDESK), EVENT_USEDESK)
    set_setting(pending_events_key(ticket.id, EVENT_TELEGRAM_NEW), EVENT_TELEGRAM_NEW)

    stats = poll_once()

    assert no_outgoing_calls(usedesk_calls)
    assert len(telegram_calls) == 1
    assert stats.outgoing_sent == 1
    session.expire_all()
    stored = session.get(Ticket, ticket.id)
    assert stored.sync_state == "sent"
    assert stored.usedesk_ticket_id == "42"


def no_outgoing_calls(usedesk_calls: list[str]) -> bool:
    return all(path not in {"/create/ticket", "/create/comment", "/update/ticket"}
               for path in usedesk_calls)


def test_enqueue_during_slow_http_is_not_lost_or_resurrected(db, env, session):
    """Гонка: enqueue роута во время «медленного» HTTP поллера.

    Закрытие заявки в панели, случившееся, пока поллер досылал создание,
    не теряется и не воскрешает уже отправленные события.
    """
    both_on()
    create_started = threading.Event()
    close_enqueued = threading.Event()
    telegram_texts: list[str] = []
    usedesk_calls: list[tuple[str, dict]] = []

    def usedesk_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tickets":
            return httpx.Response(200, json=[])
        usedesk_calls.append((request.url.path, body_of(request)))
        if request.url.path == "/create/ticket":
            create_started.set()
            assert close_enqueued.wait(timeout=10), "enqueue не успел во время HTTP"
            return httpx.Response(200, json={"status": "success", "ticket_id": 501})
        if request.url.path == "/create/comment":
            return httpx.Response(200, json={"status": "success", "comment_id": 5})
        return httpx.Response(200, json={"status": "success"})

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        telegram_texts.append(json_body(request)["text"])
        return httpx.Response(200, json={"ok": True})

    set_clients(usedesk=usedesk_mock(usedesk_handler), telegram=telegram_mock(telegram_handler))
    operator = make_user(session, "TEST-op")
    ticket = create_ticket("медленное создание", "описание", None, operator)
    enqueue(ticket.id, {"usedesk", "telegram_new"})

    worker = threading.Thread(target=poll_once)
    worker.start()
    assert create_started.wait(timeout=10)
    close_ticket(ticket.id, "закрыли во время отправки", operator)
    enqueue(ticket.id, {"usedesk", "telegram_closed"})
    close_enqueued.set()
    worker.join(timeout=10)
    assert not worker.is_alive(), "поллер завис во время медленного HTTP"

    session.expire_all()
    stored = session.get(Ticket, ticket.id)
    assert pending_events(ticket.id) == {"usedesk", "telegram_closed"}
    assert stored.sync_state == "pending"
    assert stored.usedesk_ticket_id == "501"
    assert [path for path, _ in usedesk_calls] == ["/create/ticket"]
    assert len([text for text in telegram_texts if "закрыта" not in text]) == 1

    poll_once()

    session.expire_all()
    stored = session.get(Ticket, ticket.id)
    assert stored.sync_state == "sent"
    assert pending_events(ticket.id) == set()
    assert [path for path, _ in usedesk_calls] == [
        "/create/ticket",
        "/create/comment",
        "/update/ticket",
    ]
    assert len([text for text in telegram_texts if "закрыта" in text]) == 1
    assert "закрыли во время отправки" in telegram_texts[-1]


# --- отказы: error, счётчик, повтор следующим циклом ---


def test_usedesk_error_marks_error_counts_and_retries_next_cycle(db, env, session):
    both_on()
    create_attempts: list[dict] = []
    telegram_calls: list[str] = []

    def usedesk_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tickets":
            return httpx.Response(200, json=[])
        create_attempts.append(body_of(request))
        if len(create_attempts) == 1:
            return httpx.Response(500, json={"oops": True})
        return httpx.Response(200, json={"status": "success", "ticket_id": 501})

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        telegram_calls.append(request.url.path)
        return httpx.Response(200, json={"ok": True})

    set_clients(usedesk=usedesk_mock(usedesk_handler), telegram=telegram_mock(telegram_handler))
    operator = make_user(session, "TEST-op")
    ticket = create_ticket("упрямая заявка", "описание", None, operator)
    enqueue(ticket.id, {"usedesk", "telegram_new"})

    first = poll_once()

    assert first.outgoing_errors == 1
    assert session.get(Ticket, ticket.id).sync_state == "error"
    assert get_setting(SETTING_SYNC_ERROR_COUNT) == "1"
    assert len(telegram_calls) == 1

    second = poll_once()

    assert second.outgoing_sent == 1
    stored = session.get(Ticket, ticket.id)
    assert stored.sync_state == "sent"
    assert stored.usedesk_ticket_id == "501"
    assert len(create_attempts) == 2
    assert len(telegram_calls) == 1
    assert get_setting(SETTING_SYNC_ERROR_COUNT) == "1"


def test_close_send_failure_after_successful_create_requeues_event(db, env, session):
    """Отказ comment+status после удачного create: событие вернётся в очередь.

    Заодно фиксирует отсутствие write-дедлока: usedesk_ticket_id коммитится
    сразу после create, поэтому возврат события (другое соединение) не ждёт
    блокировку, удерживаемую поллером.
    """
    enable(SETTING_USEDESK_ENABLED)
    attempts: list[str] = []

    def usedesk_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tickets":
            return httpx.Response(200, json=[])
        attempts.append(request.url.path)
        if request.url.path == "/create/ticket":
            return httpx.Response(200, json={"status": "success", "ticket_id": 501})
        if request.url.path == "/create/comment" and attempts.count("/create/comment") == 1:
            return httpx.Response(200, json={"error": {"code": 114, "text": "invalid message"}})
        return httpx.Response(200, json={"status": "success"})

    set_clients(usedesk=usedesk_mock(usedesk_handler))
    operator = make_user(session, "TEST-op")
    ticket = create_ticket("создали и сразу закрыли в панели", "описание", None, operator)
    close_ticket(ticket.id, "готово", operator)
    enqueue(ticket.id, {"usedesk"})

    poll_once()

    assert attempts == ["/create/ticket", "/create/comment"]
    session.expire_all()
    stored = session.get(Ticket, ticket.id)
    assert stored.usedesk_ticket_id == "501"
    assert stored.sync_state == "error"
    assert pending_events(ticket.id) == {"usedesk"}

    poll_once()

    assert attempts == ["/create/ticket", "/create/comment", "/create/comment", "/update/ticket"]
    session.expire_all()
    assert session.get(Ticket, ticket.id).sync_state == "sent"
    assert pending_events(ticket.id) == set()


def test_update_failure_after_delivered_comment_retries_status_only(db, env, session):
    """Ревью C3: комментарий доставлен, /update/ticket упал — ретрай шлёт
    ТОЛЬКО статус, приватный комментарий клиенту не дублируется."""
    enable(SETTING_USEDESK_ENABLED)
    attempts: list[str] = []

    def usedesk_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tickets":
            return httpx.Response(200, json=[])
        attempts.append(request.url.path)
        if request.url.path == "/update/ticket" and attempts.count("/update/ticket") == 1:
            return httpx.Response(200, json={"error": {"code": 114, "text": "boom"}})
        return httpx.Response(200, json={"status": "success"})

    set_clients(usedesk=usedesk_mock(usedesk_handler))
    make_user(session, "TEST-op")
    ticket = seed_ticket(
        session, number=7, title="дубль-тест", description="",
        usedesk_ticket_id="501", status="closed", comment="готово",
    )
    enqueue(ticket.id, {"usedesk"})

    first = poll_once()
    assert first.outgoing_errors == 1
    assert attempts == ["/create/comment", "/update/ticket"]

    second = poll_once()

    assert second.outgoing_sent == 1
    assert attempts == [
        "/create/comment", "/update/ticket", "/update/ticket",
    ], "ретрай отправляет только статус — комментарий не дублируется"
    session.expire_all()
    stored = session.get(Ticket, ticket.id)
    assert stored.sync_state == "sent"
    assert pending_events(ticket.id) == set()


def test_rate_limit_121_retries_next_cycle(db, env, session):
    enable(SETTING_USEDESK_ENABLED)
    attempts: list[dict] = []

    def usedesk_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tickets":
            return httpx.Response(200, json=[])
        attempts.append(body_of(request))
        if len(attempts) == 1:
            return httpx.Response(200, json={"error": {"code": 121, "text": "request limits"}})
        return httpx.Response(200, json={"status": "success", "ticket_id": 77})

    set_clients(usedesk=usedesk_mock(usedesk_handler))
    operator = make_user(session, "TEST-op")
    ticket = create_ticket("попавшая в лимит", "описание", None, operator)
    enqueue(ticket.id, {"usedesk"})

    first = poll_once()
    assert first.outgoing_errors == 1
    assert session.get(Ticket, ticket.id).sync_state == "error"

    second = poll_once()
    assert second.outgoing_sent == 1
    stored = session.get(Ticket, ticket.id)
    assert stored.usedesk_ticket_id == "77"
    assert stored.sync_state == "sent"


def test_telegram_failure_does_not_break_usedesk_close_and_retries(db, env, session):
    both_on()
    usedesk_calls: list[tuple[str, dict]] = []
    telegram_attempts: list[int] = []

    def usedesk_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tickets":
            return httpx.Response(200, json=[])
        usedesk_calls.append((request.url.path, body_of(request)))
        if request.url.path == "/create/comment":
            return httpx.Response(200, json={"status": "success", "comment_id": 5})
        return httpx.Response(200, json={"status": "success"})

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        telegram_attempts.append(1)
        if len(telegram_attempts) == 1:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"ok": True})

    set_clients(usedesk=usedesk_mock(usedesk_handler), telegram=telegram_mock(telegram_handler))
    operator = make_user(session, "TEST-op")
    ticket = seed_ticket(session, number=2, title="закрытие с падением телеграмы",
                         usedesk_ticket_id="501")
    close_ticket(ticket.id, "готово", operator)
    enqueue(ticket.id, {"usedesk", "telegram_closed"})

    first = poll_once()

    assert first.outgoing_errors == 1
    assert [path for path, _ in usedesk_calls] == ["/create/comment", "/update/ticket"]
    session.expire_all()
    assert session.get(Ticket, ticket.id).sync_state == "error"

    second = poll_once()

    assert second.outgoing_sent == 1
    session.expire_all()
    assert session.get(Ticket, ticket.id).sync_state == "sent"
    assert len(telegram_attempts) == 2
    assert len(usedesk_calls) == 2


# --- тумблеры: выкл копит pending, вкл догоняет; пустой токен глушит ---


def test_disabled_integration_keeps_pending_until_enabled(db, env, session):
    usedesk_calls: list[str] = []

    def usedesk_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tickets":
            return httpx.Response(200, json=[])
        usedesk_calls.append(request.url.path)
        return httpx.Response(200, json={"status": "success", "ticket_id": 501})

    set_clients(usedesk=usedesk_mock(usedesk_handler))
    operator = make_user(session, "TEST-op")
    ticket = create_ticket("ждёт включения", "описание", None, operator)
    enqueue(ticket.id, {"usedesk"})

    stats = poll_once()

    assert stats.outgoing_sent == 0
    assert usedesk_calls == []
    stored = session.get(Ticket, ticket.id)
    assert stored.sync_state == "pending"
    assert pending_events(ticket.id) == {"usedesk"}

    enable(SETTING_USEDESK_ENABLED)
    poll_once()

    assert usedesk_calls == ["/create/ticket"]
    session.expire_all()
    assert session.get(Ticket, ticket.id).sync_state == "sent"


def test_empty_tokens_make_no_external_calls(db, no_tokens, session):
    both_on()
    usedesk_calls: list[str] = []
    telegram_calls: list[str] = []

    def usedesk_handler(request: httpx.Request) -> httpx.Response:
        usedesk_calls.append(request.url.path)
        return httpx.Response(200, json={"status": "success", "ticket_id": 1})

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        telegram_calls.append(request.url.path)
        return httpx.Response(200, json={"ok": True})

    set_clients(usedesk=usedesk_mock(usedesk_handler), telegram=telegram_mock(telegram_handler))
    operator = make_user(session, "TEST-op")
    ticket = create_ticket("без токенов", "описание", None, operator)
    enqueue(ticket.id, {"usedesk", "telegram_new"})

    stats = poll_once()

    assert stats.outgoing_sent == 0
    assert usedesk_calls == []
    assert telegram_calls == []
    stored = session.get(Ticket, ticket.id)
    assert stored.sync_state == "pending"
    assert stored.usedesk_ticket_id is None
    assert get_setting(SETTING_SYNC_ERROR_COUNT) == "0"


# --- watermark: первое включение, входящие, границы ---


def test_first_enable_watermark_is_enable_moment_no_history(db, env, session):
    enable(SETTING_USEDESK_ENABLED)
    set_setting(SETTING_USEDESK_WATERMARK, "2026-01-01 00:00:00")
    seen_updated_after: list[str] = []

    def usedesk_handler(request: httpx.Request) -> httpx.Response:
        seen_updated_after.append(body_of(request)["updated_after"])
        return httpx.Response(200, json=[])

    set_clients(usedesk=usedesk_mock(usedesk_handler))
    before_poll = datetime.now(timezone.utc).replace(tzinfo=None)

    stats = poll_once()

    assert stats.incoming_created == 0
    assert len(seen_updated_after) == 1
    since = datetime.strptime(seen_updated_after[0], "%Y-%m-%d %H:%M")
    floor = (before_poll - timedelta(minutes=1)).replace(second=0, microsecond=0)
    assert since >= floor
    assert get_setting(SETTING_USEDESK_ARMED) == "1"
    watermark = parse_usedesk_datetime(get_setting(SETTING_USEDESK_WATERMARK))
    assert watermark is not None and watermark >= before_poll.replace(microsecond=0)
    assert session.scalar(select(Ticket)) is None


def test_reenable_keeps_watermark_and_catches_up(db, env, session):
    enable(SETTING_USEDESK_ENABLED)
    seen: list[str] = []

    def usedesk_handler(request: httpx.Request) -> httpx.Response:
        seen.append(body_of(request)["updated_after"])
        return httpx.Response(200, json=[])

    set_clients(usedesk=usedesk_mock(usedesk_handler))
    poll_once()
    set_setting(SETTING_USEDESK_WATERMARK, "2026-10-12 09:00:00")
    set_setting(SETTING_USEDESK_ENABLED, "0")
    poll_once()
    enable(SETTING_USEDESK_ENABLED)
    seen.clear()

    poll_once()

    assert seen == ["2026-10-12 09:00"]


def test_incoming_new_usedesk_ticket_becomes_panel_ticket(db, env, session):
    enable_incoming()

    def usedesk_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tickets":
            item = make_useodesk_item(9001, 8, "2026-10-12 09:00:00")
            return httpx.Response(200, json=[item])
        return httpx.Response(200, json={"status": "success", "comments": []})

    set_clients(usedesk=usedesk_mock(usedesk_handler))

    first = poll_once()

    assert first.incoming_created == 1
    imported = session.scalar(select(Ticket))
    assert imported.source == "usedesk"
    assert imported.usedesk_ticket_id == "9001"
    assert imported.number == 1
    assert imported.title == "Не приходят письма"
    assert imported.description == "перестали приходить"
    assert imported.status == "open"
    assert imported.sync_state is None
    assert get_setting(SETTING_USEDESK_WATERMARK) == "2026-10-12 09:00:00"

    second = poll_once()

    assert second.incoming_created == 0
    assert len(session.scalars(select(Ticket)).all()) == 1


def test_incoming_numbers_continue_panel_sequence(db, env, session):
    enable_incoming()

    def usedesk_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tickets":
            item = make_useodesk_item(9002, 8, "2026-10-12 09:10:00")
            return httpx.Response(200, json=[item])
        return httpx.Response(200, json={"status": "success", "comments": []})

    set_clients(usedesk=usedesk_mock(usedesk_handler))
    seed_ticket(session, number=17, title="уже есть")

    poll_once()

    imported = session.scalar(select(Ticket).where(Ticket.usedesk_ticket_id == "9002"))
    assert imported.number == 18


# --- LWW: конфликт статусов решается по updated_at ---


def test_lww_newer_usedesk_close_closes_panel_ticket(db, env, session):
    enable_incoming()

    def usedesk_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tickets":
            item = make_useodesk_item(501, 2, "2026-10-12 13:00:00", subject="заголовок панели")
            return httpx.Response(200, json=[item])
        return httpx.Response(200, json={"status": "success", "comments": []})

    set_clients(usedesk=usedesk_mock(usedesk_handler))
    ticket = seed_ticket(
        session, number=1, title="заголовок панели", description="не трогать",
        status="open", usedesk_ticket_id="501",
        created_at=datetime(2026, 10, 12, 10, 0), updated_at=datetime(2026, 10, 12, 11, 0),
    )

    stats = poll_once()

    assert stats.incoming_updated == 1
    session.expire_all()
    stored = session.get(Ticket, ticket.id)
    assert stored.status == "closed"
    assert stored.closed_at == datetime(2026, 10, 12, 13, 0)
    assert stored.updated_at == datetime(2026, 10, 12, 13, 0)
    assert stored.title == "заголовок панели"
    assert stored.description == "не трогать"


def test_lww_older_usedesk_close_is_ignored(db, env, session):
    enable(SETTING_USEDESK_ENABLED)
    set_setting(SETTING_USEDESK_WATERMARK, "2026-10-11 00:00:00")

    def usedesk_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tickets":
            item = make_useodesk_item(501, 2, "2026-10-12 09:00:00")
            return httpx.Response(200, json=[item])
        return httpx.Response(200, json={"status": "success", "comments": []})

    set_clients(usedesk=usedesk_mock(usedesk_handler))
    ticket = seed_ticket(
        session, number=1, title="свежее в панели", status="open", usedesk_ticket_id="501",
        created_at=datetime(2026, 10, 12, 10, 0), updated_at=datetime(2026, 10, 12, 12, 0),
    )

    stats = poll_once()

    assert stats.incoming_updated == 0
    stored = session.get(Ticket, ticket.id)
    assert stored.status == "open"
    assert stored.updated_at == datetime(2026, 10, 12, 12, 0)


def test_lww_usedesk_reopen_reopens_panel_ticket(db, env, session):
    enable_incoming()

    def usedesk_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tickets":
            item = make_useodesk_item(501, 1, "2026-10-12 14:00:00")
            return httpx.Response(200, json=[item])
        return httpx.Response(200, json={"status": "success", "comments": []})

    set_clients(usedesk=usedesk_mock(usedesk_handler))
    ticket = seed_ticket(
        session, number=1, title="переоткрытая", status="closed",
        closed_at=datetime(2026, 10, 12, 10, 0), usedesk_ticket_id="501",
        created_at=datetime(2026, 10, 12, 9, 0), updated_at=datetime(2026, 10, 12, 10, 0),
    )

    poll_once()

    session.expire_all()
    stored = session.get(Ticket, ticket.id)
    assert stored.status == "open"
    assert stored.closed_at is None


def test_lww_survives_sync_bookkeeping_inside_enqueue_flush_window(db, env, session):
    """Окно до 60 с: UseDesk закрыл тикет, пока панель досылала создание.

    Служебные записи синка (sync_state/usedesk_ticket_id) не должны двигать
    updated_at — иначе входящее закрытие (t_closed между enqueue и флашем)
    признаётся «старее» и отбрасывается навсегда.
    """
    enable_incoming()
    set_setting(SETTING_USEDESK_WATERMARK, "2000-01-01 00:00:00")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    t_created = now - timedelta(hours=2)
    t_closed = (now - timedelta(hours=1)).replace(microsecond=0)
    usedesk_calls: list[tuple[str, dict]] = []

    def usedesk_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tickets":
            item = make_useodesk_item(501, 2, t_closed.strftime("%Y-%m-%d %H:%M:%S"))
            return httpx.Response(200, json=[item])
        usedesk_calls.append((request.url.path, body_of(request)))
        if request.url.path == "/create/ticket":
            return httpx.Response(200, json={"status": "success", "ticket_id": 501})
        return httpx.Response(200, json={"status": "success", "comments": []})

    set_clients(usedesk=usedesk_mock(usedesk_handler))
    operator = make_user(session, "TEST-op")
    ticket = create_ticket("досылка создания", "описание", None, operator)
    stored = session.get(Ticket, ticket.id)
    stored.updated_at = t_created
    session.commit()

    enqueue(ticket.id, {"usedesk"})
    session.expire_all()
    assert session.get(Ticket, ticket.id).updated_at == t_created

    stats = poll_once()

    assert stats.outgoing_sent == 1
    session.expire_all()
    closed = session.get(Ticket, ticket.id)
    assert closed.status == "closed"
    assert closed.closed_at == t_closed
    assert closed.updated_at == t_closed
    outgoing = [path for path, _ in usedesk_calls if path != "/ticket"]
    assert outgoing == ["/create/ticket"]


def test_incoming_closed_statuses_setting_is_editable(db, env, session):
    enable_incoming()
    set_setting(SETTING_USEDESK_CLOSED_STATUSES, "2,3")

    def usedesk_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tickets":
            item = make_useodesk_item(501, 7, "2026-10-12 09:30:00")
            return httpx.Response(200, json=[item])
        return httpx.Response(200, json={"status": "success", "comments": []})

    set_clients(usedesk=usedesk_mock(usedesk_handler))
    ticket = seed_ticket(
        session, number=1, title="спам по маппингу", status="open", usedesk_ticket_id="501",
        created_at=datetime(2026, 10, 12, 9, 0), updated_at=datetime(2026, 10, 12, 9, 0),
    )

    poll_once()

    assert session.get(Ticket, ticket.id).status == "open"


# --- R12.7: активность клиентов UseDesk ---


def test_usedesk_activity_recorded_from_client_comments(db, env, session):
    enable_incoming()

    def usedesk_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tickets":
            item = make_useodesk_item(501, 8, "2026-10-12 09:00:00")
            return httpx.Response(200, json=[item])
        comments = [
            {"message": "вопрос", "from": "client", "published_at": "2026-10-12 09:30:00"},
            {"message": "ответ агента", "from": "user", "published_at": "2026-10-12 09:45:00"},
            {"message": "уточнение", "from": "client", "published_at": "2026-10-12 10:00:00"},
        ]
        return httpx.Response(200, json={"status": "success", "comments": comments})

    set_clients(usedesk=usedesk_mock(usedesk_handler))

    poll_once()

    imported = session.scalar(select(Ticket).where(Ticket.usedesk_ticket_id == "501"))
    assert imported.usedesk_activity == datetime(2026, 10, 12, 10, 0)


def test_activity_failure_does_not_break_import(db, env, session):
    enable_incoming()

    def usedesk_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tickets":
            item = make_useodesk_item(501, 8, "2026-10-12 09:00:00")
            return httpx.Response(200, json=[item])
        return httpx.Response(500, text="boom")

    set_clients(usedesk=usedesk_mock(usedesk_handler))

    stats = poll_once()

    assert stats.incoming_created == 1
    imported = session.scalar(select(Ticket).where(Ticket.usedesk_ticket_id == "501"))
    assert imported.usedesk_activity is None


# --- HTTP-шов: enqueue из роутов, бейдж и активность на карточке ---


def test_create_route_enqueues_and_close_route_too(db, ui, session, client):
    both_on()
    set_clients(usedesk=failing_usedesk(), telegram=failing_telegram())
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    created = client.post(
        "/tickets/create",
        data={"title": "через форму", "description": "описание", "planned_at": ""},
        follow_redirects=False,
    )
    assert created.status_code == 303
    ticket = session.scalar(select(Ticket))
    assert ticket.sync_state == "pending"
    assert pending_events(ticket.id) == {"usedesk", "telegram_new"}

    closed = client.post(
        f"/tickets/{ticket.id}/close", data={"comment": "готово"},
        follow_redirects=False,
    )
    assert closed.status_code == 303
    session.expire_all()
    assert pending_events(ticket.id) == {"usedesk", "telegram_new", "telegram_closed"}


def test_card_shows_error_badge_after_failed_sync(db, ui, session, client):
    both_on()
    set_clients(usedesk=failing_usedesk(), telegram=failing_telegram())
    operator = make_user(session, "TEST-op")
    ticket = create_ticket("не ушла", "описание", None, operator)
    enqueue(ticket.id, {"usedesk"})
    poll_once()
    assert session.get(Ticket, ticket.id).sync_state == "error"

    login(client, "TEST-op")
    page = client.get("/tickets")
    assert "Не отправлено" in page.text


def test_card_shows_usedesk_activity_line(db, ui, session, client):
    enable_incoming()

    def usedesk_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tickets":
            item = make_useodesk_item(501, 8, "2026-10-12 09:00:00")
            return httpx.Response(200, json=[item])
        comments = [
            {"message": "уточнение", "from": "client", "published_at": "2026-10-12 10:00:00"}
        ]
        return httpx.Response(200, json={"status": "success", "comments": comments})

    set_clients(usedesk=usedesk_mock(usedesk_handler))
    make_user(session, "TEST-op")
    poll_once()

    login(client, "TEST-op")
    page = client.get("/tickets")
    assert "Активность в UseDesk" in page.text
    assert f"{format_msk(datetime(2026, 10, 12, 10, 0))} МСК" in page.text


def test_delete_removes_pending_queue_rows(db, ui, session):
    """C2: удаление заявки чистит её строки очереди — поллер не таскает мёртвый id."""
    from app.tickets import delete_ticket

    make_user(session, "TEST-admin", role="admin")
    admin = session.scalar(select(User).where(User.role == "admin"))
    ticket = create_ticket("удаляемая с очередью", "описание", None, admin)
    enqueue(ticket.id, {EVENT_TELEGRAM_NEW})
    assert pending_events(ticket.id) == {EVENT_TELEGRAM_NEW}

    delete_ticket(ticket.id, admin)

    assert pending_events(ticket.id) == set()
    stats = poll_once()
    assert stats.outgoing_sent == 0 and stats.outgoing_errors == 0
