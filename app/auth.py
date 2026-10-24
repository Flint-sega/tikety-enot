"""Аутентификация и сессии: bcrypt, подписанная cookie, require(role), бутстрап админа.

Контракт шва:
- hash_password / verify_password — bcrypt, пароль никогда не попадает в логи;
- issue_session_token(user) / current_user(request, session) — сессия в подписанной
  cookie PANEL_SECRET_KEY (itsdangerous, HttpOnly, SameSite=Lax, 7 дней);
  активность пользователя перепроверяется по БД на каждом запросе; токен несёт
  отпечаток хеша пароля — смена пароля отзывает все живые сессии пользователя;
- parse_form(request) — разбор urlencoded-форм без python-multipart (stdlib)
  с проверкой CSRF внутри: токен из скрытого поля или заголовка X-CSRF-Token
  сверяется с cookie CSRF_COOKIE, несовпадение — CsrfError (403);
- PanelTemplates — рендер с автоподстановкой csrf_token в контекст страницы;
- троттлинг входа: login_is_blocked/record_login_failure/reset_login_failures
  по паре (логин, request.client.host) в памяти процесса, X-Forwarded-For не
  читается; client_ip(request) — единственный источник IP;
- SecurityHeadersMiddleware — X-Frame-Options/X-Content-Type-Options/
  Referrer-Policy/CSP на каждом ответе;
- templates — общий Jinja2Templates для модулей auth (без цикла импорта с main).

Времена в БД — naive-UTC; секреты — только имена из .env.
"""

import logging
import secrets
from hashlib import sha256
from pathlib import Path
from time import monotonic
from urllib.parse import parse_qs, quote

import bcrypt
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer, URLSafeTimedSerializer
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.config import Settings, get_settings
from app.db import get_engine, get_session
from app.models import User

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent


def _cookie_secure() -> bool:
    return get_settings().cookie_secure


class PanelTemplates(Jinja2Templates):
    """Шаблоны с CSRF-контекстом: токен подставляется в каждую страницу автоматически.

    Токен хранится в подписанной cookie (CSRF_COOKIE); при первом рендере cookie
    ставится на ответ. Скрытые поля форм и мета-тег читают {{ csrf_token }}.
    Туда же подставляется внешний вид страницы page_theme/page_cards/page_buttons:
    личный вид пользователя из контекста (user), если админ это разрешил, иначе
    общий из админки; personal_allowed — показывать ли переключатель темы.
    theme_default/card_shape/button_shape — общие значения админа (селекторы
    в админке); страница, передавшая своё значение, важнее (setdefault).
    """

    def TemplateResponse(self, request: Request, name: str, context=None, *args, **kwargs):
        from app.sync import (  # локально: цикл импортов
            appearance_for,
            global_appearance,
            personal_appearance_allowed,
        )

        token, fresh_cookie = ensure_csrf_token(request)
        merged = dict(context or {})
        merged.setdefault("csrf_token", token)
        common = global_appearance()
        merged.setdefault("theme_default", common["theme"])
        merged.setdefault("card_shape", common["card_shape"])
        merged.setdefault("button_shape", common["button_shape"])
        page = appearance_for(merged.get("user"))
        merged.setdefault("page_theme", page["theme"])
        merged.setdefault("page_cards", page["card_shape"])
        merged.setdefault("page_buttons", page["button_shape"])
        merged.setdefault(
            "personal_allowed", bool(merged.get("user")) and personal_appearance_allowed()
        )
        response = super().TemplateResponse(request, name, merged, *args, **kwargs)
        if fresh_cookie is not None:
            response.set_cookie(
                CSRF_COOKIE,
                fresh_cookie,
                max_age=SESSION_MAX_AGE,
                httponly=True,
                samesite="lax",
                secure=_cookie_secure(),
                path="/",
            )
        return response


templates = PanelTemplates(directory=str(BASE_DIR / "templates"))

SESSION_COOKIE = "session"
SESSION_MAX_AGE = 7 * 24 * 3600
SESSION_SALT = "tikety.session.v1"
BCRYPT_ROUNDS = 12
PASSWORD_MAX_BYTES = 72
PASSWORD_TOO_LONG_MESSAGE = f"Пароль не может быть длиннее {PASSWORD_MAX_BYTES} байт"
ROLE_LEVEL = {"operator": 1, "admin": 2}

CSRF_COOKIE = "csrf_token"
CSRF_FIELD = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"
CSRF_SALT = "tikety.csrf.v1"
CSRF_ERROR_MESSAGE = "Сессия формы истекла: обновите страницу и попробуйте снова"

LOGIN_THROTTLE_LIMIT = 5
LOGIN_THROTTLE_PAUSE_SECONDS = 60
THROTTLE_MESSAGE = "Слишком много попыток входа. Подождите минуту и попробуйте снова."

SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
        "img-src 'self' data:"
    ),
}

DUMMY_PASSWORD_HASH = bcrypt.hashpw(
    b"tikety-timing-equalizer", bcrypt.gensalt(rounds=BCRYPT_ROUNDS)
).decode()


class LoginRequired(Exception):
    """Аноним на защищённом маршруте: 303 на /login."""


class AccessDenied(Exception):
    """Роль ниже требуемой: 403 и экран «Нет доступа»."""

    def __init__(self, minimum_role: str) -> None:
        self.minimum_role = minimum_role


def password_length_error(password: str) -> str | None:
    """Русская ошибка, если пароль длиннее 72 байт (предел bcrypt); иначе None.

    Байты, не символы: кириллица весит 2 байта на букву. Без пароля в сообщении.
    """

    if len(password.encode("utf-8")) > PASSWORD_MAX_BYTES:
        return PASSWORD_TOO_LONG_MESSAGE
    return None


PASSWORD_MIN_LENGTH = 8


def password_rule_error(password: str) -> str | None:
    """Правило нового пароля (профиль, создание и смена админом): 8+ символов, ≤ 72 байт.

    Первый админ из .env (bootstrap_admin) этим правилом не проверяется.
    """
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"Пароль короче {PASSWORD_MIN_LENGTH} символов"
    return password_length_error(password)


def set_session_cookie(response: Response, user: User) -> None:
    """Выдать cookie сессии под текущий хеш пароля (вход и смена своего пароля)."""
    response.set_cookie(
        SESSION_COOKIE,
        issue_session_token(user),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(),
        path="/",
    )


def hash_password(password: str) -> str:
    error = password_length_error(password)
    if error is not None:
        raise ValueError(error)
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def client_ip(request: Request) -> str:
    """IP из request.client; X-Forwarded-For не доверяем (панель за localhost-nginx)."""
    return request.client.host if request.client else "unknown"


_login_failures: dict[tuple[str, str], tuple[int, float, float]] = {}

LOGIN_THROTTLE_WINDOW_SECONDS = 300


def login_is_blocked(key: tuple[str, str]) -> bool:
    entry = _login_failures.get(key)
    return entry is not None and entry[1] > monotonic()


def record_login_failure(key: tuple[str, str]) -> None:
    now = monotonic()
    window = LOGIN_THROTTLE_WINDOW_SECONDS
    stale = [
        k
        for k, v in _login_failures.items()
        if (v[1] and v[1] <= now) or (now - v[2] >= window)
    ]
    for old in stale:
        del _login_failures[old]
    count, blocked_until, first_fail = _login_failures.get(key, (0, 0.0, now))
    if blocked_until and blocked_until <= now:
        count = 0
    count += 1
    if count >= LOGIN_THROTTLE_LIMIT:
        _login_failures[key] = (count, now + LOGIN_THROTTLE_PAUSE_SECONDS, first_fail)
    else:
        _login_failures[key] = (count, 0.0, first_fail)


def reset_login_failures(key: tuple[str, str]) -> None:
    _login_failures.pop(key, None)


def reset_login_throttle() -> None:
    _login_failures.clear()


async def parse_form(request: Request) -> dict[str, str]:
    """Разбор urlencoded-форм с проверкой CSRF: каждый POST-роут получает её автоматически.

    Токен берётся из скрытого поля формы либо из заголовка X-CSRF-Token (HTMX)
    и сверяется с подписанной cookie; отсутствие или несовпадение — CsrfError (403).
    Тело не в UTF-8 — MalformedBodyError (400), а не падение сервера.
    """

    raw = await request.body()
    try:
        body = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise MalformedBodyError from error
    form = {key: values[0] for key, values in parse_qs(body, keep_blank_values=True).items()}
    require_csrf(request, form)
    return form


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().panel_secret_key, salt=SESSION_SALT)


def _csrf_serializer() -> URLSafeSerializer:
    return URLSafeSerializer(get_settings().panel_secret_key, salt=CSRF_SALT)


def issue_csrf_token() -> str:
    return _csrf_serializer().dumps(secrets.token_urlsafe(32))


def valid_csrf_cookie(value: str) -> bool:
    try:
        _csrf_serializer().loads(value)
    except BadSignature:
        return False
    return True


def valid_csrf_pair(cookie: str, supplied: str) -> bool:
    if not cookie or not supplied:
        return False
    return valid_csrf_cookie(cookie) and secrets.compare_digest(cookie, supplied)


def ensure_csrf_token(request: Request) -> tuple[str, str | None]:
    """Токен для форм из cookie; второй элемент — новая cookie, если её не было."""
    cookie = request.cookies.get(CSRF_COOKIE, "")
    if valid_csrf_cookie(cookie):
        return cookie, None
    fresh = issue_csrf_token()
    return fresh, fresh


class CsrfError(Exception):
    """POST без CSRF-токена или с неверным: 403."""


class MalformedBodyError(Exception):
    """Тело запроса не является корректным UTF-8/urlencoded: 400."""


def require_csrf(request: Request, form: dict[str, str]) -> None:
    """Шов CSRF: вызывается из parse_form, чтобы каждый POST-роут проверялся автоматически."""
    cookie = request.cookies.get(CSRF_COOKIE, "")
    supplied = form.get(CSRF_FIELD, "") or request.headers.get(CSRF_HEADER, "")
    if not valid_csrf_pair(cookie, supplied):
        raise CsrfError


STATIC_CACHE_VERSIONED = "public, max-age=31536000, immutable"
STATIC_CACHE_PLAIN = "public, max-age=86400"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """X-Frame-Options, X-Content-Type-Options, Referrer-Policy и CSP на каждом ответе.

    HTML-страницы дополнительно получают Cache-Control: no-store — контент
    персональный, кешировать нечего. Статика кешируется браузером:
    с ?v=… в адресе (версия в base.html меняется при правке файла) — на год,
    immutable; без версии — на сутки. Экспорты не трогаются.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        content_type = response.headers.get("content-type", "")
        if content_type.startswith("text/html"):
            response.headers.setdefault("Cache-Control", "no-store")
        elif request.url.path.startswith("/static/") and response.status_code in (200, 304):
            versioned = "v=" in request.url.query
            response.headers["Cache-Control"] = (
                STATIC_CACHE_VERSIONED if versioned else STATIC_CACHE_PLAIN
            )
        return response


def _password_fingerprint(password_hash: str) -> str:
    return sha256(password_hash.encode("utf-8")).hexdigest()[:8]


def issue_session_token(user: User) -> str:
    return _serializer().dumps(
        {"user_id": user.id, "pw": _password_fingerprint(user.password_hash)}
    )


def current_user(request: Request, session: Session) -> User | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None
    user = session.get(User, int(data["user_id"]))
    if user is None or not user.is_active:
        return None
    if data.get("pw") != _password_fingerprint(user.password_hash):
        return None
    return user


def current_user_dependency(
    request: Request, session: Session = Depends(get_session)
) -> User | None:
    return current_user(request, session)


def needs_setup(session: Session) -> bool:
    """Панель не инициализирована: таблица пользователей пуста и админ-env не задан."""
    count = session.scalar(select(func.count()).select_from(User))
    if count:
        return False
    settings = get_settings()
    return not (settings.admin_login and settings.admin_password)


def bootstrap_admin(session: Session, admin_login: str, admin_password: str) -> bool:
    """Первый администратор ровно один раз: только пока таблица пользователей пуста.

    Пароль длиннее 72 байт bcrypt не принимает: бутстрап не выполняется, в лог
    пишется понятная ошибка (без значения пароля), панель продолжает стартовать.
    """

    if not admin_login or not admin_password:
        return False
    count = session.scalar(select(func.count()).select_from(User))
    if count:
        return False
    if password_length_error(admin_password) is not None:
        logger.error(
            "ADMIN_PASSWORD длиннее %d байт — bcrypt такой пароль не принимает, "
            "администратор не создан. Впишите в .env пароль короче и перезапустите панель.",
            PASSWORD_MAX_BYTES,
        )
        return False
    session.add(
        User(login=admin_login, password_hash=hash_password(admin_password), role="admin")
    )
    session.commit()
    return True


def bootstrap_admin_from_settings() -> bool:
    """Точка входа для lifespan: бутстрап из ADMIN_LOGIN/ADMIN_PASSWORD, пароль не логируется."""
    settings: Settings = get_settings()
    with Session(get_engine()) as session:
        created = bootstrap_admin(session, settings.admin_login, settings.admin_password)
    if created:
        logger.info("Создан первый администратор: %s", settings.admin_login)
    return created


def render_setup_screen(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "auth/setup.html")


def render_denied_screen(request: Request, user: object = None) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "auth/denied.html", {"user": user}, status_code=403
    )


def require(minimum_role: str) -> Depends:
    """Зависимость для роутов: аноним — 303 /login, роль ниже — 403 «Нет доступа».

    Роли упорядочены: admin проходит и там, где требуется operator.
    Использование: @router.get("/path", dependencies=[require("admin")]).
    """

    def guard(request: Request, session: Session = Depends(get_session)) -> None:
        user = current_user(request, session)
        if user is None:
            raise LoginRequired
        if ROLE_LEVEL.get(user.role, 0) < ROLE_LEVEL[minimum_role]:
            denied = AccessDenied(minimum_role)
            denied.user = user  # навигация «Нет доступа» знает, кому показать путь назад
            raise denied

    return Depends(guard)


async def _login_required_handler(request: Request, __: LoginRequired) -> Response:
    target = quote(request.url.path, safe="/")
    if request.url.query:
        target = f"{target}?{quote(request.url.query, safe='')}"
    return RedirectResponse(f"/login?next={target}", status_code=303)


def safe_next_path(raw: str) -> str:
    """Параметр next после входа: только локальные пути, иначе «/».

    Защита от open-redirect: принимаем относительный путь с одним ведущим
    слэшем (не «//», не «/\\» — протокол-относительные схемы запрещены).
    """

    if raw.startswith("/") and not raw.startswith(("//", "/\\")) and len(raw) < 512:
        return raw
    return "/"


async def _access_denied_handler(request: Request, exc: AccessDenied) -> Response:
    return render_denied_screen(request, getattr(exc, "user", None))


async def _csrf_error_handler(request: Request, __: CsrfError) -> Response:
    return HTMLResponse(CSRF_ERROR_MESSAGE, status_code=403)


async def _malformed_body_handler(_: Request, __: MalformedBodyError) -> Response:
    return HTMLResponse("Некорректное тело запроса", status_code=400)


async def _overflow_handler(_: Request, __: OverflowError) -> Response:
    # Числовой мусор вне диапазона SQLite INTEGER в path/?q — это «не найдено»,
    # а не падение процесса (ревью SEC-101).
    return HTMLResponse("Страница не найдена", status_code=404)


def register_auth(app: FastAPI) -> None:
    """Подключить обработчики guard-исключений и CSRF (вызывается из main)."""
    app.add_exception_handler(LoginRequired, _login_required_handler)
    app.add_exception_handler(AccessDenied, _access_denied_handler)
    app.add_exception_handler(CsrfError, _csrf_error_handler)
    app.add_exception_handler(MalformedBodyError, _malformed_body_handler)
    # Числовый мусор вне диапазона SQLite INTEGER (path id, ?q) — 404, не 500.
    app.add_exception_handler(OverflowError, _overflow_handler)


class SetupRequiredMiddleware(BaseHTTPMiddleware):
    """Пока панель не инициализирована, любая страница показывает экран настройки."""

    SKIP_PREFIXES = ("/static",)
    SKIP_PATHS = ("/healthz",)

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        path = request.url.path
        if path.startswith(self.SKIP_PREFIXES) or path in self.SKIP_PATHS:
            return await call_next(request)
        with Session(get_engine()) as session:
            if needs_setup(session):
                return render_setup_screen(request)
        return await call_next(request)
