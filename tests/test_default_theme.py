"""Тема по умолчанию из админки (тикет T14): Setting default_theme, селектор
«Внешний вид», инъекция theme_default в контекст каждой страницы и пресет,
не перебивающий серверный выбор.

Сервер рендерит data-theme из настройки (PanelTemplates -> base.html), поэтому
новый посетитель без cookie видит выбранную админом тему. Cookie panel-theme
применяется на клиенте (theme-preset.js) и приоритетнее настройки — этот
контракт проверяется по тексту статического файла, браузера в тестах нет.
"""


from conftest import CsrfClient, login, make_user

from app.main import app
from app.sync import (
    SETTING_DEFAULT_THEME,
    VALID_THEMES,
    default_settings,
    get_setting,
    set_setting,
)


def _admin_login(client, session) -> None:
    make_user(session, "TEST-chief", "TEST-pass", role="admin")
    login(client, "TEST-chief")


def _set_theme(client, value: str):
    return client.post(
        "/admin/settings/theme-default", data={"value": value}, follow_redirects=False
    )


# --- единый источник констант и дефолт первого старта ---


def test_valid_themes_fixed_set():
    assert VALID_THEMES == ("new", "classic")


def test_default_settings_carry_classic_theme():
    assert default_settings()[SETTING_DEFAULT_THEME] == "classic"


# --- новый посетитель видит тему, выбранную админом ---


def test_new_visitor_sees_classic_before_admin_chooses(client, ui):
    html = client.get("/login").text
    assert 'data-theme="classic"' in html


def test_admin_change_saves_and_serves_to_new_visitor(client, ui, session):
    _admin_login(client, session)
    response = _set_theme(client, "new")
    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/admin"
    assert get_setting(SETTING_DEFAULT_THEME) == "new"
    newcomer = CsrfClient(app)
    html = newcomer.get("/login").text
    assert 'data-theme="new"' in html


def test_removed_theme_in_setting_falls_back_to_classic(client, ui):
    set_setting(SETTING_DEFAULT_THEME, "silach")  # снятый id темы
    assert 'data-theme="classic"' in client.get("/login").text


def test_admin_page_selector_shows_current_value(client, ui, session):
    _admin_login(client, session)
    assert "Тема по умолчанию" in client.get("/admin").text
    _set_theme(client, "new")
    html = client.get("/admin").text
    assert 'action="/admin/settings/theme-default"' in html
    assert '<option value="new" selected>Ночь</option>' in html
    assert 'value="soft"' not in html, "«Ночь мягкая» снята"
    assert '<option value="classic">Классика</option>' in html
    assert 'value="silach"' not in html


# --- невалидное значение: русская ошибка, Setting не тронут ---


def test_invalid_value_keeps_setting_and_reports_in_russian(client, ui, session):
    _admin_login(client, session)
    response = _set_theme(client, "hacker")
    assert response.status_code == 200
    assert "Неизвестная тема" in response.text
    assert get_setting(SETTING_DEFAULT_THEME, "classic") == "classic"


# --- права: admin пишет, оператору 403, аноним 303 на /login ---


def test_operator_post_is_forbidden(client, ui, session):
    make_user(session, "TEST-op", "TEST-pass")
    login(client, "TEST-op")
    response = _set_theme(client, "new")
    assert response.status_code == 403
    assert get_setting(SETTING_DEFAULT_THEME, "classic") == "classic"


def test_anonymous_post_redirects_to_login(client, ui):
    response = client.post(
        "/admin/settings/theme-default",
        data={"value": "new"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


# --- форма плашек и кнопок: «Круглые» / «Прямоугольные» ---


def _set_shapes(client, card: str, button: str):
    return client.post(
        "/admin/settings/shapes",
        data={"card_shape": card, "button_shape": button},
        follow_redirects=False,
    )


def test_shapes_defaults_on_html(client, ui):
    html = client.get("/login").text
    assert 'data-cards="round"' in html
    assert 'data-buttons="square"' in html


def test_admin_changes_shapes_for_everyone(client, ui, session):
    from app.sync import SETTING_BUTTON_SHAPE, SETTING_CARD_SHAPE

    _admin_login(client, session)
    admin_html = client.get("/admin").text
    assert "Круглые" in admin_html and "Прямоугольные" in admin_html
    response = _set_shapes(client, "square", "round")
    assert response.status_code == 303
    assert get_setting(SETTING_CARD_SHAPE) == "square"
    assert get_setting(SETTING_BUTTON_SHAPE) == "round"
    html = CsrfClient(app).get("/login").text
    assert 'data-cards="square"' in html and 'data-buttons="round"' in html


def test_invalid_shape_rejected_in_russian(client, ui, session):
    from app.sync import SETTING_CARD_SHAPE

    _admin_login(client, session)
    response = _set_shapes(client, "oval", "round")
    assert response.status_code == 200
    assert "Неизвестная форма" in response.text
    assert get_setting(SETTING_CARD_SHAPE, "round") == "round"


def test_operator_cannot_change_shapes(client, ui, session):
    make_user(session, "TEST-op", "TEST-pass")
    login(client, "TEST-op")
    assert _set_shapes(client, "square", "square").status_code == 403


def test_css_shape_tokens_exist(client, ui):
    css = client.get("/static/tailwind.css").text
    for selector in ('[data-cards="round"]', '[data-cards="square"]',
                     '[data-buttons="round"]', '[data-buttons="square"]'):
        assert selector in css
    assert "border-radius: var(--r-btn)" in css and "border-radius: var(--r-card)" in css
