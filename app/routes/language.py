"""Переключатель языка RU/EN: POST /lang — для всех, включая страницу входа.

Ставит cookie panel-lang на год и, если человек вошёл, запоминает выбор в его
профиле (user_pref:{id}:lang): при входе с другого устройства язык вернётся сам.
Возврат — на ту же страницу (next, только локальный путь); мусорный код языка
ничего не меняет.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth import current_user, parse_form, safe_next_path, set_language_cookie
from app.db import get_session
from app.i18n import normalize_lang
from app.sync import set_user_language

router = APIRouter()


@router.post("/lang")
async def switch_language(request: Request, session: Session = Depends(get_session)):
    form = await parse_form(request)
    response = RedirectResponse(safe_next_path(form.get("next", "")), status_code=303)
    lang = normalize_lang(form.get("lang"))
    if lang is None:
        return response
    set_language_cookie(response, lang)
    user = current_user(request, session)
    if user is not None:
        set_user_language(user.id, lang)
    return response
