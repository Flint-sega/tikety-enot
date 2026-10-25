"""Срочность открытых заявок, короткие подписи времени, группы списка, счётчики,
строка-подтверждение после действия и «Повторить» (редизайн «Ночь»/«Классика»)."""
from datetime import datetime, timedelta

from conftest import login, make_user, seed_ticket

from app.clock import utcnow_naive
from app.routes.tickets import done_message, page_numbers
from app.tickets import (
    closed_day_label,
    group_open_tickets,
    msk_now,
    planned_urgency,
    planned_view,
    relative_label,
    short_planned,
)

NOW = datetime(2026, 10, 19, 22, 54)


def test_urgency_exact_time():
    assert planned_urgency("2026-10-19 18:00", NOW) == "overdue"
    assert planned_urgency("2026-10-20 02:00", NOW) == "soon"
    assert planned_urgency("2026-10-22 10:00", NOW) == "later"


def test_urgency_soon_boundary_is_24_hours():
    assert planned_urgency("2026-10-20 22:54", NOW) == "soon"
    assert planned_urgency("2026-10-20 22:55", NOW) == "later"


def test_urgency_interval_in_progress_is_not_overdue():
    assert planned_urgency("2026-10-19 22:00–23:30", NOW) == "soon"
    assert planned_urgency("2026-10-19 21:00–22:00", NOW) == "overdue"


def test_urgency_legacy_day_without_time_is_whole_day():
    assert planned_urgency("19.10.2026", NOW) == "soon"
    assert planned_urgency("18.10.2026 на крайний", NOW) == "overdue"
    assert planned_urgency("2026-10-25", NOW) == "later"


def test_urgency_none_for_empty_and_garbage():
    assert planned_urgency(None, NOW) is None
    assert planned_urgency("", NOW) is None
    assert planned_urgency("после обеда", NOW) is None


def test_short_planned_labels():
    assert short_planned("2026-10-20 02:00", NOW) == "20.10 · 02:00"
    assert short_planned("2026-10-20 03:00–04:00", NOW) == "20.10 · 03:00–04:00"
    assert short_planned("2027-01-05 10:00", NOW) == "05.01.2027 · 10:00"
    assert short_planned("2026-10-20T02:00", NOW) == "20.10 · 02:00"
    assert short_planned("после обеда", NOW) == "после обеда"
    assert short_planned(None, NOW) == ""


def test_relative_labels():
    assert relative_label(NOW + timedelta(hours=3, minutes=6), NOW) == "через 3 ч"
    assert relative_label(NOW - timedelta(hours=5), NOW) == "5 ч назад"
    assert relative_label(NOW + timedelta(minutes=40), NOW) == "через 40 мин"
    assert relative_label(NOW + timedelta(days=3), NOW) == "через 3 дн"


def test_planned_view_for_overdue_counts_from_end():
    view = planned_view("2026-10-19 17:00–18:00", NOW)
    assert view["urgency"] == "overdue"
    assert view["relative"] == "5 ч назад"
    assert planned_view("2026-10-19 22:00–23:30", NOW)["relative"] == "идёт сейчас"


def test_group_open_tickets_order_and_sorting(session):
    seed_ticket(session, number=1, title="позже", description="d", planned_at="2026-10-22 10:00")
    seed_ticket(session, number=2, title="без срока", description="d")
    seed_ticket(session, number=3, title="ночью 3", description="d", planned_at="2026-10-20 03:00")
    seed_ticket(session, number=4, title="просрочена", description="d",
                planned_at="2026-10-19 18:00")
    seed_ticket(session, number=5, title="ночью 2", description="d", planned_at="2026-10-20 02:00")
    from app.tickets import open_tickets

    groups = group_open_tickets(open_tickets(), NOW)
    assert [group["key"] for group in groups] == ["overdue", "soon", "later", "none"]
    assert [ticket.number for ticket in groups[1]["items"]] == [5, 3]


def test_closed_day_labels():
    now = datetime(2026, 10, 19, 22, 0)
    assert closed_day_label(datetime(2026, 10, 19, 10, 0), now) == "Сегодня, 19 октября"
    assert closed_day_label(datetime(2026, 10, 18, 10, 0), now) == "Вчера, 18 октября"
    # 21:30 UTC 18.10 = 00:30 МСК 19.10 — уже «сегодня»
    assert closed_day_label(datetime(2026, 10, 18, 21, 30), now) == "Сегодня, 19 октября"
    assert closed_day_label(datetime(2025, 3, 1, 10, 0), now) == "1 марта 2025"


def test_done_message_rejects_garbage():
    assert done_message("closed", "1148") == "Заявка №1148 закрыта"
    assert done_message("closed", "<script>") is None
    assert done_message("hacked", "1") is None
    assert done_message("closed", "9" * 20) is None


def test_page_numbers_with_gaps():
    assert page_numbers(1, 1) == [1]
    assert page_numbers(1, 115) == [1, 2, 3, None, 115]
    assert page_numbers(50, 115) == [1, None, 48, 49, 50, 51, 52, None, 115]


def _soon(hours: int) -> str:
    moment = msk_now() + timedelta(hours=hours)
    return moment.strftime("%Y-%m-%d %H:%M")


def test_open_list_groups_and_home_counters(client, ui, session):
    make_user(session, "TEST-op", "TEST-pass")
    seed_ticket(session, number=10, title="Просроченная", description="d", planned_at=_soon(-5))
    seed_ticket(session, number=11, title="Скоро", description="d", planned_at=_soon(3))
    seed_ticket(session, number=12, title="Без срока", description="d")
    login(client, "TEST-op")

    page = client.get("/tickets?status=open").text
    assert page.index("Просрочено · 1") < page.index("Ближайшие 24 часа · 1")
    assert "Без срока · 1" in page
    assert "stripe-overdue" in page and 'class="rail rail-overdue"' in page
    assert 'class="rail rail-none"' in page
    assert 'aria-label="без планового времени">——<' in page, "без срока — прочерк в колонке времени"

    home = client.get("/").text
    assert "Сводка смены" in home
    # табло: просроченная первой, затем ближайшая; без срока в табло нет, но посчитана
    assert home.index("№10") < home.index("№11")
    assert "board-overdue" in home and "Просрочено · " in home
    assert "№12" not in home.split('class="home-cards"')[0]
    assert "Ещё 1 открытых без планового времени" in home
    assert "1 просрочено" in home and "1 в ближайшие сутки" in home and "1 без времени" in home


def test_close_redirects_with_confirmation(client, ui, session):
    make_user(session, "TEST-op", "TEST-pass")
    ticket = seed_ticket(session, number=21, title="Закрыть меня", description="d")
    login(client, "TEST-op")
    response = client.post(
        f"/tickets/{ticket.id}/close", data={"comment": "Готово"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/tickets?status=open&done=closed&n=21"
    assert "Заявка №21 закрыта" in client.get(response.headers["location"]).text


def test_history_rows_and_grouping_toggle(client, ui, session):
    from app.sync import SETTING_HISTORY_GROUPED, set_setting

    make_user(session, "TEST-op", "TEST-pass")
    seed_ticket(session, number=31, title="Старая", description="d", status="closed",
                comment="готово", closed_at=utcnow_naive())
    login(client, "TEST-op")

    flat = client.get("/tickets?status=closed").text
    assert "hist-row" in flat and "«готово»" in flat
    assert "Сегодня," not in flat

    set_setting(SETTING_HISTORY_GROUPED, "1")
    grouped = client.get("/tickets?status=closed").text
    assert "Сегодня," in grouped


def test_repeat_prefills_title_and_description(client, ui, session):
    make_user(session, "TEST-op", "TEST-pass")
    ticket = seed_ticket(session, number=41, title="5746 обновить", description="на крайний",
                         status="closed", planned_at="2026-10-16 01:00")
    login(client, "TEST-op")
    page = client.get(f"/tickets/new?from={ticket.id}").text
    assert "Повтор заявки №41" in page
    assert 'value="5746 обновить"' in page
    assert "на крайний" in page
    assert 'name="planned_date" type="date" value=""' in page
    assert client.get("/tickets/new?from=abc").status_code == 200
    assert client.get("/tickets/new?from=99999").status_code == 200
