"""Безопасность (G07): троттлинг входа, CSRF на POST, тайминг логина,
CSV-формулы, security-заголовки.

Каждый блок проверяется через HTTP (TestClient) как чёрный ящик; время в
троттлинге подменяется на шве app.auth.monotonic; живой сети нет.
"""

import csv
import io
import time

import httpx
from conftest import login, make_user, seed_ticket, telegram_mock, usedesk_mock
from fastapi import Request
from sqlalchemy import func, select

from app import auth
from app.auth import client_ip
from app.config import reset_settings_cache
from app.main import app
from app.models import Ticket, User
from app.sync import SETTING_TELEGRAM_ENABLED, get_setting
from app.tickets import close_ticket, create_ticket


def _ok_telegram(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True})

# --- security-заголовки (middleware) ---


def test_security_headers_on_every_page(client, ui):
    for path in ("/login", "/healthz", "/static/tailwind.css"):
        response = client.get(path)
        assert response.headers["x-frame-options"] == "DENY", path
        assert response.headers["x-content-type-options"] == "nosniff", path
        assert response.headers["referrer-policy"] == "same-origin", path
        csp = response.headers["content-security-policy"]
        assert "default-src 'self'" in csp, path
        assert "style-src 'self' 'unsafe-inline'" in csp, path
        assert "script-src 'self'" in csp, path
        assert "script-src 'self' 'unsafe-inline'" not in csp, path


def test_csp_keeps_static_assets_available(client, ui):
    page = client.get("/login")
    assert page.status_code == 200
    assert client.get("/static/tailwind.css").status_code == 200
    assert client.get("/static/htmx.min.js").status_code == 200
    assert client.get("/static/theme.js").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/favicon.svg").status_code == 200


def test_pages_load_no_inline_scripts(client, ui, session):
    """CSP script-src 'self': инлайн-скриптов и on*-атрибутов в разметке быть не должно."""
    seed_ticket(session, number=1, title="разметка", description="")
    make_user(session, "TEST-admin", "TEST-pass", role="admin")
    login(client, "TEST-admin")
    for path in ("/login", "/", "/tickets/new", "/tickets/1/edit", "/users", "/admin"):
        page = client.get(path)
        assert page.status_code == 200, path
        assert "<script>" not in page.text, path
        assert "onclick=" not in page.text, path
        assert "onsubmit=" not in page.text, path


# --- CSV-формулы ---


def test_export_csv_escapes_formula_cells(client, ui, session):
    actor = make_user(session, "TEST-op")
    first = create_ticket("=cmd()", "описание с формулой", None, actor)
    second = create_ticket("+1", "текст", None, actor)
    third = create_ticket("@SUM(A1)", "текст", None, actor)
    fourth = create_ticket("минус", "-2-2=минус", None, actor)
    close_ticket(first.id, None, actor)
    fifth = seed_ticket(
        session, number=9001, title="табуляция",
        description="\tTAB-начало описания", status="open",
    )
    make_user(session, "chief", "TEST-pass", role="admin")
    login(client, "chief", "TEST-pass")
    response = client.get("/admin/export.csv")
    assert response.status_code == 200
    body = response.content.decode("utf-8").removeprefix("\ufeff")
    rows = {int(row[0]): row for row in csv.reader(io.StringIO(body)) if row and row[0].isdigit()}
    assert rows[first.number][1] == "'=cmd()", "title с = экранирован"
    assert rows[second.number][1] == "'+1", "title с + экранирован"
    assert rows[third.number][1] == "'@SUM(A1)", "title с @ экранирован"
    assert rows[fourth.number][2] == "'-2-2=минус", "description с - экранирован"
    assert rows[fifth.number][2] == "'\tTAB-начало описания", "description с табом экранирован"


def test_export_csv_keeps_normal_cells_untouched(client, ui, session):
    actor = make_user(session, "TEST-op")
    ticket = create_ticket("Обновление Клиент-Мед", "обычное описание", None, actor)
    make_user(session, "chief", "TEST-pass", role="admin")
    login(client, "chief", "TEST-pass")
    body = client.get("/admin/export.csv").content.decode("utf-8").removeprefix("\ufeff")
    rows = {int(row[0]): row for row in csv.reader(io.StringIO(body)) if row and row[0].isdigit()}
    assert rows[ticket.number][1] == "Обновление Клиент-Мед"
    assert rows[ticket.number][2] == "обычное описание"


# --- троттлинг входа ---


def test_client_ip_ignores_forwarded_for():
    scope = {
        "type": "http",
        "client": ("10.0.0.9", 50000),
        "headers": [(b"x-forwarded-for", b"8.8.8.8")],
    }
    assert client_ip(Request(scope)) == "10.0.0.9"


def test_sixth_login_attempt_gets_429_russian_message(client, ui, session):
    make_user(session, "operator-t", "TEST-pass")
    client.get("/login")
    for _ in range(5):
        failed = client.post(
            "/login", data={"login": "operator-t", "password": "TEST-wrong"}
        )
        assert failed.status_code == 200, "первые пять неудач — обычная ошибка входа"
    sixth = client.post("/login", data={"login": "operator-t", "password": "TEST-wrong"})
    assert sixth.status_code == 429
    assert "Слишком много попыток" in sixth.text


def test_correct_password_admitted_during_pause(client, ui, session):
    make_user(session, "operator-p", "TEST-pass")
    client.get("/login")
    for _ in range(5):
        client.post("/login", data={"login": "operator-p", "password": "TEST-wrong"})
    wrong = client.post(
        "/login", data={"login": "operator-p", "password": "TEST-still-wrong"},
        follow_redirects=False,
    )
    assert wrong.status_code == 429, "неверный пароль во время паузы — 429"
    admitted = client.post(
        "/login", data={"login": "operator-p", "password": "TEST-pass"},
        follow_redirects=False,
    )
    assert admitted.status_code == 303, "верный пароль во время паузы проходит"


def test_login_failures_prune_stale_entries(client, ui):
    auth._login_failures.clear()
    now = auth.monotonic()
    auth._login_failures[("old", "ip")] = (
        1, 0.0, now - auth.LOGIN_THROTTLE_WINDOW_SECONDS - 1
    )
    auth._login_failures[("fresh", "ip")] = (1, 0.0, now)
    auth.record_login_failure(("another", "ip"))
    assert ("old", "ip") not in auth._login_failures, "записи старше окна вычищаются"
    assert ("fresh", "ip") in auth._login_failures
    assert ("another", "ip") in auth._login_failures


def test_success_resets_failure_counter(client, ui, session):
    make_user(session, "operator-r", "TEST-pass")
    client.get("/login")
    for _ in range(4):
        client.post("/login", data={"login": "operator-r", "password": "TEST-wrong"})
    client.post(
        "/login", data={"login": "operator-r", "password": "TEST-pass"},
        follow_redirects=False,
    )
    client.post("/logout")
    client.get("/login")
    after = client.post("/login", data={"login": "operator-r", "password": "TEST-wrong"})
    assert after.status_code == 200, "успех сбросил счётчик — одна неудача ещё не блокирует"
    assert "Слишком много попыток" not in after.text


# --- сессии: смена пароля отзывает живые cookie ---


def test_password_change_revokes_sessions(client, ui, session):
    from conftest import CsrfClient

    rotated = "TEST-rotated-pass"
    make_user(session, "TEST-admin", "TEST-pass", role="admin")
    make_user(session, "TEST-victim", "TEST-pass")
    operator_client = CsrfClient(app)
    login(operator_client, "TEST-victim")
    assert operator_client.get("/", follow_redirects=False).status_code == 200

    login(client, "TEST-admin")
    victim = session.scalar(select(User).where(User.login == "TEST-victim"))
    changed = client.post(
        f"/users/{victim.id}/password",
        data={"password": rotated},
        follow_redirects=False,
    )
    assert changed.status_code == 303
    revoked = operator_client.get("/", follow_redirects=False)
    assert revoked.status_code == 303, "старая cookie сессии умерла со сменой пароля"


# --- поверхность API ---


def test_openapi_docs_closed_for_anonymous(client, ui):
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404


def test_malformed_body_returns_400_not_500(client, ui):
    response = client.post(
        "/login",
        content=b"\xff\xfe\x00broken",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 400
    assert "Некорректное тело запроса" in response.text


def test_title_length_limit(client, ui, session):
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    response = client.post(
        "/tickets/create",
        data={"title": "а" * 300, "description": "описание"},
    )
    assert response.status_code == 200
    assert "Заголовок длиннее 255 символов" in response.text
    assert session.scalar(select(func.count()).select_from(Ticket)) == 0


def test_user_login_length_limit(client, ui, session):
    make_user(session, "TEST-admin", "TEST-pass", role="admin")
    login(client, "TEST-admin")
    response = client.post(
        "/users/create",
        data={"login": "b" * 70, "password": "TEST-pass"},
    )
    assert response.status_code == 200
    assert "Логин длиннее 64 символов" in response.text
    assert session.scalar(select(func.count()).select_from(User)) == 1


def test_throttle_is_per_login_and_ip(client, ui, session):
    make_user(session, "operator-a", "TEST-pass")
    make_user(session, "operator-b", "TEST-pass")
    client.get("/login")
    for _ in range(5):
        client.post("/login", data={"login": "operator-a", "password": "TEST-wrong"})
    other = client.post("/login", data={"login": "operator-b", "password": "TEST-wrong"})
    assert other.status_code == 200, "другой логин с того же IP не наследует блокировку"
    assert "Слишком много попыток" not in other.text


# --- тайминг-выравнивание логина ---


def test_unknown_login_takes_as_long_as_known_one(client, ui, session):
    make_user(session, "operator-slow", "TEST-pass")
    client.get("/login")

    def timed_attempt(login_value: str) -> float:
        start = time.perf_counter()
        response = client.post("/login", data={"login": login_value, "password": "TEST-wrong"})
        elapsed = time.perf_counter() - start
        assert response.status_code == 200
        return elapsed

    known = timed_attempt("operator-slow")
    unknown = timed_attempt("nobody-here")
    assert unknown > 0.10, "неизвестный логин обязан пройти фиктивный bcrypt"
    assert unknown >= 0.3 * known, "время ответа не отличает существование логина"


# --- CSRF ---


def test_mutation_without_csrf_token_is_403(client, ui, session):
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    response = client.post(
        "/tickets/create",
        data={"csrf_token": "", "title": "заявка без токена", "description": "описание"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert session.scalar(select(func.count()).select_from(Ticket)) == 0


def test_mutation_with_forged_token_is_403(client, ui, session):
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    response = client.post(
        "/tickets/create",
        data={"csrf_token": "TEST-forged-token", "title": "подделка", "description": "описание"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert session.scalar(select(func.count()).select_from(Ticket)) == 0


def test_mutation_with_valid_token_works_as_before(client, ui, session):
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    token = client.cookies.get("csrf_token", "")
    response = client.post(
        "/tickets/create",
        data={"csrf_token": token, "title": "с настоящим токеном", "description": "описание"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    stored = session.scalar(select(Ticket))
    assert stored.title == "с настоящим токеном"


def test_htmx_mutation_without_token_403_with_header_passes(client, ui, session):
    make_user(session, "chief", "TEST-pass", role="admin")
    login(client, "chief", "TEST-pass")
    token = client.cookies.get("csrf_token", "")
    denied = client.post(
        "/admin/features/telegram_enabled",
        data={"value": "1", "csrf_token": ""},
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert denied.status_code == 403
    assert get_setting(SETTING_TELEGRAM_ENABLED, "0") == "0"
    allowed = client.post(
        "/admin/features/telegram_enabled",
        data={"value": "1", "csrf_token": ""},
        headers={"HX-Request": "true", "X-CSRF-Token": token},
    )
    assert allowed.status_code == 200, "HTMX-мутация проходит через X-CSRF-Token"
    assert "Включена" in allowed.text
    assert get_setting(SETTING_TELEGRAM_ENABLED, "0") == "1"


def test_login_post_without_any_token_is_403(client, ui, session):
    make_user(session, "operator-csrf", "TEST-pass")
    response = client.post(
        "/login",
        data={"login": "operator-csrf", "password": "TEST-pass", "csrf_token": ""},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_anonymous_mutation_still_redirects_to_login(client, ui, session):
    make_user(session, "TEST-op")
    ticket = create_ticket("чужая заявка", "описание", None, session.scalar(select(User)))
    response = client.post(f"/tickets/{ticket.id}/delete", data={}, follow_redirects=False)
    assert response.status_code == 303, "аноним попадает на вход раньше CSRF"
    assert response.headers["location"].startswith("/login"), response.headers["location"]


def test_pages_render_csrf_meta_and_hidden_fields(client, ui, session):
    make_user(session, "TEST-op")
    make_user(session, "chief", "TEST-pass", role="admin")
    login(client, "chief", "TEST-pass")
    token = client.cookies.get("csrf_token", "")
    assert token, "csrf-cookie выдана при первом рендере"
    list_page = client.get("/tickets")
    assert 'name="csrf-token"' in list_page.text, "мета-тег в head"
    assert 'name="csrf_token"' in list_page.text, "скрытое поле в форме"
    assert f'value="{token}"' in list_page.text
    for path in ("/tickets/new", "/users", "/admin"):
        page = client.get(path)
        assert page.text.count('name="csrf_token"') >= 1, path
    admin_page = client.get("/admin")
    assert admin_page.text.count('name="csrf_token"') >= 4, "все формы админки с токеном"


def test_diagnostic_probe_posts_require_csrf_token(client, ui, session, monkeypatch):
    make_user(session, "chief", "TEST-pass", role="admin")
    login(client, "chief", "TEST-pass")
    token = client.cookies.get("csrf_token", "")
    ui.setenv("USEDESK_API_TOKEN", "")
    ui.setenv("TELEGRAM_BOT_TOKEN", "")
    reset_settings_cache()
    for path in ("/admin/diagnostics/test-usedesk", "/admin/diagnostics/test-telegram"):
        denied = client.post(path, data={"csrf_token": ""}, follow_redirects=False)
        assert denied.status_code == 403, f"{path} без токена обязан быть 403"
    ui.setenv("USEDESK_API_TOKEN", "TEST-ud-token")
    ui.setenv("TELEGRAM_BOT_TOKEN", "TEST-tg-token")
    reset_settings_cache()
    monkeypatch.setattr(
        "app.routes.admin._usedesk_probe_client",
        lambda _token: usedesk_mock(lambda _: httpx.Response(200, json=[])),
    )
    monkeypatch.setattr(
        "app.routes.admin._telegram_probe_client",
        lambda _token: telegram_mock(_ok_telegram),
    )
    for path, ok_text in (
        ("/admin/diagnostics/test-usedesk", "Подключение ок"),
        ("/admin/diagnostics/test-telegram", "Тестовое сообщение отправлено"),
    ):
        allowed = client.post(path, data={"csrf_token": ""}, headers={"X-CSRF-Token": token})
        assert allowed.status_code == 200, path
        assert ok_text in allowed.text, f"{path} с токеном работает как раньше"
