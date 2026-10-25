"""Домен заявок: нумерация, CRUD, поиск, счётчики, закрытие.

Контракт шва (interfaces.md, менять нельзя):
- create_ticket(title, description, planned_at, actor) -> Ticket
- edit_ticket(ticket_id, fields, actor) -> Ticket
- close_ticket(ticket_id, comment, actor) -> Ticket
- delete_ticket(ticket_id, actor) -> None (admin и operator — права владельца 13.10)
- list_tickets(status, query, page) -> Page{items, total, page, pages}
- summary() -> {open, closed_today, closed_week}

Инвариант 1: number = max(number)+1 читается и вставляется в одной транзакции
с UNIQUE(number); при честной гонке проигравший INSERT получает IntegrityError
и повторяет транзакцию — номера остаются сплошными, без дублей.
Инвариант 8: closed_at пишется naive-UTC, planned_at хранится строкой как есть,
границы суток для счётчиков считаются по МСК (UTC+3, без перехода на летнее время).

Поиск: нижний регистр в SQL считается функцией ulower (Python str.lower по соединению),
потому что встроенный lower() в SQLite сворачивает только ASCII и «енот» не нашёл бы «Енот».
"""

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import ceil

from sqlalchemy import Engine, and_, event, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.clock import utcnow_naive
from app.db import get_engine
from app.i18n import N_, _, current_lang
from app.models import Setting, Ticket, User

PAGE_SIZE = 30
NUMBER_ATTEMPTS = 5
RECENT_DUPLICATE_WINDOW_SECONDS = 10
COMMENT_MAX_LENGTH = 8192
PLANNED_LEGACY_MAX_LENGTH = 8192
MSK = timezone(timedelta(hours=3))
VALID_STATUSES = ("open", "closed")

ISO_PLANNED_PREFIX = re.compile(
    r"^\s*(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{1,2}):(\d{2})(?::\d{2})?)?"
)
PLANNED_RANGE_END = re.compile(r"^\s*[–—-]\s*(\d{1,2}):(\d{2})")
DAY_DD_MM_YYYY = re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})\.(\d{2,4})(?!\d)")
DAY_YYYY_MM_DD = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")


@dataclass(frozen=True)
class PlannedFilter:
    """Границы диапазона исполнения включительно, с точностью до минуты; None = граница открыта."""

    start: datetime | None
    end: datetime | None


@dataclass(frozen=True)
class Page:
    items: list[Ticket]
    total: int
    page: int
    pages: int


class TicketNotFound(LookupError):
    """Заявки с таким id нет."""


def _to_lower(value: object) -> object:
    return value.lower() if isinstance(value, str) else value


@event.listens_for(Engine, "connect")
def _register_unicode_lower(dbapi_connection, _connection_record) -> None:
    if isinstance(dbapi_connection, sqlite3.Connection):
        dbapi_connection.create_function("ulower", 1, _to_lower, deterministic=True)


def _clean_required(value: str | None, message: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError(message)
    return cleaned


def _clean_optional(value: str | None) -> str | None:
    return (value or "").strip() or None


def create_ticket(title: str, description: str, planned_at: str | None, actor: User) -> Ticket:
    """Создать заявку с номером max+1, выданным в той же транзакции, что и INSERT."""
    clean_title = _clean_required(title, _("Укажите заголовок"))
    clean_description = _clean_required(description, _("Укажите описание"))
    clean_planned = _clean_optional(planned_at)
    for _attempt in range(NUMBER_ATTEMPTS):
        try:
            with Session(get_engine(), expire_on_commit=False) as session:
                current_max = session.scalar(select(func.max(Ticket.number)))
                ticket = Ticket(
                    number=(current_max or 0) + 1,
                    title=clean_title,
                    description=clean_description,
                    planned_at=clean_planned,
                    created_by=actor.id,
                )
                session.add(ticket)
                session.commit()
                return ticket
        except IntegrityError:
            continue
    raise RuntimeError("Не удалось выдать номер заявки: слишком много параллельных созданий")


def edit_ticket(ticket_id: int, fields: dict, actor: User) -> Ticket:
    """Обновить переданные поля (title/description/planned_at), номер не меняется."""
    with Session(get_engine(), expire_on_commit=False) as session:
        ticket = session.get(Ticket, ticket_id)
        if ticket is None:
            raise TicketNotFound(ticket_id)
        if "title" in fields:
            ticket.title = _clean_required(fields["title"], _("Укажите заголовок"))
        if "description" in fields:
            ticket.description = _clean_required(fields["description"], _("Укажите описание"))
        if "planned_at" in fields:
            ticket.planned_at = _clean_optional(fields["planned_at"])
        session.commit()
        session.refresh(ticket)
        return ticket


def close_ticket(ticket_id: int, comment: str | None, actor: User) -> Ticket:
    """Закрыть заявку: статус closed, комментарий, closed_at (naive-UTC) и closed_by."""
    with Session(get_engine(), expire_on_commit=False) as session:
        ticket = session.get(Ticket, ticket_id)
        if ticket is None:
            raise TicketNotFound(ticket_id)
        if ticket.status == "closed":
            raise ValueError(_("Заявка уже закрыта"))
        if comment is not None and len(comment) > COMMENT_MAX_LENGTH:
            raise ValueError(_("Комментарий длиннее {n} символов", n=COMMENT_MAX_LENGTH))
        ticket.status = "closed"
        ticket.comment = _clean_optional(comment)
        ticket.closed_at = utcnow_naive()
        ticket.closed_by = actor.id
        session.commit()
        session.refresh(ticket)
        return ticket


def delete_ticket(ticket_id: int, actor: User) -> None:
    """Удалить заявку; доступно ролям admin и operator (права владельца от 13.10).

    Вместе с заявкой удаляются её строки очереди синка sync_pending:{id}:{event},
    иначе поллер вечно перебирает мёртвый id.
    """
    if getattr(actor, "role", "") not in {"admin", "operator"}:
        raise PermissionError(_("Удаление доступно администратору и оператору"))
    with Session(get_engine()) as session:
        ticket = session.get(Ticket, ticket_id)
        if ticket is None:
            raise TicketNotFound(ticket_id)
        session.delete(ticket)
        session.query(Setting).filter(
            Setting.key.startswith(f"sync_pending:{ticket_id}:")
        ).delete(synchronize_session=False)
        session.query(Setting).filter(
            Setting.key == f"usedesk_comment_done:{ticket_id}"
        ).delete(synchronize_session=False)
        session.commit()


# Окончания русских слов: «Ивана» ищется как «иван» и находит «Ивану»,
# «Иваном», «Иване». Сначала длинные, чтобы «-ами» не срезалось как «-и».
RU_ENDINGS = (
    "ами", "ями", "ого", "его", "ому", "ему", "ыми", "ими",
    "ой", "ей", "ом", "ем", "ам", "ям", "ах", "ях", "ов", "ев",
    "ую", "юю", "ая", "яя", "ое", "ее", "ые", "ие", "ый", "ий",
    "а", "я", "у", "ю", "е", "ы", "и", "о", "ь",
)
STEM_MIN_LENGTH = 4
CYRILLIC_WORD = re.compile(r"^[а-яё]+$")


def search_stem(word: str) -> str:
    """Основа слова для поиска: русское окончание срезается, если остаётся ≥ 4 букв."""
    lowered = word.lower()
    if not CYRILLIC_WORD.match(lowered):
        return lowered
    for ending in RU_ENDINGS:
        if lowered.endswith(ending) and len(lowered) - len(ending) >= STEM_MIN_LENGTH:
            return lowered[: -len(ending)]
    return lowered


def _search_clause(query: str):
    """Поиск: каждое слово запроса (по основе) — в заголовке, описании или комментарии;
    все слова обязательны. Число — ещё и номер заявки целиком."""
    fields = (Ticket.title, Ticket.description, Ticket.comment)
    word_clauses = []
    for word in query.split():
        stem = search_stem(word)
        word_clauses.append(
            or_(*(func.instr(func.ulower(func.coalesce(field, "")), stem) > 0 for field in fields))
        )
    text_clause = and_(*word_clauses) if word_clauses else None
    clauses: list = [text_clause] if text_clause is not None else []
    # Числа вне диапазона SQLite INTEGER (2^63) дают OverflowError на бинде —
    # в поиск числом уходит только то, что гарантированно влезает.
    # isdecimal, а не isdigit: «²»/«①» проходят isdigit, но int() бросает ValueError
    # (ревью B1); числа вне диапазона SQLite INTEGER отсеивает проверка длины.
    if query.isdecimal() and len(query.lstrip("0") or "0") < 19:
        clauses.append(Ticket.number == int(query))
    return or_(*clauses)


def _list_statement(status: str | None, query: str | None):
    statement = select(Ticket)
    if status in VALID_STATUSES:
        statement = statement.where(Ticket.status == status)
    clean_query = (query or "").strip()
    if clean_query:
        statement = statement.where(_search_clause(clean_query))
    return statement


def list_tickets(status: str | None, query: str | None, page: int) -> Page:
    """Страница списка: сортировка по номеру вниз, 30 заявок на страницу."""
    with Session(get_engine()) as session:
        statement = _list_statement(status, query)
        total = int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
        pages = max(1, ceil(total / PAGE_SIZE))
        current = min(max(1, page), pages)
        items = session.scalars(
            statement.order_by(Ticket.number.desc())
            .limit(PAGE_SIZE)
            .offset((current - 1) * PAGE_SIZE)
        ).all()
        return Page(items=list(items), total=total, page=current, pages=pages)


def _minute_of(day: datetime, hour: int, minute: int) -> datetime | None:
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return day.replace(hour=hour, minute=minute)


def parse_planned_at(raw: str | None) -> tuple[datetime, datetime] | None:
    """Момент или интервал исполнения из planned_at: (начало, конец); None — дата не распознана.

    Шов фильтра списка (G04). Новые заявки читаются точно, до минуты:
    префикс ISO `ГГГГ-ММ-ДД[Т| ЧЧ:ММ[:СС]]`; хвост `–ЧЧ:ММ` трактуется как конец
    интервала того же дня (формат виджета волны 3 `ГГГГ-ММ-ДД ЧЧ:ММ–ЧЧ:ММ`),
    попадание в фильтр — по пересечению интервала с окном. Секунды отбрасываются.
    Старые свободные строки разбираются best-effort: первый валидный день в строке
    в виде дд.мм.гггг / дд.мм.гг (год < 100 => 20ВВ) или гггг-мм-дд; время старых
    строк не разбирается (берётся 00:00). Мусор, невалидные даты и пустота дают
    None: при активном фильтре такие заявки отбрасываются, ничего не роняя.
    """

    def date_or_none(year: int, month: int, day: int) -> datetime | None:
        try:
            return datetime(year if year >= 100 else 2000 + year, month, day)
        except ValueError:
            return None

    if raw is None or not raw.strip():
        return None
    iso = ISO_PLANNED_PREFIX.match(raw)
    if iso is not None:
        year, month, day = int(iso[1]), int(iso[2]), int(iso[3])
        base = date_or_none(year, month, day)
        if base is None:
            return None
        start = base
        if iso[4] is not None:
            minute_start = _minute_of(base, int(iso[4]), int(iso[5]))
            if minute_start is None:
                return None
            start = minute_start
        end = start
        tail = PLANNED_RANGE_END.match(raw[iso.end():])
        if tail is not None:
            minute_end = _minute_of(base, int(tail[1]), int(tail[2]))
            if minute_end is None:
                return None
            end = minute_end
        return start, end
    candidates: list[tuple[int, datetime]] = []
    for match in DAY_DD_MM_YYYY.finditer(raw):
        found = date_or_none(int(match[3]), int(match[2]), int(match[1]))
        if found is not None:
            candidates.append((match.start(), found))
    for match in DAY_YYYY_MM_DD.finditer(raw):
        found = date_or_none(int(match[1]), int(match[2]), int(match[3]))
        if found is not None:
            candidates.append((match.start(), found))
    if not candidates:
        return None
    first = min(candidates, key=lambda item: item[0])[1]
    return first, first


def _parse_date_param(value: str) -> datetime | None:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d")
    except ValueError:
        return None


def parse_hhmm(value: str) -> tuple[int, int] | None:
    cleaned = value.strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            moment = datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        return moment.hour, moment.minute
    return None


def _filter_bound(date_value: str, time_value: str, default: tuple[int, int]) -> datetime | None:
    day = _parse_date_param(date_value)
    if day is None:
        return None
    parsed_time = parse_hhmm(time_value) or default
    return _minute_of(day, *parsed_time)


def planned_filter(
    date_from: str, date_to: str, time_from: str, time_to: str
) -> PlannedFilter | None:
    """Окно исполнения из GET-параметров формы поиска; None — фильтр не задан.

    Даты — ГГГГ-ММ-ДД (type=date), время — ЧЧ:ММ или ЧЧ:ММ:СС (type=time).
    Время учитывается только при заданной одноимённой дате: «от» задаёт нижнюю
    границу (по умолчанию 00:00), «до» — верхнюю (по умолчанию 23:59), обе
    включительно. Невалидные значения параметров игнорируются; если после этого
    ни одна граница не задана, фильтра нет и список ведёт себя как раньше.
    """
    start = _filter_bound(date_from, time_from, (0, 0))
    end = _filter_bound(date_to, time_to, (23, 59))
    if start is None and end is None:
        return None
    return PlannedFilter(start=start, end=end)


def _matches_planned(planned_at: str | None, flt: PlannedFilter) -> bool:
    parsed = parse_planned_at(planned_at)
    if parsed is None:
        return False
    start, end = parsed
    if flt.start is not None and end < flt.start:
        return False
    if flt.end is not None and start > flt.end:
        return False
    return True


def list_tickets_by_planned(
    status: str | None, query: str | None, planned: PlannedFilter, page: int
) -> Page:
    """Страница списка с диапазонным фильтром по planned_at (G04).

    Отбор идёт по пересечению: заявка-момент попадает в окно, интервал
    `ЧЧ:ММ–ЧЧ:ММ` — при пересечении с окном. Заявки без распознаваемой даты
    отбрасываются. SQL применяет статус и текстовый поиск, диапазон считается
    в Python (planned_at — свободные строки), поэтому выборка читается целиком
    и режется на страницы уже после фильтра.
    """
    with Session(get_engine()) as session:
        rows = session.scalars(
            _list_statement(status, query).order_by(Ticket.number.desc())
        ).all()
    matched = [ticket for ticket in rows if _matches_planned(ticket.planned_at, planned)]
    total = len(matched)
    pages = max(1, ceil(total / PAGE_SIZE))
    current = min(max(1, page), pages)
    offset = (current - 1) * PAGE_SIZE
    return Page(items=matched[offset:offset + PAGE_SIZE], total=total, page=current, pages=pages)


def _count(session: Session, *conditions) -> int:
    statement = select(func.count()).select_from(Ticket).where(*conditions)
    return int(session.scalar(statement) or 0)


def _msk_midnight_utc(now: datetime) -> datetime:
    """Полночь текущего МСК-дня в naive-UTC — граница «сегодня» для closed_at."""
    msk_now = now.astimezone(MSK)
    midnight_msk = msk_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight_msk.astimezone(timezone.utc).replace(tzinfo=None)


def summary() -> dict:
    """Сводка: открыто всего, закрыто сегодня, закрыто за 7 МСК-дней (включая сегодня)."""
    now_utc = datetime.now(timezone.utc)
    today_start = _msk_midnight_utc(now_utc)
    week_start = today_start - timedelta(days=6)
    with Session(get_engine()) as session:
        open_count = _count(session, Ticket.status == "open")
        closed_today = _count(
            session, Ticket.status == "closed", Ticket.closed_at >= today_start
        )
        closed_week = _count(
            session,
            Ticket.status == "closed",
            Ticket.closed_at >= week_start,
        )
    return {"open": open_count, "closed_today": closed_today, "closed_week": closed_week}


URGENCY_SOON = timedelta(hours=24)
URGENCY_GROUPS = (
    ("overdue", N_("Просрочено")),
    ("soon", N_("Ближайшие 24 часа")),
    ("later", N_("Позже")),
    (None, N_("Без срока")),
)


def msk_now() -> datetime:
    """Текущий момент по МСК как naive — в той же шкале, что разбор planned_at."""
    return utcnow_naive() + timedelta(hours=3)


def _planned_window(raw: str | None) -> tuple[datetime, datetime, bool] | None:
    """(начало, конец, есть_время): строка без времени — весь день 00:00–23:59."""
    parsed = parse_planned_at(raw)
    if parsed is None:
        return None
    start, end = parsed
    iso = ISO_PLANNED_PREFIX.match(raw or "")
    has_time = iso is not None and iso[4] is not None
    if not has_time:
        end = start.replace(hour=23, minute=59)
    return start, end, has_time


def planned_urgency(planned_at: str | None, now_msk: datetime) -> str | None:
    """Срочность открытой заявки: overdue | soon | later | None (нет или не разобрано).

    overdue — конец интервала уже прошёл; soon — начало в ближайшие 24 часа
    (или интервал идёт сейчас); later — дальше. Старые строки без времени
    трактуются как весь день: просрочена, только когда день закончился.
    """
    window = _planned_window(planned_at)
    if window is None:
        return None
    start, end, __ = window
    if end < now_msk:
        return "overdue"
    if start <= now_msk + URGENCY_SOON:
        return "soon"
    return "later"


def relative_label(moment: datetime, now_msk: datetime) -> str:
    """«через 3 ч», «5 ч назад», «через 2 дн», «через 40 мин» — грубо, для глаз."""
    seconds = (moment - now_msk).total_seconds()
    minutes = round(abs(seconds) / 60)
    if minutes < 1:
        return _("сейчас")
    if minutes < 60:
        amount = _("{n} мин", n=minutes)
    elif minutes < 48 * 60:
        amount = _("{n} ч", n=round(minutes / 60))
    else:
        amount = _("{n} дн", n=round(minutes / 1440))
    if seconds > 0:
        return _("через {amount}", amount=amount)
    return _("{amount} назад", amount=amount)


def _planned_parts(planned_at: str | None, now_msk: datetime) -> tuple[str, str] | None:
    """Части плановой подписи: (день «20.10» или «20.10.2025», время «02:00» /
    «03:00–04:00» / «» без времени). None — строка не разобрана."""
    window = _planned_window((planned_at or "").strip())
    if window is None:
        return None
    start, end, has_time = window
    day = f"{start.day:02d}.{start.month:02d}"
    if start.year != now_msk.year:
        day += f".{start.year}"
    if not has_time:
        return day, ""
    time = f"{start.hour:02d}:{start.minute:02d}"
    if end != start:
        time += f"–{end.hour:02d}:{end.minute:02d}"
    return day, time


def short_planned(planned_at: str | None, now_msk: datetime) -> str:
    """Короткая подпись планового времени: «20.10 · 02:00», «20.10 · 03:00–04:00».

    Год показывается, только если он не текущий; нераспознанная старая
    строка возвращается как есть (без «T» ISO), пустая — пустой строкой.
    """
    parts = _planned_parts(planned_at, now_msk)
    if parts is None:
        return (planned_at or "").strip().replace("T", " ")
    day, time = parts
    return f"{day} · {time}" if time else day


def planned_view(planned_at: str | None, now_msk: datetime) -> dict:
    """Всё для ячейки времени карточки: срочность, короткая подпись, день и время
    отдельно (первая колонка строки) и относительное время."""
    urgency = planned_urgency(planned_at, now_msk)
    window = _planned_window(planned_at)
    relative = ""
    if window is not None:
        start, end, has_time = window
        if urgency == "overdue":
            relative = relative_label(end, now_msk) if has_time else _("день прошёл")
        elif start <= now_msk <= end:
            relative = _("идёт сейчас") if has_time else _("сегодня")
        elif has_time:
            relative = relative_label(start, now_msk)
    day, time = _planned_parts(planned_at, now_msk) or ("", "")
    return {
        "urgency": urgency,
        "label": short_planned(planned_at, now_msk),
        "day": day,
        "time": time,
        "relative": relative,
    }


def _urgency_sort_key(ticket: Ticket) -> tuple:
    window = _planned_window(ticket.planned_at)
    if window is None:
        return (1, datetime.max, -ticket.number)
    return (0, window[0], ticket.number)


def group_open_tickets(tickets: list[Ticket], now_msk: datetime) -> list[dict]:
    """Открытые заявки по группам срочности, внутри — по началу планового времени.

    Порядок: Просрочено → Ближайшие 24 часа → Позже → Без срока; пустые группы
    не возвращаются. Без срока — новые сверху (по номеру вниз).
    """
    buckets: dict[str | None, list[Ticket]] = {key: [] for key, __ in URGENCY_GROUPS}
    for ticket in sorted(tickets, key=_urgency_sort_key):
        buckets[planned_urgency(ticket.planned_at, now_msk)].append(ticket)
    return [
        {"key": key or "none", "title": _(title), "items": buckets[key]}
        for key, title in URGENCY_GROUPS
        if buckets[key]
    ]


def open_tickets() -> list[Ticket]:
    """Все открытые заявки — их единицы и десятки, группировка идёт в Python."""
    with Session(get_engine()) as session:
        return list(
            session.scalars(
                select(Ticket).where(Ticket.status == "open").order_by(Ticket.number.desc())
            ).all()
        )


def open_overview(now_msk: datetime | None = None) -> dict:
    """Счётчики шапки и главной: открыто, просрочено, ближайшая заявка.

    next_due — {number, relative} ближайшей ещё не просроченной заявки со сроком
    или None.
    """
    now = now_msk or msk_now()
    tickets = open_tickets()
    overdue = 0
    upcoming: list[tuple[datetime, Ticket]] = []
    for ticket in tickets:
        urgency = planned_urgency(ticket.planned_at, now)
        if urgency == "overdue":
            overdue += 1
        elif urgency is not None:
            upcoming.append((_planned_window(ticket.planned_at)[0], ticket))
    next_due = None
    if upcoming:
        _start, ticket = min(upcoming, key=lambda item: (item[0], item[1].number))
        next_due = {
            "number": ticket.number,
            "relative": planned_view(ticket.planned_at, now)["relative"],
        }
    return {"open": len(tickets), "overdue": overdue, "next_due": next_due}


HOME_BOARD_LIMIT = 6


def home_board(now_msk: datetime | None = None, limit: int = HOME_BOARD_LIMIT) -> dict:
    """Табло главной («Сводка смены»): ближайшие открытые заявки с плановым временем.

    rows — до limit строк по началу планового времени (просроченные выходят
    первыми): {ticket, time, state, urgency}; time — «10:30», «10:30–12:00» или
    день «24.10» для заявок без времени суток; state — «Просрочено · 42 мин назад»,
    «через 48 мин» (ближайшие сутки) или день («Сегодня», «Завтра», «26 октября»).
    soon_count — открытых в ближайшие 24 часа, none_count — без планового времени,
    more — сколько заявок со временем не поместилось в табло.
    """
    now = now_msk or msk_now()
    timed: list[tuple[datetime, int, Ticket]] = []
    none_count = 0
    for ticket in open_tickets():
        window = _planned_window(ticket.planned_at)
        if window is None:
            none_count += 1
        else:
            timed.append((window[0], ticket.number, ticket))
    timed.sort(key=lambda item: (item[0], item[1]))
    rows: list[dict] = []
    soon_count = 0
    for start, _number, ticket in timed:
        view = planned_view(ticket.planned_at, now)
        urgency = view["urgency"]
        if urgency == "soon":
            soon_count += 1
        if len(rows) >= limit:
            continue
        if urgency == "overdue":
            state = _("Просрочено · {ago}", ago=view["relative"])
        elif urgency == "soon":
            state = view["relative"]
        else:
            state = planned_day_label(start.date(), now)
        rows.append({
            "ticket": ticket,
            "time": view["time"] or view["day"],
            "state": state,
            "urgency": urgency,
        })
    return {
        "rows": rows,
        "soon_count": soon_count,
        "none_count": none_count,
        "more": max(0, len(timed) - limit),
    }


WEEKDAYS_RU = (
    "Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье",
)
WEEKDAYS_EN = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def home_now_parts(now_msk: datetime | None = None) -> tuple[str, str]:
    """Строка даты над «Сводкой смены»: («Пятница, 24 октября», «09:42»); время по Москве,
    в браузере оно тикает (data-clock-tz="Europe/Moscow")."""
    now = now_msk or msk_now()
    weekday = (WEEKDAYS_EN if current_lang() == "en" else WEEKDAYS_RU)[now.weekday()]
    return f"{weekday}, {_day_text(now.date(), now)}", f"{now:%H:%M}"


MONTHS_GENITIVE = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
MONTHS_EN = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def _day_text(day, now_msk: datetime) -> str:
    """«19 октября» / «October 19»; год — только если не текущий."""
    if current_lang() == "en":
        text = f"{MONTHS_EN[day.month - 1]} {day.day}"
        if day.year != now_msk.year:
            text += f", {day.year}"
        return text
    text = f"{day.day} {MONTHS_GENITIVE[day.month - 1]}"
    if day.year != now_msk.year:
        text += f" {day.year}"
    return text


def planned_day_label(day, now_msk: datetime) -> str:
    """День планового времени для табло: «Сегодня», «Завтра», «Вчера» или «26 октября»."""
    delta = (day - now_msk.date()).days
    if delta == 0:
        return _("Сегодня")
    if delta == 1:
        return _("Завтра")
    if delta == -1:
        return _("Вчера")
    return _day_text(day, now_msk)


def closed_day_label(closed_at: datetime | None, now_msk: datetime) -> str:
    """Заголовок группы истории по МСК-дню закрытия: «Сегодня, 19 октября»."""
    if closed_at is None:
        return _("Без даты закрытия")
    day = (closed_at + timedelta(hours=3)).date()
    text = _day_text(day, now_msk)
    delta = (now_msk.date() - day).days
    if delta == 0:
        return _("Сегодня, {day}", day=text)
    if delta == 1:
        return _("Вчера, {day}", day=text)
    return text


def find_recent_duplicate(
    title: str,
    description: str,
    planned_at: str | None,
    actor_id: int,
    window_seconds: int = RECENT_DUPLICATE_WINDOW_SECONDS,
) -> Ticket | None:
    """Заявка того же автора с тем же текстом за последние window_seconds секунд.

    Шов для защиты от двойной отправки формы (R06.2): второй быстрый сабмит
    возвращается к уже созданной заявке вместо создания дубля.
    """
    since = utcnow_naive() - timedelta(seconds=window_seconds)
    planned = _clean_optional(planned_at)
    planned_clause = (
        Ticket.planned_at.is_(None) if planned is None else Ticket.planned_at == planned
    )
    with Session(get_engine()) as session:
        return session.scalar(
            select(Ticket)
            .where(
                Ticket.created_by == actor_id,
                Ticket.title == (title or "").strip(),
                Ticket.description == (description or "").strip(),
                planned_clause,
                Ticket.created_at >= since,
            )
            .order_by(Ticket.id.desc())
        )


def status_counts() -> dict[str, int]:
    """Всего открытых и закрытых заявок — для бейджей вкладок."""
    with Session(get_engine()) as session:
        return {
            status: _count(session, Ticket.status == status) for status in VALID_STATUSES
        }


def user_login_map(user_ids: list[int | None]) -> dict[int, str]:
    """Логины по id для подписей «кто создал/закрыл»; отсутствующие не попадают."""
    present = {user_id for user_id in user_ids if user_id is not None}
    if not present:
        return {}
    with Session(get_engine()) as session:
        rows = session.execute(
            select(User.id, User.login).where(User.id.in_(present))
        ).all()
    return {row_id: login for row_id, login in rows}


def format_msk(moment: datetime | None) -> str:
    """Naive-UTC -> «дд.мм.гггг чч:мм» МСК; None -> «—» (полустёртые данные импорта)."""
    if moment is None:
        return "—"
    msk = moment.replace(tzinfo=timezone.utc).astimezone(MSK)
    return f"{msk.day:02d}.{msk.month:02d}.{msk.year} {msk.hour:02d}:{msk.minute:02d}"


def ticket_or_none(ticket_id: int) -> Ticket | None:
    with Session(get_engine()) as session:
        ticket = session.get(Ticket, ticket_id)
        if ticket is not None:
            session.expunge(ticket)
        return ticket

