"""Язык интерфейса RU/EN: каталог перевода, переключатель, личный выбор, язык по умолчанию.

Строки пишутся по-русски в `_()` / `N_()`; английский — app/locales/en.json.
Каталог проверяется на полноту в обе стороны: нет непереведённых строк и нет
мусорных ключей, поля {…} в переводе те же, что в оригинале.
"""

import json
import re
from pathlib import Path

import httpx
from conftest import CsrfClient, login, make_user, seed_ticket, telegram_mock

from app.i18n import CATALOG_PATH, LANG_COOKIE, SETTING_DEFAULT_LANGUAGE
from app.main import app
from app.sync import (
    SETTING_TELEGRAM_ENABLED,
    enqueue,
    poll_once,
    set_clients,
    set_setting,
    user_language,
)
from app.tickets import create_ticket

ROOT = Path(__file__).resolve().parent.parent
MSGID = re.compile(r'(?<![\w.])N?_\(\s*"((?:[^"\\]|\\.)*)"')
FIELD = re.compile(r"\{([^{}]+)\}")
CYRILLIC = re.compile(r"[А-Яа-яЁё]")
# Подписи кнопки RU — на своём языке и в английском интерфейсе.
OWN_LANGUAGE_LABELS = ("Переключить на русский", "Русский")


def _source_msgids() -> set[str]:
    files = [*(ROOT / "templates").rglob("*.html"), *(ROOT / "app").rglob("*.py")]
    return {match for path in files for match in MSGID.findall(path.read_text("utf-8"))}


def _catalog() -> dict[str, str]:
    return json.loads(CATALOG_PATH.read_text("utf-8"))


def test_catalog_covers_every_string_and_has_no_leftovers():
    msgids = _source_msgids()
    catalog = _catalog()
    missing = sorted(msgids - catalog.keys())
    unused = sorted(catalog.keys() - msgids)
    assert not missing, f"нет перевода: {missing}"
    assert not unused, f"лишние ключи в en.json: {unused}"


def test_translations_keep_placeholders_and_have_no_cyrillic():
    for source, english in _catalog().items():
        if "{" in source and not FIELD.search(english):
            raise AssertionError(f"потеряна подстановка: {source!r} -> {english!r}")
        if FIELD.search(source) and all(CYRILLIC.search(f) is None for f in FIELD.findall(source)):
            assert sorted(FIELD.findall(source)) == sorted(FIELD.findall(english)), source
        assert not CYRILLIC.search(english), f"кириллица в переводе: {english!r}"


def _switch(client, lang: str, next_path: str = "/tickets"):
    return client.post("/lang", data={"lang": lang, "next": next_path}, follow_redirects=False)


def test_login_page_is_russian_by_default_and_offers_english(client, ui):
    html = client.get("/login").text
    assert '<html lang="ru"' in html
    assert "Вход в панель управления" in html
    assert 'action="/lang"' in html and ">English<" in html


def test_switch_sets_cookie_and_redirects_back(client, ui):
    client.get("/login")
    response = _switch(client, "en", "/login?next=/tickets")
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/tickets"
    assert client.cookies.get(LANG_COOKIE) == "en"
    html = client.get("/login").text
    assert '<html lang="en"' in html
    assert "Sign in to the control panel" in html
    assert ">Русский<" in html


def test_switch_ignores_garbage_language_and_foreign_next(client, ui):
    client.get("/login")
    response = _switch(client, "de", "https://evil.test/")
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert client.cookies.get(LANG_COOKIE) is None


def test_switch_requires_csrf(client, ui):
    client.get("/login")
    response = client.post(
        "/lang", data={"lang": "en", "next": "/", "csrf_token": "bad"}, follow_redirects=False
    )
    assert response.status_code == 403


def test_signed_in_choice_follows_user_to_another_device(client, ui, session):
    operator = make_user(session, "TEST-op")
    login(client, "TEST-op")
    _switch(client, "en")
    assert user_language(operator.id) == "en"
    assert "Open tickets" in client.get("/tickets?status=open").text

    other_device = CsrfClient(app)
    login(other_device, "TEST-op")
    assert other_device.cookies.get(LANG_COOKIE) == "en"
    assert '<html lang="en"' in other_device.get("/").text


def test_appearance_reset_keeps_language(client, ui, session):
    operator = make_user(session, "TEST-op")
    login(client, "TEST-op")
    _switch(client, "en")
    client.post("/profile/appearance", data={"theme": "classic"}, follow_redirects=False)
    client.post("/profile/appearance", data={"reset": "1"}, follow_redirects=False)
    assert user_language(operator.id) == "en"


def test_admin_default_language_applies_without_cookie(client, ui, session):
    make_user(session, "TEST-admin", role="admin")
    login(client, "TEST-admin")
    response = client.post(
        "/admin/settings/language-default", data={"value": "en"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert 'lang="en"' in CsrfClient(app).get("/login").text

    bad = client.post("/admin/settings/language-default", data={"value": "xx"})
    assert "Unknown language" in bad.text


def test_english_pages_have_no_russian_left(client, ui, session):
    admin = make_user(session, "TEST-admin", role="admin")
    make_user(session, "TEST-op")
    open_ticket = seed_ticket(
        session, number=1, title="Update 1038", description="latest release",
        planned_at="2026-10-20 02:00", status="open", created_by=admin.id,
    )
    seed_ticket(
        session, number=2, title="Check access", description="new credentials",
        status="closed", comment="Done", created_by=admin.id, closed_by=admin.id,
    )
    login(client, "TEST-admin")
    _switch(client, "en")
    client.post(
        "/templates/create",
        data={"name": "Release", "title_tpl": "Release {db}", "description_tpl": "to storage"},
    )
    pages = [
        "/",
        "/tickets?status=open",
        "/tickets?status=closed",
        "/tickets/new",
        f"/tickets/{open_ticket.id}/edit",
        f"/tickets/{open_ticket.id}/full",
        "/tickets/template-panel",
        "/templates",
        "/profile",
        "/admin",
        "/users",
    ]
    for path in pages:
        response = client.get(path)
        assert response.status_code == 200, path
        text = response.text
        for label in OWN_LANGUAGE_LABELS:
            text = text.replace(label, "")
        leftover = sorted(set(CYRILLIC.findall(text)))
        snippet = text[max(0, text.find(leftover[0]) - 60):][:120] if leftover else ""
        assert not leftover, f"{path}: русский текст в английском интерфейсе: {snippet!r}"


def test_poller_writes_telegram_in_default_language(ui, session):
    set_setting(SETTING_DEFAULT_LANGUAGE, "en")
    set_setting(SETTING_TELEGRAM_ENABLED, "1")
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True})

    set_clients(telegram=telegram_mock(handler))
    operator = make_user(session, "TEST-op")
    ticket = create_ticket("Update 1038", "latest release", None, operator)
    enqueue(ticket.id, {"telegram_new"})
    poll_once()
    assert sent and sent[0].startswith("New ticket #1: Update 1038")
    assert "Author: TEST-op" in sent[0]
