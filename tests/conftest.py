"""Фикстуры каркаса и общие тестовые хелперы: чистая временная SQLite,
TestClient поверх приложения и единые копии помощников (пользователь, заявка,
логин, моки клиентов, окружение ui).

Хелперы здесь одни на весь набор: тесты импортируют их `from conftest import ...`.
Значения токенов/паролей — только TEST-заглушки.
"""

from collections.abc import Iterator
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import CSRF_COOKIE, CSRF_FIELD, hash_password, reset_login_throttle
from app.config import reset_settings_cache
from app.db import init_db, make_engine, set_engine
from app.main import app
from app.models import Ticket, User
from app.sync import reset_clients
from app.telegram import TelegramClient
from app.usedesk import UsedeskClient


@pytest.fixture()
def db(tmp_path):
    engine = make_engine(tmp_path / "test.sqlite")
    init_db(engine)
    set_engine(engine)
    yield engine
    engine.dispose()
    set_engine(None)


@pytest.fixture()
def session(db) -> Iterator[Session]:
    with Session(db) as db_session:
        yield db_session


@pytest.fixture(autouse=True)
def _fresh_login_throttle():
    """Троттлинг входа живёт в памяти процесса — чистить между тестами."""
    reset_login_throttle()
    yield
    reset_login_throttle()


class CsrfClient(TestClient):
    """TestClient, добавляющий csrf_token из cookie-jar в тело каждого POST.

    Тест, которому нужен отказ CSRF, передаёт свой csrf_token — тогда подстановка
    не срабатывает (setdefault).
    """

    def post(self, url, *, data=None, **kwargs):
        return super().post(url, data=self._with_csrf(data), **kwargs)

    def request(self, method, url, *, data=None, **kwargs):
        if method.upper() == "POST":
            data = self._with_csrf(data)
        return super().request(method, url, data=data, **kwargs)

    def _with_csrf(self, data) -> dict:
        fields = dict(data) if data is not None else {}
        fields.setdefault(CSRF_FIELD, self.cookies.get(CSRF_COOKIE, ""))
        return fields


@pytest.fixture()
def client(db) -> CsrfClient:
    return CsrfClient(app)


# --- пользователи и заявки: подготовка данных ---


def make_user(
    session: Session,
    login: str,
    password: str = "TEST-pass",
    role: str = "operator",
    active: bool = True,
) -> User:
    user = User(login=login, password_hash=hash_password(password), role=role, is_active=active)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def seed_ticket(session: Session, **fields) -> Ticket:
    ticket = Ticket(**fields)
    session.add(ticket)
    session.commit()
    session.refresh(ticket)
    return ticket


def seed_csrf(client: TestClient) -> str:
    """CSRF-токен из cookie-jar; при отсутствии — GET /login, панель ставит cookie."""
    token = client.cookies.get(CSRF_COOKIE, "")
    if not token:
        client.get("/login", follow_redirects=False)
        token = client.cookies.get(CSRF_COOKIE, "")
    return token


def login(client: CsrfClient, login_name: str, password: str = "TEST-pass") -> None:
    seed_csrf(client)
    response = client.post(
        "/login", data={"login": login_name, "password": password}, follow_redirects=False
    )
    assert response.status_code == 303, "логин в подготовке теста должен проходить"


# --- моки внешних клиентов (httpx.MockTransport, живой сети нет) ---


def body_of(request: httpx.Request) -> dict:
    """Тело form-urlencoded POST -> плоский dict (поля UseDesk)."""
    request.read()
    parsed = parse_qs(request.content.decode("utf-8"), keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items()}


def usedesk_mock(handler) -> UsedeskClient:
    return UsedeskClient(
        "TEST-usedesk-token",
        "https://usedesk.test",
        transport=httpx.MockTransport(handler),
        min_interval=0,
    )


def telegram_mock(handler) -> TelegramClient:
    return TelegramClient(
        "TEST-tg-token", "https://tg.test", transport=httpx.MockTransport(handler)
    )


# --- окружение ui: полный env панели (админ + токены интеграций) ---


@pytest.fixture()
def ui(db, monkeypatch):
    monkeypatch.setenv("ADMIN_LOGIN", "TEST-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "TEST-pass")
    monkeypatch.setenv("PANEL_SECRET_KEY", "TEST-secret")
    monkeypatch.setenv("USEDESK_API_TOKEN", "TEST-ud-token")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TEST-tg-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100TEST")
    reset_settings_cache()
    yield monkeypatch
    reset_settings_cache()
    reset_clients()
