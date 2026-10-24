"""Точка входа панели: приложение FastAPI, статика, старт схемы.

Контракт шва:
- app — объект ASGI (uvicorn app.main:app);
- get_session из app.db — зависимость для сессий БД.

Запуск дев: uv run uvicorn app.main:app --reload --port 8072.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.gzip import GZipMiddleware

from app.auth import (
    SecurityHeadersMiddleware,
    SetupRequiredMiddleware,
    bootstrap_admin_from_settings,
    register_auth,
)
from app.config import get_settings
from app.db import get_engine, init_db
from app.i18n import LanguageMiddleware
from app.importer import run_import
from app.models import Ticket
from app.routes.admin import router as admin_router
from app.routes.auth import router as auth_router
from app.routes.language import router as language_router
from app.routes.profile import router as profile_router
from app.routes.templates import router as templates_router
from app.routes.tickets import router as tickets_router
from app.routes.users import router as users_router
from app.sync import ensure_default_settings, poll_loop

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent


def _import_seed_on_first_start() -> None:
    """Импорт seed-файла только при пустой таблице tickets; сбой не мешает старту."""
    settings = get_settings()
    if not Path(settings.seed_path).is_file():
        return
    with Session(get_engine()) as session:
        existing = session.scalar(select(func.count()).select_from(Ticket))
    if existing:
        return
    try:
        run_import(settings.seed_path)
    except Exception:
        logger.exception(
            "Импорт seed-файла %s не выполнен — панель запущена без импорта",
            settings.seed_path,
        )


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    init_db()
    logger.info("Схема БД готова: %s", settings.db_path)
    bootstrap_admin_from_settings()
    _import_seed_on_first_start()
    ensure_default_settings()
    poller = asyncio.create_task(poll_loop())
    try:
        yield
    finally:
        poller.cancel()


class StaticGZipMiddleware(GZipMiddleware):
    """GZip только для /static/ — HTML с CSRF-токеном не сжимается (BREACH)."""

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope.get("path", "").startswith("/static/"):
            await super().__call__(scope, receive, send)
            return
        await self.app(scope, receive, send)


app = FastAPI(
    title="Тикеты Енот",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(SetupRequiredMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
# Сжатие только статики (CSS, htmx, app.js — в 3–5 раз меньше). HTML не сжимаем:
# в нём CSRF-токен рядом с отражённым вводом (поиск), а сжатие такой страницы
# открывает атаку BREACH по длине ответа. Картинки webp уже сжаты.
app.add_middleware(StaticGZipMiddleware, minimum_size=1024)
# Язык запроса (cookie panel-lang или язык по умолчанию) — снаружи всех, чтобы
# экран настройки, ошибки и страницы видели один и тот же язык.
app.add_middleware(LanguageMiddleware)
register_auth(app)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(auth_router)
app.include_router(language_router)
app.include_router(users_router)
app.include_router(profile_router)
app.include_router(templates_router)
app.include_router(tickets_router)
app.include_router(admin_router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
