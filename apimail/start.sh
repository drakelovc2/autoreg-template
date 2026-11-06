#!/bin/bash
# apimail-panel — catch-all mailbox panel (apimail / catch-all example.com).
# Порт 9971 зарезервирован в docs/PANEL-PORTS.md.
#   bash start.sh            # поднять панель на 127.0.0.1:9971 и открыть браузер
#   bash start.sh --no-browser  # поднять панель без открытия браузера
#   Ctrl+C                   # чисто погасить
set -uo pipefail
cd "$(dirname "$0")"

# Разбор аргументов (как в active/freebuff/start.sh): по умолчанию браузер открываем.
NO_BROWSER=0
for arg in "$@"; do
  case "$arg" in
    --no-browser) NO_BROWSER=1 ;;
    *) echo "неизвестный аргумент: $arg (использование: bash start.sh [--no-browser])"; exit 1 ;;
  esac
done

# на этой машине иначе ломаются urllib/SSL (см. autoreg/GUIDE.md)
export -n PYTHONHOME PYTHONPATH 2>/dev/null
unset PYTHONHOME PYTHONPATH LD_LIBRARY_PATH

PY="python3"
if ! command -v "$PY" >/dev/null 2>&1; then
  PY="python3"
fi
PORT="${APIMAIL_PANEL_PORT:-9971}"
PANEL_URL="http://127.0.0.1:${PORT}"

mkdir -p data
echo "== apimail-panel: панель на 127.0.0.1:${PORT} ($(date '+%Y-%m-%d %H:%M:%S')) ==" | tee -a data/panel.log

# Браузер открываем фоновым сабшеллом и только когда сервер уже отвечает (как в
# active/freebuff/start.sh); --no-browser ветку открытия отключает.
( for i in $(seq 1 40); do
    env -u PYTHONHOME -u PYTHONPATH -u LD_LIBRARY_PATH curl -s -o /dev/null -m 1 "${PANEL_URL}/api/summary" && break
    sleep 0.3
  done
  [[ "$NO_BROWSER" != "1" ]] && xdg-open "$PANEL_URL" >/dev/null 2>&1 || true
) &

exec "$PY" panel/server.py --port "$PORT" 2>&1 | tee -a data/panel.log