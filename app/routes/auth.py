"""Вход и выход: GET/POST /login, POST /logout.

Неудачный вход всегда отвечает одним сообщением «Неверный логин или пароль»
(деактивированный и несуществующий пользователь не различаются), введённый
логин сохраняется в форме. Время ответа выровнено: несуществующий логин
проходит фиктивный bcrypt. Пять неудач по паре логин+IP включают паузу 60 с —
неверные попытки в паузе получают 429 «Слишком много попыток…»; верный пароль
в паузе проходит (перебор это не ослабляет, честного пользователя не запирает).
Успех сбрасывает счётчик. Успешный вход ставит подписанную cookie сессии
(HttpOnly, SameSite=Lax), ротирует CSRF-cookie и ведёт на «/».
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import (
    CSRF_COOKIE,
    DUMMY_PASSWORD_HASH,
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    THROTTLE_MESSAGE,
    _cookie_secure,
    client_ip,
    current_user_dependency,
    issue_csrf_token,
    login_is_blocked,
    parse_form,
    record_login_failure,
    reset_login_failures,
    safe_next_path,
    set_session_cookie,
    templates,
    verify_password,
)
from app.db import get_session
from app.models import User

router = APIRouter()

LOGIN_ERROR = "Неверный логин или пароль"


@router.get("/login")
def login_page(
    request: Request,
    next: str = "",
    session: Session = Depends(get_session),
    user: User | None = Depends(current_user_dependency),
):
    if user is not None:
        return RedirectResponse(safe_next_path(next), status_code=303)
    return _login_page(request, next=next)


def _login_page(
    request: Request,
    login: str = "",
    error: str | None = None,
    next: str = "",
    status_code: int = 200,
):
    return templates.TemplateResponse(
        request,
        "auth/login.html",
        {"login": login, "error": error, "next": next},
        status_code=status_code,
    )


@router.post("/login")
async def login_submit(
    request: Request,
    next: str = "",
    session: Session = Depends(get_session),
):
    form = await parse_form(request)
    next = safe_next_path(form.get("next") or next)
    login_value = form.get("login", "").strip()
    password = form.get("password", "")
    throttle_key = (login_value, client_ip(request))
    blocked = login_is_blocked(throttle_key)
    user = None
    if login_value:
        user = session.scalar(select(User).where(User.login == login_value))
    if user is None:
        verify_password(password, DUMMY_PASSWORD_HASH)
        password_ok = False
    else:
        password_ok = verify_password(password, user.password_hash)
    if user is None or not user.is_active or not password_ok:
        if blocked:
            return _login_page(
                request,
                login=login_value,
                error=THROTTLE_MESSAGE,
                next=next,
                status_code=429,
            )
        record_login_failure(throttle_key)
        return _login_page(request, login=login_value, error=LOGIN_ERROR, next=next)
    reset_login_failures(throttle_key)
    response = RedirectResponse(safe_next_path(next), status_code=303)
    set_session_cookie(response, user)
    response.set_cookie(
        CSRF_COOKIE,
        issue_csrf_token(),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(),
        path="/",
    )
    return response


@router.post("/logout")
async def logout(request: Request):
    await parse_form(request)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return response
