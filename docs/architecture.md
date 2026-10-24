# Архитектура — Тикеты Енот v2

Указатель для разработчика: поток данных, границы модулей, швы, инварианты, конвенции.
Сигнатуры из раздела «Швы» — контракт: менять только вместе с этим файлом.
«Почему так» — `docs/adr/`; API UseDesk по пунктам — `docs/usedesk-api.md`.

## Облик

- Один процесс FastAPI: HTTP-роуты и фоновый поллер живут в нём же (asyncio-таска из lifespan, `app/main.py`); хранилище — один файл SQLite с WAL, внешней БД нет (ADR-0001).
- Представление — сервер-рендер Jinja2 + HTMX (`static/htmx.min.js`), стили — `static/tailwind.css` (токены двух тем → утилиты с именами Tailwind → компоненты), CDN в рантайме нет; успешные мутации форм — 303-редирект (PRG) на `/tickets?status=…&done=created|saved|closed|deleted&n=<номер>` (строка-подтверждение).
- Внешний вид страницы рендерит сервер (`PanelTemplates` → `page_theme/page_cards/page_buttons` из `sync.appearance_for(user)`): личный, если `personal_appearance` = «1» и он задан, иначе общий из админки; cookie темы и `theme-preset.js` больше нет. Переключатель темы сохраняет выбор через `POST /profile/appearance` (fetch + `X-CSRF-Token`).
- Шаблоны заявок — `app/ticket_templates.py` (видимость: общие + свои; права: автор, админ — любые общие) и страница `/templates` (`app/routes/templates.py`, operator+; `?from=<id заявки>` — «Сохранить как шаблон»). `/admin/templates/*` остались для админа и работают только с общими и своими. Колонки `templates.created_by`/`is_shared` дописываются в старые БД при старте (`db.ADDITIVE_COLUMNS`, ADR-0007).
- `/profile` (`app/routes/profile.py`, operator+) — смена своего пароля (`password_rule_error`: 8+ символов, ≤ 72 байт; неверный текущий — тот же троттлинг, что вход; cookie сессии переиздаётся через `set_session_cookie`) и личный вид.
- Темы — `new` («Ночь», акцент бирюза `--btn-primary`) и `classic` («Классика», акцент синий); различаются только значениями CSS-токенов в `:root[data-theme=…]` (включая фон hero и показ енота), перекраска элементов селекторами по теме запрещена. Форма элементов — атрибуты `data-cards`/`data-buttons` на `<html>` (Setting `card_shape`/`button_shape`, round|square, `sync.ui_shapes()`) → токены радиусов `--r-card/--r-inner/--r-field/--r-chip/--r-btn`. Неизвестное значение `default_theme` (снятые soft/silach/light/dark) читается как classic (`sync.default_theme()`).
- Язык интерфейса — `app/i18n.py`: строки пишутся по-русски в `_()` (константы помечаются `N_()` и переводятся при показе), английский каталог `app/locales/en.json` (ключ — русская строка). Язык запроса держит ContextVar: `LanguageMiddleware` берёт cookie `panel-lang`, без неё — Setting `language_default`. `POST /lang` (`app/routes/language.py`) ставит cookie и пишет `user_pref:{id}:lang`; вход переносит личный язык в cookie. Поллер пишет в Телеграм и UseDesk на языке по умолчанию. Полноту каталога и отсутствие русского текста на английских страницах проверяет `tests/test_i18n.py`. Как сделать свою тему — `docs/themes.md`.
- Синхронизация с UseDesk — поллинг раз в 60 с, не вебхуки (ADR-0002); телеграм — только исходящие уведомления, best-effort.

## Поток данных

- Браузер → `app/routes/*` (`require("operator"|"admin")`) → домены `app/tickets.py` и `app/sync.enqueue` → SQLite.
- Создание/закрытие заявки роутом ставит события в очередь — строки `sync_pending:{ticket_id}:{event}` в settings, `sync_state` = pending.
- Поллер (`app/sync.poll_loop` → `poll_once` в `asyncio.to_thread`): снимает событие с очереди ДО отправки (атомарный single-row DELETE, возврат в очередь при отказе, ADR-0004), шлёт в UseDesk/Telegram, затем тянет входящие UseDesk по watermark (`usedesk_watermark`) и создаёт/обновляет заявки `source=usedesk`.
- Конфликт статусов — LWW по `updated_at` (naive-UTC, ADR-0005); тексты панели входящими не перезаписываются, синхронизируются только создание и статус.
- Первый старт (`app/main.py` lifespan): `init_db` → бутстрап админа из env → seed-импорт (только при пустой таблице tickets, сбой не мешает старту) → `ensure_default_settings` → `poll_loop`.

## Границы модулей

| Модуль | Владеет | Скрывает |
|--------|---------|----------|
| `app/db.py` | движок, сессии, `create_all`, WAL; PRAGMA на каждом соединении | строку подключения |
| `app/models.py` | таблицы users/tickets/settings/templates | — |
| `app/auth.py` | логин, сессии-cookie, `require(role)`, бутстрап админа, `parse_form` | хеширование |
| `app/clock.py` | `utcnow_naive()` — единая точка «сейчас» | — |
| `app/config.py` | `ENV_NAMES`, `Settings` (чистая `settings_from_env` + кэш `get_settings`) | `.env` |
| `app/tickets.py` | CRUD, нумерация, поиск, счётчики, сводка, срочность планового времени | SQL |
| `app/importer.py` | `run_import` (insert-missing по id), отчёт | формат JSON-файла |
| `app/usedesk.py` | `UsedeskClient`: `create_ticket`, `add_comment + set_status`, `list_updated`, `last_client_activity` | эндпоинты API |
| `app/telegram.py` | `TelegramClient.send_message` | Bot API |
| `app/sync.py` | очередь, watermark, LWW, все ключи Setting | расписание |
| `app/routes/*` | HTTP-слой; в синк ходят только через `enqueue` | — |
| `templates/`, `static/` | представление | — |

## Схема данных (`app/models.py`)

- `users(login UNIQUE, password_hash bcrypt, role admin|operator, is_active)`.
- `tickets(id, number UNIQUE, title, description, planned_at TEXT NULL, status open|closed, comment, closed_at, closed_by, created_by, created_at, updated_at, source local|usedesk, usedesk_ticket_id UNIQUE, usedesk_activity, sync_state)`.
- `settings(key UNIQUE, value)` — двойная роль: конфиг (тумблеры `usedesk_enabled`/`telegram_enabled`/`templates_enabled`/`summary_enabled`/`datefilter_enabled`/`history_grouped`, `default_theme`, `card_shape`/`button_shape`, `personal_appearance`, `language_default`, личный вид `user_pref:{user_id}:theme|card_shape|button_shape|lang`, `sync_last_poll`/`sync_last_error` (пишет поллер), маппинг `usedesk_status_closed`/`usedesk_status_open`, `usedesk_closed_statuses`, `usedesk_watermark`, `sync_error_count`) и очередь `sync_pending:{ticket_id}:{event}`.
- `templates(name, title_tpl, description_tpl, created_by NULL, is_shared)` — личные и общие шаблоны.

## Швы (сигнатуры, менять только вместе с этим файлом)

```python
# app/tickets.py
create_ticket(title, description, planned_at, actor) -> Ticket
edit_ticket(ticket_id, fields, actor) -> Ticket
close_ticket(ticket_id, comment, actor) -> Ticket
delete_ticket(ticket_id, actor) -> None            # admin и operator (владелец 13.10)
list_tickets(status, query, page) -> Page          # Page{items, total, page, pages}, PAGE_SIZE=30
summary() -> dict                                  # {open, closed_today, closed_week}
planned_urgency(planned_at, now_msk) -> str | None # overdue | soon (≤24 ч) | later | None
planned_view(planned_at, now_msk) -> dict          # {urgency, label «20.10 · 02:00», relative}
group_open_tickets(tickets, now_msk) -> list[dict] # группы Просрочено → 24 ч → Позже → Без срока
open_overview(now_msk=None) -> dict                # {open, overdue, next_due} — шапка и главная

# app/importer.py
run_import(dump_path) -> ImportReport              # {added, skipped}

# app/usedesk.py / app/telegram.py — base_url и transport обязательны (тест-шов)
UsedeskClient(token, base_url).create_ticket(subject, message) -> int
UsedeskClient.add_comment + set_status(usedesk_id, text, status) -> None
UsedeskClient.list_updated(since: datetime) -> list[dict]
TelegramClient(token, base_url).send_message(chat_id, text) -> None

# app/sync.py — единственный писатель sync_state и watermark
enqueue(ticket_id, events: set[str]) -> None       # "usedesk", "telegram_new", "telegram_closed"
poll_once() -> SyncStats
set_clients(usedesk=..., telegram=...) / reset_clients()   # подмена моками в тестах
```

## Инварианты (нарушать нельзя)

1. `number` = max+1 читается и вставляется в одной транзакции, `UNIQUE(number)`; проигравший гонку повторяет транзакцию после `IntegrityError`.
2. Импорт никогда не перезаписывает существующий `id` (insert-missing).
3. Заявка `source=usedesk` никогда не порождает исходящие UseDesk-события (анти-цикл).
4. Синхронизируются только создание и статус; конфликт — LWW по `updated_at`.
5. Пустой токен в `.env` = интеграция выключена, что бы ни стоял тумблер.
6. Поллер — единственный писатель `sync_state`/watermark; роуты только `enqueue`.
7. Пароли — bcrypt; пароли и токены не попадают в логи, шаблоны и тексты исключений.
8. Хранение: `closed_at`/`created_at`/`updated_at` — naive-UTC; `planned_at` — строка как есть (МСК); показ пользователю — МСК.

## Конвенции

- Формы — только `await parse_form(request)` из `app/auth`: python-multipart в стеке нет.
- Время: писать только `app.clock.utcnow_naive` («сейчас» по МСК для срочности — `app.tickets.msk_now`); watermark — строка `"%Y-%m-%d %H:%M:%S"`; показ — `app.tickets.format_msk`; границы «сегодня» для счётчиков считаются по МСК (UTC+3).
- Внешний HTTP — только клиенты с `base_url` (+`transport`); тесты — `httpx.MockTransport` через `conftest.usedesk_mock`/`telegram_mock` и `sync.set_clients`, живой сети в тестах нет (ADR-0003); тест-кнопки админки собирают клиентов через `_usedesk_probe_client`/`_telegram_probe_client` в `app/routes/admin.py`.
- Поиск: каждое слово запроса обязательно и ищется по основе (`app.tickets.search_stem` срезает русское окончание, если остаётся ≥ 4 букв: «Ивана» → «иван») в заголовке, описании и комментарии; число — ещё и номер целиком.
- Поиск кириллицы: SQL UDF `ulower` (Python `str.lower`) регистрируется на каждое соединение event-listener'ом в `app/tickets.py` — встроенный `lower()` SQLite сворачивает только ASCII.
- Служебные записи синка — через `sync._write_sync_fields`: пинит `updated_at`, чтобы часы LWW не убегали от `onupdate`.
- UseDesk: все методы POST, `api_token` в теле каждого запроса; HTTP всегда 200, успех и ошибки различаются по телу (коды 111–121; 121 = превышение лимита → `UsedeskRateLimited`, повтор следующим циклом); клиент троттлит `min_interval` (1 с); у поиска в `/tickets` опубликован лимит 10 req/min.
- Роли упорядочены: admin проходит везде, где требуется operator; `/admin` и `/users` — только admin, остальным 403.
- Сессии: itsdangerous cookie (`salt tikety.session.v1`, 7 дней, HttpOnly, SameSite=Lax); активность пользователя перепроверяется по БД на каждом запросе.
- Тесты: фикстуры в `tests/conftest.py` (`db` — чистая tmp-SQLite через `make_engine`+`set_engine`, `client` — TestClient, `ui` — полный env с `TEST-*`); секреты в тестах только `TEST-*`.
- Гейт перед коммитом: `make check` зелёный (ruff line-length 100, py312; pytest).

## ADR (`docs/adr/`)

- `0001` — SQLite и один процесс: без внешней БД, без Alembic (отсрочка осознанная).
- `0002` — поллинг вместо вебхуков.
- `0003` — клиенты с `base_url`, тесты на `MockTransport`.
- `0004` — очередь синка at-most-once: снятие события до отправки, возврат при отказе.
- `0005` — LWW по `updated_at` для конфликтов статусов.
- `0006` — импорт insert-missing по id.
- `0007` — новые колонки дописываются при старте (`ADD COLUMN` с DEFAULT), без Alembic.
