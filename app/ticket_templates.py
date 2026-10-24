"""Шаблоны заявок: общие и личные (волна 4).

Шаблон видят: общий (is_shared) — все; личный — только автор (created_by).
Править и удалять может автор; админ — ещё и любой общий шаблон (в том числе
старые, у которых автора нет). Оператор сам решает, делиться ли своим шаблоном
(«Показывать всем»). Название до 128 символов (колонка String(128)).

Подстановки {…} в масках — договорённость интерфейса: форма заявки выделяет
первую из них после клика по шаблону (static/app.js), сервер их не разбирает.
"""

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db import get_engine
from app.i18n import _
from app.models import Template, User

NAME_MAX_LENGTH = 128
TEXT_MAX_LENGTH = 32768


def template_fields(form: dict[str, str]) -> dict[str, str]:
    return {
        "name": form.get("name", "").strip(),
        "title_tpl": form.get("title_tpl", "").strip(),
        "description_tpl": form.get("description_tpl", "").strip(),
    }


def validate_template(fields: dict[str, str]) -> str | None:
    """Русская ошибка для формы шаблона; None — всё заполнено и влезает."""
    if not fields["name"]:
        return _("Укажите название шаблона")
    if not fields["title_tpl"]:
        return _("Укажите маску заголовка")
    if not fields["description_tpl"]:
        return _("Укажите маску описания")
    if len(fields["name"]) > NAME_MAX_LENGTH:
        return _("Название длиннее {n} символов", n=NAME_MAX_LENGTH)
    if max(len(fields["title_tpl"]), len(fields["description_tpl"])) > TEXT_MAX_LENGTH:
        return _("Маска длиннее {n} символов", n=TEXT_MAX_LENGTH)
    return None


def visible_templates(user: User) -> list[Template]:
    """Шаблоны для формы заявки и страницы «Шаблоны»: свои сначала, потом общие."""
    with Session(get_engine()) as session:
        rows = session.scalars(
            select(Template)
            .where(or_(Template.is_shared.is_(True), Template.created_by == user.id))
            .order_by(Template.name, Template.id)
        ).all()
        for row in rows:
            session.expunge(row)
    return sorted(rows, key=lambda row: (row.created_by != user.id, row.name.lower(), row.id))


def can_edit(user: User, template: Template) -> bool:
    if template.created_by == user.id:
        return True
    return user.role == "admin" and template.is_shared


def can_see(user: User, template: Template) -> bool:
    return template.is_shared or template.created_by == user.id


def template_or_none(template_id: int) -> Template | None:
    with Session(get_engine()) as session:
        template = session.get(Template, template_id)
        if template is not None:
            session.expunge(template)
        return template


def create_template(fields: dict[str, str], author: User, shared: bool) -> Template:
    with Session(get_engine(), expire_on_commit=False) as session:
        template = Template(**fields, created_by=author.id, is_shared=shared)
        session.add(template)
        session.commit()
        return template


def update_template(template_id: int, fields: dict[str, str], shared: bool | None) -> None:
    with Session(get_engine()) as session:
        template = session.get(Template, template_id)
        if template is None:
            return
        for name, value in fields.items():
            setattr(template, name, value)
        if shared is not None:
            template.is_shared = shared
        session.commit()


def delete_template(template_id: int) -> None:
    with Session(get_engine()) as session:
        template = session.get(Template, template_id)
        if template is not None:
            session.delete(template)
            session.commit()


def author_logins(templates: list[Template]) -> dict[int, str]:
    ids = {row.created_by for row in templates if row.created_by is not None}
    if not ids:
        return {}
    with Session(get_engine()) as session:
        return dict(session.execute(select(User.id, User.login).where(User.id.in_(ids))).all())
