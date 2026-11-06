#!/usr/bin/env bash
# Rootless process manager for AskIISc (no sudo, no systemd).
#   askiisc.sh start [slm|vllm|gateway|all]   stop [...]   status   logs <svc>   ensure   (ensure = start whatever is dead; for cron)
set -uo pipefail
HOME_DIR="${ASKIISC_HOME:-$HOME/askiisc}"; ENV_FILE="${ASKIISC_ENV:-$HOME_DIR/askiisc.env}"
RUN="$HOME_DIR/run"; LOGS="$HOME_DIR/logs"; mkdir -p "$RUN" "$LOGS"; chmod 700 "$HOME_DIR" "$RUN" "$LOGS" 2>/dev/null || true
[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE"; exit 1; }
if grep -nE '^[A-Z_]+=.*<[A-Za-z]' "$ENV_FILE"; then echo "ERROR: unfilled <PLACEHOLDER> values above in $ENV_FILE"; exit 1; fi
set -a; . "$ENV_FILE"; set +a
VENV_GW="${VENV_GW:-$HOME_DIR/venv-gw}"; VENV_VLLM="${VENV_VLLM:-$HOME_DIR/venv-vllm}"
SVCS="slm vllm gateway"

pidfile() { echo "$RUN/$1.pid"; }
alive()   { [ -f "$(pidfile "$1")" ] && kill -0 "$(cat "$(pidfile "$1")")" 2>/dev/null; }
port_in_use() { python3 - "$1" <<'PY'
import socket, sys
s = socket.socket(); s.settimeout(0.5)
try: s.connect(("127.0.0.1", int(sys.argv[1]))); print(1)
except OSError: print(0)
PY
}
launch() {  # launch <svc> <cmd...>   (own session so we can kill the whole group)
  local svc="$1"; shift
  setsid nohup "$@" >>"$LOGS/$svc.log" 2>&1 < /dev/null & echo $! > "$(pidfile "$svc")"
}
start_one() {
  local svc="$1"
  if alive "$svc"; then echo "$svc already running (pid $(cat "$(pidfile "$svc")"))"; return 0; fi
  case "$svc" in
    slm)
      [ "$(port_in_use "$SLM_PORT")" = 1 ] && { echo "port $SLM_PORT already in use (another user?) - change SLM_PORT/SLM_URL"; return 1; }
      launch slm "$LLAMA_BIN" -m "$SLM_MODEL_PATH" --alias "$SLM_MODEL" --host 127.0.0.1 --port "$SLM_PORT" -c "$SLM_CTX" --parallel 2 -t "$SLM_THREADS" ;;
    vllm)
      [ "$(port_in_use "$LLM_PORT")" = 1 ] && { echo "port $LLM_PORT already in use (another user?) - change LLM_PORT/LLM_URL"; return 1; }
      CUDA_VISIBLE_DEVICES="$GPU_ID" VLLM_NO_USAGE_STATS=1 launch vllm "$VENV_VLLM/bin/vllm" serve "$LLM_MODEL_PATH" --served-model-name "$LLM_MODEL" \
        --quantization fp8 --kv-cache-dtype fp8 --max-model-len "$VLLM_MAX_LEN" --gpu-memory-utilization "$VLLM_GPU_UTIL" \
        --max-num-seqs 32 --enable-prefix-caching --host 127.0.0.1 --port "$LLM_PORT" ;;
    gateway)
      [ "$(port_in_use "$BIND_PORT")" = 1 ] && { echo "port $BIND_PORT already in use - change BIND_PORT"; return 1; }
      cd "$HOME_DIR/app" && CUDA_VISIBLE_DEVICES=-1 OMP_NUM_THREADS="${GW_THREADS:-2}" TOKENIZERS_PARALLELISM=false PYTHONPATH=. \
        launch gateway "$VENV_GW/bin/python" -m askiisc.app ;;
    *) echo "unknown service $svc"; return 2 ;;
  esac
  sleep 1; alive "$svc" && echo "started $svc (pid $(cat "$(pidfile "$svc")")), log: $LOGS/$svc.log" || { echo "$svc died at startup - see $LOGS/$svc.log"; return 1; }
}
stop_one() {
  local svc="$1"; alive "$svc" || { echo "$svc not running"; rm -f "$(pidfile "$svc")"; return 0; }
  local pid; pid="$(cat "$(pidfile "$svc")")"; kill -- "-$pid" 2>/dev/null || kill "$pid" 2>/dev/null
  for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
  kill -0 "$pid" 2>/dev/null && kill -9 -- "-$pid" 2>/dev/null; rm -f "$(pidfile "$svc")"; echo "stopped $svc"
}
wait_http() {  # wait_http <url> <key> <secs>
  for _ in $(seq 1 "$3"); do [ "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $2" "$1" 2>/dev/null)" = 200 ] && return 0; sleep 1; done; return 1
}
status() {
  for s in $SVCS; do alive "$s" && echo "$s: running (pid $(cat "$(pidfile "$s")"))" || echo "$s: STOPPED"; done
  echo "slm  /v1/models: $(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $SLM_KEY" "$SLM_URL/models" 2>/dev/null)   (200 = ok, 401 = wrong key)"
  echo "vllm /v1/models: $(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $LLM_KEY" "$LLM_URL/models" 2>/dev/null)"
  echo "gateway /health: $(curl -s --max-time 5 "http://$BIND_HOST:$BIND_PORT/health" 2>/dev/null || echo unreachable)"
}
targets() { [ "${1:-all}" = all ] && echo "$SVCS" || echo "$1"; }
cmd="${1:-status}"; arg="${2:-all}"
case "$cmd" in
  start) for s in $(targets "$arg"); do
           start_one "$s" || exit 1
           [ "$s" = vllm ] && { echo "waiting for vLLM (first load can take several minutes)..."; wait_http "$LLM_URL/models" "$LLM_KEY" "${VLLM_WAIT:-900}" || { echo "vLLM not ready; see $LOGS/vllm.log"; exit 1; }; }
           [ "$s" = slm ] && { wait_http "$SLM_URL/models" "$SLM_KEY" 120 || { echo "SLM not ready; see $LOGS/slm.log"; exit 1; }; }
         done; status ;;
  stop)  for s in $(for x in $(targets "$arg"); do echo $x; done | tac); do stop_one "$s"; done ;;
  ensure) for s in $SVCS; do alive "$s" || { echo "$(date -Is) restarting $s" >>"$LOGS/ensure.log"; "$0" start "$s" >>"$LOGS/ensure.log" 2>&1; }; done ;;
  status) status ;;
  logs)  tail -n "${3:-100}" -f "$LOGS/$arg.log" ;;
  *) echo "usage: $0 {start|stop|status|ensure|logs} [slm|vllm|gateway|all]"; exit 2 ;;
esac
