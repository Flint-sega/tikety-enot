"""Темы: «Ночь» (id new) и «Классика» (classic).
data-theme на <html>, переключатель, cookie-контракт theme.js, смоук страниц во всех
темах, CSRF-артефакты.

Механика темы — клиентская (CSS-переменные + JS), поэтому шов проверяется
через публичные HTTP-страницы: разметка задаёт контракт (атрибут, пресет,
кнопка, статический theme.js), а сами палитры живут в static/tailwind.css.
"""

import re

from conftest import login, make_user

THEMES = ("new", "classic")
REMOVED_THEMES = ("soft", "silach", "light", "dark")

PAGES = (
    "/", "/tickets", "/tickets?status=closed", "/tickets/new", "/profile", "/users", "/admin",
)


def test_html_carries_server_rendered_theme(client, ui):
    html = client.get("/login").text
    assert 'data-theme="classic"' in html, "стартовая тема — классика"
    assert "theme-preset.js" not in html, "cookie-пресета нет: тему рендерит сервер"
    assert "/static/theme.js" in html
    assert 'id="theme-toggle"' not in html, "на входе — тема админа, без переключателя"


def test_theme_js_knows_two_themes_and_saves_to_profile(client, ui):
    body = client.get("/static/theme.js").text
    assert "ORDER = ['new', 'classic']" in body
    assert "Ночь" in body and "Классика" in body
    assert "soft" not in body, "«Ночь мягкая» снята: одна ночная тема"
    assert "/profile/appearance" in body, "выбор сохраняется в профиль пользователя"
    assert "X-CSRF-Token" in body
    assert "document.cookie" not in body, "тема больше не живёт в cookie браузера"
    assert client.get("/static/theme-preset.js").status_code == 404


def test_css_has_token_blocks_for_each_theme_only(client, ui):
    css = client.get("/static/tailwind.css").text
    for theme in THEMES:
        assert f':root[data-theme="{theme}"]' in css
    for theme in REMOVED_THEMES:
        assert f'data-theme="{theme}"' not in css, f"снятая тема {theme} не должна остаться в CSS"
    # темы различаются только токенами: никаких перекрасок вида [data-theme] .класс
    assert not re.search(r'\[data-theme="[a-z]+"\][^{,]*\s[.#a-z]', css)


def test_smoke_pages_render_in_every_theme(client, ui, session):
    make_user(session, "TEST-chief", "TEST-pass", role="admin")
    login(client, "TEST-chief")
    for theme in THEMES:
        client.post("/profile/appearance", data={"theme": theme})
        for path in PAGES:
            response = client.get(path)
            assert response.status_code == 200, (theme, path)
            if path != "/login":
                assert f'data-theme="{theme}"' in response.text, (theme, path)
                assert 'id="theme-toggle"' in response.text, (theme, path)


def test_csrf_artifacts_survive_theme_markup(client, ui):
    html = client.get("/login").text
    assert 'name="csrf_token"' in html, "скрытое поле CSRF в форме не должно пропасть"
    assert 'name="csrf-token"' in html, "мета-тег CSRF в head не должен пропасть"


def test_owner_images_served(client, ui):
    images = (
        "raccoon-hero.webp", "hero-bg.webp", "hero-bg-900.webp",
        "raccoon-avatar.webp", "favicon.png",
    )
    for name in images:
        assert client.get(f"/static/{name}").status_code == 200, name


def test_topbar_on_work_pages_not_on_home(client, ui, session):
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    assert 'class="topbar"' in client.get("/tickets").text
    assert 'class="topbar"' not in client.get("/").text, "главная — hero-экран без шапки"


def test_search_is_live_and_has_hotkey(client, ui, session):
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    html = client.get("/tickets").text
    assert re.search(r'<input name="q"[^>]*data-hotkey-search', html)
    assert 'hx-trigger="input changed delay:300ms' in html


def test_card_shows_short_planned_time_without_iso_separator(client, ui, session):
    from app.tickets import create_ticket

    actor = make_user(session, "TEST-op")
    create_ticket("Обновление баз", "описание", "2026-10-12T20:24", actor)
    login(client, "TEST-op")
    html = client.get("/tickets").text
    assert "2026-10-12T20:24" not in html, "ISO-разделитель T не должен попадать в карточку"
    assert "12.10" in html and "20:24" in html
