"""Клиент UseDesk: эндпоинты и правила строго по docs/usedesk-api.md (спайк T02).

Контракт шва (interfaces.md, менять нельзя):
- UsedeskClient(token, base_url): create_ticket(subject, message) -> int (ticket_id),
  add_comment(usedesk_id, text) -> None, set_status(usedesk_id, status) -> None,
  list_updated(since: datetime) -> list[dict];
- дополнительно шов R12.7: last_client_activity(usedesk_id) -> datetime | None
  (POST /ticket, последняя опубликованная реплика клиента) и парсер дат
  parse_usedesk_datetime (ответы UseDesk — «YYYY-MM-DD HH:MM[:SS]» UTC+0, naive).

Факты API из спайка: все методы POST, api_token в теле каждого запроса; HTTP
всегда 200, успех различается по телу («status»: «success»), ошибки — коды
111–121 в теле (121 — превышение лимита запросов: UsedeskRateLimited, повтор
следующим циклом поллера); страницы /tickets — максимум 100 записей, offset —
номер страницы; updated_after — «YYYY-MM-DD HH:MM» UTC, включительно. Закрытие
через шов — два запроса: /create/comment (type=private) затем /update/ticket
(status — число-идентификатор). Клиент держит не больше min_interval запросов
в секунду (по умолчанию 1 — осторожный предел при неопубликованных лимитах).
Токен никогда не попадает в тексты исключений.
"""

import time
from datetime import datetime

import httpx

DEFAULT_BASE_URL = "https://api.usedesk.ru"
DEFAULT_TIMEOUT_SECONDS = 10.0
LIST_PAGE_SIZE = 100
MAX_LIST_PAGES = 50
RATE_LIMIT_CODE = 121


class UsedeskError(Exception):
    """Отказ UseDesk: не-HTTP-200, ошибка 111–120 в теле, нечитаемый ответ."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class UsedeskRateLimited(UsedeskError):
    """Код 121 — превышение лимита запросов; повтор следующим циклом поллера."""


def parse_usedesk_datetime(value: object) -> datetime | None:
    """Дата UseDesk («2021-04-29 11:31:23», UTC+0) -> naive-UTC; мусор -> None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


class UsedeskClient:
    def __init__(
        self,
        token: str,
        base_url: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        min_interval: float = 1.0,
    ) -> None:
        self._token = token
        self._min_interval = min_interval
        self._last_request_at = 0.0
        self._http = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)

    def close(self) -> None:
        self._http.close()

    def create_ticket(self, subject: str, message: str) -> int:
        """POST /create/ticket -> ticket_id нового тикета."""
        payload = self._post("/create/ticket", {"subject": subject, "message": message})
        ticket_id = payload.get("ticket_id")
        if not isinstance(ticket_id, int):
            raise UsedeskError("UseDesk не вернул ticket_id при создании тикета")
        return ticket_id

    def add_comment(self, usedesk_id: str, text: str) -> None:
        """POST /create/comment — приватный комментарий (тип private)."""
        self._post(
            "/create/comment",
            {"ticket_id": str(usedesk_id), "message": text, "type": "private"},
        )

    def set_status(self, usedesk_id: str, status: int) -> None:
        """POST /update/ticket — числовой статус тикета."""
        self._post("/update/ticket", {"ticket_id": str(usedesk_id), "status": int(status)})

    def list_updated(self, since: datetime) -> list[dict]:
        """Тикеты, обновлённые после since (UTC): страницы по 100, offset 0,1,2…"""
        updated_after = since.strftime("%Y-%m-%d %H:%M")
        items: list[dict] = []
        for offset in range(MAX_LIST_PAGES):
            payload = self._post(
                "/tickets",
                {
                    "updated_after": updated_after,
                    "sort": "last_updated_at",
                    "order": "asc",
                    "offset": offset,
                },
            )
            page = payload if isinstance(payload, list) else []
            items.extend(item for item in page if isinstance(item, dict))
            if len(page) < LIST_PAGE_SIZE:
                break
        return items

    def last_client_activity(self, usedesk_id: str) -> datetime | None:
        """Последняя реплика клиента (POST /ticket -> comments[from=client]) или None."""
        payload = self._post("/ticket", {"ticket_id": str(usedesk_id)})
        comments = payload.get("comments", []) if isinstance(payload, dict) else []
        moments = [
            moment
            for comment in comments
            if isinstance(comment, dict) and comment.get("from") == "client"
            for moment in [parse_usedesk_datetime(comment.get("published_at"))]
            if moment is not None
        ]
        return max(moments) if moments else None

    def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        now = time.monotonic()
        wait = self._last_request_at + self._min_interval - now
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _post(self, path: str, fields: dict) -> dict | list:
        self._throttle()
        body = {"api_token": self._token, **fields}
        try:
            response = self._http.post(path, data=body)
        except httpx.HTTPError as error:
            raise UsedeskError(f"UseDesk недоступен на {path}: {type(error).__name__}") from error
        if response.status_code != 200:
            raise UsedeskError(f"UseDesk вернул HTTP {response.status_code} на {path}")
        try:
            payload = response.json()
        except ValueError as error:
            raise UsedeskError(f"UseDesk вернул не-JSON на {path}") from error
        if isinstance(payload, dict) and payload.get("status") == "success":
            return payload
        if isinstance(payload, list):
            return payload
        code: int | None = None
        text = "неизвестная ошибка"
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                code = error.get("code") if isinstance(error.get("code"), int) else None
                text = str(error.get("text", text))
            elif "error_code" in payload:
                code = payload["error_code"] if isinstance(payload["error_code"], int) else None
        if code == RATE_LIMIT_CODE:
            raise UsedeskRateLimited(f"UseDesk: превышен лимит запросов (121) на {path}", code)
        raise UsedeskError(f"UseDesk: ошибка {code} на {path}: {text}", code)
