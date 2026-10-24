"""Страница «Шаблоны» — для всех вошедших (оператор и админ).

Оператор создаёт свои шаблоны: личные (видит только он) или «для всех».
Правит и удаляет — свои; админ — ещё и любые общие (app.ticket_templates).
?from=<id заявки> — «Сохранить как шаблон»: форма предзаполнена заголовком
и описанием заявки (заявка любая, которую видит пользователь — все заявки общие).
Первый созданный шаблон включает кнопки шаблонов на форме заявки (Setting
templates_enabled), иначе сделанный шаблон «не виден» и кажется сломанным.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import current_user_dependency, parse_form, require, templates
from app.i18n import N_, _
from app.models import User
from app.sync import SETTING_TEMPLATES_ENABLED, get_setting, set_setting
from app.ticket_templates import (
    author_logins,
    can_edit,
    can_see,
    create_template,
    delete_template,
    template_fields,
    template_or_none,
    update_template,
    validate_template,
    visible_templates,
)
from app.tickets import ticket_or_none

router = APIRouter()

PREFILL_NAME_LENGTH = 40
DONE_MESSAGES = {
    "created": N_("Шаблон создан — он уже есть на форме новой заявки"),
    "saved": N_("Шаблон сохранён"),
    "deleted": N_("Шаблон удалён"),
}


def render_templates_page(
    request: Request,
    user: User,
    *,
    form: dict | None = None,
    error: str | None = None,
    done: str = "",
    status_code: int = 200,
):
    items = visible_templates(user)
    logins = author_logins(items)
    rows = [
        {
            "template": item,
            "mine": item.created_by == user.id,
            "editable": can_edit(user, item),
            "author": logins.get(item.created_by),
        }
        for item in items
    ]
    return templates.TemplateResponse(
        request,
        "templates/index.html",
        {
            "user": user,
            "tab": "templates",
            "mine": [row for row in rows if row["mine"]],
            "shared": [row for row in rows if not row["mine"]],
            "form": form or {"name": "", "title_tpl": "", "description_tpl": "", "shared": ""},
            "error": error,
            "done_message": _(DONE_MESSAGES[done]) if done in DONE_MESSAGES else None,
            "buttons_on": get_setting(SETTING_TEMPLATES_ENABLED, "1") == "1",
        },
        status_code=status_code,
    )


@router.get("/templates", dependencies=[require("operator")])
def templates_page(
    request: Request,
    done: str = "",
    user: User | None = Depends(current_user_dependency),
):
    form = None
    source_id = request.query_params.get("from", "")
    if source_id.isdecimal() and len(source_id) < 10:
        source = ticket_or_none(int(source_id))
        if source is not None:
            form = {
                "name": source.title[:PREFILL_NAME_LENGTH],
                "title_tpl": source.title,
                "description_tpl": source.description,
                "shared": "",
            }
    return render_templates_page(request, user, form=form, done=done)


def _shared_flag(form: dict[str, str]) -> bool:
    return form.get("shared") == "1"


@router.post("/templates/create", dependencies=[require("operator")])
async def create_template_form(
    request: Request, user: User | None = Depends(current_user_dependency)
):
    form = await parse_form(request)
    fields = template_fields(form)
    error = validate_template(fields)
    if error is not None:
        return render_templates_page(
            request, user, form={**fields, "shared": form.get("shared", "")}, error=error
        )
    create_template(fields, user, _shared_flag(form))
    set_setting(SETTING_TEMPLATES_ENABLED, "1")
    return RedirectResponse("/templates?done=created", status_code=303)


def _editable_or_error(user: User, template_id: int):
    template = template_or_none(template_id)
    if template is None or not can_see(user, template):
        return None, HTMLResponse(_("Шаблон не найден"), status_code=404)
    if not can_edit(user, template):
        return None, HTMLResponse(_("Этот шаблон может править только автор"), status_code=403)
    return template, None


@router.get("/templates/{template_id}/edit", dependencies=[require("operator")])
def edit_template_page(
    template_id: int, request: Request, user: User | None = Depends(current_user_dependency)
):
    template, failure = _editable_or_error(user, template_id)
    if failure is not None:
        return failure
    return templates.TemplateResponse(
        request,
        "templates/edit.html",
        {"user": user, "tab": "templates", "template": template, "error": None},
    )


@router.post("/templates/{template_id}/edit", dependencies=[require("operator")])
async def edit_template_form(
    template_id: int, request: Request, user: User | None = Depends(current_user_dependency)
):
    template, failure = _editable_or_error(user, template_id)
    if failure is not None:
        return failure
    form = await parse_form(request)
    fields = template_fields(form)
    error = validate_template(fields)
    if error is not None:
        for name, value in fields.items():
            setattr(template, name, value)
        return templates.TemplateResponse(
            request,
            "templates/edit.html",
            {"user": user, "tab": "templates", "template": template, "error": error},
        )
    # Делиться может только автор; админ, правящий чужой общий шаблон, флаг не меняет.
    shared = _shared_flag(form) if template.created_by == user.id else None
    update_template(template_id, fields, shared)
    return RedirectResponse("/templates?done=saved", status_code=303)


@router.post("/templates/{template_id}/delete", dependencies=[require("operator")])
async def delete_template_form(
    template_id: int, request: Request, user: User | None = Depends(current_user_dependency)
):
    await parse_form(request)
    _template, failure = _editable_or_error(user, template_id)
    if failure is not None:
        return failure
    delete_template(template_id)
    return RedirectResponse("/templates?done=deleted", status_code=303)
