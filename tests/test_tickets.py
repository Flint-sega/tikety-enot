"""Заявки: нумерация max+1 в одной транзакции, поиск, счётчики, закрытие, правка, удаление.

Домен проверяется через публичный шов app.tickets, HTTP — через TestClient
под гвардами ролей из таска 03. Секреты в тестах — только TEST-*.
"""

import threading
from datetime import datetime, timedelta, timezone

from conftest import login, make_user, seed_ticket
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Ticket, User
from app.tickets import (
    PAGE_SIZE,
    TicketNotFound,
    close_ticket,
    create_ticket,
    delete_ticket,
    edit_ticket,
    list_tickets,
    summary,
)

MSK = timezone(timedelta(hours=3))


def msk_midnight_utc(now: datetime | None = None) -> datetime:
    """Полночь текущего МСК-дня, назад в naive-UTC (границы суток по инварианту 8)."""
    moment = now or datetime.now(timezone.utc)
    msk_now = moment.astimezone(MSK)
    midnight_msk = msk_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight_msk.astimezone(timezone.utc).replace(tzinfo=None)


def actor_in(engine, user_id: int) -> User:
    with Session(engine) as thread_session:
        return thread_session.get(User, user_id)


def test_new_ticket_gets_max_number_plus_one(session):
    make_user(session, "TEST-op")
    actor = session.scalar(select(User))
    seed_ticket(session, number=7, title="седьмая")
    first = create_ticket("первая новая", "описание", None, actor)
    second = create_ticket("вторая новая", "описание", "2026-10-13 06:00", actor)
    assert (first.number, second.number) == (8, 9)
    assert first.created_by == actor.id
    assert first.status == "open"
    assert first.planned_at is None
    assert second.planned_at == "2026-10-13 06:00"
    assert first.sync_state is None


def test_create_ticket_strips_whitespace_and_requires_fields(session):
    make_user(session, "TEST-op")
    actor = session.scalar(select(User))
    ticket = create_ticket("  заголовок с пробелами  ", "  описание  ", "  ", actor)
    assert ticket.title == "заголовок с пробелами"
    assert ticket.description == "описание"
    assert ticket.planned_at is None
    for empty_call in (
        lambda: create_ticket("   ", "описание", None, actor),
        lambda: create_ticket("заголовок", "   ", None, actor),
    ):
        try:
            empty_call()
            raise AssertionError("пустое обязательное поле прошло")
        except ValueError:
            pass


def test_concurrent_creation_gives_contiguous_numbers(db, session):
    make_user(session, "TEST-op")
    actor_id = session.scalar(select(User.id))
    seed_ticket(session, number=1, title="раз")
    seed_ticket(session, number=2, title="два")
    created: list[Ticket] = []
    failures: list[Exception] = []
    barrier = threading.Barrier(2)

    def worker(index: int) -> None:
        try:
            actor = actor_in(db, actor_id)
            barrier.wait()
            created.append(create_ticket(f"параллельная {index}", "описание", None, actor))
        except Exception as error:  # noqa: BLE001 — падение потока фиксируем в список
            failures.append(error)

    threads = [threading.Thread(target=worker, args=(index,)) for index in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []
    numbers = sorted(ticket.number for ticket in created)
    assert numbers == [3, 4]
    with Session(db) as check:
        assert check.scalar(select(Ticket.number).where(Ticket.number == 3)) == 3
        assert check.scalar(select(Ticket.number).where(Ticket.number == 4)) == 4


def test_edit_ticket_updates_fields_and_keeps_number(session):
    make_user(session, "TEST-op")
    actor = session.scalar(select(User))
    ticket = create_ticket("до правки", "старое описание", None, actor)
    updated = edit_ticket(
        ticket.id, {"title": "после правки", "description": "новое описание",
                    "planned_at": "завтра утром"}, actor,
    )
    assert updated.number == ticket.number
    assert updated.title == "после правки"
    assert updated.description == "новое описание"
    assert updated.planned_at == "завтра утром"


def test_edit_missing_ticket_raises(session):
    make_user(session, "TEST-op")
    actor = session.scalar(select(User))
    try:
        edit_ticket(9999, {"title": "нет такой"}, actor)
        raise AssertionError("TicketNotFound не поднят")
    except TicketNotFound:
        pass


def test_close_ticket_records_status_comment_actor_time(session):
    make_user(session, "TEST-closer", role="admin")
    actor = session.scalar(select(User))
    ticket = create_ticket("закрываемая", "описание", None, actor)
    closed = close_ticket(ticket.id, "  готово, базы обновлены  ", actor)
    assert closed.status == "closed"
    assert closed.comment == "готово, базы обновлены"
    assert closed.closed_by == actor.id
    assert closed.closed_at is not None
    assert closed.closed_at.tzinfo is None
    age = (datetime.now(timezone.utc).replace(tzinfo=None) - closed.closed_at).total_seconds()
    assert abs(age) < 60
    try:
        close_ticket(ticket.id, "повторно", actor)
        raise AssertionError("повторное закрытие прошло")
    except ValueError:
        pass


def test_close_without_comment_keeps_comment_empty(session):
    make_user(session, "TEST-op")
    actor = session.scalar(select(User))
    ticket = create_ticket("без комментария", "описание", None, actor)
    closed = close_ticket(ticket.id, "", actor)
    assert closed.status == "closed"
    assert closed.comment is None


def test_delete_ticket_by_operator_and_admin(session):
    admin = make_user(session, "TEST-admin", role="admin")
    operator = make_user(session, "TEST-op")
    ticket = create_ticket("удаляемая оператором", "описание", None, admin)
    delete_ticket(ticket.id, operator)
    assert session.get(Ticket, ticket.id) is None
    ticket2 = create_ticket("удаляемая админом", "описание", None, admin)
    delete_ticket(ticket2.id, admin)
    assert session.get(Ticket, ticket2.id) is None


def test_list_tickets_paginates_30_per_page(session):
    make_user(session, "TEST-op")
    actor = session.scalar(select(User))
    for index in range(PAGE_SIZE + 5):
        create_ticket(f"заявка {index:02d}", "описание", None, actor)
    first = list_tickets("open", None, 1)
    second = list_tickets("open", None, 2)
    assert (first.total, first.page, first.pages) == (PAGE_SIZE + 5, 1, 2)
    assert len(first.items) == PAGE_SIZE
    assert len(second.items) == 5
    assert first.items[0].number > first.items[1].number
    assert second.items[0].number == 5


def test_list_tickets_filters_by_status(session):
    make_user(session, "TEST-op")
    actor = session.scalar(select(User))
    opened = create_ticket("открытая", "описание", None, actor)
    closed = create_ticket("закрываемая", "описание", None, actor)
    close_ticket(closed.id, "готово", actor)
    open_numbers = [ticket.number for ticket in list_tickets("open", None, 1).items]
    closed_numbers = [ticket.number for ticket in list_tickets("closed", None, 1).items]
    assert open_numbers == [opened.number]
    assert closed_numbers == [closed.number]
    both = list_tickets(None, None, 1)
    assert both.total == 2


def test_search_finds_cyrillic_case_insensitively(session):
    make_user(session, "TEST-op")
    actor = session.scalar(select(User))
    ticket = create_ticket("Енот обновил базы", "Обновление Клиент-Мед у клиента", None, actor)
    for query in ("енот", "Енот", "ЕНОт", "клиент-мед", "КЛИЕНТ-МЕД"):
        found = list_tickets(None, query, 1)
        assert found.total == 1, f"запрос {query!r} не нашёл заявку"
        assert found.items[0].id == ticket.id


def test_search_finds_by_number_and_description(session):
    make_user(session, "TEST-op")
    actor = session.scalar(select(User))
    ticket = create_ticket("заголовок", "обычное описание 12345", None, actor)
    by_number = list_tickets(None, str(ticket.number), 1)
    assert by_number.total == 1
    assert by_number.items[0].id == ticket.id
    by_description = list_tickets(None, "12345", 1)
    assert by_description.total == 1


def test_search_miss_gives_empty_page(session):
    make_user(session, "TEST-op")
    actor = session.scalar(select(User))
    create_ticket("Енот", "описание", None, actor)
    missed = list_tickets("open", "крокодил", 1)
    assert missed.total == 0
    assert missed.items == []
    assert missed.pages == 1


def test_search_finds_imported_ticket(session):
    seed_ticket(
        session,
        id=1183,
        number=1234,
        title="Обновление Клиент-Мед 8390",
        description="Обновление Клиент-Мед 8390 в 6:00",
        planned_at="2026-10-13T06:00",
        status="open",
    )
    by_title = list_tickets("open", "клиент-мед", 1)
    assert by_title.total == 1
    assert by_title.items[0].planned_at == "2026-10-13T06:00"
    by_number = list_tickets("open", "1234", 1)
    assert by_number.total == 1


def test_summary_counts_msk_days(session):
    make_user(session, "TEST-op")
    actor = session.scalar(select(User))
    create_ticket("ещё открытая", "описание", None, actor)
    midnight = msk_midnight_utc()
    seed_ticket(session, number=101, title="сегодня", status="closed",
                closed_at=midnight + timedelta(seconds=1))
    seed_ticket(session, number=102, title="вчера", status="closed",
                closed_at=midnight - timedelta(seconds=1))
    seed_ticket(session, number=103, title="неделю назад", status="closed",
                closed_at=midnight - timedelta(days=5))
    seed_ticket(session, number=104, title="давно", status="closed",
                closed_at=midnight - timedelta(days=8))
    result = summary()
    assert result["open"] == 1
    assert result["closed_today"] == 1
    assert result["closed_week"] == 3


# --- HTTP-шов: роуты заявок под гвардами ролей (через TestClient) ---


def test_anonymous_on_main_screen_redirects_to_login(client, ui):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login"), response.headers["location"]


def test_list_renders_tabs_search_and_summary(client, ui, session):
    make_user(session, "TEST-op")
    actor = session.scalar(select(User))
    create_ticket("Енот обновил базы", "описание", None, actor)
    closed = create_ticket("старая заявка", "описание", None, actor)
    close_ticket(closed.id, "готово", actor)
    login(client, "TEST-op")
    response = client.get("/tickets")
    assert response.status_code == 200
    assert "Открытые" in response.text
    assert "История" in response.text
    assert "Открыто: 1" in response.text
    assert "Закрыто сегодня: 1" in response.text
    assert "За 7 дней: 1" in response.text
    assert 'name="q"' in response.text
    assert 'href="/tickets/new"' in response.text
    assert "Создать заявку" in response.text


def test_create_via_form_appears_with_next_number(client, ui, session):
    make_user(session, "TEST-op")
    seed_ticket(session, number=5, title="прошлая")
    login(client, "TEST-op")
    response = client.post(
        "/tickets/create",
        data={"title": "Енот обновил базы", "description": "полное описание",
              "planned_at": "завтра утром"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/tickets?status=open&done=created&n=6"
    page = client.get(response.headers["location"])
    assert "Заявка №6 создана" in page.text
    assert "Енот обновил базы" in page.text
    assert ">№6<" in page.text
    assert "завтра утром" in page.text


def test_create_empty_fields_russian_errors_keep_input(client, ui, session):
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    response = client.post(
        "/tickets/create",
        data={"title": "   ", "description": "черновик описания", "planned_at": ""},
    )
    assert response.status_code == 200
    assert "Укажите заголовок" in response.text
    assert "черновик описания" in response.text
    assert session.scalar(select(func.count()).select_from(Ticket)) == 0


def test_double_submit_does_not_create_duplicate(client, ui, session):
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    payload = {"title": "одинаковая заявка", "description": "то же описание",
               "planned_at": ""}
    first = client.post("/tickets/create", data=payload, follow_redirects=False)
    second = client.post("/tickets/create", data=payload, follow_redirects=False)
    assert first.status_code == 303
    assert second.status_code == 303
    assert session.scalar(select(func.count()).select_from(Ticket)) == 1


def test_tabs_split_open_and_closed(client, ui, session):
    make_user(session, "TEST-op")
    actor = session.scalar(select(User))
    create_ticket("живая заявка", "описание", None, actor)
    closed = create_ticket("закрытая заявка", "описание", None, actor)
    close_ticket(closed.id, "готово", actor)
    login(client, "TEST-op")
    open_page = client.get("/tickets", params={"status": "open"})
    closed_page = client.get("/tickets", params={"status": "closed"})
    assert "живая заявка" in open_page.text
    assert "закрытая заявка" not in open_page.text
    assert "закрытая заявка" in closed_page.text
    assert "живая заявка" not in closed_page.text
    assert "num num-green" in closed_page.text, "история — зелёные номера закрытых"


def test_search_via_ui_and_miss_message(client, ui, session):
    make_user(session, "TEST-op")
    actor = session.scalar(select(User))
    create_ticket("Енот обновил базы", "описание", None, actor)
    login(client, "TEST-op")
    found = client.get("/tickets", params={"q": "енот"})
    assert "Енот обновил базы" in found.text
    missed = client.get("/tickets", params={"q": "крокодил"})
    assert "Ничего не найдено" in missed.text
    assert "Енот обновил базы" not in missed.text


def test_empty_open_list_shows_invite(client, ui, session):
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    response = client.get("/tickets")
    assert "Открытых заявок нет" in response.text
    assert 'href="/tickets/new"' in response.text


def test_pagination_30_per_page_with_links(client, ui, session):
    make_user(session, "TEST-op")
    actor = session.scalar(select(User))
    for index in range(35):
        ticket = create_ticket(f"заявка {index:02d}", "описание", None, actor)
        close_ticket(ticket.id, "готово", actor)
    login(client, "TEST-op")
    # история листается по 30; открытые показываются все сразу группами срочности
    first = client.get("/tickets", params={"status": "closed"})
    second = client.get("/tickets", params={"status": "closed", "page": 2})
    assert first.text.count("data-ticket-id") == 30
    assert second.text.count("data-ticket-id") == 5
    assert 'page=2' in first.text
    assert "заявка 34" in first.text
    assert "заявка 04" in second.text


def test_close_via_form_green_card_with_comment_and_author(client, ui, session):
    make_user(session, "TEST-op")
    actor = session.scalar(select(User))
    ticket = create_ticket("закрываем через форму", "описание", None, actor)
    login(client, "TEST-op")
    response = client.post(
        f"/tickets/{ticket.id}/close", data={"comment": "готово"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    closed_page = client.get("/tickets", params={"status": "closed"})
    assert "закрываем через форму" in closed_page.text
    assert "готово" in closed_page.text
    assert "TEST-op" in closed_page.text
    assert "МСК" in closed_page.text


def test_double_close_shows_russian_error(client, ui, session):
    make_user(session, "TEST-op")
    actor = session.scalar(select(User))
    ticket = create_ticket("уже закрытая", "описание", None, actor)
    close_ticket(ticket.id, "первый раз", actor)
    login(client, "TEST-op")
    response = client.post(f"/tickets/{ticket.id}/close", data={"comment": "снова"})
    assert response.status_code == 200
    assert "Заявка уже закрыта" in response.text


def test_edit_via_form_updates_and_shows(client, ui, session):
    make_user(session, "TEST-op")
    actor = session.scalar(select(User))
    ticket = create_ticket("до правки", "старое", None, actor)
    login(client, "TEST-op")
    response = client.post(
        f"/tickets/{ticket.id}/edit",
        data={"title": "после правки", "description": "новое", "planned_at": "13.10 в 6 утра"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get("/tickets")
    assert "после правки" in page.text
    assert "13.10 в 6 утра" in page.text
    assert "до правки" not in page.text


def test_edit_empty_title_shows_russian_error_and_keeps_old(client, ui, session):
    make_user(session, "TEST-op")
    actor = session.scalar(select(User))
    ticket = create_ticket("исходный заголовок", "описание", None, actor)
    login(client, "TEST-op")
    response = client.post(
        f"/tickets/{ticket.id}/edit",
        data={"title": " ", "description": "описание", "planned_at": ""},
    )
    assert response.status_code == 200
    assert "Укажите заголовок" in response.text
    session.expire_all()
    stored = session.get(Ticket, ticket.id)
    assert stored.title == "исходный заголовок"


def test_full_text_fragment_returns_whole_description(client, ui, session):
    make_user(session, "TEST-op")
    actor = session.scalar(select(User))
    long_description = "\n".join(f"строка {index:02d} длинного описания" for index in range(10))
    ticket = create_ticket("длинное описание", long_description, None, actor)
    login(client, "TEST-op")
    response = client.get(f"/tickets/{ticket.id}/full")
    assert response.status_code == 200
    assert "строка 09 длинного описания" in response.text


def test_imported_ticket_number_one_visible_and_searchable(client, ui, session):
    seed_ticket(session, id=1, number=1, title="Первая заявка из импорта",
                description="старое описание", planned_at=None, status="open")
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    page = client.get("/tickets")
    assert "Первая заявка из импорта" in page.text
    assert ">№1<" in page.text
    assert "Без срока" in page.text
    found = client.get("/tickets", params={"q": "первая"})
    assert "Первая заявка из импорта" in found.text
    by_number = client.get("/tickets", params={"q": "1"})
    assert "Первая заявка из импорта" in by_number.text


def test_delete_by_operator_and_anonymous_redirected(client, ui, session):
    make_user(session, "TEST-admin", role="admin")
    make_user(session, "TEST-op")
    admin = session.scalar(select(User).where(User.role == "admin"))
    ticket = create_ticket("удаляемая", "описание", None, admin)
    anon = client.post(f"/tickets/{ticket.id}/delete", follow_redirects=False)
    assert anon.status_code == 303
    login(client, "TEST-op")
    allowed = client.post(f"/tickets/{ticket.id}/delete", follow_redirects=False)
    assert allowed.status_code == 303
    assert session.get(Ticket, ticket.id) is None


def test_card_forms_vertically_centered(client, ui, session):
    seed_ticket(session, number=1, title="центр", description="")
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    assert "form-card" in client.get("/tickets/new").text
    assert "form-card" in client.get("/tickets/1/edit").text
    client.post("/logout")
    assert "page-hero" in client.get("/login").text


def test_delete_button_visible_to_operator(client, ui, session):
    """C1: владелец разрешил оператору удаление — кнопка видна на обеих вкладках."""
    seed_ticket(session, number=1, title="открытая", description="", status="open")
    seed_ticket(session, number=2, title="закрытая", description="", status="closed")
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    open_page = client.get("/tickets", params={"status": "open"})
    closed_page = client.get("/tickets", params={"status": "closed"})
    assert "/tickets/1/delete" in open_page.text
    assert "/tickets/2/delete" in closed_page.text


# --- поиск: комментарий и формы русских слов ---


def test_search_finds_word_in_closing_comment(client, ui, session):
    seed_ticket(session, number=1, title="Снять копию 8001", description="в 22:00",
                status="closed", comment="готово, передал Ивана")
    seed_ticket(session, number=2, title="Другое", description="без имени", status="closed")
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    html = client.get("/tickets", params={"status": "closed", "q": "Ивана"}).text
    assert "Снять копию 8001" in html
    assert "Другое" not in html


def test_search_matches_russian_word_forms(client, ui, session):
    for number, text in enumerate(("сказать Ивану", "доступ с Иваном", "об Иване"), 1):
        seed_ticket(session, number=number, title=f"Заявка {number}", description=text,
                    status="closed")
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    html = client.get("/tickets", params={"status": "closed", "q": "Ивана"}).text
    for number in (1, 2, 3):
        assert f"Заявка {number}" in html, number


def test_search_requires_every_word(client, ui, session):
    seed_ticket(session, number=1, title="Обновить 1038", description="на крайний")
    seed_ticket(session, number=2, title="Обновить 5011", description="на крайний")
    make_user(session, "TEST-op")
    login(client, "TEST-op")
    html = client.get("/tickets", params={"q": "обновить 1038"}).text
    assert "Обновить 1038" in html and "Обновить 5011" not in html


def test_search_stem_keeps_short_words_whole():
    from app.tickets import search_stem

    assert search_stem("Ивана") == "иван"
    assert search_stem("базы") == "базы", "короткое слово не режется до 3 букв"
    assert search_stem("Ivana") == "ivana"
    assert search_stem("1038") == "1038"
