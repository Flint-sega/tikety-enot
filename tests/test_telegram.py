"""Telegram-клиент: Bot API sendMessage против base_url-шва (только MockTransport).

Факты Bot API: POST /bot{token}/sendMessage с JSON {chat_id, text}; успех —
HTTP 200 и «ok»: true в теле; иначе ошибка (описание не содержит токен).
"""

import json

import httpx
import pytest
from conftest import telegram_mock

from app.telegram import TelegramError

TOKEN = "TEST-tg-token"
BASE_URL = "https://tg.test"


def test_send_message_posts_chat_id_and_text_to_bot_api():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    telegram_mock(handler).send_message("-100500", "Новая заявка № 1: Енот")

    assert len(calls) == 1
    request = calls[0]
    assert request.method == "POST"
    assert request.url.path == f"/bot{TOKEN}/sendMessage"
    assert request.headers["content-type"].startswith("application/json")
    body = json.loads(request.read())
    assert body == {"chat_id": "-100500", "text": "Новая заявка № 1: Енот"}


def test_api_refusal_raises_telegram_error():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "description": "chat not found"})

    with pytest.raises(TelegramError) as raised:
        telegram_mock(handler).send_message("-100500", "текст")
    assert "chat not found" in str(raised.value)
    assert TOKEN not in str(raised.value)


def test_http_error_raises_telegram_error():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(TelegramError):
        telegram_mock(handler).send_message("-100500", "текст")


def test_network_error_raises_telegram_error():
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("нет сети")

    with pytest.raises(TelegramError):
        telegram_mock(handler).send_message("-100500", "текст")
