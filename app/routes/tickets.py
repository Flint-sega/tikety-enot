"""HTTP-роуты заявок: список с вкладками и поиском, создание, правка, закрытие, удаление.

Все маршруты под require("operator") — включая удаление
(права владельца от 13.10: оператор создаёт, правит и удаляет заявки).
Формы разбираются только app.auth.parse_form (python-multipart в стеке нет).
Успешные мутации заканчиваются 303-редиректом (PRG), поэтому повторное открытие
страницы форму не отправляет; вторая мгновенная отправка тех же данных гасится
в find_recent_duplicate (R06.2 — двойная отправка не создаёт дубль).
Времена показываются в МСК (naive-UTC в БД, инвариант 8); planned_at — как есть.
Плановое время в формах собирается виджетом (G05): build_planned_at даёт строку
«ГГГГ-ММ-ДД ЧЧ:ММ» или «ГГГГ-ММ-ДД ЧЧ:ММ–ЧЧ:ММ», совместимую с парсером фильтра
app.tickets.parse_planned_at; свободное поле planned_at принимается как есть,
когда блок виджета пуст (старые клиенты и старые строки заявок).
"""

import re
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import (
    ROLE_LEVEL,
    current_user_dependency,
    parse_form,
    require,
    templates,
)
from app.i18n import N_, _
from app.models import User
from app.sync import (
    EVENT_TELEGRAM_CLOSED,
    EVENT_TELEGRAM_NEW,
    EVENT_USEDESK,
    SETTING_HISTORY_GROUPED,
    enqueue,
    get_setting,
)
from app.tickets import (
    PLANNED_LEGACY_MAX_LENGTH,
    VALID_STATUSES,
    TicketNotFound,
    close_ticket,
    closed_day_label,
    create_ticket,
    delete_ticket,
    edit_ticket,
    find_recent_duplicate,
    format_msk,
    group_open_tickets,
    home_board,
    home_now_parts,
    list_tickets,
    list_tickets_by_planned,
    msk_now,
    open_overview,
    open_tickets,
    parse_hhmm,
    planned_filter,
    planned_view,
    status_counts,
    summary,
    ticket_or_none,
    user_login_map,
)

router = APIRouter()

FILTER_PARAMS = ("date_from", "date_to", "time_from", "time_to")

PLANNED_MODES = ("exact", "range")
PLANNED_WIDGET_FORMAT = re.compile(
    r"^\s*(\d{4})-(\d{2})-(\d{2}) ([01]\d|2[0-3]):([0-5]\d)"
    r"(?:–([01]\d|2[0-3]):([0-5]\d))?\s*$"
)

NAV_SECTIONS = (
    ("tickets", N_("Заявки"), "/tickets", "operator"),
    ("users", N_("Пользователи"), "/users", "admin"),
    ("admin", N_("Админ"), "/admin", "admin"),
)


def _section_active(path: str, href: str) -> bool:
    if href == "/tickets":
        return path == "/tickets" or path.startswith("/tickets/")
    return path == href or path.startswith(f"{href}/")


def nav_sections(request: Request, user: object) -> list[dict]:
    """Шапочная навигация base.html (G03): разделы по роли, текущий подсвечен.

    Шов: регистрируется Jinja-глобалом, шаблон вызывает nav_sections(request, user) —
    пользователь уже есть в контексте каждого роута, своя сессия БД не открывается.
    «Заявки» — всем вошедшим (operator+), «Пользователи» и «Админ» — только
    admin; без пользователя — пустой список, навигация не рендерится.
    """
    if not user:  # None или Jinja-Undefined (страницы без user в контексте)
        return []
    path = request.url.path
    sections = []
    for key, label, href, min_role in NAV_SECTIONS:
        if ROLE_LEVEL.get(user.role, 0) < ROLE_LEVEL[min_role]:
            continue
        sections.append(
            {"key": key, "label": _(label), "href": href, "active": _section_active(path, href)}
        )
    return sections


templates.env.globals["nav_sections"] = nav_sections
# Счётчики шапки (открыто / просрочено) — base.html зовёт только для вошедших.
templates.env.globals["topbar_counts"] = open_overview

DONE_MESSAGES = {
    "created": N_("Заявка №{n} создана"),
    "saved": N_("Заявка №{n} сохранена"),
    "closed": N_("Заявка №{n} закрыта"),
    "deleted": N_("Заявка №{n} удалена"),
}


def done_message(done: str, number: str) -> str | None:
    """Строка-подтверждение после PRG-редиректа; мусор в параметрах — ничего."""
    template = DONE_MESSAGES.get(done)
    if template is None or not number.isdecimal() or len(number) > 9:
        return None
    return _(template, n=int(number))


def _done_url(done: str, number: int) -> str:
    target = "closed" if done == "deleted-closed" else "open"
    kind = "deleted" if done == "deleted-closed" else done
    return f"/tickets?status={target}&done={kind}&n={number}"


def page_numbers(page: int, pages: int) -> list[int | None]:
    """Номера для пагинации: 1 … p-2..p+2 … последняя; None — пропуск «…»."""
    shown = {1, pages, *range(max(1, page - 2), min(pages, page + 2) + 1)}
    result: list[int | None] = []
    previous = 0
    for number in sorted(shown):
        if number - previous > 1:
            result.append(None)
        result.append(number)
        previous = number
    return result


def _not_found() -> HTMLResponse:
    return HTMLResponse(_("Заявка не найдена"), status_code=404)


def _card_views(tickets: list, now_msk=None):
    """Вид карточек: плановое время (срочность, подпись), закрытие в МСК, логины, UseDesk."""
    now = now_msk or msk_now()
    logins = user_login_map(
        [ticket.closed_by for ticket in tickets] + [ticket.created_by for ticket in tickets]
    )
    return [
        {
            "ticket": ticket,
            "planned": planned_view(ticket.planned_at, now),
            "closed_at_msk": format_msk(ticket.closed_at),
            "closed_by_login": logins.get(ticket.closed_by),
            "created_by_login": logins.get(ticket.created_by),
            "usedesk_activity_msk": format_msk(ticket.usedesk_activity),
        }
        for ticket in tickets
    ]


def _history_groups(cards: list[dict], grouped: bool, now_msk) -> list[dict]:
    """Строки истории: одной группой без заголовка или по МСК-дню закрытия."""
    if not cards:
        return []
    if not grouped:
        return [{"title": None, "rows": cards}]
    groups: list[dict] = []
    for card in cards:
        title = closed_day_label(card["ticket"].closed_at, now_msk)
        if not groups or groups[-1]["title"] != title:
            groups.append({"title": title, "rows": []})
        groups[-1]["rows"].append(card)
    return groups


def _filter_qs(values: dict[str, str]) -> str:
    """Хвост строки запроса с непустыми параметрами фильтра: «&name=value», уже в порядке шва."""
    return "".join(
        f"&{name}={quote(values[name], safe=':')}" for name in FILTER_PARAMS if values[name]
    )


def _render_list(
    request: Request,
    user: User | None,
    *,
    status: str,
    q: str,
    page: int,
    date_from: str = "",
    date_to: str = "",
    time_from: str = "",
    time_to: str = "",
    error: str | None = None,
    done: str = "",
    number: str = "",
):
    clean_status = status if status in VALID_STATUSES else "open"
    filter_values = {
        "date_from": date_from or "",
        "date_to": date_to or "",
        "time_from": time_from or "",
        "time_to": time_to or "",
    }
    planned = planned_filter(
        filter_values["date_from"],
        filter_values["date_to"],
        filter_values["time_from"],
        filter_values["time_to"],
    )
    now = msk_now()
    groups: list[dict] = []
    cards: list[dict] = []
    hist_groups: list[dict] = []
    page_number, pages, total = 1, 1, 0
    if clean_status == "open" and not (q or "").strip() and planned is None:
        # Рабочий вид: все открытые по группам срочности, без пагинации.
        tickets = open_tickets()
        total = len(tickets)
        for group in group_open_tickets(tickets, now):
            groups.append({**group, "cards": _card_views(group["items"], now)})
    else:
        if planned is None:
            result = list_tickets(clean_status, q, page)
        else:
            result = list_tickets_by_planned(clean_status, q, planned, page)
        page_number, pages, total = result.page, result.pages, result.total
        views = _card_views(result.items, now)
        if clean_status == "closed":
            grouped = get_setting(SETTING_HISTORY_GROUPED, "0") == "1"
            hist_groups = _history_groups(views, grouped, now)
        else:
            cards = views
    return templates.TemplateResponse(
        request,
        "tickets/list.html",
        {
            "user": user,
            "tab": clean_status,
            "groups": groups,
            "cards": cards,
            "hist_groups": hist_groups,
            "done_message": done_message(done, number),
            "summary": summary(),
            "counts": status_counts(),
            "current_status": clean_status,
            "q": q or "",
            "page": page_number,
            "pages": pages,
            "page_numbers": page_numbers(page_number, pages),
            "total": total,
            "error": error,
            "filter_qs": _filter_qs(filter_values),
            # вкладки шапки сохраняют поиск и период при переключении Открытые/История
            "tab_qs": (f"&q={quote(q.strip())}" if (q or "").strip() else "")
            + _filter_qs(filter_values),
            "filter_active": planned is not None,
            **filter_values,
        },
    )


@router.get("/", dependencies=[require("operator")])
def home_page(
    request: Request,
    user: User | None = Depends(current_user_dependency),
):
    """Главная «Сводка смены»: табло ближайших заявок по плановому времени и три
    плашки разделов со счётчиками (открыто, просрочено, закрыто сегодня);
    список живёт на /tickets."""
    now = msk_now()
    now_date, now_time = home_now_parts(now)
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "user": user,
            "tab": "home",
            "overview": open_overview(now),
            "board": home_board(now),
            "summary": summary(),
            "now_date": now_date,
            "now_time": now_time,
        },
    )


@router.get("/tickets", dependencies=[require("operator")])
def tickets_page(
    request: Request,
    status: str = "open",
    q: str = "",
    page: str = "1",
    date_from: str = "",
    date_to: str = "",
    time_from: str = "",
    time_to: str = "",
    done: str = "",
    n: str = "",
    user: User | None = Depends(current_user_dependency),
):
    try:
        page_number = max(int(page), 1)
    except ValueError:
        page_number = 1  # мусорный ?page=abc — первая страница, а не 422-JSON
    return _render_list(
        request,
        user,
        status=status,
        q=q,
        page=page_number,
        date_from=date_from,
        date_to=date_to,
        time_from=time_from,
        time_to=time_to,
        done=done,
        number=n,
    )


@router.get("/tickets/new", dependencies=[require("operator")])
def new_ticket_page(
    request: Request,
    from_: str = Query("", alias="from"),
    user: User | None = Depends(current_user_dependency),
):
    """Форма новой заявки; ?from=<id> — «Повторить»: заголовок и описание из старой
    заявки, плановое время пустое (его задают заново)."""
    title, description = "", ""
    source = ticket_or_none(int(from_)) if from_.isdecimal() and len(from_) < 10 else None
    if source is not None:
        title, description = source.title, source.description
    return templates.TemplateResponse(
        request,
        "tickets/new.html",
        {
            "user": user,
            "errors": {},
            "repeat_of": source.number if source is not None else None,
            "form": {"title": title, "description": description, **planned_context(None)},
        },
    )


def _planned_defaults() -> dict[str, str]:
    return {
        "planned_mode": "exact",
        "planned_date": "",
        "planned_time_from": "",
        "planned_time_to": "",
        "planned_clear": "",
        "planned_legacy": "",
        "planned_has_value": "",
    }


def planned_widget_fields(
    form: dict[str, str], legacy: str = "", has_value: bool = False
) -> dict[str, str]:
    """Поля виджета планового времени из отправленной формы — повторный рендер с ошибкой.

    Введённые значения сохраняются, чтобы пользователь не потерял ввод;
    legacy — старая строка заявки для поля «Текущее значение» (edit.html);
    has_value — у заявки есть сохранённое плановое время (чекбокс очистки).
    """
    fields = _planned_defaults()
    mode = form.get("planned_mode", "")
    fields["planned_mode"] = mode if mode in PLANNED_MODES else "exact"
    fields["planned_date"] = form.get("planned_date", "")
    fields["planned_time_from"] = form.get("planned_time_from", "")
    fields["planned_time_to"] = form.get("planned_time_to", "")
    fields["planned_clear"] = form.get("planned_clear", "")
    fields["planned_legacy"] = legacy
    fields["planned_has_value"] = "1" if (legacy or has_value) else ""
    return fields


def planned_context(planned_at: str | None) -> dict[str, str]:
    """Поля виджета планового времени из сохранённой строки (edit.html, GET-правка).

    Строка формата виджета «ГГГГ-ММ-ДД ЧЧ:ММ» / «ГГГГ-ММ-ДД ЧЧ:ММ–ЧЧ:ММ»
    заполняет поля и переключатель (диапазон — по хвосту «–ЧЧ:ММ»); любая
    другая непустая строка, включая старые свободные, идёт в planned_legacy
    как «Текущее значение», виджет остаётся пустым. planned_has_value — у
    заявки есть хоть какое-то плановое время (чекбокс очистки в edit.html).
    """
    fields = _planned_defaults()
    fields["planned_has_value"] = "1" if (planned_at or "").strip() else ""
    match = PLANNED_WIDGET_FORMAT.match(planned_at or "")
    if match is None:
        fields["planned_legacy"] = (planned_at or "").strip()
        return fields
    fields["planned_date"] = f"{match[1]}-{match[2]}-{match[3]}"
    fields["planned_time_from"] = f"{match[4]}:{match[5]}"
    if match[6] is not None:
        fields["planned_mode"] = "range"
        fields["planned_time_to"] = f"{match[6]}:{match[7]}"
    return fields


def _planned_time(value: str) -> str | None:
    moment = parse_hhmm(value)
    return f"{moment[0]:02d}:{moment[1]:02d}" if moment else None


def build_planned_at(form: dict[str, str]) -> tuple[str, str]:
    """Собрать planned_at из полей виджета: (строка, русская ошибка); ошибка пуста при успехе.

    Формат согласован с app.tickets.parse_planned_at (фильтр T10):
    «ГГГГ-ММ-ДД ЧЧ:ММ» или «ГГГГ-ММ-ДД ЧЧ:ММ–ЧЧ:ММ» (тире — EN DASH U+2013).
    Блок целиком необязателен: все три поля пусты — пустая строка. Если заполнено
    хоть одно поле, дата обязательна и время «с» обязательно; в режиме диапазона
    обязательно время «до», которое не раньше «с» (равные допустимы). Секунды
    отбрасываются, введённое при ошибке сохраняется повторным рендером формы.
    """
    mode = form.get("planned_mode", "")
    if mode not in PLANNED_MODES:
        mode = "exact"
    day = (form.get("planned_date") or "").strip()
    time_from = (form.get("planned_time_from") or "").strip()
    time_to = (form.get("planned_time_to") or "").strip()
    if not (day or time_from or time_to):
        return "", ""
    try:
        parsed_day = datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return "", _("Укажите дату планового времени (ГГГГ-ММ-ДД)")
    clean_from = _planned_time(time_from)
    if clean_from is None:
        return "", _("Укажите время «с» (ЧЧ:ММ)") if mode == "range" else _("Укажите время (ЧЧ:ММ)")
    start = f"{parsed_day.strftime('%Y-%m-%d')} {clean_from}"
    if mode != "range":
        return start, ""
    clean_to = _planned_time(time_to)
    if clean_to is None:
        return "", _("Укажите время «до» (ЧЧ:ММ)")
    if clean_to < clean_from:
        return "", _("Время «до» раньше времени «с»")
    return f"{start}–{clean_to}", ""


TITLE_MAX_LENGTH = 255
DESCRIPTION_MAX_LENGTH = 32768


def _form_errors(form: dict[str, str]) -> dict[str, str]:
    errors: dict[str, str] = {}
    title = form.get("title", "").strip()
    description = form.get("description", "")
    if not title:
        errors["title"] = _("Укажите заголовок")
    elif len(title) > TITLE_MAX_LENGTH:
        errors["title"] = _("Заголовок длиннее {n} символов", n=TITLE_MAX_LENGTH)
    if not description.strip():
        errors["description"] = _("Укажите описание")
    elif len(description) > DESCRIPTION_MAX_LENGTH:
        errors["description"] = _("Описание длиннее {n} символов", n=DESCRIPTION_MAX_LENGTH)
    return errors


def _legacy_planned_fallback(form: dict[str, str], built: str, error: str) -> str:
    """Свободное поле planned_at как запасной путь, когда блок виджета пуст.

    Виджет собрал строку — он выигрывает; виджет пуст и пришло поле planned_at
    (старые клиенты) — берётся оно как есть; иначе пустая строка.
    """
    if built or error:
        return built
    # Старые клиенты: строка как есть, но с потолком — без лимита поле тащит мегабайты.
    return (form.get("planned_at") or "").strip()[:PLANNED_LEGACY_MAX_LENGTH]


@router.post("/tickets/create", dependencies=[require("operator")])
async def create_ticket_form(
    request: Request, user: User | None = Depends(current_user_dependency)
):
    form = await parse_form(request)
    title = form.get("title", "")
    description = form.get("description", "")
    built, planned_error = build_planned_at(form)
    planned_at = _legacy_planned_fallback(form, built, planned_error)
    errors = _form_errors(form)
    if planned_error:
        errors["planned"] = planned_error
    if errors:
        return templates.TemplateResponse(
            request,
            "tickets/new.html",
            {
                "user": user,
                "errors": errors,
                "form": {
                    "title": title,
                    "description": description,
                    **planned_widget_fields(form),
                },
            },
        )
    ticket = find_recent_duplicate(title, description, planned_at or None, user.id)
    if ticket is None:
        ticket = create_ticket(title, description, planned_at or None, user)
        enqueue(ticket.id, {EVENT_USEDESK, EVENT_TELEGRAM_NEW})
    return RedirectResponse(_done_url("created", ticket.number), status_code=303)


@router.get("/tickets/{ticket_id}/full", dependencies=[require("operator")])
def ticket_full_fragment(
    ticket_id: int, request: Request, user: User | None = Depends(current_user_dependency)
):
    ticket = ticket_or_none(ticket_id)
    if ticket is None:
        return _not_found()
    return templates.TemplateResponse(
        request,
        "tickets/full.html",
        {
            "user": user,
            "ticket": ticket,
            "closed_at_msk": format_msk(ticket.closed_at),
            "planned_label": planned_view(ticket.planned_at, msk_now())["label"],
        },
    )


@router.get("/tickets/{ticket_id}/edit", dependencies=[require("operator")])
def edit_ticket_page(
    ticket_id: int, request: Request, user: User | None = Depends(current_user_dependency)
):
    ticket = ticket_or_none(ticket_id)
    if ticket is None:
        return _not_found()
    return templates.TemplateResponse(
        request,
        "tickets/edit.html",
        {
            "user": user,
            "ticket": ticket,
            "errors": {},
            "form": {
                "title": ticket.title,
                "description": ticket.description,
                **planned_context(ticket.planned_at),
            },
        },
    )


@router.post("/tickets/{ticket_id}/edit", dependencies=[require("operator")])
async def edit_ticket_form(
    ticket_id: int,
    request: Request,
    user: User | None = Depends(current_user_dependency),
):
    ticket = ticket_or_none(ticket_id)
    if ticket is None:
        return _not_found()
    form = await parse_form(request)
    built, planned_error = build_planned_at(form)
    errors = _form_errors(form)
    if planned_error:
        errors["planned"] = planned_error
    if errors:
        return templates.TemplateResponse(
            request,
            "tickets/edit.html",
            {
                "user": user,
                "ticket": ticket,
                "errors": errors,
                "form": {
                    "title": form.get("title", ""),
                    "description": form.get("description", ""),
                    **planned_widget_fields(
                        form,
                        planned_context(ticket.planned_at)["planned_legacy"],
                        has_value=bool((ticket.planned_at or "").strip()),
                    ),
                },
            },
        )
    fields = {"title": form.get("title", ""), "description": form.get("description", "")}
    if built:
        fields["planned_at"] = built
    elif form.get("planned_at", "").strip():
        fields["planned_at"] = form["planned_at"].strip()
    elif form.get("planned_clear"):
        fields["planned_at"] = ""
    try:
        edited = edit_ticket(ticket_id, fields, user)
    except TicketNotFound:
        return _not_found()  # удалена между просмотром и отправкой
    target = f"/tickets?status={edited.status}&done=saved&n={edited.number}"
    return RedirectResponse(target, status_code=303)


@router.post("/tickets/{ticket_id}/close", dependencies=[require("operator")])
async def close_ticket_form(
    ticket_id: int,
    request: Request,
    user: User | None = Depends(current_user_dependency),
):
    form = await parse_form(request)
    try:
        closed = close_ticket(ticket_id, form.get("comment"), user)
    except TicketNotFound:
        return _not_found()
    except ValueError as error:
        return _render_list(request, user, status="open", q="", page=1, error=str(error))
    try:
        enqueue(ticket_id, {EVENT_USEDESK, EVENT_TELEGRAM_CLOSED})
    except TicketNotFound:
        return _not_found()  # удалена сразу после закрытия
    return RedirectResponse(_done_url("closed", closed.number), status_code=303)


@router.post("/tickets/{ticket_id}/delete", dependencies=[require("operator")])
async def delete_ticket_form(
    ticket_id: int,
    request: Request,
    user: User | None = Depends(current_user_dependency),
):
    await parse_form(request)
    ticket = ticket_or_none(ticket_id)
    if ticket is None:
        return _not_found()
    try:
        delete_ticket(ticket_id, user)
    except TicketNotFound:
        return _not_found()
    kind = "deleted-closed" if ticket.status == "closed" else "deleted"
    return RedirectResponse(_done_url(kind, ticket.number), status_code=303)
