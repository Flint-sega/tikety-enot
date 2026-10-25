"""Плановое время виджетом (G05, T11): сборка строки, ошибки диапазона, правка обоих форматов.

Чистые функции роутов (build_planned_at — сборка строки виджета, planned_context —
заполнение виджета сохранённой строкой) плюс HTTP-сценарии создания и правки.
Формат согласован с парсером фильтра T10 (app.tickets.parse_planned_at):
«ГГГГ-ММ-ДД ЧЧ:ММ» или «ГГГГ-ММ-ДД ЧЧ:ММ–ЧЧ:ММ», тире — EN DASH U+2013.
"""

from conftest import login, make_user, seed_ticket
from sqlalchemy import func, select

from app.models import Template, Ticket
from app.routes.tickets import build_planned_at, planned_context

EN_DASH = "–"


def _widget(mode: str = "exact", day: str = "", time_from: str = "", time_to: str = "") -> dict:
    return {
        "planned_mode": mode,
        "planned_date": day,
        "planned_time_from": time_from,
        "planned_time_to": time_to,
    }


# --- сборка строки на сервере ---


def test_build_exact_string():
    built, error = build_planned_at(_widget(day="2026-10-20", time_from="21:00"))
    assert built == "2026-10-20 21:00"
    assert error == ""


def test_build_range_string_with_en_dash():
    built, error = build_planned_at(
        _widget(mode="range", day="2026-10-20", time_from="21:00", time_to="23:00")
    )
    assert built == f"2026-10-20 21:00{EN_DASH}23:00"
    assert error == ""


def test_build_range_equal_times_allowed():
    built, error = build_planned_at(
        _widget(mode="range", day="2026-10-20", time_from="21:00", time_to="21:00")
    )
    assert built == f"2026-10-20 21:00{EN_DASH}21:00"
    assert error == ""


def test_build_empty_block_is_empty_string():
    for mode in ("exact", "range"):
        assert build_planned_at(_widget(mode=mode)) == ("", "")


def test_build_range_to_before_from_gives_russian_error():
    built, error = build_planned_at(
        _widget(mode="range", day="2026-10-20", time_from="23:00", time_to="05:00")
    )
    assert built == ""
    assert error == "Время «до» раньше времени «с»"


def test_build_missing_date_gives_russian_error():
    built, error = build_planned_at(_widget(time_from="21:00"))
    assert built == ""
    assert "дату" in error


def test_build_invalid_date_gives_russian_error():
    built, error = build_planned_at(_widget(day="20.10.2026", time_from="21:00"))
    assert built == ""
    assert "ГГГГ-ММ-ДД" in error


def test_build_bad_time_gives_russian_error():
    built, error = build_planned_at(_widget(day="2026-10-20", time_from="21:99"))
    assert built == ""
    assert "ЧЧ:ММ" in error


def test_build_range_missing_to_gives_russian_error():
    built, error = build_planned_at(
        _widget(mode="range", day="2026-10-20", time_from="21:00")
    )
    assert built == ""
    assert "до" in error


def test_build_truncates_seconds():
    built, error = build_planned_at(_widget(day="2026-10-20", time_from="21:00:30"))
    assert built == "2026-10-20 21:00"
    assert error == ""


# --- заполнение виджета сохранённой строкой (правка) ---


def test_context_fills_exact_widget():
    fields = planned_context("2026-10-20 21:00")
    assert fields["planned_mode"] == "exact"
    assert fields["planned_date"] == "2026-10-20"
    assert fields["planned_time_from"] == "21:00"
    assert fields["planned_time_to"] == ""
    assert fields["planned_legacy"] == ""


def test_context_fills_range_widget():
    fields = planned_context(f"2026-10-20 21:00{EN_DASH}23:00")
    assert fields["planned_mode"] == "range"
    assert fields["planned_date"] == "2026-10-20"
    assert fields["planned_time_from"] == "21:00"
    assert fields["planned_time_to"] == "23:00"


def test_context_keeps_legacy_string_as_is():
    fields = planned_context("  завтра утром после обеда  ")
    assert fields["planned_mode"] == "exact"
    assert fields["planned_date"] == ""
    assert fields["planned_time_from"] == ""
    assert fields["planned_legacy"] == "завтра утром после обеда"


def test_context_empty_value_is_empty_widget():
    fields = planned_context(None)
    assert fields["planned_legacy"] == ""
    assert fields["planned_date"] == ""
    assert fields["planned_mode"] == "exact"


# --- HTTP: создание заявки ---


def test_create_exact_via_form_saves_and_shows(client, ui, session):
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    response = client.post(
        "/tickets/create",
        data={
            "title": "Обновление баз",
            "description": "точное время",
            "planned_mode": "exact",
            "planned_date": "2026-10-20",
            "planned_time_from": "21:00",
            "planned_time_to": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    stored = session.scalar(select(Ticket.planned_at))
    assert stored == "2026-10-20 21:00"
    page = client.get("/tickets")
    assert '<span class="when-time">21:00</span>' in page.text, "время в первой колонке строки"
    assert "20.10" in page.text, "день под временем"


def test_create_range_via_form_saves_en_dash_string(client, ui, session):
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    response = client.post(
        "/tickets/create",
        data={
            "title": "Обновление баз",
            "description": "интервал",
            "planned_mode": "range",
            "planned_date": "2026-10-20",
            "planned_time_from": "21:00",
            "planned_time_to": "23:30",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    stored = session.scalar(select(Ticket.planned_at))
    assert stored == f"2026-10-20 21:00{EN_DASH}23:30"
    page = client.get("/tickets")
    assert f"21:00{EN_DASH}23:30" in page.text


def test_create_range_to_before_from_russian_error_keeps_input(client, ui, session):
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    response = client.post(
        "/tickets/create",
        data={
            "title": "Обновление баз",
            "description": "перепутали границы",
            "planned_mode": "range",
            "planned_date": "2026-10-20",
            "planned_time_from": "23:00",
            "planned_time_to": "05:00",
        },
    )
    assert response.status_code == 200
    assert "Время «до» раньше времени «с»" in response.text
    assert session.scalar(select(func.count()).select_from(Ticket)) == 0
    assert 'value="2026-10-20"' in response.text
    assert 'value="23:00"' in response.text
    assert 'value="05:00"' in response.text


def test_create_empty_widget_leaves_planned_at_empty(client, ui, session):
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    response = client.post(
        "/tickets/create",
        data={"title": "без времени", "description": "описание"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert session.scalar(select(Ticket.planned_at)) is None


def test_create_widget_error_keeps_title_and_description(client, ui, session):
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    response = client.post(
        "/tickets/create",
        data={
            "title": "Обновление баз",
            "description": "черновик",
            "planned_mode": "exact",
            "planned_time_from": "21:00",
        },
    )
    assert response.status_code == 200
    assert "Укажите дату" in response.text
    assert 'value="Обновление баз"' in response.text
    assert "черновик" in response.text
    assert session.scalar(select(func.count()).select_from(Ticket)) == 0


# --- HTTP: правка заявки ---


def test_edit_prefills_widget_from_exact_value(client, ui, session):
    seed_ticket(
        session, number=1, title="точная", description="", planned_at="2026-10-20 21:00"
    )
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    html = client.get("/tickets/1/edit").text
    assert 'name="planned_date" type="date" value="2026-10-20"' in html
    assert 'name="planned_time_from" type="time" value="21:00"' in html
    assert 'name="planned_mode" value="exact" checked' in html
    assert "Текущее значение" not in html


def test_edit_prefills_widget_from_range_value(client, ui, session):
    seed_ticket(
        session,
        number=1,
        title="интервал",
        description="",
        planned_at=f"2026-10-20 08:15{EN_DASH}12:45",
    )
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    html = client.get("/tickets/1/edit").text
    assert 'name="planned_time_from" type="time" value="08:15"' in html
    assert 'name="planned_time_to" type="time" value="12:45"' in html
    assert 'name="planned_mode" value="range" checked' in html


def test_edit_legacy_string_shown_as_is_and_survives_save(client, ui, session):
    seed_ticket(
        session, number=1, title="старая", description="", planned_at="завтра утром"
    )
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    html = client.get("/tickets/1/edit").text
    assert "Текущее значение" in html
    assert 'value="завтра утром"' in html
    response = client.post(
        "/tickets/1/edit",
        data={"title": "старая правленая", "description": "описание"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    session.expire_all()
    assert session.scalar(select(Ticket.planned_at)) == "завтра утром"


def test_edit_legacy_replaced_by_widget_value(client, ui, session):
    seed_ticket(
        session, number=1, title="старая", description="", planned_at="завтра утром"
    )
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    response = client.post(
        "/tickets/1/edit",
        data={
            "title": "старая правленая",
            "description": "описание",
            "planned_mode": "range",
            "planned_date": "2026-10-21",
            "planned_time_from": "09:00",
            "planned_time_to": "11:00",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    session.expire_all()
    assert session.scalar(select(Ticket.planned_at)) == f"2026-10-21 09:00{EN_DASH}11:00"


def test_edit_clear_checkbox_removes_value(client, ui, session):
    seed_ticket(
        session, number=1, title="старая", description="", planned_at="завтра утром"
    )
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    response = client.post(
        "/tickets/1/edit",
        data={"title": "старая правленая", "description": "описание", "planned_clear": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    session.expire_all()
    assert session.scalar(select(Ticket.planned_at)) is None


def test_edit_widget_error_keeps_old_value(client, ui, session):
    seed_ticket(
        session, number=1, title="точная", description="", planned_at="2026-10-20 21:00"
    )
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    response = client.post(
        "/tickets/1/edit",
        data={
            "title": "точная правленая",
            "description": "",
            "planned_mode": "range",
            "planned_date": "2026-10-20",
            "planned_time_from": "23:00",
            "planned_time_to": "05:00",
        },
    )
    assert response.status_code == 200
    assert "Время «до» раньше времени «с»" in response.text
    session.expire_all()
    assert session.scalar(select(Ticket.planned_at)) == "2026-10-20 21:00"


def test_edit_new_format_shows_clear_checkbox(client, ui, session):
    seed_ticket(
        session, number=1, title="точная", description="", planned_at="2026-10-20 21:00"
    )
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    page = client.get("/tickets/1/edit")
    assert 'name="planned_clear"' in page.text
    assert "Убрать текущее значение" in page.text


def test_edit_clear_removes_new_format_value(client, ui, session):
    seed_ticket(
        session, number=1, title="точная", description="", planned_at="2026-10-20 21:00"
    )
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    response = client.post(
        "/tickets/1/edit",
        data={"title": "точная правленая", "description": "описание", "planned_clear": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    session.expire_all()
    assert session.scalar(select(Ticket.planned_at)) is None


def test_create_page_has_no_clear_checkbox(client, ui, session):
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    page = client.get("/tickets/new")
    assert 'name="planned_clear"' not in page.text


# --- шаблоны заявок (T07) не сломаны ---


def test_template_panel_still_fills_form(client, ui, session):
    make_user(session, "chief", "TEST-pass", role="admin")
    make_user(session, "op", "TEST-pass")
    session.add(
        Template(
            name="Обновление баз",
            title_tpl="Обновление баз [ДАТА]",
            description_tpl="Просьба обновить базы.",
        )
    )
    session.commit()
    login(client, "chief", "TEST-pass")
    assert (
        client.post(
            "/admin/features/templates_enabled", data={"value": "1"}, follow_redirects=False
        ).status_code
        == 303
    )
    login(client, "op", "TEST-pass")
    new_page = client.get("/tickets/new")
    assert 'hx-get="/tickets/template-panel"' in new_page.text
    assert 'name="planned_date" type="date"' in new_page.text
    panel = client.get("/tickets/template-panel")
    assert 'data-title="Обновление баз [ДАТА]"' in panel.text
    assert 'data-description="Просьба обновить базы."' in panel.text
