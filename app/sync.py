"""Поллер синхронизации: исходящие события (sync_state) и входящие (watermark).

Контракт шва (interfaces.md, менять нельзя):
- enqueue(ticket_id, events: set[str]) -> None — события «usedesk»,
  «telegram_new», «telegram_closed»; единственный вход роутов в синхронизацию;
- poll_once() -> SyncStats — один цикл (тесты и кнопка «Тест UseDesk»);
- poll_loop(interval_seconds) — фоновая задача FastAPI (цикл 60 с);
- set_clients(usedesk=..., telegram=...) / reset_clients() — тест-шов:
  подмена клиентов моками httpx.MockTransport, живой сети в тестах нет.

Инварианты:
- поллер — единственный писатель sync_state и watermark; роуты только enqueue;
- заявка source=usedesk никогда не порождает исходящие UseDesk-запросы
  (анти-цикл, инвариант 3) — отбрасывается и в enqueue, и в poll_once;
- синхронизируются только создание и статус; конфликт — LWW по updated_at
  (naive-UTC, инвариант 4), тексты панели не перезаписываются;
- watermark первого включения тумблера UseDesk = момент включения (история
  не подтягивается); повторное включение догоняет накопленное (R13);
- пустой токен = интеграция выкл независимо от тумблера (инвариант 5);
- телеграм — best-effort (R09.1): отказ не мешает заявке, остальным событиям
  и повторяется следующим циклом.

Тумблеры/маппинг/watermark — строки Setting (UI — таск 07):
- usedesk_enabled, telegram_enabled, templates_enabled — «0»/«1»;
- usedesk_status_closed (по умолчанию «2» = «Выполнен»)
  («1» = «Открыт») — маппинг панель -> UseDesk;
- usedesk_closed_statuses («2,3,4,7,10») — входящий маппинг UseDesk -> панель;
- usedesk_watermark (naive-UTC «YYYY-MM-DD HH:MM:SS»), usedesk_poll_armed и
  usedesk_poll_ever_armed — внутренние зеркала тумблера (детект включений);
- usedesk_api_token, telegram_bot_token, telegram_chat_id — ключи из админки
  (тикет T13): чтение только через геттеры usedesk_api_token()/telegram_bot_token()/
  telegram_chat_id() «непустая настройка -> она, иначе env»; очистка настройки
  возвращает env-значение или выключает интеграцию;
- sync_error_count — счётчик ошибок для админки;
- default_theme («classic») — тема для посетителей без cookie panel-theme,
  валидные значения — VALID_THEMES; снятые темы (silach/light/dark) читаются
  как classic через default_theme();
- history_grouped («0») — группировка истории по дням закрытия;
- card_shape («round») / button_shape («square») — форма плашек и кнопок
  (round = «Круглые», square = «Прямоугольные»), VALID_SHAPES;
- personal_appearance («1») — разрешено ли каждому менять вид для себя;
  личный вид — строки user_pref:{user_id}:theme|card_shape|button_shape
  (appearance_for / set_user_appearance / reset_user_appearance);
- sync_last_poll (naive-UTC) и sync_last_error («время | текст») — пишет только
  поллер, читает блок «Диагностика» в админке;
- sync_pending:{ticket_id}:{event} — очередь ожидающих событий, по строке на
  событие: добавление и снятие — атомарные single-row операции без read-merge-
  write, поэтому параллельный enqueue не теряется и не воскрешает отправленное.
"""

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.clock import utcnow_naive
from app.config import get_settings
from app.db import get_engine
from app.i18n import (
    FALLBACK_LANG,
    N_,
    SETTING_DEFAULT_LANGUAGE,
    _,
    default_language,
    normalize_lang,
    reset_lang,
    set_lang,
)
from app.models import Setting, Ticket
from app.telegram import TelegramClient, TelegramError
from app.tickets import NUMBER_ATTEMPTS, TicketNotFound, user_login_map
from app.usedesk import UsedeskClient, UsedeskError, parse_usedesk_datetime

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 60.0
USEDESK_BASE_URL = "https://api.usedesk.ru"
TELEGRAM_BASE_URL = "https://api.telegram.org"

EVENT_USEDESK = "usedesk"
EVENT_TELEGRAM_NEW = "telegram_new"
EVENT_TELEGRAM_CLOSED = "telegram_closed"
KNOWN_EVENTS = {EVENT_USEDESK, EVENT_TELEGRAM_NEW, EVENT_TELEGRAM_CLOSED}
PENDING_PREFIX = "sync_pending:"

SETTING_USEDESK_ENABLED = "usedesk_enabled"
SETTING_TELEGRAM_ENABLED = "telegram_enabled"
SETTING_TEMPLATES_ENABLED = "templates_enabled"
SETTING_DEFAULT_THEME = "default_theme"
SETTING_HISTORY_GROUPED = "history_grouped"
SETTING_CARD_SHAPE = "card_shape"
SETTING_BUTTON_SHAPE = "button_shape"
SETTING_PERSONAL_APPEARANCE = "personal_appearance"
SETTING_SYNC_LAST_POLL = "sync_last_poll"
SETTING_SYNC_LAST_ERROR = "sync_last_error"
USER_PREF_PREFIX = "user_pref:"

# Форма элементов (админка → «Внешний вид»): round = «Круглые», square = «Прямоугольные».
VALID_SHAPES = ("round", "square")
SHAPE_DEFAULTS = {SETTING_CARD_SHAPE: "round", SETTING_BUTTON_SHAPE: "square"}

# Темы панели: единый источник для админки, профиля и static/theme.js. id «new» =
# «Ночь» (id исторический: так сохранились настройки тех, кто её уже выбрал);
# «classic» — светлая строгая тема, запасная; «tablo» — светлый рабочий вид
# «Табло». Снятые id (soft, silach, light, dark) в Setting и личных настройках
# читаются как тема по умолчанию.
VALID_THEMES = ("new", "classic", "tablo")
FALLBACK_THEME = "classic"

# Вторые часы в шапке: пояс задаёт админ (clock_zone), профиль может переопределить
# (user_pref:{id}:clock_zone). Только показ — плановое время заявок остаётся по Москве.
# Список зон закрытый (id IANA + подпись), чтобы в Setting не попал мусор; переходы на
# летнее время считает браузер (Intl) и zoneinfo на сервере.
SETTING_CLOCK_ZONE = "clock_zone"
DEFAULT_CLOCK_ZONE = "Europe/Moscow"
CLOCK_ZONES = (
    ("Europe/Kaliningrad", N_("Калининград")),
    ("Europe/Moscow", N_("Москва")),
    ("Europe/Samara", N_("Самара")),
    ("Asia/Yekaterinburg", N_("Екатеринбург")),
    ("Asia/Omsk", N_("Омск")),
    ("Asia/Novosibirsk", N_("Новосибирск")),
    ("Asia/Krasnoyarsk", N_("Красноярск")),
    ("Asia/Irkutsk", N_("Иркутск")),
    ("Asia/Yakutsk", N_("Якутск")),
    ("Asia/Vladivostok", N_("Владивосток")),
    ("Asia/Magadan", N_("Магадан")),
    ("Asia/Kamchatka", N_("Камчатка")),
    ("Europe/Minsk", N_("Минск")),
    ("Asia/Almaty", N_("Алматы")),
    ("Asia/Tashkent", N_("Ташкент")),
    ("Asia/Bishkek", N_("Бишкек")),
    ("Asia/Dushanbe", N_("Душанбе")),
    ("Asia/Baku", N_("Баку")),
    ("Asia/Yerevan", N_("Ереван")),
    ("Asia/Tbilisi", N_("Тбилиси")),
    ("Europe/Chisinau", N_("Кишинёв")),
    ("Europe/Istanbul", N_("Стамбул")),
    ("Asia/Dubai", N_("Дубай")),
    ("Asia/Bangkok", N_("Бангкок")),
    ("Europe/Berlin", N_("Берлин")),
    ("Europe/London", N_("Лондон")),
    ("America/New_York", N_("Нью-Йорк")),
    ("UTC", N_("UTC")),
)
VALID_CLOCK_ZONES = tuple(zone for zone, _label in CLOCK_ZONES)
SETTING_USEDESK_API_TOKEN = "usedesk_api_token"
SETTING_TELEGRAM_BOT_TOKEN = "telegram_bot_token"
SETTING_TELEGRAM_CHAT_ID = "telegram_chat_id"
SETTING_USEDESK_STATUS_CLOSED = "usedesk_status_closed"
SETTING_USEDESK_CLOSED_STATUSES = "usedesk_closed_statuses"
SETTING_USEDESK_WATERMARK = "usedesk_watermark"
SETTING_USEDESK_ARMED = "usedesk_poll_armed"
SETTING_USEDESK_ARMED_EVER = "usedesk_poll_ever_armed"
SETTING_SYNC_ERROR_COUNT = "sync_error_count"

DEFAULT_CLOSED_STATUS_ID = 2
DEFAULT_CLOSED_STATUSES = "2,3,4,7,10"

PANEL_NAME = N_("Тикеты Енот")


@dataclass(frozen=True)
class SyncStats:
    outgoing_sent: int = 0
    outgoing_errors: int = 0
    incoming_created: int = 0
    incoming_updated: int = 0


# --- строки Setting: чтение, запись, дефолты первого старта ---


def default_settings() -> dict[str, str]:
    return {
        SETTING_USEDESK_ENABLED: "0",
        SETTING_TELEGRAM_ENABLED: "0",
        SETTING_TEMPLATES_ENABLED: "1",
        SETTING_DEFAULT_THEME: FALLBACK_THEME,
        SETTING_HISTORY_GROUPED: "0",
        SETTING_PERSONAL_APPEARANCE: "1",
        SETTING_DEFAULT_LANGUAGE: FALLBACK_LANG,
        SETTING_CLOCK_ZONE: DEFAULT_CLOCK_ZONE,
        **SHAPE_DEFAULTS,
        SETTING_USEDESK_API_TOKEN: "",
        SETTING_TELEGRAM_BOT_TOKEN: "",
        SETTING_TELEGRAM_CHAT_ID: "",
        SETTING_USEDESK_STATUS_CLOSED: str(DEFAULT_CLOSED_STATUS_ID),
        SETTING_USEDESK_CLOSED_STATUSES: DEFAULT_CLOSED_STATUSES,
        SETTING_USEDESK_WATERMARK: utcnow_naive().strftime("%Y-%m-%d %H:%M:%S"),
        SETTING_USEDESK_ARMED: "0",
        SETTING_USEDESK_ARMED_EVER: "0",
        SETTING_SYNC_ERROR_COUNT: "0",
    }


def ensure_default_settings() -> None:
    """Создать отсутствующие строки настроек; существующие не трогаются."""
    with Session(get_engine()) as session:
        for key, value in default_settings().items():
            if session.get(Setting, key) is None:
                session.add(Setting(key=key, value=value))
        session.commit()


def default_theme() -> str:
    """Тема по умолчанию из админки; неизвестное (снятое) значение — classic."""
    value = get_setting(SETTING_DEFAULT_THEME, FALLBACK_THEME)
    return value if value in VALID_THEMES else FALLBACK_THEME


def ui_shapes() -> dict[str, str]:
    """Форма плашек и кнопок для <html data-cards/data-buttons>; мусор — значение по умолчанию."""
    shapes = {}
    for key, default in SHAPE_DEFAULTS.items():
        value = get_setting(key, default)
        shapes[key] = value if value in VALID_SHAPES else default
    return shapes


def clock_zone() -> str:
    """Пояс вторых часов, заданный админом; мусор в Setting читается как Москва."""
    value = get_setting(SETTING_CLOCK_ZONE, DEFAULT_CLOCK_ZONE)
    return value if value in VALID_CLOCK_ZONES else DEFAULT_CLOCK_ZONE


def clock_zone_label(zone: str) -> str:
    """Подпись зоны на языке страницы («Москва»); неизвестная зона — её id."""
    return next((_(label) for key, label in CLOCK_ZONES if key == zone), zone)


def clock_zone_choices() -> list[tuple[str, str]]:
    return [(zone, _(label)) for zone, label in CLOCK_ZONES]


def clock_time(zone: str, now: datetime | None = None) -> str:
    """Начальное «ЧЧ:ММ» для вторых часов (дальше тикает браузер); без базы зон — пусто."""
    try:
        moment = (now or datetime.now(ZoneInfo("UTC"))).astimezone(ZoneInfo(zone))
    except (ZoneInfoNotFoundError, ValueError):
        return ""
    return moment.strftime("%H:%M")


APPEARANCE_KEYS = ("theme", SETTING_CARD_SHAPE, SETTING_BUTTON_SHAPE, SETTING_CLOCK_ZONE)


def _valid_appearance(key: str, value: str) -> bool:
    if key == "theme":
        return value in VALID_THEMES
    if key == SETTING_CLOCK_ZONE:
        return value in VALID_CLOCK_ZONES
    return key in SHAPE_DEFAULTS and value in VALID_SHAPES


def personal_appearance_allowed() -> bool:
    return get_setting(SETTING_PERSONAL_APPEARANCE, "1") == "1"


def _user_pref_key(user_id: int, key: str) -> str:
    return f"{USER_PREF_PREFIX}{user_id}:{key}"


def global_appearance() -> dict[str, str]:
    """Вид, заданный админом: тема, форма и пояс вторых часов (вход, setup, запрет личного)."""
    return {"theme": default_theme(), **ui_shapes(), SETTING_CLOCK_ZONE: clock_zone()}


def appearance_for(user: object | None) -> dict[str, str]:
    """Вид страницы для пользователя: личный (если админ разрешил и он задан), иначе общий.

    Личные значения — строки Setting user_pref:{id}:theme|card_shape|button_shape|clock_zone;
    неизвестное значение (снятая тема, мусор) молча заменяется общим.
    """
    appearance = global_appearance()
    user_id = getattr(user, "id", None)
    if user_id is None or not personal_appearance_allowed():
        return appearance
    with Session(get_engine()) as session:
        rows = session.scalars(
            select(Setting).where(Setting.key.startswith(f"{USER_PREF_PREFIX}{user_id}:"))
        ).all()
    for row in rows:
        key = row.key.rsplit(":", 1)[-1]
        if _valid_appearance(key, row.value):
            appearance[key] = row.value
    return appearance


def set_user_appearance(user_id: int, values: dict[str, str]) -> str | None:
    """Сохранить личный вид; вернуть русскую ошибку при мусоре (тогда ничего не пишется)."""
    clean = {key: (values.get(key) or "").strip() for key in APPEARANCE_KEYS if key in values}
    if not clean:
        return _("Нечего сохранять")
    for key, value in clean.items():
        if not _valid_appearance(key, value):
            return _("Неизвестное значение внешнего вида")
    for key, value in clean.items():
        set_setting(_user_pref_key(user_id, key), value)
    return None


def reset_user_appearance(user_id: int) -> None:
    """«Как у всех»: удалить личные ключи вида — дальше действует вид админа (язык остаётся)."""
    keys = [_user_pref_key(user_id, key) for key in APPEARANCE_KEYS]
    with Session(get_engine()) as session:
        session.query(Setting).filter(Setting.key.in_(keys)).delete(synchronize_session=False)
        session.commit()


def user_language(user_id: int) -> str | None:
    """Личный язык (user_pref:{id}:lang); не выбран или мусор — None."""
    return normalize_lang(get_setting(_user_pref_key(user_id, "lang")))


def set_user_language(user_id: int, lang: str) -> None:
    set_setting(_user_pref_key(user_id, "lang"), lang)


def get_setting(key: str, default: str = "") -> str:
    with Session(get_engine()) as session:
        row = session.get(Setting, key)
        return row.value if row is not None else default


def set_setting(key: str, value: str) -> None:
    with Session(get_engine()) as session:
        row = session.get(Setting, key)
        if row is None:
            session.add(Setting(key=key, value=value))
        else:
            row.value = value
        session.commit()


def secret_setting(key: str, env_value: str) -> str:
    """Единственный путь чтения ключей интеграций: непустая настройка -> она, иначе env.

    Ключ, вписанный в админке (тикет T13), приоритетнее .env; очистка настройки
    возвращает env-значение, а без него интеграция выключена (инвариант 5).
    Полное значение живёт только в БД/памяти: в HTML уходит маска, в логи —
    результат sanitize_secret_text.
    """
    return get_setting(key).strip() or env_value


def usedesk_api_token() -> str:
    return secret_setting(SETTING_USEDESK_API_TOKEN, get_settings().usedesk_api_token)


def telegram_bot_token() -> str:
    return secret_setting(SETTING_TELEGRAM_BOT_TOKEN, get_settings().telegram_bot_token)


def telegram_chat_id() -> str:
    return secret_setting(SETTING_TELEGRAM_CHAT_ID, get_settings().telegram_chat_id)


def sanitize_secret_text(text: str, *secrets: str) -> str:
    """Вычистить значения секретов из текста перед показом или записью в лог.

    Ошибки httpx несут URL запроса, а у телеграма токен — часть URL
    (/bot<token>/...), поэтому текст исключения всегда прогоняется через это.
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, "••••")
    return text


def _int_setting(key: str, fallback: int) -> int:
    raw = get_setting(key, str(fallback)).strip()
    try:
        return int(raw)
    except ValueError:
        return fallback


def _closed_statuses() -> set[int]:
    raw = get_setting(SETTING_USEDESK_CLOSED_STATUSES, DEFAULT_CLOSED_STATUSES)
    # isdecimal-гвард: мусор в настройке (прошлая валидация/ручная правка БД)
    # не должен ронять каждый цикл синка — неподходящие части пропускаются.
    statuses = {int(part) for part in raw.split(",") if part.strip().isdecimal()}
    return statuses or {2, 3, 4, 7, 10}


# --- ожидающие события: по строке на событие, sync_pending:{ticket_id}:{event} ---
#
# Очередь атомарна относительно параллельных enqueue: любая операция — один
# атомарный statement (добавление через SAVEPOINT, снятие — single-row DELETE),
# read-merge-write нет. Поллер снимает событие из очереди ДО отправки и возвращает
# только при отказе, поэтому устаревший набор не может ни затереть новые события,
# ни воскресить уже отправленные.


def pending_events_key(ticket_id: int, event: str) -> str:
    return f"{PENDING_PREFIX}{ticket_id}:{event}"


def _ticket_id_of_key(key: str) -> int | None:
    parts = key.split(":")
    if len(parts) == 3 and parts[1].isdigit():
        return int(parts[1])
    return None


def pending_events(ticket_id: int) -> set[str]:
    with Session(get_engine()) as session:
        keys = session.scalars(
            select(Setting.key).where(Setting.key.like(f"{PENDING_PREFIX}{ticket_id}:%"))
        ).all()
    return {
        parts[2]
        for key in keys
        for parts in [key.split(":")]
        if len(parts) == 3 and parts[2] in KNOWN_EVENTS
    }


def _queue_add(ticket_id: int, events: set[str]) -> None:
    """Вернуть события в очередь; добавление атомарно, дубль игнорируется."""
    if not events:
        return
    with Session(get_engine()) as session:
        for event in sorted(events):
            try:
                with session.begin_nested():
                    session.add(Setting(key=pending_events_key(ticket_id, event), value=event))
            except IntegrityError:
                continue
        session.commit()


def _queue_take(ticket_id: int, event: str) -> bool:
    """Снять событие с очереди перед отправкой (атомарный single-row DELETE)."""
    with Session(get_engine()) as session:
        row = session.get(Setting, pending_events_key(ticket_id, event))
        if row is None:
            return False
        session.delete(row)
        session.commit()
        return True


def _tickets_with_queue() -> list[int]:
    with Session(get_engine()) as session:
        keys = session.scalars(
            select(Setting.key).where(Setting.key.like(PENDING_PREFIX + "%"))
        ).all()
    ticket_ids = {ticket_id for key in keys if (ticket_id := _ticket_id_of_key(key)) is not None}
    return sorted(ticket_ids)


def _write_sync_fields(session: Session, ticket: Ticket, **fields) -> None:
    """Служебная запись синка не двигает часы LWW: updated_at остаётся прежним.

    Core-update с явным updated_at в SET: onupdate=utcnow модели применяется
    только к колонкам, отсутствующим в списке значений. Присваивание того же
    значения через ORM-атрибут не годится — оно не попадает в SET, и часы
    уходят вперёд, ломая LWW-сравнение с UseDesk.
    """
    values = {"updated_at": ticket.updated_at, **fields}
    session.execute(
        update(Ticket).where(Ticket.id == ticket.id).values(**values),
        execution_options={"synchronize_session": False},
    )
    for name, value in fields.items():
        set_committed_value(ticket, name, value)


# --- клиенты: тест-шов подмены и сборка из конфига ---


_usedesk_override: UsedeskClient | None = None
_telegram_override: TelegramClient | None = None


def set_clients(
    *, usedesk: UsedeskClient | None = None, telegram: TelegramClient | None = None
) -> None:
    global _usedesk_override, _telegram_override
    _usedesk_override = usedesk
    _telegram_override = telegram


def reset_clients() -> None:
    set_clients()


def _usedesk_client() -> UsedeskClient | None:
    if _usedesk_override is not None:
        return _usedesk_override
    token = usedesk_api_token()
    if not token:
        return None
    return UsedeskClient(token, USEDESK_BASE_URL)


def _telegram_client() -> TelegramClient | None:
    if _telegram_override is not None:
        return _telegram_override
    token = telegram_bot_token()
    if not token:
        return None
    return TelegramClient(token, TELEGRAM_BASE_URL)


# --- enqueue: вход роутов; роуты сами sync_state не пишут ---


def enqueue(ticket_id: int, events: set[str]) -> None:
    """Поставить заявке исходящие события; отправку выполнит poll_once.

    Очередь — добавление строк, атомарное само по себе; sync_state здесь
    только отображение («Отправляется…»), планирование идёт по строкам очереди.
    """
    clean = {event for event in events if event in KNOWN_EVENTS}
    if not clean:
        return
    with Session(get_engine(), expire_on_commit=False) as session:
        ticket = session.get(Ticket, ticket_id)
        if ticket is None:
            raise TicketNotFound(ticket_id)
        if ticket.source == "usedesk":
            clean.discard(EVENT_USEDESK)
        if not clean:
            return
        _queue_add(ticket_id, clean)
        if ticket.sync_state is None or ticket.sync_state == "sent":
            _write_sync_fields(session, ticket, sync_state="pending")
            session.commit()


# --- цикл поллера ---


def poll_once() -> SyncStats:
    """Один цикл: (1) исходящие pending, (2) входящие UseDesk с watermark.

    Сообщения в Телеграм и UseDesk — на языке по умолчанию из админки.
    """
    ensure_default_settings()
    token = set_lang(default_language())
    try:
        return _poll_once()
    finally:
        reset_lang(token)


def _poll_once() -> SyncStats:
    usedesk = _usedesk_client()
    telegram = _telegram_client()
    usedesk_on = (
        usedesk is not None
        and bool(usedesk_api_token())
        and get_setting(SETTING_USEDESK_ENABLED, "0") == "1"
    )
    telegram_on = (
        telegram is not None
        and bool(telegram_bot_token())
        and bool(telegram_chat_id())
        and get_setting(SETTING_TELEGRAM_ENABLED, "0") == "1"
    )
    _arm_watermark(usedesk_on)
    stats = _flush_outgoing(usedesk_on, telegram_on, usedesk, telegram)
    if usedesk_on:
        incoming = _pull_incoming(usedesk)
        stats = replace(
            stats,
            outgoing_errors=stats.outgoing_errors + incoming.outgoing_errors,
            incoming_created=incoming.incoming_created,
            incoming_updated=incoming.incoming_updated,
        )
    set_setting(SETTING_SYNC_LAST_POLL, utcnow_naive().strftime("%Y-%m-%d %H:%M:%S"))
    return stats


def sync_status() -> dict:
    """Состояние синка для «Диагностики»: опрос, watermark, очередь, ошибки.

    Только чтение; пишет эти строки поллер (poll_once/_bump_error_counter).
    """
    def parsed(value: str) -> datetime | None:
        try:
            return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    last_error_raw = get_setting(SETTING_SYNC_LAST_ERROR, "")
    error_at, __, error_text = last_error_raw.partition(" | ")
    with Session(get_engine()) as session:
        queue = int(
            session.scalar(
                select(func.count()).select_from(Setting).where(
                    Setting.key.startswith(PENDING_PREFIX)
                )
            )
            or 0
        )
    return {
        "usedesk_on": bool(usedesk_api_token())
        and get_setting(SETTING_USEDESK_ENABLED, "0") == "1",
        "telegram_on": bool(telegram_bot_token())
        and bool(telegram_chat_id())
        and get_setting(SETTING_TELEGRAM_ENABLED, "0") == "1",
        "last_poll": parsed(get_setting(SETTING_SYNC_LAST_POLL, "")),
        "watermark": parsed(get_setting(SETTING_USEDESK_WATERMARK, "")),
        "queue": queue,
        "errors": _int_setting(SETTING_SYNC_ERROR_COUNT, 0),
        "last_error_at": parsed(error_at) if error_text else None,
        "last_error": error_text,
    }


def _arm_watermark(usedesk_on: bool) -> None:
    """Первое включение тумблера: watermark = момент включения (история мимо).

    Повторные включения watermark не сбрасывают: накопленное за время «выкл»
    догоняется (R13), а первое включение историю не подтягивает (R12.5).
    """
    state = "1" if usedesk_on else "0"
    armed = get_setting(SETTING_USEDESK_ARMED, "0")
    if state == armed:
        return
    if usedesk_on and get_setting(SETTING_USEDESK_ARMED_EVER, "0") != "1":
        set_setting(SETTING_USEDESK_WATERMARK, utcnow_naive().strftime("%Y-%m-%d %H:%M:%S"))
        set_setting(SETTING_USEDESK_ARMED_EVER, "1")
    set_setting(SETTING_USEDESK_ARMED, state)


def _flush_outgoing(
    usedesk_on: bool, telegram_on: bool,
    usedesk: UsedeskClient | None, telegram: TelegramClient | None,
) -> SyncStats:
    """Отправка исходящих: снапшот БД не держится во время HTTP-вызовов.

    Тикет читается короткой сессией и отправляется отсоединённым; каждая запись
    (usedesk_ticket_id, sync_state) — собственная короткая транзакция. Иначе
    deferred-читающая транзакция через HTTP даёт SQLITE_BUSY_SNAPSHOT при
    конкурентной записи роута — и цикл поллера падает целиком.
    """
    sent = 0
    errors = 0
    for ticket_id in _tickets_with_queue():
        with Session(get_engine(), expire_on_commit=False) as session:
            ticket = session.get(Ticket, ticket_id)
        if ticket is None:
            continue
        had_failure = False
        for event in sorted(pending_events(ticket_id)):
            if event == EVENT_USEDESK and ticket.source == "usedesk":
                _queue_take(ticket_id, event)  # анти-цикл: снять и выбросить
                continue
            if event == EVENT_USEDESK and not usedesk_on:
                continue  # останется в очереди до включения
            if event in (EVENT_TELEGRAM_NEW, EVENT_TELEGRAM_CLOSED) and not telegram_on:
                continue
            if not _queue_take(ticket_id, event):
                continue  # событие забрано параллельным писателем
            try:
                if event == EVENT_USEDESK:
                    _send_usedesk(usedesk, ticket)
                elif event == EVENT_TELEGRAM_NEW:
                    _send_telegram_new(telegram, ticket)
                elif event == EVENT_TELEGRAM_CLOSED:
                    _send_telegram_closed(telegram, ticket)
            except (UsedeskError, TelegramError) as error:
                had_failure = True
                errors += 1
                _bump_error_counter(
                    _("{event} заявки {id}: {error}", event=event, id=ticket_id, error=error)
                )
                _queue_add(ticket_id, {event})  # возврат в очередь, атомарно
                logger.warning(
                    "Событие %s заявки %s не отправлено: %s",
                    event,
                    ticket_id,
                    sanitize_secret_text(str(error), usedesk_api_token(), telegram_bot_token()),
                )
                continue
            sent += 1
        remaining = pending_events(ticket_id)
        if had_failure:
            state = "error"
        elif remaining:
            state = "pending"
        else:
            state = "sent"
        with Session(get_engine()) as session:
            fresh = session.get(Ticket, ticket_id)
            if fresh is None:
                continue  # удалена под рукой; очередь чистится удалением
            _write_sync_fields(session, fresh, sync_state=state)
            session.commit()
    return SyncStats(outgoing_sent=sent, outgoing_errors=errors)


def _comment_done_key(ticket_id: int) -> str:
    return f"usedesk_comment_done:{ticket_id}"


def _send_usedesk(client: UsedeskClient, ticket: Ticket) -> None:
    """Отправка закрытия в UseDesk без дублей комментария при ретрае (ревью C3).

    Комментарий и статус — два разных запроса: если комментарий доставлен, а
    статус упал, флаг usedesk_comment_done:{id} позволяет ретраю отправить
    ТОЛЬКО статус. Флаг сбрасывается при переоткрытии заявки (новое закрытие —
    новый комментарий) и чистится вместе с заявкой.
    """
    comment_key = _comment_done_key(ticket.id)
    comment_done = get_setting(comment_key) == "1"

    if ticket.usedesk_ticket_id is None:
        usedesk_id = client.create_ticket(ticket.title, ticket.description)
        stored = str(usedesk_id)
        with Session(get_engine()) as session:
            fresh = session.get(Ticket, ticket.id)
            if fresh is not None:
                _write_sync_fields(session, fresh, usedesk_ticket_id=stored)
                session.commit()
        set_committed_value(ticket, "usedesk_ticket_id", stored)
        if ticket.status == "closed":
            if not comment_done:
                client.add_comment(stored, _close_message(ticket))
                set_setting(comment_key, "1")
            client.set_status(stored, _closed_status_id())
    elif ticket.status == "closed":
        if not comment_done:
            client.add_comment(ticket.usedesk_ticket_id, _close_message(ticket))
            set_setting(comment_key, "1")
        client.set_status(ticket.usedesk_ticket_id, _closed_status_id())


def _close_message(ticket: Ticket) -> str:
    message = _("Заявка № {n} закрыта в панели {panel}.", n=ticket.number, panel=_(PANEL_NAME))
    if ticket.comment:
        message += " " + _("Комментарий: {text}", text=ticket.comment)
    return message


def _closed_status_id() -> int:
    return _int_setting(SETTING_USEDESK_STATUS_CLOSED, DEFAULT_CLOSED_STATUS_ID)


def _send_telegram_new(client: TelegramClient, ticket: Ticket) -> None:
    logins = user_login_map([ticket.created_by])
    lines = [
        _("Новая заявка № {n}: {title}", n=ticket.number, title=ticket.title),
        _("Автор: {login}", login=logins.get(ticket.created_by) or "—"),
        _("Плановое время: {at}", at=ticket.planned_at or "—"),
    ]
    client.send_message(telegram_chat_id(), "\n".join(lines))


def _send_telegram_closed(client: TelegramClient, ticket: Ticket) -> None:
    logins = user_login_map([ticket.closed_by])
    lines = [
        _("Заявка № {n} закрыта", n=ticket.number),
        _("Закрыл: {login}", login=logins.get(ticket.closed_by) or "—"),
        _("Комментарий: {text}", text=ticket.comment or "—"),
    ]
    client.send_message(telegram_chat_id(), "\n".join(lines))


def _bump_error_counter(message: str = "") -> None:
    """+1 к счётчику ошибок; текст последней ошибки — без секретов, для «Диагностики»."""
    current = _int_setting(SETTING_SYNC_ERROR_COUNT, 0)
    set_setting(SETTING_SYNC_ERROR_COUNT, str(current + 1))
    if message:
        clean = sanitize_secret_text(message, usedesk_api_token(), telegram_bot_token())
        stamp = utcnow_naive().strftime("%Y-%m-%d %H:%M:%S")
        set_setting(SETTING_SYNC_LAST_ERROR, f"{stamp} | {clean[:500]}")


# --- входящие: UseDesk -> панель ---


def _pull_incoming(client: UsedeskClient) -> SyncStats:
    created = 0
    updated = 0
    watermark = _parse_watermark()
    try:
        items = client.list_updated(watermark)
    except UsedeskError as error:
        _bump_error_counter(_("входящие UseDesk: {error}", error=error))
        logger.warning(
            "Входящая выборка UseDesk не удалась: %s",
            sanitize_secret_text(str(error), usedesk_api_token(), telegram_bot_token()),
        )
        return SyncStats(outgoing_errors=1)
    highest = watermark
    for item in items:
        usedesk_id = str(item.get("id", "")).strip()
        change_at = parse_usedesk_datetime(item.get("last_updated_at"))
        if not usedesk_id.isdigit() or change_at is None or change_at <= watermark:
            continue
        with Session(get_engine(), expire_on_commit=False) as session:
            ticket = session.scalar(
                select(Ticket).where(Ticket.usedesk_ticket_id == usedesk_id)
            )
            if ticket is None:
                ticket = _create_incoming(session, item, usedesk_id, change_at)
                created += 1
            elif ticket.updated_at is None or change_at > ticket.updated_at:
                _apply_status(session, ticket, item.get("status"), change_at)
                updated += 1
            _refresh_activity(session, client, ticket)
            session.commit()
        highest = max(highest, change_at)
    if highest > watermark:
        set_setting(SETTING_USEDESK_WATERMARK, highest.strftime("%Y-%m-%d %H:%M:%S"))
    return SyncStats(incoming_created=created, incoming_updated=updated)


def _parse_watermark() -> datetime:
    stored = parse_usedesk_datetime(get_setting(SETTING_USEDESK_WATERMARK))
    return stored or utcnow_naive()


def _create_incoming(session: Session, item: dict, usedesk_id: str, change_at: datetime) -> Ticket:
    subject = str(item.get("subject") or "").strip() or _("Без темы")
    last_comment = item.get("last_comment")
    description = last_comment.get("message") if isinstance(last_comment, dict) else ""
    created_at = parse_usedesk_datetime(item.get("created_at")) or change_at
    for _attempt in range(NUMBER_ATTEMPTS):
        try:
            current_max = session.scalar(select(func.max(Ticket.number)))
            ticket = Ticket(
                number=(current_max or 0) + 1,
                title=subject[:255],
                description=description or "",
                status="open",
                source="usedesk",
                usedesk_ticket_id=usedesk_id,
                created_at=created_at,
                updated_at=change_at,
            )
            session.add(ticket)
            session.flush()
            _apply_status(session, ticket, item.get("status"), change_at)
            session.commit()
            return ticket
        except IntegrityError:
            session.rollback()
    raise RuntimeError("Не удалось выдать номер заявке из UseDesk: гонка нумерации")


def _apply_status(
    session: Session, ticket: Ticket, status_id: object, change_at: datetime
) -> None:
    closed = isinstance(status_id, int) and status_id in _closed_statuses()
    if closed:
        ticket.status = "closed"
        ticket.closed_at = change_at
    else:
        ticket.status = "open"
        ticket.closed_at = None
        ticket.closed_by = None
        # Переоткрытие: следующее закрытие обязано отправить комментарий заново
        # (чистка в той же сессии — чужое соединение ловит database is locked).
        session.query(Setting).filter(
            Setting.key == _comment_done_key(ticket.id)
        ).delete(synchronize_session=False)
    ticket.updated_at = change_at


def _refresh_activity(session: Session, client: UsedeskClient, ticket: Ticket) -> None:
    """Последняя реплика клиента UseDesk (R12.7); сбой не мешает синку."""
    if ticket.usedesk_ticket_id is None:
        return
    try:
        activity = client.last_client_activity(ticket.usedesk_ticket_id)
        _write_sync_fields(session, ticket, usedesk_activity=activity)
    except UsedeskError as error:
        logger.warning(
            "Активность UseDesk заявки %s не получена: %s",
            ticket.id,
            sanitize_secret_text(str(error), usedesk_api_token(), telegram_bot_token()),
        )


# --- фоновая задача FastAPI ---


async def poll_loop(interval_seconds: float = POLL_INTERVAL_SECONDS) -> None:
    """Цикл 60 с; БД и HTTP — в потоке, чтобы не блокировать event loop."""
    logger.info("Поллер синхронизации запущен: цикл %s с", interval_seconds)
    while True:
        try:
            await asyncio.to_thread(poll_once)
        except Exception:
            logger.exception("Цикл синхронизации не выполнен — повтор через %s с",
                             interval_seconds)
        await asyncio.sleep(interval_seconds)
