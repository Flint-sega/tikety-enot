"""Движок, сессии, схема (create_all) и WAL для SQLite.

Контракт шва:
- make_engine(path) — движок для произвольного файла БД (тесты, скрипты);
- get_engine() — движок процесса (DB_PATH из конфига, лениво);
- set_engine(engine) — подмена движка (фикстуры тестов);
- init_db(engine) — полная схема через create_all + дописывание новых колонок
  в старые таблицы (ADDITIVE_COLUMNS, ADR-0007), WAL включён на каждом соединении;
- get_session() — зависимость FastAPI для сессий.
"""

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Base

_engine: Engine | None = None


@event.listens_for(Engine, "connect")
def _enable_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def make_engine(db_path: str | Path) -> Engine:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # В БД лежат токены интеграций, хеши паролей и заявки клиентов — файл и
    # папка только для владельца, независимо от umask процесса (ревью SEC-102).
    path.touch(exist_ok=True)
    path.chmod(0o600)
    path.parent.chmod(0o700)
    return create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})


def set_engine(engine: Engine | None) -> None:
    global _engine
    _engine = engine


def get_engine() -> Engine:
    if _engine is None:
        set_engine(make_engine(get_settings().db_path))
    assert _engine is not None
    return _engine


# Колонки, появившиеся после первого выпуска: create_all не добавляет их в уже
# существующие таблицы, поэтому init_db дописывает недостающие (ADR-0007).
# Только добавление с безопасным значением по умолчанию — данные не трогаются.
ADDITIVE_COLUMNS = {
    "templates": (
        ("created_by", "INTEGER"),
        ("is_shared", "BOOLEAN NOT NULL DEFAULT 1"),
    ),
}


def _add_missing_columns(engine: Engine) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as connection:
        for table, columns in ADDITIVE_COLUMNS.items():
            if table not in tables:
                continue
            present = {column["name"] for column in inspector.get_columns(table)}
            for name, ddl in columns:
                if name not in present:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def init_db(engine: Engine | None = None) -> None:
    target = engine if engine is not None else get_engine()
    Base.metadata.create_all(target)
    _add_missing_columns(target)


def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session
