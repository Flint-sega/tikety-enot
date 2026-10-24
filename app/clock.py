"""Единые часы панели: naive-UTC для времён в БД и сравнений (инвариант 8).

Контракт шва:
- utcnow_naive() — текущее время UTC без таймзоны: формат хранения
  closed_at/created_at/updated_at/watermark и база LWW-сравнений.

Отображение пользователю — МСК (app.tickets.format_msk); здесь только хранение.
"""

from datetime import datetime, timezone


def utcnow_naive() -> datetime:
    """Текущее время UTC без таймзоны — единая точка «сейчас» для всех модулей."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
