"""Единый источник конфигурации: переменные окружения (.env + os.environ).

Контракт шва:
- ENV_NAMES — полный список имён, .env.example обязан совпадать с ним имя-в-имя;
- settings_from_env(mapping) — чистая функция (для тестов);
- get_settings() — точка входа для приложения (кэшируется на процесс).
"""

import logging
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

ENV_NAMES = (
    "ADMIN_LOGIN",
    "ADMIN_PASSWORD",
    "PANEL_SECRET_KEY",
    "USEDESK_API_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "SEED_PATH",
    "DB_PATH",
    "COOKIE_SECURE",
    "DOMAIN",  # только для docker compose + Caddy (HTTPS на хостинге); панель его не читает
)

DEFAULT_SEED_PATH = "seed/tickets-2026-10-12.json"
DEFAULT_DB_PATH = "data/tickets.sqlite"


@dataclass(frozen=True)
class Settings:
    admin_login: str
    admin_password: str
    panel_secret_key: str
    usedesk_api_token: str
    telegram_bot_token: str
    telegram_chat_id: str
    seed_path: str
    db_path: str
    cookie_secure: bool


def settings_from_env(env: Mapping[str, str]) -> Settings:
    """Чистое чтение конфигурации: пустое значение или отсутствие — дефолт."""
    secret_key = env.get("PANEL_SECRET_KEY", "")
    if not secret_key:
        secret_key = secrets.token_urlsafe(32)
        logger.warning(
            "PANEL_SECRET_KEY не задан — сгенерирован эфемерный ключ, "
            "сессии слетят при перезапуске панели"
        )
    return Settings(
        admin_login=env.get("ADMIN_LOGIN", ""),
        admin_password=env.get("ADMIN_PASSWORD", ""),
        panel_secret_key=secret_key,
        usedesk_api_token=env.get("USEDESK_API_TOKEN", ""),
        telegram_bot_token=env.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=env.get("TELEGRAM_CHAT_ID", ""),
        seed_path=env.get("SEED_PATH") or DEFAULT_SEED_PATH,
        db_path=env.get("DB_PATH") or DEFAULT_DB_PATH,
        cookie_secure=env.get("COOKIE_SECURE", "") == "1",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Конфигурация процесса: .env проекта поверх дефолтов, кэш на один расчёт."""
    load_dotenv()
    return settings_from_env(os.environ)


def reset_settings_cache() -> None:
    """Сбросить кэш конфигурации (для тестов и смены окружения)."""
    get_settings.cache_clear()
