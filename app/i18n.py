"""Язык интерфейса: русский (исходный) и английский.

Строки в коде и шаблонах пишутся по-русски и оборачиваются в `_()`; английский
перевод лежит в app/locales/en.json (ключ — русская строка, значение — перевод).
Нет перевода — показывается русская строка, страница не ломается.

Язык запроса держит ContextVar. Его ставит LanguageMiddleware из cookie
panel-lang; без cookie — язык по умолчанию из админки (Setting language_default).
Поллер (сообщения в Телеграм и UseDesk) пишет на языке по умолчанию.
Личный выбор вошедшего хранится в user_pref:{id}:lang и при входе
переносится в cookie — так язык едет за человеком на любое устройство.
"""

from __future__ import annotations

import json
from contextvars import ContextVar, Token
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError
from starlette.requests import HTTPConnection

LANGS = ("ru", "en")
FALLBACK_LANG = "ru"
LANG_COOKIE = "panel-lang"
LANG_COOKIE_MAX_AGE = 365 * 24 * 3600
SETTING_DEFAULT_LANGUAGE = "language_default"
LANG_LABELS = {"ru": "Русский", "en": "English"}

CATALOG_PATH = Path(__file__).resolve().parent / "locales" / "en.json"
CATALOG: dict[str, str] = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))

_current: ContextVar[str] = ContextVar("panel_lang", default=FALLBACK_LANG)


def normalize_lang(value: str | None) -> str | None:
    """Код языка из cookie или формы: ru | en; мусор — None."""
    value = (value or "").strip().lower()
    return value if value in LANGS else None


def current_lang() -> str:
    return _current.get()


def set_lang(value: str) -> Token:
    return _current.set(normalize_lang(value) or FALLBACK_LANG)


def reset_lang(token: Token) -> None:
    _current.reset(token)


def gettext(message: str, /, **values: object) -> str:
    """Строка на текущем языке; именованные {поля} подставляются после перевода."""
    text = CATALOG.get(message, message) if current_lang() == "en" else message
    return text.format(**values) if values else text


_ = gettext


def N_(message: str) -> str:
    """Пометить строку-константу для каталога; переводится там, где показывается: _(CONST)."""
    return message


def default_language() -> str:
    """Язык по умолчанию из админки; нет таблицы или мусор — русский."""
    from app.sync import get_setting  # локально: sync сам импортирует i18n

    try:
        value = get_setting(SETTING_DEFAULT_LANGUAGE, FALLBACK_LANG)
    except SQLAlchemyError:
        return FALLBACK_LANG
    return normalize_lang(value) or FALLBACK_LANG


class LanguageMiddleware:
    """Ставит язык запроса: cookie panel-lang, иначе язык по умолчанию из админки."""

    SKIP_PREFIXES = ("/static/",)

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope.get("path", "").startswith(self.SKIP_PREFIXES):
            await self.app(scope, receive, send)
            return
        chosen = normalize_lang(HTTPConnection(scope).cookies.get(LANG_COOKIE))
        token = set_lang(chosen or default_language())
        try:
            await self.app(scope, receive, send)
        finally:
            reset_lang(token)
