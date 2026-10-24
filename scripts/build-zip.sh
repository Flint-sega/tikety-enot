#!/usr/bin/env bash
# Сборка ZIP «всё в одном» для переноса панели на другую машину:
#   scripts/build-zip.sh  →  ../tikety-enot-v2-ГГГГММДД.zip
#
# В архив попадает отслеживаемое git-ом без мусора (исключаются .git, .venv, data/,
# __pycache__/.pyc) плюс INSTALL.md — инструкция «установка одной
# командой» (в распакованном архиве: bash scripts/install.sh). Папка seed/ в архив
# не входит — JSON с заявками для импорта докладывается вручную (INSTALL.md, раздел 4).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ZIP_PATH="${REPO_DIR}/../tikety-enot-v2-$(date +%Y%m%d).zip"
LIST_TMP=""
LIST_OK_TMP=""

cleanup() {
  if [ -n "$LIST_TMP" ]; then
    rm -f "$LIST_TMP" "$LIST_OK_TMP"
  fi
}
trap cleanup EXIT

die() { echo "Ошибка: $*" >&2; exit 1; }

info() { echo "$*"; }

main() {
  command -v git >/dev/null 2>&1 || die "не найден git — архив собирается из отслеживаемых им файлов"
  command -v zip >/dev/null 2>&1 || die "не найден zip — установите его (Debian/Ubuntu: apt install zip) и запустите снова"
  command -v unzip >/dev/null 2>&1 || die "не найден unzip — он нужен для проверки готового архива"
  [ -d "$REPO_DIR/.git" ] || die "это не git-репозиторий: сборка берёт файлы через git ls-files"
  [ -f "$REPO_DIR/INSTALL.md" ] || die "нет INSTALL.md в корне проекта — он обязан быть внутри архива"

  cd "$REPO_DIR"
  LIST_TMP="$(mktemp)"
  LIST_OK_TMP="$(mktemp)"
  # Основа — отслеживаемое git-ом; поверх — явные исключения из тикета:
  # данные (data/), кэши (__pycache__, .pyc), виртуальное
  # окружение, git и seed/ — файлов с реальными заявками в архиве установки быть
  # не должно (JSON для импорта докладывается вручную — см. INSTALL.md, раздел 4),
  # иначе цель guard-а «секреты не просочились» теряется.
  git -c core.quotePath=false ls-files |
    grep -Ev '(^|/)(data|\.venv|\.git|__pycache__|seed)(/|$)|\.pyc$' >"$LIST_TMP"
  printf '%s\n' "INSTALL.md" >>"$LIST_TMP"
  LC_ALL=C sort -u "$LIST_TMP" | while IFS= read -r path; do
    if [ -f "$path" ]; then
      printf '%s\n' "$path"
    fi
  done >"$LIST_OK_TMP"
  file_count="$(awk 'END { print NR }' "$LIST_OK_TMP")"
  [ "$file_count" -gt 0 ] || die "список файлов для архива пуст"

  rm -f "$ZIP_PATH"
  zip -q -r "$ZIP_PATH" -@ < "$LIST_OK_TMP"
  [ -s "$ZIP_PATH" ] || die "архив получился пустым: $ZIP_PATH"

  # Проверки: INSTALL.md внутри; секреты, база и файлы заявок не просочились
  # (git ls-files их не отдаёт, но проверка защищает от будущих правок сборки).
  # Листинг — в файл: grep -q в пайпе закрывает его рано, unzip ловит SIGPIPE,
  # а pipefail превращает это в ложную ошибку (известные грабли bash pipefail).
  unzip -l "$ZIP_PATH" > "$LIST_TMP"
  grep -q 'INSTALL\.md' "$LIST_TMP" || die "в архиве нет INSTALL.md"
  if grep -Eq '(^|[[:space:]])(\.env|seed/)([[:space:]]|$)|data/tickets\.sqlite' "$LIST_TMP"; then
    rm -f "$ZIP_PATH"
    die "в архив попали секреты, база или файлы заявок (.env / seed/ / data/) — архив удалён, проверьте git ls-files"
  fi

  info "Собран архив: $ZIP_PATH"
  info "Файлов: $file_count"
  info "Размер: $(du -h "$ZIP_PATH" | awk '{ print $1 }')"
  info "Первые строки оглавления:"
  awk 'NR <= 15' "$LIST_TMP"
  info "Папка seed/ в архив не входит — импорт заявок из JSON: INSTALL.md, раздел 4."
}

main "$@"
