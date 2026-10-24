"""«Мой профиль» — у каждого вошедшего (оператор и админ): смена своего пароля
и личный внешний вид (тема, форма плашек и кнопок).

Пароль: нужен текущий; новый — по правилу password_rule_error (8+ символов,
≤ 72 байт), совпадает с повтором и отличается от текущего. Неверный текущий
пароль считается неудачной попыткой входа (тот же троттлинг, ключ (логин, ip)).
После смены cookie сессии переиздаётся под новый хеш — сам пользователь остаётся
в системе, а его другие сессии отваливаются (отпечаток пароля в токене).

Внешний вид: строки Setting user_pref:{id}:* (app.sync). Если админ запретил
личный вид (personal_appearance = «0»), POST отвечает 403 с русским текстом.
Тот же POST принимает fetch от переключателя темы в шапке (только поле theme).
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import (
    THROTTLE_MESSAGE,
    client_ip,
    current_user_dependency,
    hash_password,
    login_is_blocked,
    parse_form,
    password_rule_error,
    record_login_failure,
    require,
    reset_login_failures,
    set_session_cookie,
    templates,
    verify_password,
)
from app.db import get_session
from app.models import User
from app.sync import (
    personal_appearance_allowed,
    reset_user_appearance,
    set_user_appearance,
)

router = APIRouter()

APPEARANCE_FORBIDDEN = "Внешний вид задаёт администратор: личные настройки выключены"
DONE_MESSAGES = {
    "password": "Пароль изменён",
    "appearance": "Внешний вид сохранён",
    "reset": "Вернули общий вид — как у всех",
}


def _profile_page(
    request: Request,
    user: User,
    *,
    password_error: str | None = None,
    appearance_error: str | None = None,
    done: str = "",
    status_code: int = 200,
):
    return templates.TemplateResponse(
        request,
        "profile.html",
        {
            "user": user,
            "tab": "profile",
            "password_error": password_error,
            "appearance_error": appearance_error,
            "done_message": DONE_MESSAGES.get(done),
        },
        status_code=status_code,
    )


@router.get("/profile", dependencies=[require("operator")])
def profile_page(
    request: Request,
    done: str = "",
    user: User | None = Depends(current_user_dependency),
):
    return _profile_page(request, user, done=done)


def _password_change_error(user: User, form: dict[str, str]) -> str | None:
    current = form.get("current_password", "")
    new = form.get("new_password", "")
    repeat = form.get("new_password_repeat", "")
    if not current or not new:
        return "Укажите текущий и новый пароль"
    if not verify_password(current, user.password_hash):
        return "Текущий пароль неверный"
    rule = password_rule_error(new)
    if rule is not None:
        return rule
    if new != repeat:
        return "Новый пароль и повтор не совпадают"
    if new == current:
        return "Новый пароль совпадает с текущим"
    return None


@router.post("/profile/password", dependencies=[require("operator")])
async def change_own_password(
    request: Request,
    session: Session = Depends(get_session),
    user: User | None = Depends(current_user_dependency),
):
    form = await parse_form(request)
    throttle_key = (user.login, client_ip(request))
    if login_is_blocked(throttle_key):
        return _profile_page(request, user, password_error=THROTTLE_MESSAGE, status_code=429)
    error = _password_change_error(user, form)
    if error is not None:
        if error == "Текущий пароль неверный":
            record_login_failure(throttle_key)
        return _profile_page(request, user, password_error=error)
    reset_login_failures(throttle_key)
    account = session.get(User, user.id)
    account.password_hash = hash_password(form["new_password"])
    session.commit()
    session.refresh(account)
    response = RedirectResponse("/profile?done=password", status_code=303)
    set_session_cookie(response, account)
    return response


@router.post("/profile/appearance", dependencies=[require("operator")])
async def save_own_appearance(
    request: Request,
    user: User | None = Depends(current_user_dependency),
):
    form = await parse_form(request)
    from_fetch = request.headers.get("X-Requested-With") == "fetch"
    if not personal_appearance_allowed():
        if from_fetch:
            return HTMLResponse(APPEARANCE_FORBIDDEN, status_code=403)
        return _profile_page(
            request, user, appearance_error=APPEARANCE_FORBIDDEN, status_code=403
        )
    if form.get("reset") == "1":
        reset_user_appearance(user.id)
        return RedirectResponse("/profile?done=reset", status_code=303)
    error = set_user_appearance(user.id, form)
    if from_fetch:
        return HTMLResponse(error or "ok", status_code=400 if error else 200)
    if error is not None:
        return _profile_page(request, user, appearance_error=error)
    return RedirectResponse("/profile?done=appearance", status_code=303)
