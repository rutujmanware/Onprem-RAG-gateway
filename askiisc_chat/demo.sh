#!/usr/bin/env bash
# STAGE 1 DEMO: login page + chat UI running with STUB models (no GPU, no model weights, no real Redis).
# Purpose: prove the web app, sign-in and chat plumbing work on this machine, before wiring real components.
# Usage: ./demo.sh start | stop | status | code        (run from the askiisc_chat folder; no sudo)
# Settings (optional env): PY=python3  DEMO_PORT=9000
set -uo pipefail
cd "$(dirname "$0")"; mkdir -p run logs
PY="${PY:-python3}"; PORT="${DEMO_PORT:-9000}"; SLM_P=9101; LLM_P=9102
pidf() { echo "run/$1.pid"; }
alive() { [ -f "$(pidf "$1")" ] && kill -0 "$(cat "$(pidf "$1")")" 2>/dev/null; }
busy() { "$PY" - "$1" <<'PY'
import socket, sys
s = socket.socket(); s.settimeout(0.5)
try: s.connect(("127.0.0.1", int(sys.argv[1]))); print(1)
except OSError: print(0)
PY
}
case "${1:-status}" in
  start)
    for p in "$PORT" "$SLM_P" "$LLM_P"; do
      [ "$(busy "$p")" = 1 ] && { echo "ERROR: port $p is already in use. Run './demo.sh stop' or set DEMO_PORT."; exit 1; }
    done
    export STUB_KEY=demo-key
    setsid nohup "$PY" tests/stub_server.py SLM STUB_KEY "$SLM_P" >>logs/stub_slm.log 2>&1 < /dev/null & echo $! > "$(pidf stub_slm)"
    setsid nohup "$PY" tests/stub_server.py LLM STUB_KEY "$LLM_P" >>logs/stub_llm.log 2>&1 < /dev/null & echo $! > "$(pidf stub_llm)"
    # DEMO settings: code goes to the log (no mail relay), plain-HTTP cookie, no public/restricted split, hash embedder, Redis deliberately unreachable.
    env AUTH_MODE=otp OTP_DEV_LOG=1 COOKIE_SECURE=0 REQUIRE_LOGIN=1 ACCESS_TIERS=0 \
        SESSION_SECRET="$("$PY" -c 'import secrets;print(secrets.token_hex(32))')" \
        EMBED_BACKEND=hash EMBED_DIM=384 REDIS_URL=redis://127.0.0.1:1/0 \
        SLM_URL="http://127.0.0.1:$SLM_P/v1" SLM_KEY=demo-key LLM_URL="http://127.0.0.1:$LLM_P/v1" LLM_KEY=demo-key \
        BIND_HOST=127.0.0.1 BIND_PORT="$PORT" ALLOWED_CIDRS=127.0.0.0/8 \
        setsid nohup "$PY" -m askiisc.app >>logs/app.log 2>&1 < /dev/null & echo $! > "$(pidf app)"
    for _ in $(seq 1 30); do [ "$(busy "$PORT")" = 1 ] && break; sleep 0.5; done
    [ "$(busy "$PORT")" = 1 ] && echo "UP: open http://localhost:$PORT  (sign in with any @iisc.ac.in address, then run: ./demo.sh code)" \
                              || { echo "NOT UP - last log lines:"; tail -n 15 logs/app.log; exit 1; } ;;
  stop)
    for s in app stub_llm stub_slm; do
      if alive "$s"; then kill "$(cat "$(pidf "$s")")" 2>/dev/null; echo "stopped $s"; fi; rm -f "$(pidf "$s")"; done ;;
  status)
    for s in app stub_slm stub_llm; do alive "$s" && echo "$s: running pid $(cat "$(pidf "$s")")" || echo "$s: STOPPED"; done
    curl -s "http://127.0.0.1:$PORT/health" 2>/dev/null; echo ;;
  code)
    grep "DEMO OTP" logs/app.log | tail -n 1 || true ;;
  *) echo "usage: ./demo.sh start|stop|status|code"; exit 2 ;;
esac
