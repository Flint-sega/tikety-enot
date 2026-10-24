"""Таблицы панели: users, tickets, settings, templates (SQLAlchemy 2, SQLite).

Контракт шва — схема из спецификации:
- users(login UNIQUE, password_hash, role admin|operator, is_active);
- tickets(id, legacy_id UNIQUE NULL — id заявки в JSON-файле при импорте, number UNIQUE,
  title, description, planned_at TEXT NULL,
  status open|closed, comment NULL, closed_at NULL, closed_by NULL,
  created_by NULL, created_at, updated_at, source local|usedesk,
  usedesk_ticket_id UNIQUE NULL, usedesk_activity NULL, sync_state NULL);
- settings(key UNIQUE, value);
- templates(id, name, title_tpl, description_tpl, created_by NULL, is_shared) —
  created_by = автор (NULL у шаблонов до волны 4), is_shared — виден всем;
  личный шаблон (is_shared = 0) видит и правит только автор. Колонки
  created_by/is_shared добавляются в старые БД при старте (db.init_db, ADR-0007).

Времена: closed_at/created_at/updated_at — UTC; planned_at — строка как есть (МСК).
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.clock import utcnow_naive


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16))
    is_active: Mapped[bool] = mapped_column(default=True)


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(primary_key=True)
    legacy_id: Mapped[int | None] = mapped_column(unique=True, index=True)
    number: Mapped[int] = mapped_column(unique=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    planned_at: Mapped[str | None]
    status: Mapped[str] = mapped_column(String(16), default="open")
    comment: Mapped[str | None] = mapped_column(Text)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)
    closed_by: Mapped[int | None]
    created_by: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow_naive, onupdate=utcnow_naive
    )
    source: Mapped[str] = mapped_column(String(16), default="local")
    usedesk_ticket_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    usedesk_activity: Mapped[datetime | None] = mapped_column(DateTime)
    sync_state: Mapped[str | None] = mapped_column(String(16))


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Template(Base):
    __tablename__ = "templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    title_tpl: Mapped[str] = mapped_column(Text, default="")
    description_tpl: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    is_shared: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
