"""«Мой профиль»: смена своего пароля и личный внешний вид у каждого пользователя.

Личный вид хранится на сервере (Setting user_pref:{id}:*), поэтому работает
с любого устройства; админ может запретить личные настройки (personal_appearance).
"""
from conftest import CsrfClient, login, make_user
from sqlalchemy import select

from app.auth import verify_password
from app.main import app
from app.models import User
from app.sync import SETTING_DEFAULT_THEME, SETTING_PERSONAL_APPEARANCE, set_setting


def _change(client, current: str, new: str, repeat: str | None = None):
    return client.post(
        "/profile/password",
        data={
            "current_password": current,
            "new_password": new,
            "new_password_repeat": new if repeat is None else repeat,
        },
        follow_redirects=False,
    )


def test_profile_requires_login(client, ui):
    response = client.get("/profile", follow_redirects=False)
    assert response.status_code == 303


def test_profile_page_for_operator(client, ui, session):
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    html = client.get("/profile").text
    assert "Мой профиль" in html
    assert 'action="/profile/password"' in html
    assert 'action="/profile/appearance"' in html
    assert 'href="/profile"' in client.get("/tickets").text, "ссылка на профиль в шапке"


def test_wrong_current_password_rejected(client, ui, session):
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    response = _change(client, "TEST-wrong", "TEST-new-pass-1")
    assert response.status_code == 200
    assert "Текущий пароль неверный" in response.text


def test_short_and_mismatched_passwords_rejected(client, ui, session):
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    assert "короче 8" in _change(client, "TEST-pass", "TEST-1").text
    mismatch = _change(client, "TEST-pass", "TEST-new-pass-1", "TEST-new-pass-2")
    assert "не совпадают" in mismatch.text
    same = _change(client, "TEST-pass", "TEST-pass", "TEST-pass")
    assert "короче 8" in same.text or "совпадает с текущим" in same.text


def test_password_change_keeps_own_session_and_drops_others(client, ui, session):
    make_user(session, "TEST-op")
    other = CsrfClient(app)
    login(other, "TEST-op")
    login(client, "TEST-op")
    response = _change(client, "TEST-pass", "TEST-new-pass-1")
    assert response.status_code == 303
    assert response.headers["location"] == "/profile?done=password"
    page = client.get(response.headers["location"])
    assert page.status_code == 200 and "Пароль изменён" in page.text, "своя сессия жива"
    assert other.get("/tickets", follow_redirects=False).status_code == 303, (
        "сессия на другом устройстве отвалилась"
    )
    session.expire_all()
    user = session.scalar(select(User).where(User.login == "TEST-op"))
    assert verify_password("TEST-new-pass-1", user.password_hash)


def test_wrong_current_password_is_throttled(client, ui, session):
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    for _ in range(10):
        response = _change(client, "TEST-wrong", "TEST-new-pass-1")
        if response.status_code == 429:
            break
    assert response.status_code == 429


def _attrs(html: str) -> tuple[str, str, str]:
    import re

    match = re.search(r'data-theme="(\w+)" data-cards="(\w+)" data-buttons="(\w+)"', html)
    assert match, "атрибуты вида на <html>"
    return match.group(1), match.group(2), match.group(3)


def test_personal_appearance_is_per_user(client, ui, session):
    make_user(session, "TEST-a")
    make_user(session, "TEST-b")
    login(client, "TEST-a")
    response = client.post(
        "/profile/appearance",
        data={"theme": "new", "card_shape": "square", "button_shape": "round"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert _attrs(client.get("/tickets").text) == ("new", "square", "round")

    second = CsrfClient(app)  # «другое устройство» того же пользователя
    login(second, "TEST-a")
    assert _attrs(second.get("/tickets").text) == ("new", "square", "round")

    other = CsrfClient(app)
    login(other, "TEST-b")
    assert _attrs(other.get("/tickets").text) == ("classic", "round", "square")


def test_theme_toggle_fetch_saves_theme_only(client, ui, session):
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    response = client.post(
        "/profile/appearance", data={"theme": "new"}, headers={"X-Requested-With": "fetch"}
    )
    assert response.status_code == 200
    assert _attrs(client.get("/").text)[0] == "new"


def test_reset_returns_to_common_look(client, ui, session):
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    client.post("/profile/appearance", data={"theme": "new", "card_shape": "square"})
    client.post("/profile/appearance", data={"reset": "1"})
    assert _attrs(client.get("/tickets").text) == ("classic", "round", "square")


def test_garbage_appearance_rejected(client, ui, session):
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    response = client.post("/profile/appearance", data={"theme": "silach"})
    assert "Неизвестное значение" in response.text
    assert _attrs(client.get("/tickets").text)[0] == "classic"


def test_admin_can_forbid_personal_appearance(client, ui, session):
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    client.post("/profile/appearance", data={"theme": "classic"})
    set_setting(SETTING_PERSONAL_APPEARANCE, "0")
    set_setting(SETTING_DEFAULT_THEME, "new")
    html = client.get("/tickets").text
    assert _attrs(html)[0] == "new", "при запрете — вид админа, личный игнорируется"
    assert 'id="theme-toggle"' not in html, "переключателя нет"
    forbidden = client.post(
        "/profile/appearance", data={"theme": "new"}, headers={"X-Requested-With": "fetch"}
    )
    assert forbidden.status_code == 403
    assert "задаёт администратор" in client.get("/profile").text


def test_personal_toggle_in_admin(client, ui, session):
    make_user(session, "TEST-chief", "TEST-pass", role="admin")
    login(client, "TEST-chief")
    html = client.get("/admin").text
    assert "Разрешить менять вид индивидуально" in html
    response = client.post(
        f"/admin/features/{SETTING_PERSONAL_APPEARANCE}", data={"value": "0"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert 'id="theme-toggle"' not in client.get("/tickets").text
