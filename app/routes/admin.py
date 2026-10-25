"""Раздел «Админ»: функции и ключи, маппинг статусов, шаблоны, экспорт, диагностика.

Каждый /admin-маршрут закрыт require("admin") — оператор получает 403 «Нет
доступа»; исключение — GET /tickets/template-panel, панель кнопок-шаблонов на
форме создания заявки (require("operator")), при выключенном тумблере она
отвечает пустым телом.

Тумблеры и маппинг пишут только строки Setting (ключи и семантика — T06):
usedesk_enabled / telegram_enabled / templates_enabled — «0»/«1»;
usedesk_status_closed и usedesk_closed_statuses — маппинг статусов; работу по
ним выполняет поллер. sync_error_count здесь читается и сбрасывается, пишет
его только поллер. Переключение тумблеров и результаты тест-кнопок возвращаются
HTMX-фрагментом (HX-Request) или обычной отдачей/редиректом без JS.

Ключи интеграций (тикет T13) — строки Setting usedesk_api_token /
telegram_bot_token / telegram_chat_id; чтение в поллере и тест-кнопках только
через геттеры app.sync «настройка -> env». Поля формы write-only: пустое поле
не меняет сохранённое, очистка — отдельной галочкой с подтверждением и возвращает
env-значение или выключает интеграцию. Полное значение никогда не возвращается
в HTML (только маска «••••последние4» или «не задан») и не попадает в логи;
тексты ошибок httpx прогоняются через sanitize_secret_text, потому что у
телеграма токен — часть URL запроса. После сохранения вызывается reset_clients():
следующий цикл поллера и тест-кнопки идут с новым значением без рестарта.

Тема по умолчанию (тикет T14) — строка Setting default_theme, значения
VALID_THEMES из sync.py; для посетителей без cookie panel-theme её в контекст
каждой страницы подставляет PanelTemplates (app/auth.py).

Тест-кнопки делают ровно один запрос через клиенты T06 (UsedeskClient.list_updated,
TelegramClient.send_message); watermark и sync_state они не трогают. Сборка
клиентов — _usedesk_probe_client/_telegram_probe_client, тесты подменяют их
моками httpx.MockTransport; без токена — «Токен не задан».

Экспорт выгружает текущий фильтр (?status=open|closed&q=поиск) целиком, без
пагинации; CSV — utf-8-sig (BOM для Excel), JSON — utf-8, ensure_ascii=False.
"""

import csv
import io
import json
import re
from datetime import timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import current_user, current_user_dependency, parse_form, require, templates
from app.clock import utcnow_naive
from app.db import get_engine, get_session
from app.i18n import LANG_LABELS, LANGS, N_, SETTING_DEFAULT_LANGUAGE, _, default_language
from app.models import Template, User
from app.routes.templates import render_templates_page
from app.sync import (
    DEFAULT_CLOSED_STATUS_ID,
    DEFAULT_CLOSED_STATUSES,
    SETTING_BUTTON_SHAPE,
    SETTING_CARD_SHAPE,
    SETTING_CLOCK_ZONE,
    SETTING_DEFAULT_THEME,
    SETTING_HISTORY_GROUPED,
    SETTING_PERSONAL_APPEARANCE,
    SETTING_SYNC_ERROR_COUNT,
    SETTING_TELEGRAM_BOT_TOKEN,
    SETTING_TELEGRAM_CHAT_ID,
    SETTING_TELEGRAM_ENABLED,
    SETTING_TEMPLATES_ENABLED,
    SETTING_USEDESK_API_TOKEN,
    SETTING_USEDESK_CLOSED_STATUSES,
    SETTING_USEDESK_ENABLED,
    SETTING_USEDESK_STATUS_CLOSED,
    TELEGRAM_BASE_URL,
    USEDESK_BASE_URL,
    VALID_CLOCK_ZONES,
    VALID_SHAPES,
    VALID_THEMES,
    clock_zone,
    clock_zone_choices,
    default_theme,
    get_setting,
    reset_clients,
    sanitize_secret_text,
    set_setting,
    sync_status,
    telegram_bot_token,
    telegram_chat_id,
    usedesk_api_token,
)
from app.telegram import TelegramClient, TelegramError
from app.ticket_templates import (
    create_template,
    template_fields,
    validate_template,
    visible_templates,
)
from app.tickets import (
    VALID_STATUSES,
    format_msk,
    list_tickets,
    msk_now,
    relative_label,
    user_login_map,
)
from app.usedesk import UsedeskClient, UsedeskError

router = APIRouter()

MSK_SHIFT = timedelta(hours=3)

FEATURES = (
    (SETTING_USEDESK_ENABLED, "UseDesk", N_("Синк заявок с UseDesk: поллер и исходящие")),
    (
        SETTING_TELEGRAM_ENABLED,
        N_("Телеграм"),
        N_("Сообщения в группу о новых и закрытых заявках"),
    ),
    (
        SETTING_TEMPLATES_ENABLED,
        N_("Шаблоны заявок"),
        N_("Кнопки шаблонов на форме создания заявки"),
    ),
    (
        SETTING_HISTORY_GROUPED,
        N_("История по дням"),
        N_("Группировать закрытые заявки по дню закрытия (Сегодня, Вчера, …)"),
    ),
)
# Тумблер секции «Внешний вид» — тот же механизм /admin/features/{key}.
APPEARANCE_FEATURES = (
    (
        SETTING_PERSONAL_APPEARANCE,
        N_("Разрешить менять вид индивидуально"),
        N_("Каждый выбирает тему и форму у себя в профиле; выкл — у всех вид по умолчанию"),
    ),
)
FEATURE_KEYS = frozenset(key for key, __, ___ in FEATURES + APPEARANCE_FEATURES)
FEATURE_DEFAULTS = {SETTING_PERSONAL_APPEARANCE: "1"}

# «Добавить примеры» в секции «Шаблоны заявок»: {…} — подстановка, её выделяет форма.
# Примеры создаются на языке админа, нажавшего кнопку.
EXAMPLE_TEMPLATES = (
    (N_("Обновление базы"), N_("Обновить {база}"), N_("на крайний релиз, сначала бэкап")),
    (
        N_("Релиз в хранилище"),
        N_("Релиз {база} в хранилище"),
        N_("релиз {номер релиза} положить в хранилище и обновить"),
    ),
    (
        N_("Проверка доступов"),
        N_("Проверить доступы {база}"),
        N_("клиент прислал новые доступы — проверить вход и записать в карточку"),
    ),
)

EXPORT_FIELDS = (
    "number",
    "title",
    "description",
    "planned_at",
    "status",
    "comment",
    "created_at",
    "created_by",
    "closed_at",
    "closed_by",
    "source",
    "usedesk_ticket_id",
)

CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t")


def escape_csv_cell(value: str) -> str:
    """Префикс ' для ячеек, начинающихся с =, +, -, @ или таба (formula injection)."""
    if value.startswith(CSV_FORMULA_PREFIXES):
        return "'" + value
    return value


def _usedesk_probe_client(token: str) -> UsedeskClient:
    """Сборка клиента тест-кнопки; тесты подменяют моком httpx.MockTransport."""
    return UsedeskClient(token, USEDESK_BASE_URL)


def _telegram_probe_client(token: str) -> TelegramClient:
    return TelegramClient(token, TELEGRAM_BASE_URL)


# --- контекст страницы «Админ» ---


def _mask_secret(value: str) -> str:
    """Маска для страницы: последние 4 символа значения; пустое — «не задан»."""
    return f"••••{value[-4:]}" if value else _("не задан")


def _feature_views(features=FEATURES) -> list[dict]:
    return [
        {
            "key": key,
            "label": _(label),
            "hint": _(hint),
            "on": get_setting(key, FEATURE_DEFAULTS.get(key, "0")) == "1",
        }
        for key, label, hint in features
    ]


def _feature_view(key: str) -> dict:
    return next(
        view
        for view in _feature_views(FEATURES + APPEARANCE_FEATURES)
        if view["key"] == key
    )


def _moment_label(moment) -> str:
    """naive-UTC -> «дд.мм.гггг чч:мм МСК (N мин назад)»; None -> «ещё не было»."""
    if moment is None:
        return _("ещё не было")
    relative = relative_label(moment + MSK_SHIFT, msk_now())
    return _("{at} МСК ({relative})", at=format_msk(moment), relative=relative)


def _sync_view() -> dict:
    status = sync_status()
    return {
        "last_poll": _moment_label(status["last_poll"]),
        "watermark": _moment_label(status["watermark"]),
        "last_error_at": _("{at} МСК", at=format_msk(status["last_error_at"]))
        if status["last_error_at"]
        else "",
    }


def _request_user(request: Request) -> User | None:
    """Вошедший админ для шапки на страницах-ответах POST (ошибки форм)."""
    with Session(get_engine()) as session:
        user = current_user(request, session)
        if user is not None:
            session.expunge(user)
        return user


def _list_templates() -> list[Template]:
    with Session(get_engine()) as session:
        return list(session.scalars(select(Template).order_by(Template.name, Template.id)).all())


def _admin_context(**extra: object) -> dict:
    context: dict = {
        "features": _feature_views(),
        "appearance_features": _feature_views(APPEARANCE_FEATURES),
        "user": None,
        "tab": "admin",
        "theme_default": default_theme(),
        "language_default": default_language(),
        "language_choices": [(code, LANG_LABELS[code]) for code in LANGS],
        "clock_zone_default": clock_zone(),
        "clock_zone_choices": clock_zone_choices(),
        "sync": sync_status(),
        "templates_on": get_setting(SETTING_TEMPLATES_ENABLED, "0") == "1",
        "usedesk_token_mask": _mask_secret(usedesk_api_token()),
        "telegram_token_mask": _mask_secret(telegram_bot_token()),
        "telegram_chat_mask": _mask_secret(telegram_chat_id()),
        "status_closed": get_setting(
            SETTING_USEDESK_STATUS_CLOSED, str(DEFAULT_CLOSED_STATUS_ID)
        ),
        "closed_statuses": get_setting(SETTING_USEDESK_CLOSED_STATUSES, DEFAULT_CLOSED_STATUSES),
        "templates": _list_templates(),
        "sync_error_count": get_setting(SETTING_SYNC_ERROR_COUNT, "0"),
        "sync_view": _sync_view(),
        "mapping_error": None,
        "theme_error": None,
        "shape_error": None,
        "language_error": None,
        "clock_error": None,
        "tokens_error": None,
        "usedesk_result": None,
        "telegram_result": None,
    }
    context.update(extra)
    return context


def _admin_page(request: Request, **extra: object):
    extra.setdefault("user", _request_user(request))
    return templates.TemplateResponse(request, "admin/index.html", _admin_context(**extra))


# --- страница и тумблеры функций ---


@router.get("/admin", dependencies=[require("admin")])
def admin_page(
    request: Request,
    tpl_from: str = "",
    admin: User = Depends(current_user_dependency),
):
    if tpl_from:  # старая ссылка «Сохранить как шаблон» — теперь страница «Шаблоны»
        return RedirectResponse(f"/templates?from={quote(tpl_from)}", status_code=303)
    return _admin_page(request, user=admin)


@router.post("/admin/features/{key}", dependencies=[require("admin")])
async def toggle_feature(key: str, request: Request):
    if key not in FEATURE_KEYS:
        return HTMLResponse(_("Неизвестная функция"), status_code=404)
    form = await parse_form(request)
    set_setting(key, "1" if form.get("value") == "1" else "0")
    if request.headers.get("HX-Request"):
        feature = _feature_view(key)
        return templates.TemplateResponse(
            request, "admin/_feature_toggle.html", {"feature": feature}
        )
    return RedirectResponse("/admin", status_code=303)


# --- тема по умолчанию (тикет T14) ---


@router.post("/admin/settings/theme-default", dependencies=[require("admin")])
async def save_default_theme(request: Request):
    """Тема для новых посетителей без cookie panel-theme (сами куки приоритетнее).

    Значение сверяется с VALID_THEMES (sync.py — единый источник): невалидное
    возвращает страницу «Админ» с русской ошибкой и не меняет Setting.
    """
    form = await parse_form(request)
    value = form.get("value", "").strip()
    if value not in VALID_THEMES:
        return _admin_page(
            request,
            theme_error=_("Неизвестная тема: выберите «Ночь», «Классика» или «Табло»"),
        )
    set_setting(SETTING_DEFAULT_THEME, value)
    return RedirectResponse("/admin", status_code=303)


# --- язык по умолчанию: вход, Телеграм и UseDesk ---


@router.post("/admin/settings/language-default", dependencies=[require("admin")])
async def save_default_language(request: Request):
    """Язык для страницы входа без cookie panel-lang и для сообщений поллера."""
    form = await parse_form(request)
    value = form.get("value", "").strip()
    if value not in LANGS:
        return _admin_page(
            request, language_error=_("Неизвестный язык: выберите русский или английский")
        )
    set_setting(SETTING_DEFAULT_LANGUAGE, value)
    return RedirectResponse("/admin", status_code=303)


# --- пояс вторых часов в шапке: только показ, плановое время остаётся по Москве ---


@router.post("/admin/settings/clock-zone", dependencies=[require("admin")])
async def save_clock_zone(request: Request):
    """Пояс вторых часов для всех (профиль может переопределить); значение — из CLOCK_ZONES."""
    form = await parse_form(request)
    value = form.get("value", "").strip()
    if value not in VALID_CLOCK_ZONES:
        return _admin_page(
            request, clock_error=_("Неизвестный часовой пояс: выберите город из списка")
        )
    set_setting(SETTING_CLOCK_ZONE, value)
    return RedirectResponse("/admin", status_code=303)


# --- форма плашек и кнопок: «Круглые» / «Прямоугольные» ---


@router.post("/admin/settings/shapes", dependencies=[require("admin")])
async def save_shapes(request: Request):
    """Форма плашек и кнопок для всех посетителей (data-cards/data-buttons на <html>).

    Оба значения сверяются с VALID_SHAPES; мусор — страница «Админ» с русской
    ошибкой, Setting не меняется.
    """
    form = await parse_form(request)
    card = form.get("card_shape", "").strip()
    button = form.get("button_shape", "").strip()
    if card not in VALID_SHAPES or button not in VALID_SHAPES:
        return _admin_page(
            request, shape_error=_("Неизвестная форма: выберите «Круглые» или «Прямоугольные»")
        )
    set_setting(SETTING_CARD_SHAPE, card)
    set_setting(SETTING_BUTTON_SHAPE, button)
    return RedirectResponse("/admin", status_code=303)


# --- ключи и функции: write-only поля токенов ---


CHAT_ID_GROUP_RE = re.compile(r"^-?[0-9]+$")
CHAT_ID_CHANNEL_RE = re.compile(r"^@[A-Za-z][A-Za-z0-9_]{3,63}$")


def _validate_chat_id(value: str) -> str | None:
    """TELEGRAM_CHAT_ID — число (id группы, бывает отрицательным) или @имя_канала."""
    if not value or CHAT_ID_GROUP_RE.fullmatch(value) or CHAT_ID_CHANNEL_RE.fullmatch(value):
        return None
    return _("TELEGRAM_CHAT_ID: укажите число (id группы, например -1001234567890) или @имя_канала")


@router.post("/admin/settings/tokens", dependencies=[require("admin")])
async def save_tokens(request: Request):
    """Ключи интеграций из раздела «Функции»: ввод, замена, очистка с галочкой.

    Поля write-only: пустое поле ввода не меняет сохранённое значение, очистка —
    только явной галочкой (клиентское подтверждение, затем запись пустой строки,
    после чего геттер возвращает значение из env или интеграция выключена).
    Введённое значение никогда не возвращается в HTML: при ошибке валидации
    страница рендерится без подстановки полей. Применение мгновенное: записи в
    Setting сопровождаются reset_clients() — следующий цикл поллера и тест-кнопки
    собирают клиентов с новым значением, рестарт не нужен.
    """
    form = await parse_form(request)
    chat_error = _validate_chat_id(form.get("telegram_chat_id", "").strip())
    if chat_error is not None:
        return _admin_page(request, tokens_error=chat_error)
    changed = False
    for key, field, clear_field in (
        (SETTING_USEDESK_API_TOKEN, "usedesk_api_token", "clear_usedesk"),
        (SETTING_TELEGRAM_BOT_TOKEN, "telegram_bot_token", "clear_telegram"),
        (SETTING_TELEGRAM_CHAT_ID, "telegram_chat_id", "clear_telegram_chat"),
    ):
        value = form.get(field, "").strip()
        if form.get(clear_field):
            set_setting(key, "")
            changed = True
        elif value:
            set_setting(key, value)
            changed = True
    if changed:
        reset_clients()
    return RedirectResponse("/admin", status_code=303)


# --- маппинг статусов UseDesk ---


def _validate_mapping(status_closed: str, closed_statuses: str) -> str | None:
    # isdecimal, а не isdigit: «²» проходит isdigit, но int() бросает ValueError
    # и роняет поллер на каждом цикле (ревью C1).
    if not status_closed.isdecimal():
        return _("Статус закрытия — число-id UseDesk (по умолчанию 2 «Выполнен»)")
    parts = [part.strip() for part in closed_statuses.split(",")]
    if not closed_statuses or any(not part.isdecimal() for part in parts):
        return _("Закрытые статусы — числа-id через запятую (по умолчанию 2,3,4,7,10)")
    return None


def _normalize_closed_statuses(closed_statuses: str) -> str:
    return ",".join(part.strip() for part in closed_statuses.split(","))


@router.post("/admin/settings/mapping", dependencies=[require("admin")])
async def save_mapping(request: Request):
    form = await parse_form(request)
    status_closed = form.get("usedesk_status_closed", "").strip()
    closed_statuses = form.get("usedesk_closed_statuses", "").strip()
    error = _validate_mapping(status_closed, closed_statuses)
    if error is not None:
        return _admin_page(
            request,
            mapping_error=error,
            status_closed=status_closed,
            closed_statuses=closed_statuses,
        )
    set_setting(SETTING_USEDESK_STATUS_CLOSED, status_closed)
    set_setting(SETTING_USEDESK_CLOSED_STATUSES, _normalize_closed_statuses(closed_statuses))
    return RedirectResponse("/admin", status_code=303)


# --- шаблоны заявок: CRUD ---


@router.post("/admin/templates/create", dependencies=[require("admin")])
async def admin_create_template(request: Request):
    form = await parse_form(request)
    fields = template_fields(form)
    error = validate_template(fields)
    if error is not None:
        return render_templates_page(
            request, _request_user(request), form={**fields, "shared": "1"}, error=error
        )
    create_template(fields, _request_user(request), shared=True)
    # Шаблон создан, чтобы им пользовались: кнопки на форме включаются сами.
    set_setting(SETTING_TEMPLATES_ENABLED, "1")
    return RedirectResponse("/templates?done=created", status_code=303)


@router.post("/admin/templates/examples", dependencies=[require("admin")])
async def create_example_templates(request: Request):
    """«Добавить примеры»: три типовых шаблона — только когда шаблонов нет совсем."""
    await parse_form(request)
    with Session(get_engine()) as session:
        if session.scalar(select(Template.id).limit(1)) is None:
            for name, title_tpl, description_tpl in EXAMPLE_TEMPLATES:
                session.add(
                    Template(
                        name=_(name), title_tpl=_(title_tpl), description_tpl=_(description_tpl)
                    )
                )
            session.commit()
    set_setting(SETTING_TEMPLATES_ENABLED, "1")
    return RedirectResponse("/admin#templates", status_code=303)


@router.get("/admin/templates/{template_id}/edit", dependencies=[require("admin")])
def edit_template_page(
    template_id: int,
    request: Request,
    session: Session = Depends(get_session),
    admin: User = Depends(current_user_dependency),
):
    template = session.get(Template, template_id)
    if template is None or not _admin_may_touch(template, admin):
        return HTMLResponse(_("Шаблон не найден"), status_code=404)
    return templates.TemplateResponse(
        request,
        "admin/template_edit.html",
        {"template": template, "error": None, "user": admin, "tab": "admin"},
    )


@router.post("/admin/templates/{template_id}/edit", dependencies=[require("admin")])
async def edit_template(
    template_id: int, request: Request, session: Session = Depends(get_session)
):
    template = session.get(Template, template_id)
    if template is None or not _admin_may_touch(template, _request_user(request)):
        return HTMLResponse(_("Шаблон не найден"), status_code=404)
    form = await parse_form(request)
    fields = template_fields(form)
    error = validate_template(fields)
    if error is not None:
        return templates.TemplateResponse(
            request,
            "admin/template_edit.html",
            {"template": template, "error": error, "user": _request_user(request)},
        )
    for name, value in fields.items():
        setattr(template, name, value)
    session.commit()
    return RedirectResponse("/templates?done=saved", status_code=303)


@router.post("/admin/templates/{template_id}/delete", dependencies=[require("admin")])
async def delete_template(
    template_id: int, request: Request, session: Session = Depends(get_session)
):
    await parse_form(request)
    template = session.get(Template, template_id)
    if template is not None and _admin_may_touch(template, _request_user(request)):
        session.delete(template)
        session.commit()
    return RedirectResponse("/templates?done=deleted", status_code=303)


def _admin_may_touch(template: Template, admin: User | None) -> bool:
    """Админ правит общие шаблоны и свои; личные шаблоны операторов — только их авторы."""
    return template.is_shared or (admin is not None and template.created_by == admin.id)


@router.get("/tickets/template-panel", dependencies=[require("operator")])
def template_panel(request: Request, user: User = Depends(current_user_dependency)):
    """Кнопки-шаблоны на форме создания заявки: свои и общие.

    Тумблер выключен админом — пустое тело; шаблонов нет — подсказка со ссылкой
    на страницу «Шаблоны», где их делают.
    """
    if get_setting(SETTING_TEMPLATES_ENABLED, "1") != "1":
        return HTMLResponse("")
    items = visible_templates(user)
    return templates.TemplateResponse(
        request,
        "admin/_template_buttons.html",
        {"templates": items, "user_id": user.id},
    )


# --- экспорт текущего фильтра ---


def _matching_tickets(status: str, query: str) -> list:
    clean_status = status if status in VALID_STATUSES else None
    tickets: list = []
    page = 1
    while True:
        result = list_tickets(clean_status, query, page)
        tickets.extend(result.items)
        if page >= result.pages:
            return tickets
        page += 1


def _export_rows(status: str, query: str) -> list[dict]:
    tickets = _matching_tickets(status, query)
    logins = user_login_map(
        [ticket.created_by for ticket in tickets] + [ticket.closed_by for ticket in tickets]
    )
    return [
        {
            "number": ticket.number,
            "title": ticket.title,
            "description": ticket.description,
            "planned_at": ticket.planned_at or "",
            "status": _("закрыта") if ticket.status == "closed" else _("открыта"),
            "comment": ticket.comment or "",
            "created_at": format_msk(ticket.created_at),
            "created_by": logins.get(ticket.created_by, ""),
            "closed_at": format_msk(ticket.closed_at),
            "closed_by": logins.get(ticket.closed_by, ""),
            "source": ticket.source,
            "usedesk_ticket_id": ticket.usedesk_ticket_id or "",
        }
        for ticket in tickets
    ]


@router.get("/admin/export.csv", dependencies=[require("admin")])
def export_csv(status: str = "", q: str = ""):
    rows = _export_rows(status, q)
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(EXPORT_FIELDS)
    for row in rows:
        writer.writerow([escape_csv_cell(str(row[field])) for field in EXPORT_FIELDS])
    return Response(
        "\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="tickets-export.csv"'},
    )


@router.get("/admin/export.json", dependencies=[require("admin")])
def export_json(status: str = "", q: str = ""):
    payload = json.dumps(_export_rows(status, q), ensure_ascii=False, indent=2)
    return Response(
        payload,
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="tickets-export.json"'},
    )


# --- диагностика: счётчик ошибок и тест-кнопки ---


@router.post("/admin/diagnostics/reset-errors", dependencies=[require("admin")])
async def reset_error_counter(request: Request):
    await parse_form(request)
    set_setting(SETTING_SYNC_ERROR_COUNT, "0")
    return RedirectResponse("/admin", status_code=303)


def _run_usedesk_probe() -> tuple[bool, str]:
    token = usedesk_api_token()
    if not token:
        return False, _("Токен не задан")
    client = _usedesk_probe_client(token)
    try:
        client.list_updated(utcnow_naive())
    except UsedeskError as error:
        return False, _("Ошибка: {text}", text=sanitize_secret_text(str(error), token))
    finally:
        client.close()
    return True, _("Подключение ок — UseDesk отвечает")


def _run_telegram_probe() -> tuple[bool, str]:
    token = telegram_bot_token()
    chat_id = telegram_chat_id()
    if not token:
        return False, _("Токен не задан")
    if not chat_id:
        return False, _("Не задан chat_id группы (TELEGRAM_CHAT_ID или «Ключи и функции»)")
    client = _telegram_probe_client(token)
    try:
        client.send_message(chat_id, _("Тестовое сообщение из панели «Тикеты Енот»."))
    except TelegramError as error:
        return False, _("Ошибка: {text}", text=sanitize_secret_text(str(error), token))
    finally:
        client.close()
    return True, _("Тестовое сообщение отправлено")


def _diagnostic_response(request: Request, name: str, label: str, ok: bool, text: str):
    result = {"ok": ok, "text": text}
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request, "admin/_diagnostic.html", {"name": name, "label": label, "result": result}
        )
    return _admin_page(request, **{f"{name}_result": result})


@router.post("/admin/diagnostics/test-usedesk", dependencies=[require("admin")])
async def test_usedesk(request: Request):
    await parse_form(request)
    ok, text = _run_usedesk_probe()
    return _diagnostic_response(request, "usedesk", _("Тест UseDesk"), ok, text)


@router.post("/admin/diagnostics/test-telegram", dependencies=[require("admin")])
async def test_telegram(request: Request):
    await parse_form(request)
    ok, text = _run_telegram_probe()
    return _diagnostic_response(request, "telegram", _("Тестовое сообщение"), ok, text)
