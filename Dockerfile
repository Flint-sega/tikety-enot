# Тикеты Енот v2 — образ панели. Сборка: docker compose up --build (см. README.md)
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Зависимости из uv.lock отдельным слоем (--frozen: ровно зафиксированные версии,
# сборка воспроизводима); правки кода слой не переустанавливают
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Код и ресурсы панели; seed монтируется томом (./seed:ro в compose) — файлы
# с реальными заявками не запекаются в слои образа (приватность)
COPY app/ app/
COPY templates/ templates/
COPY static/ static/

# Непривилегированный пользователь (uid 1000); писать панель может только в ./data
RUN groupadd --gid 1000 panel \
    && useradd --uid 1000 --gid panel --create-home panel \
    && chown -R panel:panel /app
USER panel

EXPOSE 8000

# Внутри контейнера слушаем 0.0.0.0:8000 — ограничение «только мой компьютер» делает
# публикация порта в docker-compose.yml (127.0.0.1:8072:8000).
# Один процесс: панель рассчитана на SQLite и фоновый поллер внутри процесса — воркеров не масштабировать.
CMD ["/app/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
