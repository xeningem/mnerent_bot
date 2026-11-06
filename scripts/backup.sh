#!/usr/bin/env bash
# Резервная копия состояния бота: база, сессия Telegram и .env.
# Безопасно запускать при работающем боте: SQLite копируется через .backup.
# Использование: scripts/backup.sh [каталог_копий]   (по умолчанию ~/Backups/mnerent_bot)
# Переменная KEEP задаёт, сколько последних копий хранить (по умолчанию 14).
set -euo pipefail

cd "$(dirname "$0")/.."
dest_root="${1:-$HOME/Backups/mnerent_bot}"
keep="${KEEP:-14}"
dest="$dest_root/$(date +%Y%m%d-%H%M%S)"

umask 077  # внутри токены и сессия аккаунта
mkdir -p "$dest"

for db in data/*.sqlite3 data/*.session; do
  [ -e "$db" ] || continue
  sqlite3 "$db" ".backup '$dest/$(basename "$db")'"
done
[ -f .env ] && cp .env "$dest/.env"

# Удаляем старые копии, оставляя последние $keep.
ls -1d "$dest_root"/*/ | sort -r | tail -n +"$((keep + 1))" | while read -r old; do rm -rf "$old"; done

echo "Копия: $dest"
du -sh "$dest" | cut -f1
