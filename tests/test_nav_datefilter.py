"""Навигация в шапке по ролям (G03) и фильтр списка по дате исполнения (G04).

Навигация проверяется через HTTP-страницы: ссылки «Заявки»/«Пользователи»/«Админ»
видны по роли, текущий раздел подсвечен, из /users и /admin есть клик к заявкам,
анониму навигации нет.

Фильтр проверяется на двух уровнях: чистый парсер planned_at (ISO с временем и
диапазоном, старые строки dd.mm.yyyy/dd.mm.yy/yyyy-mm-dd, мусор, пусто) и
HTTP-список с seed-заявками (отбор диапазоном, скрытие заявок без даты при
активном фильтре, прежнее поведение без фильтра, параметры в ссылках вкладок
и пагинации).
"""

import re
from datetime import datetime

from conftest import login, make_user, seed_ticket

from app.tickets import parse_planned_at, planned_filter

# --- парсер planned_at ---


def test_parse_iso_minutes_exact():
    assert parse_planned_at("2026-10-20 21:00") == (
        datetime(2026, 10, 20, 21, 0),
        datetime(2026, 10, 20, 21, 0),
    )


def test_parse_iso_t_separator_seconds_truncated():
    assert parse_planned_at("2026-10-20T21:00:45") == (
        datetime(2026, 10, 20, 21, 0),
        datetime(2026, 10, 20, 21, 0),
    )


def test_parse_iso_range_interval():
    assert parse_planned_at("2026-10-20 21:00–23:00") == (
        datetime(2026, 10, 20, 21, 0),
        datetime(2026, 10, 20, 23, 0),
    )


def test_parse_iso_date_only_is_midnight():
    assert parse_planned_at("2026-10-20") == (
        datetime(2026, 10, 20),
        datetime(2026, 10, 20),
    )


def test_parse_old_string_dd_mm_yyyy_first_day():
    assert parse_planned_at("обновить базы 20.10.2026 до обеда") == (
        datetime(2026, 10, 20),
        datetime(2026, 10, 20),
    )


def test_parse_old_string_dd_mm_yy():
    assert parse_planned_at("20.10.26 созвон по базе") == (
        datetime(2026, 10, 20),
        datetime(2026, 10, 20),
    )


def test_parse_old_string_iso_day_inside_text():
    assert parse_planned_at("план на 2026-10-05, отчёт потом") == (
        datetime(2026, 10, 5),
        datetime(2026, 10, 5),
    )


def test_parse_takes_first_day_found():
    assert parse_planned_at("15.09.2026, перенос с 20.10.2026") == (
        datetime(2026, 9, 15),
        datetime(2026, 9, 15),
    )


def test_parse_garbage_and_invalid_dates_return_none():
    assert parse_planned_at("совсем без дат") is None
    assert parse_planned_at("32.13.2026") is None
    assert parse_planned_at("2026-13-45") is None


def test_parse_empty_values_return_none():
    for raw in (None, "", "   "):
        assert parse_planned_at(raw) is None


# --- сборка границ фильтра из GET-параметров ---


def test_no_filter_params_give_no_filter():
    assert planned_filter("", "", "", "") is None


def test_date_bounds_default_to_full_days():
    flt = planned_filter("2026-10-01", "2026-10-31", "", "")
    assert flt is not None
    assert flt.start == datetime(2026, 10, 1)
    assert flt.end == datetime(2026, 10, 31, 23, 59)


def test_time_bounds_narrow_the_window_to_minute():
    flt = planned_filter("2026-10-20", "2026-10-20", "21:00", "21:00")
    assert flt is not None
    assert flt.start == datetime(2026, 10, 20, 21, 0)
    assert flt.end == datetime(2026, 10, 20, 21, 0)


def test_open_sided_bounds():
    flt = planned_filter("", "2026-10-31", "", "12:30")
    assert flt is not None
    assert flt.start is None
    assert flt.end == datetime(2026, 10, 31, 12, 30)


def test_invalid_filter_values_are_ignored():
    assert planned_filter("не-дата", "", "", "") is None
    flt = planned_filter("2026-10-20", "", "25:99", "")
    assert flt is not None
    assert flt.start == datetime(2026, 10, 20)


# --- навигация в шапке ---


def _has_nav_link(html: str, href: str, label: str) -> bool:
    return re.search(rf'<a href="{re.escape(href)}"[^>]*>\s*{label}', html) is not None


def test_nav_operator_sees_only_tickets(client, ui, session):
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    for path in ("/tickets", "/tickets/new"):
        html = client.get(path).text
        assert _has_nav_link(html, "/tickets?status=open", "Открытые"), path
        assert _has_nav_link(html, "/tickets?status=closed", "История"), path
        assert not _has_nav_link(html, "/users", "Пользователи"), path
        assert not _has_nav_link(html, "/admin", "Админ"), path
    home = client.get("/").text
    assert 'href="/tickets?status=open"' in home, "главная ведёт к спискам кнопками-меню"
    assert 'href="/admin"' not in home


def test_nav_admin_sees_all_sections_and_returns_to_tickets(client, ui, session):
    make_user(session, "TEST-chief", "TEST-pass", role="admin")
    login(client, "TEST-chief")
    for path in ("/tickets", "/users", "/admin"):
        html = client.get(path).text
        assert _has_nav_link(html, "/tickets?status=open", "Открытые"), path
        assert _has_nav_link(html, "/users", "Пользователи"), path
        assert _has_nav_link(html, "/admin", "Админ"), path
    assert 'href="/admin"' in client.get("/").text, "админу на главной — ссылка в админку"


def test_nav_highlight_follows_current_section(client, ui, session):
    make_user(session, "TEST-chief", "TEST-pass", role="admin")
    login(client, "TEST-chief")
    users_html = client.get("/users").text
    assert 'href="/users" class="tab tab-active"' in users_html
    assert 'class="tab tab-active">Открытые' not in users_html
    list_html = client.get("/tickets").text
    assert 'class="tab tab-active">Открытые' in list_html
    assert 'href="/users" class="tab tab-active"' not in list_html
    closed_html = client.get("/tickets?status=closed").text
    assert 'class="tab tab-active">История' in closed_html


def test_nav_hidden_for_anonymous(client, ui):
    html = client.get("/login").text
    assert 'class="topbar"' not in html
    assert not _has_nav_link(html, "/users", "Пользователи")


def test_denied_screen_keeps_way_back_to_tickets(client, ui, session):
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    response = client.get("/users")
    assert response.status_code == 403
    assert _has_nav_link(response.text, "/tickets", "К заявкам")


# --- фильтр списка по дате исполнения ---


def _seed_mix(session) -> None:
    make_user(session, "TEST-op")
    rows = [
        (1, "2026-10-05 12:00"),
        (2, "2026-10-20 21:00"),
        (3, "15.09.2026"),
        (4, "05.10.2026"),
        (5, None),
        (6, "совсем без дат"),
    ]
    for number, planned in rows:
        seed_ticket(
            session,
            number=number,
            title=f"Заявка {number}",
            description="на фильтр",
            planned_at=planned,
        )


def _shown_numbers(html: str) -> set[int]:
    return {int(chunk) for chunk in _number_badges(html)}


def _number_badges(html: str) -> list[str]:
    return re.findall(r"№\s*(\d+)</span>", html)


def test_without_filter_all_tickets_visible(client, ui, session):
    _seed_mix(session)
    login(client, "TEST-op")
    html = client.get("/tickets?status=open").text
    assert _shown_numbers(html) == {1, 2, 3, 4, 5, 6}


def test_range_filter_selects_only_in_range(client, ui, session):
    _seed_mix(session)
    login(client, "TEST-op")
    html = client.get("/tickets?status=open&date_from=2026-10-01&date_to=2026-10-31").text
    assert _shown_numbers(html) == {1, 2, 4}


def test_from_bound_only(client, ui, session):
    _seed_mix(session)
    login(client, "TEST-op")
    html = client.get("/tickets?status=open&date_from=2026-10-10").text
    assert _shown_numbers(html) == {2}


def test_to_bound_only(client, ui, session):
    _seed_mix(session)
    login(client, "TEST-op")
    html = client.get("/tickets?status=open&date_to=2026-10-10").text
    assert _shown_numbers(html) == {1, 3, 4}


def test_time_window_filters_iso_to_minute(client, ui, session):
    make_user(session, "TEST-op")
    seed_ticket(session, number=1, title="Точная", description="", planned_at="2026-10-20 21:00")
    seed_ticket(session, number=2, title="Старая", description="", planned_at="20.10.2026")
    login(client, "TEST-op")
    exact = client.get(
        "/tickets?status=open&date_from=2026-10-20&date_to=2026-10-20&time_from=21:00&time_to=21:00"
    ).text
    assert _shown_numbers(exact) == {1}
    shifted = client.get(
        "/tickets?status=open&date_from=2026-10-20&date_to=2026-10-20&time_from=21:01&time_to=23:00"
    ).text
    assert _shown_numbers(shifted) == set()


def test_iso_range_ticket_overlaps_window(client, ui, session):
    make_user(session, "TEST-op")
    seed_ticket(
        session, number=1, title="Диапазон", description="", planned_at="2026-10-20 21:00–23:00"
    )
    login(client, "TEST-op")
    overlapping = client.get(
        "/tickets?status=open&date_from=2026-10-20&date_to=2026-10-20&time_from=22:00&time_to=22:30"
    ).text
    assert _shown_numbers(overlapping) == {1}
    outside = client.get(
        "/tickets?status=open&date_from=2026-10-20&date_to=2026-10-20&time_from=23:30&time_to=23:59"
    ).text
    assert _shown_numbers(outside) == set()


def test_invalid_filter_values_behave_like_no_filter(client, ui, session):
    _seed_mix(session)
    login(client, "TEST-op")
    html = client.get("/tickets?status=open&date_from=ерунда&time_from=99:99").text
    assert _shown_numbers(html) == {1, 2, 3, 4, 5, 6}


def test_filter_empty_result_shows_not_found_message(client, ui, session):
    _seed_mix(session)
    login(client, "TEST-op")
    html = client.get("/tickets?status=open&date_from=2025-01-01&date_to=2025-01-31").text
    assert "Ничего не найдено" in html


def test_filter_params_survive_in_tab_links(client, ui, session):
    _seed_mix(session)
    login(client, "TEST-op")
    html = client.get("/tickets?status=open&date_from=2026-10-01&date_to=2026-10-31").text
    assert 'href="/tickets?status=open&amp;date_from=2026-10-01&amp;date_to=2026-10-31"' in html
    assert 'href="/tickets?status=closed&amp;date_from=2026-10-01&amp;date_to=2026-10-31"' in html


def test_filter_params_survive_in_pagination_links(client, ui, session):
    make_user(session, "TEST-op")
    for number in range(1, 32):
        seed_ticket(
            session,
            number=number,
            title=f"Заявка {number}",
            description="страница",
            planned_at=f"2026-10-{number % 28 + 1:02d} 10:00",
        )
    login(client, "TEST-op")
    html = client.get("/tickets?status=open&date_from=2026-10-01&date_to=2026-10-31").text
    second_page = (
        'href="/tickets?status=open&amp;q=&amp;date_from=2026-10-01'
        '&amp;date_to=2026-10-31&amp;page=2"'
    )
    assert second_page in html


def test_filter_combined_with_text_search(client, ui, session):
    make_user(session, "TEST-op")
    seed_ticket(
        session, number=1, title="Обновить енот", description="", planned_at="2026-10-05 10:00"
    )
    seed_ticket(
        session,
        number=2,
        title="Обновить енот второй",
        description="",
        planned_at="2026-11-05 10:00",
    )
    login(client, "TEST-op")
    html = client.get("/tickets?status=open&q=енот&date_from=2026-10-01&date_to=2026-10-31").text
    assert _shown_numbers(html) == {1}
