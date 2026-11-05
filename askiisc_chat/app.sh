#!/usr/bin/env bash
# Run the AskIISc web app (login page + chat UI) from this folder. No sudo.   Usage: ./app.sh start | stop | restart | status | logs | code
# Settings come from app.env (copy app.env.example). Set PY=/path/to/python if the app's packages are not in python3.
set -uo pipefail
cd "$(dirname "$0")"; mkdir -p run logs
[ -f app.env ] || { echo "ERROR: app.env not found - copy app.env.example to app.env and edit it."; exit 1; }
set -a; . ./app.env; set +a
PY="${PY:-python3}"; HOST="${BIND_HOST:-127.0.0.1}"; PORT="${BIND_PORT:-9000}"
if [ "${SESSION_SECRET:-CHANGE_ME}" = "CHANGE_ME" ]; then
  echo "ERROR: set SESSION_SECRET in app.env:  sed -i \"s|^SESSION_SECRET=.*|SESSION_SECRET=\$(python3 -c 'import secrets;print(secrets.token_hex(32))')|\" app.env"; exit 1; fi
pidf=run/app.pid
alive() { [ -f "$pidf" ] && kill -0 "$(cat "$pidf")" 2>/dev/null; }
busy() { "$PY" - "$HOST" "$PORT" <<'PY'
import socket, sys
s = socket.socket(); s.settimeout(0.5)
try: s.connect((sys.argv[1], int(sys.argv[2]))); print(1)
except OSError: print(0)
PY
}
case "${1:-status}" in
  start)
    alive && { echo "already running (pid $(cat "$pidf"))"; exit 0; }
    [ "$(busy)" = 1 ] && { echo "ERROR: $HOST:$PORT is already in use. Change BIND_PORT in app.env."; exit 1; }
    setsid nohup "$PY" -m askiisc.app >>logs/app.log 2>&1 < /dev/null & echo $! > "$pidf"
    for _ in $(seq 1 40); do [ "$(busy)" = 1 ] && break; sleep 0.5; done
    if [ "$(busy)" = 1 ]; then echo "UP on http://$HOST:$PORT  (backend=${BACKEND:-own}; sign-in code: ./app.sh code)"
    else echo "NOT UP - last log lines:"; tail -n 15 logs/app.log; exit 1; fi ;;
  stop) alive && { kill "$(cat "$pidf")" 2>/dev/null; echo stopped; } || echo "not running"; rm -f "$pidf" ;;
  restart) "$0" stop; "$0" start ;;
  status) alive && echo "app: running pid $(cat "$pidf")" || echo "app: STOPPED"; curl -s "http://$HOST:$PORT/health" 2>/dev/null; echo ;;
  logs) tail -n "${2:-80}" -f logs/app.log ;;
  code) grep "DEMO OTP" logs/app.log | tail -n 1 || true ;;
  *) echo "usage: ./app.sh start|stop|restart|status|logs|code"; exit 2 ;;
esac
