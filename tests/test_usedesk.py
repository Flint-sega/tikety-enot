"""UseDesk-клиент: эндпоинты и правила строго по docs/usedesk-api.md (спайк T02).

Тесты — только на httpx.MockTransport против base_url-шва; живой сети нет.
Факты из спайка: все методы POST, api_token в теле; HTTP всегда 200, успех —
"status": "success", ошибки — код 111–121 в теле (121 — лимит запросов);
страницы /tickets по 100 записей, offset — номер страницы, updated_after —
"YYYY-MM-DD HH:MM" UTC включительно; закрытие = /create/comment (private) +
/update/ticket (status — число).
"""

from datetime import datetime

import httpx
import pytest
from conftest import body_of, usedesk_mock

from app.usedesk import (
    UsedeskError,
    UsedeskRateLimited,
    parse_usedesk_datetime,
)

TOKEN = "TEST-usedesk-token"
BASE_URL = "https://usedesk.test"


def test_create_ticket_posts_token_subject_message_and_returns_id():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"status": "success", "ticket_id": 2154861})

    client = usedesk_mock(handler)
    result = client.create_ticket("Обновление баз", "Обновление Клиент-Мед в 6:00")

    assert result == 2154861
    assert len(calls) == 1
    assert calls[0].method == "POST"
    assert calls[0].url.path == "/create/ticket"
    body = body_of(calls[0])
    assert body["api_token"] == TOKEN
    assert body["subject"] == "Обновление баз"
    assert body["message"] == "Обновление Клиент-Мед в 6:00"


def test_error_code_in_body_raises_usedesk_error():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": {"code": 112, "text": "invalid token"}})

    with pytest.raises(UsedeskError) as raised:
        usedesk_mock(handler).create_ticket("тема", "сообщение")
    assert raised.value.code == 112


def test_rate_limit_code_121_raises_retryable_error():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": {"code": 121, "text": "request limits"}})

    with pytest.raises(UsedeskRateLimited) as raised:
        usedesk_mock(handler).list_updated(datetime(2026, 10, 12, 8, 0))
    assert isinstance(raised.value, UsedeskError)
    assert raised.value.code == 121


def test_http_500_raises_usedesk_error():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"oops": True})

    with pytest.raises(UsedeskError):
        usedesk_mock(handler).create_ticket("тема", "сообщение")


def test_add_comment_then_set_status_is_private_then_numeric():
    calls: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = body_of(request)
        calls.append((path, body))
        if path == "/create/comment":
            return httpx.Response(200, json={"status": "success", "comment_id": 1})
        if path == "/update/ticket":
            return httpx.Response(200, json={"status": "success"})
        raise AssertionError(f"неожиданный путь {path}")

    client = usedesk_mock(handler)
    client.add_comment("777", "Заявка закрыта в панели")
    client.set_status("777", 2)

    assert [path for path, _ in calls] == ["/create/comment", "/update/ticket"]
    comment_body, update_body = calls[0][1], calls[1][1]
    assert comment_body["api_token"] == TOKEN
    assert comment_body["ticket_id"] == "777"
    assert comment_body["message"] == "Заявка закрыта в панели"
    assert comment_body["type"] == "private"
    assert update_body["ticket_id"] == "777"
    assert update_body["status"] == "2"


def make_list_item(index: int, updated: str) -> dict:
    return {
        "id": index,
        "subject": f"тикет {index}",
        "status": 8,
        "created_at": updated,
        "last_updated_at": updated,
    }


def test_list_updated_pages_by_offset_until_short_page():
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = body_of(request)
        calls.append(body)
        assert request.method == "POST"
        assert request.url.path == "/tickets"
        assert body["api_token"] == TOKEN
        assert body["updated_after"] == "2026-10-12 08:00"
        assert body["sort"] == "last_updated_at"
        assert body["order"] == "asc"
        offset = int(body["offset"])
        if offset == 0:
            first_page = [make_list_item(i, "2026-10-12 09:00:00") for i in range(100)]
            return httpx.Response(200, json=first_page)
        if offset == 1:
            second_page = [make_list_item(100 + i, "2026-10-12 09:30:00") for i in range(3)]
            return httpx.Response(200, json=second_page)
        raise AssertionError("третья страница не запрашивалась")

    items = usedesk_mock(handler).list_updated(datetime(2026, 10, 12, 8, 0))

    assert len(items) == 103
    assert [int(body["offset"]) for body in calls] == [0, 1]


def test_list_updated_stops_after_single_short_page():
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(body_of(request))
        page = [make_list_item(i, "2026-10-12 09:00:00") for i in range(5)]
        return httpx.Response(200, json=page)

    items = usedesk_mock(handler).list_updated(datetime(2026, 10, 12, 8, 0))

    assert len(items) == 5
    assert len(calls) == 1


def test_last_client_activity_takes_latest_client_comment():
    def handler(request: httpx.Request) -> httpx.Response:
        body = body_of(request)
        assert request.url.path == "/ticket"
        assert body["ticket_id"] == "777"
        return httpx.Response(
            200,
            json={
                "status": "success",
                "comments": [
                    {"message": "вопрос", "from": "client", "published_at": "2026-10-12 09:00:00"},
                    {
                        "message": "ответ агента",
                        "from": "user",
                        "published_at": "2026-10-12 09:05:00",
                    },
                    {
                        "message": "уточнение",
                        "from": "client",
                        "published_at": "2026-10-12 10:00:00",
                    },
                ],
            },
        )

    activity = usedesk_mock(handler).last_client_activity("777")
    assert activity == datetime(2026, 10, 12, 10, 0)


def test_last_client_activity_without_client_comments_is_none():
    def handler(_: httpx.Request) -> httpx.Response:
        agent_only = [{"message": "ответ", "from": "user", "published_at": "2026-10-12 09:05:00"}]
        return httpx.Response(200, json={"status": "success", "comments": agent_only})

    assert usedesk_mock(handler).last_client_activity("777") is None


def test_parse_usedesk_datetime_accepts_doc_formats():
    assert parse_usedesk_datetime("2021-04-29 11:31:23") == datetime(2021, 4, 29, 11, 31, 23)
    assert parse_usedesk_datetime("2021-04-29 11:31") == datetime(2021, 4, 29, 11, 31)
    assert parse_usedesk_datetime("") is None
    assert parse_usedesk_datetime("мусор") is None
    assert parse_usedesk_datetime(None) is None
