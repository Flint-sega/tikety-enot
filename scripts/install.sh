#!/usr/bin/env bash
# Установка панели «Тикеты Енот v2» одной командой: bash scripts/install.sh
#
# Что делает:
#   1. Есть docker (и демон отвечает) — поднимает панель в docker:
#      docker compose up --build -d (порт 127.0.0.1:8072 из docker-compose.yml).
#   2. Docker нет (или демон не запущен) — нативный режим: ставит uv в ~/.local/bin
#      официальным инсталлером (без sudo), ставит зависимости (uv sync --frozen --no-dev)
#      и запускает панель в фоновом режиме (журнал — nohup.out в папке проекта).
#   3. Если .env нет — создаёт его из .env.example: генерирует PANEL_SECRET_KEY
#      (в файл, не в вывод) и спрашивает логин/пароль первого администратора
#      (можно пропустить — тогда первый админ создаётся на setup-экране панели).
#      Существующий .env не трогает.
#   4. Ждёт /healthz до 60 секунд и печатает адрес панели.
#
# Хостинг с доменом и HTTPS: DOMAIN=tickets.example.ru bash scripts/install.sh
#   — только docker-режим: вписывает DOMAIN и COOKIE_SECURE=1 в .env и поднимает
#   вместе с панелью Caddy (профиль https в docker-compose.yml), который сам
#   получает и продлевает сертификат Let's Encrypt. Нужны A-запись домена на этот
#   сервер и открытые порты 80/443. Без DOMAIN — как раньше, только 127.0.0.1:8072.
#
# Порт нативного режима задаётся переменной PANEL_PORT (по умолчанию 8072);
# docker-режим всегда слушает 8072 — порт захардкожен в docker-compose.yml.
# Повторный запуск безопасен: подтягивает код и обновляет зависимости; .env и
# data/ не трогает. Работающую панель скрипт не перезапускает — чтобы обновление
# кода вступило в силу, перезапустите её вручную (нативный режим: убить процесс
# uvicorn и запустить скрипт снова).
#
# Подробности — INSTALL.md (в корне проекта и внутри ZIP-сборки).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_DIR/.env"
EXAMPLE_FILE="$REPO_DIR/.env.example"
LOG_FILE="$REPO_DIR/nohup.out"
UV_INSTALLER_URL="https://astral.sh/uv/0.9.5/install.sh"
# sha256 пинованного инсталлера: целостность скачать-и-исполнить (ревью SEC-104).
UV_INSTALLER_SHA256="8402ab80d2ef54d7044a71ea4e4e1e8db3b20c87c7bffbc30bff59f1e80ebbd5"
HEALTH_TIMEOUT_SEC=60
DOCKER_HEALTH_URL="http://127.0.0.1:8072/healthz"
UV_INSTALLER_TMP=""

cleanup() {
  if [ -n "$UV_INSTALLER_TMP" ]; then
    rm -f "$UV_INSTALLER_TMP"
  fi
}
trap cleanup EXIT

die() { echo "Ошибка: $*" >&2; exit 1; }

info() { echo "$*"; }

generate_secret() {
  # Ключ сессий для PANEL_SECRET_KEY: openssl, без него — python3. В stdout, не в терминал.
  local key=""
  if command -v openssl >/dev/null 2>&1; then
    key="$(openssl rand -base64 32 2>/dev/null || true)"
  fi
  if [ -z "$key" ]; then
    command -v python3 >/dev/null 2>&1 ||
      die "нужен openssl или python3, чтобы сгенерировать ключ сессий для .env"
    key="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  fi
  [ -n "$key" ] || die "не удалось сгенерировать ключ сессий"
  printf '%s\n' "$key"
}

ask_admin_credentials() {
  # Логин/пароль первого администратора: спросить, пустой ввод — оставить пустыми.
  # Пароль читается скрытым вводом и нигде не печатается.
  admin_login=""
  admin_password=""
  if [ ! -t 0 ]; then
    info "Панель запущена без терминала — логин и пароль администратора не спрашиваю."
    info "Первого администратора вы создадите на setup-экране при первом входе в панель."
    return 0
  fi
  printf 'Логин первого администратора (Enter — создать его позже на setup-экране панели): '
  read -r admin_login || admin_login=""
  if [ -z "$admin_login" ]; then
    info "Логин не задан — первый администратор создаётся на setup-экране при первом входе."
    return 0
  fi
  printf 'Пароль администратора (ввод скрытый, не длиннее 72 байт, без `#` и кавычек): '
  read -rs admin_password || admin_password=""
  printf '\n'
  # .env-формат искажает ` #` (инлайн-комментарий) и кавычки — такой пароль
  # молча ломает вход; запрещаем заранее, а не диагностируем после записи.
  case "$admin_password" in
    *"'"* | *'"'* | *"#"*)
      die "пароль не должен содержать одинарную кавычку, двойную кавычку или `#` — выберите другой и запустите снова"
      ;;
  esac
  if [ -z "$admin_password" ]; then
    info "Пароль пустой — логин и пароль не записаны: администратора создадите на setup-экране."
    admin_login=""
  fi
}

create_env_file() {
  [ -f "$EXAMPLE_FILE" ] || die "нет файла .env.example рядом со скриптом — запустите его из папки проекта (или распакованного архива)"
  [ -f "$ENV_FILE" ] && {
    info ".env уже существует — не трогаю."
    return 0
  }
  PANEL_SECRET_KEY="$(generate_secret)"
  ask_admin_credentials
  # Подстановка через awk+ENVIRON: значения с любыми символами не ломают запись в .env.
  TIKETY_SECRET="$PANEL_SECRET_KEY" TIKETY_LOGIN="$admin_login" TIKETY_PASSWORD="$admin_password" \
    awk '
      /^PANEL_SECRET_KEY=/ { print "PANEL_SECRET_KEY=" ENVIRON["TIKETY_SECRET"]; next }
      /^ADMIN_LOGIN=/      { if (ENVIRON["TIKETY_LOGIN"] != "") print "ADMIN_LOGIN=" ENVIRON["TIKETY_LOGIN"]; else print; next }
      /^ADMIN_PASSWORD=/   { if (ENVIRON["TIKETY_PASSWORD"] != "") print "ADMIN_PASSWORD=" ENVIRON["TIKETY_PASSWORD"]; else print; next }
      { print }
    ' "$EXAMPLE_FILE" > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  info "Создан .env (права 600): ключ сессий сгенерирован и записан в файл, не в вывод."
}

set_env_value() {
  # KEY=VALUE в .env: заменить строку ключа или дописать в конец; права 600 сохраняются.
  local key="$1" value="$2" tmp
  tmp="$(mktemp)"
  TIKETY_KEY="$key" TIKETY_VALUE="$value" awk '
    BEGIN { done = 0 }
    $0 ~ "^" ENVIRON["TIKETY_KEY"] "=" { print ENVIRON["TIKETY_KEY"] "=" ENVIRON["TIKETY_VALUE"]; done = 1; next }
    { print }
    END { if (!done) print ENVIRON["TIKETY_KEY"] "=" ENVIRON["TIKETY_VALUE"] }
  ' "$ENV_FILE" > "$tmp"
  cat "$tmp" > "$ENV_FILE"
  rm -f "$tmp"
  chmod 600 "$ENV_FILE"
}

validate_domain() {
  # Только имя хоста: буквы, цифры, точки и дефисы, хотя бы одна точка; без схемы и пути.
  printf '%s' "$DOMAIN" | grep -Eq '^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$' ||
    die "DOMAIN должен быть именем хоста без http:// и /, например tickets.example.ru (сейчас: '$DOMAIN')"
}

wait_healthz() {
  local url="$1"
  local waited=0
  while [ "$waited" -lt "$HEALTH_TIMEOUT_SEC" ]; do
    # Тело ответа сверяем с нашим /healthz: чужой сервис на порту не считается панелью.
    if curl -fsS "$url" 2>/dev/null | grep -q '"status"'; then
      info "Панель отвечает: $url"
      return 0
    fi
    sleep 1
    waited=$((waited + 1))
  done
  die "панель не ответила за ${HEALTH_TIMEOUT_SEC} с — журнал: docker compose logs -f app (docker-режим) или файл nohup.out в папке проекта (нативный режим)"
}

run_docker() {
  if [ "$PANEL_PORT" != "8072" ]; then
    info "Замечание: PANEL_PORT=${PANEL_PORT} в docker-режиме не применяется — порт задаёт docker-compose.yml (сейчас 127.0.0.1:8072)."
  fi
  if [ -n "$DOMAIN" ]; then
    set_env_value DOMAIN "$DOMAIN"
    set_env_value COOKIE_SECURE 1
    info "Режим хостинга: панель + HTTPS (Caddy) для https://${DOMAIN}…"
    (cd "$REPO_DIR" && docker compose --profile https up --build -d) ||
      die "docker compose up не удался — посмотрите сообщение выше (порты 80/443 свободны? docker запущен?)"
    wait_healthz "$DOCKER_HEALTH_URL"
    RESULT_URL="https://${DOMAIN}"
    info "Сертификат Caddy получит сам за минуту, если A-запись ${DOMAIN} указывает на этот сервер"
    info "и открыты порты 80 и 443. Журнал HTTPS: docker compose logs -f caddy"
    return 0
  fi
  info "Режим Docker: docker compose up --build -d…"
  (cd "$REPO_DIR" && docker compose up --build -d) ||
    die "docker compose up не удался — проверьте, что демон docker запущен, и посмотрите сообщение выше"
  wait_healthz "$DOCKER_HEALTH_URL"
  RESULT_URL="http://127.0.0.1:8072"
}

run_native() {
  if [ "$(id -u)" -eq 0 ]; then
    die "нативный режим нельзя запускать от root: uv и панель ставятся в домашний каталог без sudo. Запустите под обычным пользователем (либо используйте docker-режим)."
  fi
  PATH="$HOME/.local/bin:$PATH"
  export PATH
  if ! command -v uv >/dev/null 2>&1; then
    info "Ставлю uv в ~/.local/bin (официальный инсталлер, без sudo)…"
    UV_INSTALLER_TMP="$(mktemp)"
    curl -fsSL "$UV_INSTALLER_URL" -o "$UV_INSTALLER_TMP" ||
      die "не удалось скачать инсталлер uv ($UV_INSTALLER_URL) — проверьте сеть и запустите снова"
    [ -s "$UV_INSTALLER_TMP" ] || die "инсталлер uv скачался пустым — установка отменена"
    head -n 1 "$UV_INSTALLER_TMP" | grep -q '^#!' ||
      die "скачанный файл не похож на shell-скрипт — установка uv отменена"
    # Целостность: sha256 пинованной версии (shasum на macOS, sha256sum на Linux).
    actual_hash="$( (sha256sum "$UV_INSTALLER_TMP" 2>/dev/null || shasum -a 256 "$UV_INSTALLER_TMP") | awk '{ print $1 }')"
    [ "$actual_hash" = "$UV_INSTALLER_SHA256" ] ||
      die "sha256 инсталлера uv не совпал с эталонным — скачивание скомпрометировано, установка отменена"
    sh "$UV_INSTALLER_TMP"
    PATH="$HOME/.local/bin:$PATH"
    export PATH
  fi
  command -v uv >/dev/null 2>&1 ||
    die "uv не установился — поставьте вручную (curl -LsSf $UV_INSTALLER_URL | sh) и запустите скрипт снова"

  cd "$REPO_DIR"
  info "Ставлю зависимости (uv sync --frozen --no-dev)…"
  uv sync --frozen --no-dev

  native_health_url="http://127.0.0.1:${PANEL_PORT}/healthz"
  if curl -fsS "$native_health_url" >/dev/null 2>&1; then
    info "Панель уже запущена на http://127.0.0.1:${PANEL_PORT} — оставляю работающий процесс."
    RESULT_URL="http://127.0.0.1:${PANEL_PORT}"
    return 0
  fi
  info "Запускаю панель на http://127.0.0.1:${PANEL_PORT} (журнал: nohup.out в папке проекта)…"
  nohup uv run uvicorn app.main:app --host 127.0.0.1 --port "$PANEL_PORT" >>"$LOG_FILE" 2>&1 &
  wait_healthz "$native_health_url"
  RESULT_URL="http://127.0.0.1:${PANEL_PORT}"
}

print_result() {
  info ""
  info "Готово. Панель: $1"
  info "Вход: логин и пароль администратора — в .env (ADMIN_LOGIN / ADMIN_PASSWORD);"
  info "если они пусты — создайте администратора на setup-экране при первом входе."
  info "Токены интеграций (UseDesk, телеграм) вписываются уже в панели: Админ → «Ключи и функции»."
}

main() {
  command -v curl >/dev/null 2>&1 || die "не найден curl — установите его (Debian/Ubuntu: apt install curl) и запустите снова"
  command -v awk >/dev/null 2>&1 || die "не найден awk — он нужен для создания .env"
  PANEL_PORT="${PANEL_PORT:-8072}"
  case "$PANEL_PORT" in
    '' | *[!0-9]*) die "PANEL_PORT должен быть числом (сейчас: '$PANEL_PORT')" ;;
  esac
  if [ "$PANEL_PORT" -lt 1 ] || [ "$PANEL_PORT" -gt 65535 ]; then
    die "PANEL_PORT должен быть в диапазоне 1–65535 (сейчас: $PANEL_PORT)"
  fi
  RESULT_URL=""
  DOMAIN="$(printf '%s' "${DOMAIN:-}" | tr 'A-Z' 'a-z')"
  if [ -n "$DOMAIN" ]; then
    validate_domain
  fi

  # Файлы, создаваемые скриптом (.env с ключом сессий, nohup.out), — сразу 600,
  # без окна world-readable между созданием и chmod.
  umask 077

  create_env_file

  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    run_docker
  elif [ -n "$DOMAIN" ]; then
    die "для DOMAIN (хостинг с HTTPS) нужен Docker. Поставьте его официальным установщиком: curl -fsSL https://get.docker.com | sh — и запустите скрипт снова"
  else
    if command -v docker >/dev/null 2>&1; then
      info "docker установлен, но демон не отвечает — перехожу в нативный режим."
    else
      info "docker не найден — нативный режим (uv + запуск в фоне)."
    fi
    run_native
  fi

  print_result "$RESULT_URL"
}

main "$@"
