"""Импорт заявок из JSON-файла (seed) в БД (insert-missing по legacy_id).

Контракт шва:
- run_import(dump_path) -> ImportReport{added, skipped, conflicts} — единственная
  точка импорта;
- id из файла НЕ становится первичным ключом: пишется в legacy_id, панель живёт
  в собственном ряде autoincrement-id — id файла никогда не коллидирует
  с заявками, созданными самой панелью;
- существующие записи (совпадение legacy_id) никогда не перезаписываются;
- коллизия номера (в файле и в панели заявка с тем же number)
  не роняет импорт: строка пропускается в собственный SAVEPOINT, считается
  в conflicts и перечисляется в журнале — решение за человеком;
- planned_at сохраняется как есть (трактовка МСК), closed_at парсится из UTC-строки;
- CLI повторного прогона: uv run python -m app.importer (SEED_PATH/DB_PATH из конфига).

Формат файла: JSON-массив объектов
{id, number, title, description, status, comment?, datetime?, closed_at?};
datetime — произвольный текст планового времени, closed_at — UTC ISO-строка.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_engine, init_db
from app.models import Ticket

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ImportReport:
    added: int
    skipped: int
    conflicts: int = 0


def parse_closed_at(raw: str) -> datetime:
    """UTC-строка файла -> naive datetime в UTC (хранение closed_at по инварианту 8)."""
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def run_import(dump_path: str) -> ImportReport:
    """Перенести тикеты из файла в БД, не трогая уже существующие legacy_id."""
    entries = _read_dump(dump_path)
    added = 0
    conflicts = 0
    try:
        with Session(get_engine()) as session:
            present = set(
                session.scalars(
                    select(Ticket.legacy_id).where(Ticket.legacy_id.is_not(None))
                )
            )
    except OperationalError as error:
        if "legacy_id" in str(error):
            raise ValueError(
                "База данных старой схемы (нет колонки legacy_id): восстанавливать "
                "бэкапы data/, сделанные до 13.10, нельзя напрямую — экспортируйте "
                "их содержимое файлом и импортируйте как seed"
            ) from error
        raise
    with Session(get_engine()) as session:
        present = set(
            session.scalars(select(Ticket.legacy_id).where(Ticket.legacy_id.is_not(None)))
        )
        for entry in entries:
            legacy_id = entry["id"]
            if legacy_id in present:
                continue
            ticket = _to_ticket(entry)
            if ticket is None:
                continue
            try:
                with session.begin_nested():
                    session.add(ticket)
                    session.flush()
            except IntegrityError:
                conflicts += 1
                logger.warning(
                    "Конфликт номера при импорте: файл id=%s number=%r — "
                    "заявка с таким номером уже есть, строка пропущена",
                    legacy_id,
                    entry.get("number"),
                )
                continue
            present.add(legacy_id)
            added += 1
        session.commit()
    skipped = len(entries) - added
    logger.info(
        "Импортировано %d, пропущено %d (конфликты номеров: %d; файл: %s)",
        added,
        skipped,
        conflicts,
        dump_path,
    )
    return ImportReport(added=added, skipped=skipped, conflicts=conflicts)


def _read_dump(dump_path: str) -> list[dict]:
    path = Path(dump_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"Не удалось прочитать seed-файл {path}: {error}") from error
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"Seed-файл {path} не является корректным JSON: {error}"
        ) from error
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise ValueError(f"Seed-файл {path}: ожидается JSON-массив объектов тикетов")
    return data


def _to_ticket(entry: dict) -> Ticket | None:
    number = entry.get("number")
    if not isinstance(number, int) or isinstance(number, bool):
        logger.warning(
            "Тикет id=%s пропущен: number=%r не целое число",
            entry.get("id"),
            number,
        )
        return None
    closed_at = None
    raw_closed_at = entry.get("closed_at")
    if raw_closed_at:
        try:
            closed_at = parse_closed_at(raw_closed_at)
        except ValueError:
            logger.warning(
                "Тикет id=%s: не удалось разобрать closed_at=%r — сохранён NULL",
                entry.get("id"),
                raw_closed_at,
            )
    return Ticket(
        legacy_id=entry["id"],
        number=number,
        title=entry["title"],
        description=entry.get("description") or "",
        planned_at=entry.get("datetime"),
        status=entry.get("status") or "open",
        comment=entry.get("comment"),
        closed_at=closed_at,
        source="local",
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    init_db()
    try:
        report = run_import(get_settings().seed_path)
    except ValueError as error:
        logger.error("%s", error)
        return 1
    print(f"added={report.added} skipped={report.skipped} conflicts={report.conflicts}")
    if report.conflicts:
        print(
            "Конфликты номеров перечислены в журнале (строки «Конфликт номера при "
            "импорте»): сверьте пары вручную и закройте дубль в нужной панели"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
