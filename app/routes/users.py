"""Управление пользователями (только админ): список, создание оператора,
смена пароля, деактивация и возврат, смена роли.

Пароль, который задаёт админ, проверяется тем же правилом, что смена своего
пароля в профиле (password_rule_error: 8+ символов, ≤ 72 байт). Свою роль
админ не меняет, последнего активного админа понизить или деактивировать нельзя.

Все маршруты под require("admin"): оператор получает 403 «Нет доступа».
Деактивированный пользователь остаётся в базе — его имя доступно истории
заявок (created_by/closed_by — ссылки на id).
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import (
    current_user_dependency,
    hash_password,
    parse_form,
    password_rule_error,
    require,
    templates,
)
from app.db import get_session
from app.i18n import _
from app.models import User

router = APIRouter()


@router.get("/users", dependencies=[require("admin")])
def users_page(
    request: Request,
    session: Session = Depends(get_session),
    admin: User | None = Depends(current_user_dependency),
):
    users = session.scalars(select(User).order_by(User.login)).all()
    return templates.TemplateResponse(
        request,
        "users/list.html",
        {
            "users": users,
            "admin": admin,
            "user": admin,
            "tab": "users",
            "error": None,
            "form_login": "",
        },
    )


@router.post("/users/create", dependencies=[require("admin")])
async def create_user(
    request: Request,
    session: Session = Depends(get_session),
    admin: User | None = Depends(current_user_dependency),
):
    form = await parse_form(request)
    login_value = form.get("login", "").strip()
    password = form.get("password", "")
    error = _validate_new_user(session, login_value, password)
    if error is None:
        session.add(
            User(login=login_value, password_hash=hash_password(password), role="operator")
        )
        session.commit()
        return RedirectResponse("/users", status_code=303)
    return _users_page_with_error(
        request, session, error, admin=admin, form_login=login_value
    )


@router.post("/users/{user_id}/password", dependencies=[require("admin")])
async def change_password(
    user_id: int,
    request: Request,
    session: Session = Depends(get_session),
    admin: User | None = Depends(current_user_dependency),
):
    form = await parse_form(request)
    password = form.get("password", "")
    user = session.get(User, user_id)
    if user is None or not password:
        return _users_page_with_error(
            request, session, _("Укажите новый пароль"), admin=admin
        )
    rule = password_rule_error(password)
    if rule is not None:
        return _users_page_with_error(request, session, rule, admin=admin)
    user.password_hash = hash_password(password)
    session.commit()
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/deactivate", dependencies=[require("admin")])
async def deactivate_user(
    user_id: int,
    request: Request,
    session: Session = Depends(get_session),
    admin: User | None = Depends(current_user_dependency),
):
    await parse_form(request)
    user = session.get(User, user_id)
    if user is None:
        return _users_page_with_error(request, session, _("Пользователь не найден"), admin=admin)
    if admin is not None and user.id == admin.id:
        return _users_page_with_error(
            request, session, _("Нельзя деактивировать себя"), admin=admin
        )
    if user.role == "admin" and _active_admins(session) <= 1:
        return _users_page_with_error(
            request, session, _("Нельзя деактивировать последнего админа"), admin=admin
        )
    user.is_active = False
    session.commit()
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/activate", dependencies=[require("admin")])
async def activate_user(
    user_id: int,
    request: Request,
    session: Session = Depends(get_session),
    admin: User | None = Depends(current_user_dependency),
):
    await parse_form(request)
    user = session.get(User, user_id)
    if user is None:
        return _users_page_with_error(request, session, _("Пользователь не найден"), admin=admin)
    user.is_active = True
    session.commit()
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/role", dependencies=[require("admin")])
async def change_role(
    user_id: int,
    request: Request,
    session: Session = Depends(get_session),
    admin: User | None = Depends(current_user_dependency),
):
    form = await parse_form(request)
    role = form.get("role", "")
    user = session.get(User, user_id)
    if user is None:
        return _users_page_with_error(request, session, _("Пользователь не найден"), admin=admin)
    if role not in ("admin", "operator"):
        return _users_page_with_error(request, session, _("Неизвестная роль"), admin=admin)
    if admin is not None and user.id == admin.id:
        return _users_page_with_error(
            request, session, _("Свою роль поменять нельзя"), admin=admin
        )
    if user.role == "admin" and role == "operator" and user.is_active:
        if _active_admins(session) <= 1:
            return _users_page_with_error(
                request, session, _("Нельзя понизить последнего админа"), admin=admin
            )
    user.role = role
    session.commit()
    return RedirectResponse("/users", status_code=303)


def _active_admins(session: Session) -> int:
    return len(
        session.scalars(
            select(User.id).where(User.role == "admin", User.is_active.is_(True))
        ).all()
    )


LOGIN_MAX_LENGTH = 64


def _validate_new_user(session: Session, login_value: str, password: str) -> str | None:
    if not login_value or not password:
        return _("Укажите логин и пароль")
    if len(login_value) > LOGIN_MAX_LENGTH:
        return _("Логин длиннее {n} символов", n=LOGIN_MAX_LENGTH)
    existing = session.scalar(select(User).where(User.login == login_value))
    if existing is not None:
        return _("Логин уже занят")
    return password_rule_error(password)


def _users_page_with_error(
    request: Request,
    session: Session,
    error: str,
    admin: User | None = None,
    form_login: str = "",
):
    users = session.scalars(select(User).order_by(User.login)).all()
    return templates.TemplateResponse(
        request,
        "users/list.html",
        {
            "users": users,
            "admin": admin,
            "user": admin,
            "tab": "users",
            "error": error,
            "form_login": form_login,
        },
    )
