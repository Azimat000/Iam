#!/bin/sh
# Отдельные экспериментальные базы; внешняя авторизация никогда не копируется в проект.
set -eu
: "${1:?Укажите новый абсолютный каталог результатов}"
: "${2:?Укажите внешний auth.json подписки}"
case "$1" in /*) ;; *) echo 'Нужен абсолютный каталог результатов' >&2; exit 2;; esac
root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
PYTHONPATH="$root/src" "$root/.venv/bin/python" "$root/scripts/codex/verify.py" \
  --output-dir "$1-isolation"
PYTHONPATH="$root/src" "$root/.venv/bin/python" -m iam.experiments.report \
  --mode live --output "$1" --auth-file "$2" --persist-auth-updates
