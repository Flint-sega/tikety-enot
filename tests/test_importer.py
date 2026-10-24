"""Импорт seed-файла: insert-missing по id, политики дат, первый старт панели."""

import json
import logging
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import reset_settings_cache
from app.db import get_engine, init_db, make_engine, set_engine
from app.importer import run_import
from app.main import app
from app.models import Ticket


def seed_entry(**overrides):
    entry = {
        "id": 101,
        "number": 1,
        "title": "Обновить базу",
        "description": "Обновить антивирусные базы до крайнего релиза",
        "status": "closed",
        "comment": "Готово, без ошибок",
        "datetime": "2024-03-01T10:30",
        "closed_at": "2024-03-01T07:45:12.500Z",
    }
    entry.update(overrides)
    return entry


DUMP_ENTRIES = [
    seed_entry(),
    seed_entry(
        id=102,
        number=2,
        title="Ёлка-иголка",
        description="кириллица ЁёЙй",
        status="open",
        comment=None,
        closed_at=None,
    ),
    seed_entry(
        id=103,
        number=3,
        title="Без плановой даты",
        description="описание на месте",
        datetime=None,
        closed_at="2024-03-03T18:00:00Z",
    ),
    seed_entry(
        id=104,
        number=4,
        title="Закрыт без дат",
        description="полустёртые данные",
        comment="Без комментария",
        datetime=None,
        closed_at=None,
    ),
]


@pytest.fixture()
def dump_file(tmp_path):
    def write(entries):
        path = tmp_path / "seed.json"
        path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        return str(path)

    return write


@pytest.fixture()
def imported(db, dump_file):
    path = dump_file(DUMP_ENTRIES)
    return path, run_import(path)


def by_legacy(session: Session, legacy_id: int) -> Ticket:
    return session.scalar(select(Ticket).where(Ticket.legacy_id == legacy_id))


def test_import_report_counts(imported):
    _, report = imported
    assert report.added == 4
    assert report.skipped == 0
    assert report.conflicts == 0


def test_non_integer_number_skipped(db, dump_file, session):
    entries = DUMP_ENTRIES + [seed_entry(id=105, number="116A", title="мусорный номер")]
    path = dump_file(entries)
    report = run_import(path)
    assert report.added == 4, "мусорный number не попадает в БД"
    assert report.skipped == 1
    assert by_legacy(session, 105) is None


def test_import_id_never_collides_with_panel_rowids(db, dump_file, session):
    """B1-регрессия: панель завела заявки с rowid 1..4, файл несёт id 1..4 —
    это РАЗНЫЕ тикеты с разными номерами, ничего не теряется и не конфликтует."""
    from app.tickets import create_ticket

    admin = make_user_row(session)
    for index in range(1, 5):
        create_ticket(f"панельная {index}", "создана панелью", None, admin)
    entries = [seed_entry(id=index, number=100 + index,
                          title=f"из файла {index}") for index in range(1, 5)]
    report = run_import(dump_file(entries))
    assert report.added == 4, "импорт не съедается панельными rowid"
    assert report.conflicts == 0
    imported = by_legacy(session, 1)
    assert imported.title == "из файла 1"
    assert imported.id not in {
        row.id for row in session.scalars(select(Ticket).where(Ticket.legacy_id.is_(None)))
    }


def make_user_row(session: Session):
    from app.models import User

    user = User(login="TEST-import-admin", password_hash="x", role="admin")
    session.add(user)
    session.commit()
    return user


def test_number_conflict_skips_row_not_whole_import(db, dump_file, session):
    """B1-регрессия: коллизия номеров (заявка с тем же номером уже есть в панели) —
    конфликтная строка пропускается с подсчётом, остальные импортируются."""
    from app.tickets import create_ticket

    admin = make_user_row(session)
    create_ticket("панельная с тем же номером", "номер 1 занят панелью", None, admin)
    report = run_import(dump_file(DUMP_ENTRIES))
    assert report.added == 3, "остальные строки импортируются"
    assert report.conflicts == 1
    assert by_legacy(session, 101) is None, "конфликтный тикет не импортирован"
    assert by_legacy(session, 102) is not None


def test_import_preserves_original_fields(imported, session):
    row = by_legacy(session, 101)
    assert row.number == 1
    assert row.title == "Обновить базу"
    assert row.description == "Обновить антивирусные базы до крайнего релиза"
    assert row.planned_at == "2024-03-01T10:30"
    assert row.status == "closed"
    assert row.comment == "Готово, без ошибок"
    assert row.closed_at == datetime(2024, 3, 1, 7, 45, 12, 500000)


def test_import_keeps_missing_fields_null(imported, session):
    row = by_legacy(session, 103)
    assert row.planned_at is None
    assert row.closed_at == datetime(2024, 3, 3, 18, 0, 0)
    sparse = by_legacy(session, 104)
    assert sparse.planned_at is None
    assert sparse.closed_at is None
    assert sparse.status == "closed"
    assert sparse.comment == "Без комментария"
    opened = by_legacy(session, 102)
    assert opened.status == "open"
    assert opened.comment is None
    assert opened.closed_at is None


def test_import_cyrillic_roundtrip(imported, session):
    row = by_legacy(session, 102)
    assert row.title == "Ёлка-иголка"
    assert row.description == "кириллица ЁёЙй"
    assert by_legacy(session, 101).title == "Обновить базу"


def test_rerun_is_idempotent(imported):
    path, _ = imported

    def snapshot():
        with Session(get_engine()) as db_session:
            return sorted(
                (
                    ticket.id,
                    ticket.number,
                    ticket.title,
                    ticket.description,
                    ticket.planned_at,
                    ticket.status,
                    ticket.comment,
                    ticket.closed_at,
                    ticket.created_at,
                    ticket.updated_at,
                )
                for ticket in db_session.scalars(select(Ticket))
            )

    before = snapshot()
    report = run_import(path)
    assert (report.added, report.skipped) == (0, 4)
    assert snapshot() == before


def test_imported_rows_are_searchable_like_new(imported, session):
    found = session.scalars(
        select(Ticket).where(Ticket.title.contains("базу"))
    ).all()
    assert [ticket.legacy_id for ticket in found] == [101]


def test_corrupted_dump_raises_clear_error(db, dump_file):
    path = dump_file([seed_entry()])
    path = str(path)[:-5] + "-broken.json"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("это не json {{{")
    with pytest.raises(ValueError, match="JSON"):
        run_import(path)


@pytest.fixture()
def first_start_env(tmp_path, monkeypatch):
    seed_path = tmp_path / "seed.json"
    db_path = tmp_path / "first-start.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("SEED_PATH", str(seed_path))
    reset_settings_cache()
    set_engine(None)
    yield seed_path, db_path
    set_engine(None)
    reset_settings_cache()


def test_first_start_imports_seed(first_start_env, caplog):
    seed_path, _ = first_start_env
    seed_path.write_text(json.dumps(DUMP_ENTRIES, ensure_ascii=False), encoding="utf-8")
    with caplog.at_level(logging.INFO, logger="app.importer"):
        with TestClient(app) as client:
            assert client.get("/healthz").status_code == 200
    with Session(get_engine()) as db_session:
        rows = db_session.scalars(select(Ticket)).all()
    assert sorted(row.legacy_id for row in rows) == [101, 102, 103, 104]
    assert "мпортировано 4" in caplog.text


def test_first_start_with_corrupted_seed_still_boots(first_start_env, caplog):
    seed_path, _ = first_start_env
    seed_path.write_text("это не json {{{", encoding="utf-8")
    with caplog.at_level(logging.INFO):
        with TestClient(app) as client:
            assert client.get("/healthz").status_code == 200
    with Session(get_engine()) as db_session:
        assert db_session.scalars(select(Ticket)).all() == []
    assert "не выполнен" in caplog.text


def test_first_start_skips_import_when_table_not_empty(first_start_env):
    seed_path, db_path = first_start_env
    seed_path.write_text(json.dumps(DUMP_ENTRIES, ensure_ascii=False), encoding="utf-8")
    engine = make_engine(db_path)
    init_db(engine)
    with Session(engine) as db_session:
        db_session.add(Ticket(number=999, title="уже есть"))
        db_session.commit()
    engine.dispose()
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
    with Session(get_engine()) as db_session:
        rows = db_session.scalars(select(Ticket)).all()
    assert [row.title for row in rows] == ["уже есть"]
