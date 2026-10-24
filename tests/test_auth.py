"""Шов аутентификации: логин по логину/паролю, подписанная сессия-cookie, выход.

Тесты идут через HTTP (TestClient) и публичные функции app.auth;
пароли в тестах — только TEST-значения.
"""

import logging

import pytest
from conftest import make_user, seed_csrf
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import SESSION_COOKIE, hash_password, verify_password
from app.config import reset_settings_cache
from app.db import get_engine, set_engine
from app.main import app
from app.models import User


def test_password_hash_is_bcrypt_and_verifies():
    password_hash = hash_password("TEST-pass-123")
    assert password_hash.startswith("$2b$")
    assert len(password_hash) == 60
    assert verify_password("TEST-pass-123", password_hash) is True
    assert verify_password("TEST-other", password_hash) is False


def test_verify_password_survives_garbage_hash():
    assert verify_password("TEST-pass", "not-a-hash") is False


def test_login_page_renders_russian_form(client, ui):
    response = client.get("/login")
    assert response.status_code == 200
    assert "Вход" in response.text
    assert 'name="login"' in response.text
    assert 'name="password"' in response.text


def test_login_wrong_password_shows_russian_error_and_keeps_login(client, ui, session):
    make_user(session, "operator-1", "TEST-right-pass")
    client.get("/login")
    response = client.post(
        "/login", data={"login": "operator-1", "password": "TEST-wrong-pass"}
    )
    assert response.status_code == 200
    assert "Неверный логин или пароль" in response.text
    assert 'value="operator-1"' in response.text


def test_login_redirect_carries_next_and_returns_to_page(client, ui, session):
    make_user(session, "operator-2", "TEST-right-pass")
    client.get("/login")
    gated = client.get("/tickets/new", follow_redirects=False)
    assert gated.status_code == 303
    assert "/login?next=" in gated.headers["location"]
    response = client.post(
        "/login?next=%2Ftickets%2Fnew",
        data={"login": "operator-2", "password": "TEST-right-pass", "next": "/tickets/new"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/tickets/new"


def test_login_next_rejects_open_redirect(client, ui, session):
    make_user(session, "operator-3", "TEST-right-pass")
    client.get("/login")
    for hostile in ("https://evil.example", "//evil.example", "/\\evil.example"):
        response = client.post(
            "/login",
            data={"login": "operator-3", "password": "TEST-right-pass", "next": hostile},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/", hostile


def test_cookies_get_secure_flag_when_configured(client, ui):
    import os

    def set_cookie_headers():
        fresh = client.get("/login", follow_redirects=False)
        return [value for name, value in fresh.headers.multi_items() if name == "set-cookie"]

    os.environ["COOKIE_SECURE"] = "1"
    reset_settings_cache()
    try:
        client.cookies.clear()
        secure_headers = set_cookie_headers()
        assert secure_headers, "первый GET ставит CSRF-cookie"
        for cookie in secure_headers:
            assert "Secure" in cookie, cookie
    finally:
        os.environ.pop("COOKIE_SECURE")
        reset_settings_cache()

    client.cookies.clear()
    for cookie in set_cookie_headers():
        assert "Secure" not in cookie, "без COOKIE_SECURE флага нет (localhost по http)"


def test_login_unknown_user_same_error(client, ui):
    client.get("/login")
    response = client.post("/login", data={"login": "nobody", "password": "TEST-pass"})
    assert response.status_code == 200
    assert "Неверный логин или пароль" in response.text


def test_deactivated_user_cannot_login(client, ui, session):
    make_user(session, "operator-2", "TEST-pass", active=False)
    client.get("/login")
    response = client.post("/login", data={"login": "operator-2", "password": "TEST-pass"})
    assert response.status_code == 200
    assert "Неверный логин или пароль" in response.text


def test_login_success_sets_signed_cookie_and_redirects(client, ui, session):
    make_user(session, "operator-3", "TEST-pass")
    client.get("/login")
    response = client.post(
        "/login", data={"login": "operator-3", "password": "TEST-pass"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookie = response.headers["set-cookie"]
    assert f"{SESSION_COOKIE}=" in cookie
    assert "HttpOnly" in cookie
    assert "samesite=lax" in cookie.lower()


def test_authenticated_login_page_redirects_to_root(client, ui, session):
    make_user(session, "operator-4", "TEST-pass")
    client.get("/login")
    client.post("/login", data={"login": "operator-4", "password": "TEST-pass"})
    response = client.get("/login", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_logout_clears_session_and_redirects_to_login(client, ui, session):
    make_user(session, "operator-5", "TEST-pass")
    client.get("/login")
    client.post("/login", data={"login": "operator-5", "password": "TEST-pass"})
    response = client.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert "max-age=0" in response.headers["set-cookie"].lower()
    after = client.get("/login", follow_redirects=False)
    assert after.status_code == 200


def test_tampered_session_cookie_is_rejected(client, ui):
    response = client.get(
        "/login", cookies={SESSION_COOKIE: "garbage-token"}, follow_redirects=False
    )
    assert response.status_code == 200


def test_password_never_appears_in_response_or_logs(client, ui, session, caplog):
    make_user(session, "operator-6", "TEST-secret-password")
    client.get("/login")
    with caplog.at_level(logging.DEBUG):
        bad = client.post(
            "/login", data={"login": "operator-6", "password": "TEST-wrong-password"},
            follow_redirects=False,
        )
        good = client.post(
            "/login", data={"login": "operator-6", "password": "TEST-secret-password"},
            follow_redirects=False,
        )
    assert bad.status_code == 200
    assert good.status_code == 303
    assert "TEST-secret-password" not in bad.text
    assert "TEST-secret-password" not in caplog.text


@pytest.fixture()
def live(tmp_path, monkeypatch):
    """Чистый первый старт: временная БД, без seed-файла, админ-env по умолчанию пуст."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "live.sqlite"))
    monkeypatch.setenv("SEED_PATH", "TEST-no-seed.json")
    monkeypatch.setenv("ADMIN_LOGIN", "")
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    monkeypatch.setenv("PANEL_SECRET_KEY", "TEST-secret")
    reset_settings_cache()
    yield monkeypatch
    reset_settings_cache()
    get_engine().dispose()
    set_engine(None)


def test_bootstrap_creates_admin_once_from_env(live):
    live.setenv("ADMIN_LOGIN", "TEST-admin")
    live.setenv("ADMIN_PASSWORD", "TEST-pass-123")
    with TestClient(app) as c:
        with Session(get_engine()) as session:
            admins = session.scalars(select(User).where(User.role == "admin")).all()
        assert len(admins) == 1
        assert admins[0].login == "TEST-admin"
        first = c.post(
            "/login",
            data={"login": "TEST-admin", "password": "TEST-pass-123",
                  "csrf_token": seed_csrf(c)},
            follow_redirects=False,
        )
        assert first.status_code == 303
    with TestClient(app):
        with Session(get_engine()) as session:
            admins = session.scalars(select(User).where(User.role == "admin")).all()
        assert len(admins) == 1


def test_empty_db_without_env_shows_setup_screen_everywhere(live):
    with TestClient(app) as c:
        for path in ("/", "/login", "/users"):
            response = c.get(path)
            assert response.status_code == 200
            assert "Панель не инициализирована" in response.text
            assert "ADMIN_LOGIN" in response.text
            assert "ADMIN_PASSWORD" in response.text
        posted = c.post("/login", data={"login": "x", "password": "y"})
        assert posted.status_code == 200
        assert "Панель не инициализирована" in posted.text


def test_setup_screen_hides_after_bootstrap(live):
    live.setenv("ADMIN_LOGIN", "TEST-admin")
    live.setenv("ADMIN_PASSWORD", "TEST-pass-123")
    with TestClient(app) as c:
        response = c.get("/login")
        assert response.status_code == 200
        assert "Панель не инициализирована" not in response.text
        assert 'name="password"' in response.text


def test_setup_screen_keeps_static_and_health(live):
    with TestClient(app) as c:
        assert c.get("/healthz").status_code == 200
        assert c.get("/static/tailwind.css").status_code == 200


def test_hash_password_names_limit_in_russian():
    with pytest.raises(ValueError, match="длиннее"):
        hash_password("TEST-" + "x" * 100)


def test_bootstrap_with_too_long_admin_password_starts_panel_and_logs(live, caplog):
    live.setenv("ADMIN_LOGIN", "TEST-admin")
    live.setenv("ADMIN_PASSWORD", "TEST-" + "x" * 100)
    with caplog.at_level(logging.ERROR):
        with TestClient(app) as c:
            assert c.get("/healthz").status_code == 200
            assert c.get("/login").status_code == 200
    assert "72 байт" in caplog.text
    assert "TEST-" not in caplog.text
    with Session(get_engine()) as session:
        assert session.scalars(select(func.count()).select_from(User)).one() == 0
