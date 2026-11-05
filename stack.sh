#!/usr/bin/env bash
# Start/stop the on-prem RAG gateway stack from the repo root (no sudo).  Usage: ./stack.sh {start|stop|restart|status|logs} [gpu|slm|gateway]
# Settings come from .env (add the STACK_* lines, see below). Services: gpu (7B), slm (3B CPU), gateway.
set -uo pipefail
cd "$(dirname "$0")"; mkdir -p logs run
[ -f .env ] || { echo "ERROR: .env not found in $(pwd)"; exit 1; }
set -a; . ./.env; set +a
CONDA_ENV="${CONDA_ENV-rag_env}"
if [ -n "$CONDA_ENV" ]; then
  CONDA_SH="${CONDA_SH:-$HOME/miniconda3/etc/profile.d/conda.sh}"
  [ -f "$CONDA_SH" ] || CONDA_SH="$(conda info --base 2>/dev/null)/etc/profile.d/conda.sh"
  [ -f "$CONDA_SH" ] || { echo "ERROR: conda.sh not found; set CONDA_SH=/path/to/conda.sh"; exit 1; }
  . "$CONDA_SH"; conda activate "$CONDA_ENV" || { echo "ERROR: cannot activate conda env $CONDA_ENV"; exit 1; }
fi
: "${STACK_GW_HOST:?set STACK_GW_HOST in .env (the intranet IP of this server - run hostname -I)}"
GPU_ID="${STACK_GPU_ID:-0}"; GPU_PORT="${STACK_GPU_PORT:-18000}"; SLM_PORT="${STACK_SLM_PORT:-18082}"
GW_PORT="${STACK_GW_PORT:-18080}"; SLM_THREADS="${STACK_SLM_THREADS:-8}"

pidf() { echo "run/$1.pid"; }
alive() { [ -f "$(pidf "$1")" ] && kill -0 "$(cat "$(pidf "$1")")" 2>/dev/null; }
port_busy() { python3 - "$1" "$2" <<'PY'
import socket, sys
s = socket.socket(); s.settimeout(0.5)
try: s.connect((sys.argv[1], int(sys.argv[2]))); print(1)
except OSError: print(0)
PY
}
health_url() { case "$1" in gpu) echo "http://127.0.0.1:$GPU_PORT/healthz";; slm) echo "http://127.0.0.1:$SLM_PORT/healthz";; gateway) echo "http://$STACK_GW_HOST:$GW_PORT/healthz";; esac; }
is_up() { local c; c="$(curl -s -o /dev/null -w '%{http_code}' "$(health_url "$1")" 2>/dev/null)"; [ "$c" = 200 ] || { [ "$1" = gateway ] && [ "$c" = 403 ]; }; }   # 403 = gateway is up but its network gate refused this source IP
wait_health() { for _ in $(seq 1 "$2"); do is_up "$1" && return 0; sleep 1; done; return 1; }
start_one() {
  local s="$1" port
  alive "$s" && { echo "$s already running (pid $(cat "$(pidf "$s")"))"; return 0; }
  local host=127.0.0.1
  case "$s" in gpu) port=$GPU_PORT;; slm) port=$SLM_PORT;; gateway) port=$GW_PORT; host=$STACK_GW_HOST;; esac
  [ "$(port_busy "$host" "$port")" = 1 ] && { echo "ERROR: port $port already in use (Rituj's stack or another user?). Change STACK_*_PORT in .env AND the matching URL variable in .env."; return 1; }
  grep -q "$port" .env || echo "WARNING: .env never mentions port $port - the gateway will not find the $s server unless its URL variable uses it."
  case "$s" in
    gpu)     CUDA_VISIBLE_DEVICES="$GPU_ID" setsid nohup python -m pipeline.serve_gpu_llm --host 127.0.0.1 --port "$GPU_PORT" --device cuda:0 >>logs/gpu.log 2>&1 < /dev/null & ;;
    slm)     setsid nohup python -m pipeline.serve_cpu_slm --host 127.0.0.1 --port "$SLM_PORT" --threads "$SLM_THREADS" >>logs/slm.log 2>&1 < /dev/null & ;;
    gateway) setsid nohup uvicorn gateway.main:app --host "$STACK_GW_HOST" --port "$GW_PORT" --workers 1 >>logs/gateway.log 2>&1 < /dev/null & ;;
  esac
  echo $! > "$(pidf "$s")"
  echo "started $s (pid $(cat "$(pidf "$s")")); waiting for health..."
  wait_health "$s" "$([ "$s" = gpu ] && echo 600 || echo 120)" && echo "  $s healthy" || { echo "  $s NOT healthy - see logs/$s.log"; return 1; }
}
stop_one() {
  local s="$1"; alive "$s" || { echo "$s not running"; rm -f "$(pidf "$s")"; return 0; }
  local pid; pid="$(cat "$(pidf "$s")")"; kill -- "-$pid" 2>/dev/null || kill "$pid" 2>/dev/null
  for _ in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
  kill -0 "$pid" 2>/dev/null && kill -9 -- "-$pid" 2>/dev/null; rm -f "$(pidf "$s")"; echo "stopped $s"
}
case "${1:-status}" in
  start)  if [ -n "${2:-}" ]; then start_one "$2"; else start_one gpu && start_one slm && start_one gateway; fi ;;
  stop)   if [ -n "${2:-}" ]; then stop_one "$2"; else for s in gateway slm gpu; do stop_one "$s"; done; fi ;;
  restart) [ -n "${2:-}" ] || { echo "usage: ./stack.sh restart gpu|slm|gateway"; exit 2; }; stop_one "$2"; start_one "$2" ;;
  status) for s in gpu slm gateway; do
            alive "$s" && echo "$s: running pid $(cat "$(pidf "$s")") health=$(curl -s -o /dev/null -w '%{http_code}' "$(health_url "$s")" 2>/dev/null)" || echo "$s: STOPPED"; done ;;
  logs)   tail -n "${3:-80}" -f "logs/${2:?usage: ./stack.sh logs gpu|slm|gateway}.log" ;;
  *) echo "usage: ./stack.sh {start|stop|restart|status|logs} [gpu|slm|gateway]"; exit 2 ;;
esac
