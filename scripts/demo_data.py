"""Демо-данные: выдуманные заявки, пользователи и шаблоны для показа и скриншотов.

Запуск на пустой базе (реальные данные не нужны и не трогаются):

    DB_PATH=data/demo.sqlite uv run python scripts/demo_data.py --lang en

Создаёт админа demo-admin и оператора demo-operator с паролем из
--password (по умолчанию случайный, печатается один раз), ~20 заявок со сроками
относительно текущего времени (просроченные, ближайшие, позже, без срока),
историю закрытых за последние дни и три общих шаблона. Повторный запуск
на непустой базе отказывается работать — чтобы не смешать демо с настоящим.
"""

from __future__ import annotations

import argparse
import secrets
import sys
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.auth import hash_password  # noqa: E402
from app.clock import utcnow_naive  # noqa: E402
from app.db import get_engine, init_db  # noqa: E402
from app.i18n import SETTING_DEFAULT_LANGUAGE  # noqa: E402
from app.models import Template, Ticket, User  # noqa: E402
from app.sync import ensure_default_settings, set_setting  # noqa: E402

# (заголовок, описание, срок в часах от «сейчас» по МСК или None)
OPEN = {
    "ru": [
        ("Обновить базу 1038", "на крайний релиз, сначала бэкап", -5),
        ("Проверить доступы 2210", "клиент прислал новые доступы — проверить вход", -1),
        ("Перенести базу 1502 на новый сервер", "окно согласовано с клиентом", 2),
        ("Обновить конфигурацию 3307", "релиз 3.0.12, после обновления проверить обмен", 6),
        ("Настроить резервное копирование 1120", "ежедневно в 03:00, хранить 14 копий", 20),
        ("Релиз 4.2 в хранилище", "положить релиз в хранилище и разослать", 30),
        ("Обновить базу 2045", "ночью, пользователи предупреждены", 54),
        ("Сменить пароль администратора 1777", "по заявке клиента", None),
        ("Разобраться с медленным отчётом 2901", "отчёт по остаткам строится 5 минут", None),
    ],
    "en": [
        ("Update database 1038", "to the latest release, back up first", -5),
        ("Check access for 2210", "the client sent new credentials — check sign-in", -1),
        ("Move database 1502 to the new server", "maintenance window agreed with the client", 2),
        ("Update configuration 3307", "release 3.0.12, check data exchange afterwards", 6),
        ("Set up backups for 1120", "daily at 03:00, keep 14 copies", 20),
        ("Put release 4.2 into storage", "upload the release and notify the team", 30),
        ("Update database 2045", "at night, users have been warned", 54),
        ("Reset the admin password for 1777", "requested by the client", None),
        ("Look into the slow report in 2901", "the stock report takes 5 minutes to build", None),
    ],
}
# (заголовок, комментарий при закрытии, сколько часов назад закрыта)
CLOSED = {
    "ru": [
        ("Обновить базу 1101", "Готово", 1),
        ("Проверить доступы 1840", "Готово", 3),
        ("Обновить базу 2333", "Перенесено", 6),
        ("Релиз 4.1 в хранилище", "Готово", 26),
        ("Перезапустить службу обмена 1650", "", 30),
        ("Обновить базу 1990", "Готово", 50),
        ("Настроить права пользователя 2780", "Готово", 75),
    ],
    "en": [
        ("Update database 1101", "Done", 1),
        ("Check access for 1840", "Done", 3),
        ("Update database 2333", "Postponed", 6),
        ("Put release 4.1 into storage", "Done", 26),
        ("Restart the exchange service for 1650", "", 30),
        ("Update database 1990", "Done", 50),
        ("Set up user rights in 2780", "Done", 75),
    ],
}
TEMPLATES = {
    "ru": [
        ("Обновление базы", "Обновить {база}", "на крайний релиз, сначала бэкап"),
        ("Релиз в хранилище", "Релиз {база} в хранилище", "положить в хранилище и обновить"),
        ("Проверка доступов", "Проверить доступы {база}", "проверить вход и записать в карточку"),
    ],
    "en": [
        ("Database update", "Update {database}", "to the latest release, back up first"),
        ("Release to storage", "Release {database} to storage", "upload and update"),
        ("Access check", "Check access for {database}", "check sign-in and note it in the card"),
    ],
}
MSK = timedelta(hours=3)


def _planned(hours: float | None) -> str | None:
    if hours is None:
        return None
    moment = utcnow_naive() + MSK + timedelta(hours=hours)
    return moment.strftime("%Y-%m-%d %H:00")


def fill(lang: str, password: str) -> None:
    init_db()
    ensure_default_settings()
    with Session(get_engine()) as session:
        if session.scalar(select(func.count()).select_from(Ticket)):
            raise SystemExit("В базе уже есть заявки — демо-данные кладутся только в пустую базу.")
        admin = User(login="demo-admin", password_hash=hash_password(password), role="admin")
        operator = User(
            login="demo-operator", password_hash=hash_password(password), role="operator"
        )
        session.add_all([admin, operator])
        session.flush()
        now = utcnow_naive()
        number = 100
        # Старые закрытые получают меньшие номера: история идёт от свежих к старым.
        for title, comment, hours_ago in reversed(CLOSED[lang]):
            number += 1
            closed_at = now - timedelta(hours=hours_ago)
            session.add(
                Ticket(
                    number=number,
                    title=title,
                    description="",
                    status="closed",
                    comment=comment or None,
                    created_at=closed_at - timedelta(hours=4),
                    closed_at=closed_at,
                    created_by=operator.id,
                    closed_by=operator.id if number % 2 else admin.id,
                )
            )
        for title, description, hours in OPEN[lang]:
            number += 1
            session.add(
                Ticket(
                    number=number,
                    title=title,
                    description=description,
                    planned_at=_planned(hours),
                    status="open",
                    created_by=admin.id if number % 3 == 0 else operator.id,
                )
            )
        for name, title_tpl, description_tpl in TEMPLATES[lang]:
            session.add(
                Template(
                    name=name, title_tpl=title_tpl, description_tpl=description_tpl, is_shared=True
                )
            )
        session.commit()
    set_setting(SETTING_DEFAULT_LANGUAGE, lang)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lang", choices=("ru", "en"), default="ru")
    parser.add_argument("--password", default="", help="пароль demo-admin и demo-operator")
    args = parser.parse_args()
    password = args.password or secrets.token_urlsafe(9)
    fill(args.lang, password)
    print("Готово: demo-admin / demo-operator")
    if not args.password:
        print(f"Пароль (показывается один раз): {password}")


if __name__ == "__main__":
    main()
