"""Двое часов в шапке: время устройства (считает браузер) и настраиваемый пояс
(админ задаёт для всех — Setting clock_zone, профиль переопределяет —
user_pref:{id}:clock_zone). Только показ: плановое время заявок остаётся по Москве.

Сервер отдаёт зону и подпись в data-атрибутах и начальное «ЧЧ:ММ»; тикает app.js
([data-clock-tz]), поэтому контракт проверяется по разметке и по тексту скрипта.
"""

import re
from zoneinfo import ZoneInfo

from conftest import CsrfClient, login, make_user

from app.main import app
from app.sync import (
    CLOCK_ZONES,
    DEFAULT_CLOCK_ZONE,
    SETTING_CLOCK_ZONE,
    VALID_CLOCK_ZONES,
    clock_time,
    clock_zone,
    default_settings,
    get_setting,
    set_setting,
)

CLOCK_RE = re.compile(r'data-clock-tz="([A-Za-z_/]+)">(\d\d:\d\d)<')


def _zone_in_topbar(html: str) -> tuple[str, str]:
    match = CLOCK_RE.search(html)
    assert match, "вторые часы с серверным «ЧЧ:ММ» в шапке"
    return match.group(1), match.group(2)


def test_zone_list_is_closed_and_known_to_zoneinfo():
    assert default_settings()[SETTING_CLOCK_ZONE] == DEFAULT_CLOCK_ZONE == "Europe/Moscow"
    assert len(VALID_CLOCK_ZONES) == len(set(VALID_CLOCK_ZONES))
    assert "Europe/Moscow" in VALID_CLOCK_ZONES and "UTC" in VALID_CLOCK_ZONES
    for zone, label in CLOCK_ZONES:
        ZoneInfo(zone)  # зона есть в базе tzdata сервера
        assert label
    assert re.fullmatch(r"\d\d:\d\d", clock_time("Asia/Vladivostok"))
    assert clock_time("Mars/Olympus") == ""


def test_garbage_in_setting_reads_as_moscow(client, ui):
    set_setting(SETTING_CLOCK_ZONE, "Mars/Olympus")
    assert clock_zone() == "Europe/Moscow"


def test_topbar_has_device_clock_and_zone_clock(client, ui, session):
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    html = client.get("/tickets").text
    assert 'data-clock-tz="local">--:--<' in html, "первые часы заполняет браузер"
    assert 'data-clock-local data-clock-zone="Europe/Moscow"' in html
    assert _zone_in_topbar(html)[0] == "Europe/Moscow"
    assert "Москва" in html and "у вас" in html
    assert 'data-clock-tz="Europe/Moscow"' in client.get("/").text, "дата главной тикает по МСК"
    script = client.get("/static/app.js").text
    assert "data-clock-tz" in script and "timeZone" in script and "60000" in script


def test_login_page_without_topbar_has_no_clocks(client, ui):
    assert "data-clock-tz" not in client.get("/login").text


def test_admin_sets_zone_for_everyone(client, ui, session):
    make_user(session, "TEST-chief", "TEST-pass", role="admin")
    login(client, "TEST-chief")
    admin_html = client.get("/admin").text
    assert 'action="/admin/settings/clock-zone"' in admin_html
    assert '<option value="Europe/Moscow" selected>Москва</option>' in admin_html
    response = client.post(
        "/admin/settings/clock-zone", data={"value": "Asia/Vladivostok"}, follow_redirects=False
    )
    assert response.status_code == 303 and response.headers["location"] == "/admin"
    assert get_setting(SETTING_CLOCK_ZONE) == "Asia/Vladivostok"

    make_user(session, "TEST-op")
    other = CsrfClient(app)
    login(other, "TEST-op")
    html = other.get("/tickets").text
    assert _zone_in_topbar(html)[0] == "Asia/Vladivostok" and "Владивосток" in html

    bad = client.post("/admin/settings/clock-zone", data={"value": "Mars/Olympus"})
    assert bad.status_code == 200 and "Неизвестный часовой пояс" in bad.text
    assert get_setting(SETTING_CLOCK_ZONE) == "Asia/Vladivostok"


def test_operator_cannot_set_zone_for_everyone(client, ui, session):
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    response = client.post("/admin/settings/clock-zone", data={"value": "UTC"})
    assert response.status_code == 403
    assert clock_zone() == "Europe/Moscow"


def test_profile_overrides_and_reset_returns_to_common(client, ui, session):
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    profile = client.get("/profile").text
    assert 'name="clock_zone"' in profile and "Вторые часы в шапке" in profile
    response = client.post(
        "/profile/appearance", data={"clock_zone": "Asia/Yekaterinburg"}, follow_redirects=False
    )
    assert response.status_code == 303
    html = client.get("/tickets").text
    assert _zone_in_topbar(html)[0] == "Asia/Yekaterinburg" and "Екатеринбург" in html
    assert '<option value="Asia/Yekaterinburg" selected>' in client.get("/profile").text

    garbage = client.post("/profile/appearance", data={"clock_zone": "Mars/Olympus"})
    assert "Неизвестное значение" in garbage.text
    assert _zone_in_topbar(client.get("/tickets").text)[0] == "Asia/Yekaterinburg"

    client.post("/profile/appearance", data={"reset": "1"})
    assert _zone_in_topbar(client.get("/tickets").text)[0] == "Europe/Moscow"


def test_theme_toggle_fetch_keeps_personal_zone(client, ui, session):
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    client.post("/profile/appearance", data={"clock_zone": "Asia/Omsk"})
    client.post(
        "/profile/appearance", data={"theme": "tablo"}, headers={"X-Requested-With": "fetch"}
    )
    html = client.get("/tickets").text
    assert 'data-theme="tablo"' in html and _zone_in_topbar(html)[0] == "Asia/Omsk"
