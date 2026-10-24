"""Каркас: старт приложения, схема БД, конфиг из env, соответствие .env.example."""

import logging
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.config import DEFAULT_DB_PATH, DEFAULT_SEED_PATH, ENV_NAMES, settings_from_env
from app.models import Setting, Template, Ticket, User

ENV_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"

TICKET_COLUMNS = {
    "id",
    "number",
    "title",
    "description",
    "planned_at",
    "status",
    "comment",
    "closed_at",
    "closed_by",
    "created_by",
    "created_at",
    "updated_at",
    "source",
    "usedesk_ticket_id",
    "usedesk_activity",
    "sync_state",
}


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_root_serves_base_template(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Тикеты Енот" in response.text
    assert 'lang="ru"' in response.text
    assert "/static/tailwind.css" in response.text
    assert "htmx" in response.text


def test_schema_has_all_tables(db):
    tables = set(inspect(db).get_table_names())
    assert {"users", "tickets", "settings", "templates"} <= tables


def test_tickets_columns(db):
    columns = {column["name"]: column for column in inspect(db).get_columns("tickets")}
    assert TICKET_COLUMNS <= set(columns)
    assert columns["number"]["nullable"] is False
    assert columns["usedesk_ticket_id"]["nullable"] is True
    assert columns["sync_state"]["nullable"] is True


def test_number_is_unique(session):
    session.add(Ticket(number=1, title="первая"))
    session.add(Ticket(number=1, title="вторая"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_usedesk_ticket_id_unique_but_nullable(session):
    session.add(Ticket(number=1, title="a"))
    session.add(Ticket(number=2, title="b"))
    session.flush()
    session.add(Ticket(number=3, title="c", usedesk_ticket_id="42"))
    session.add(Ticket(number=4, title="d", usedesk_ticket_id="42"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_wal_mode(db):
    with db.connect() as connection:
        mode = connection.execute(text("PRAGMA journal_mode")).scalar()
    assert str(mode).lower() == "wal"


def test_other_tables_accept_rows(session):
    session.add(User(login="TEST-admin", password_hash="TEST-hash", role="admin"))
    session.add(Setting(key="TEST-KEY", value="1"))
    session.add(Template(name="TEST-шаблон", title_tpl="Заголовок", description_tpl="Описание"))
    session.flush()


def test_settings_defaults_without_env():
    settings = settings_from_env({})
    assert settings.admin_login == ""
    assert settings.admin_password == ""
    assert settings.usedesk_api_token == ""
    assert settings.telegram_bot_token == ""
    assert settings.telegram_chat_id == ""
    assert settings.seed_path == DEFAULT_SEED_PATH
    assert settings.db_path == DEFAULT_DB_PATH
    assert settings.panel_secret_key != ""


def test_empty_env_values_fall_back_to_defaults():
    settings = settings_from_env({"SEED_PATH": "", "DB_PATH": ""})
    assert settings.seed_path == DEFAULT_SEED_PATH
    assert settings.db_path == DEFAULT_DB_PATH


def test_settings_read_env_overrides():
    settings = settings_from_env(
        {
            "PANEL_SECRET_KEY": "TEST-KEY",
            "SEED_PATH": "seed/custom.json",
            "DB_PATH": "data/custom.sqlite",
            "ADMIN_LOGIN": "TEST-admin",
        }
    )
    assert settings.panel_secret_key == "TEST-KEY"
    assert settings.seed_path == "seed/custom.json"
    assert settings.db_path == "data/custom.sqlite"
    assert settings.admin_login == "TEST-admin"


def test_ephemeral_key_warns_and_never_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="app.config"):
        settings = settings_from_env({})
    assert settings.panel_secret_key != ""
    assert "PANEL_SECRET_KEY" in caplog.text
    assert settings.panel_secret_key not in caplog.text


def test_env_example_matches_config():
    names = {
        line.split("=", 1)[0].strip()
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
    }
    assert names == set(ENV_NAMES)
