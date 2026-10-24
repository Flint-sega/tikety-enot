"""Клиент Telegram Bot API: sendMessage в группу (best-effort на стороне поллера).

Контракт шва (interfaces.md, менять нельзя):
- TelegramClient(token, base_url).send_message(chat_id, text) -> None.

Bot API: POST /bot{token}/sendMessage с JSON {chat_id, text}; успех — HTTP 200
и «ok»: true. Любой отказ (сеть, HTTP, ok=false) — TelegramError с описанием
без токена: поллер считает ошибку, счётчик растёт, повтор следующим циклом;
заявку отказ телеграмы не ломает (R09.1).
"""

import httpx

DEFAULT_BASE_URL = "https://api.telegram.org"
DEFAULT_TIMEOUT_SECONDS = 10.0


class TelegramError(Exception):
    """Отказ Bot API: сеть, не-HTTP-200 или ok=false; токен в текст не попадает."""


class TelegramClient:
    def __init__(
        self,
        token: str,
        base_url: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._token = token
        self._http = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)

    def close(self) -> None:
        self._http.close()

    def send_message(self, chat_id: str, text: str) -> None:
        try:
            response = self._http.post(
                f"/bot{self._token}/sendMessage", json={"chat_id": chat_id, "text": text}
            )
        except httpx.HTTPError as error:
            raise TelegramError(f"Telegram недоступен: {type(error).__name__}") from error
        if response.status_code != 200:
            raise TelegramError(f"Telegram вернул HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as error:
            raise TelegramError("Telegram вернул не-JSON") from error
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            description = payload.get("description") if isinstance(payload, dict) else ""
            raise TelegramError(f"Telegram отклонил sendMessage: {description or 'ok=false'}")
