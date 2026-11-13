#!/usr/bin/env bash
# Verifies the red-team fix for the vLLM GPU server (port 8000):
#   - it listens on loopback only (vLLM 0.6.3 ignores --host unless started via scripts/serve_vllm.py)
#   - /v1/* rejects requests without the correct VLLM_API_KEY, and accepts the correct one
#   - it is unreachable through this machine's network IPs
# Usage: make vllm-check   (or: bash scripts/check_vllm_security.sh)
# Exit code: 0 = all checks pass, 1 = a check failed, 2 = vLLM not running / no key.
set -u
cd "$(dirname "$0")/.." || exit 2

PORT="${VLLM_PORT:-8000}"
KEY="${VLLM_API_KEY:-$(grep -E '^VLLM_API_KEY=' .env 2>/dev/null | cut -d= -f2-)}"
MODEL="${VLLM_MODEL_NAME:-Qwen/Qwen2.5-7B-Instruct}"
URL="http://127.0.0.1:${PORT}"
failed=0

ok()   { echo "  ✅ $1"; }
bad()  { echo "  ❌ $1"; failed=1; }
code() { curl -s -m 5 -o /dev/null -w '%{http_code}' "$@"; }
expect() { # expect <wanted-code> <description> <curl args...>
    local want="$1" desc="$2"; shift 2
    local got; got=$(code "$@")
    if [ "$got" = "$want" ]; then ok "$desc -> $got"; else bad "$desc -> $got (expected $want)"; fi
}

echo "vLLM security check (port ${PORT})"

listen=$(ss -ltnH "sport = :${PORT}" 2>/dev/null | awk '{print $4}' | sort -u)
if [ -z "$listen" ]; then
    echo "  ⚠️  nothing is listening on :${PORT} (vLLM is down). Start it with: make prod-up"
    exit 2
fi
for addr in $listen; do
    case "$addr" in
        "127.0.0.1:${PORT}" | "[::1]:${PORT}") ok "listening on ${addr} (loopback only)" ;;
        *) bad "listening on ${addr} - exposed to the network! Restart with: make prod-restart-vllm" ;;
    esac
done

if [ -z "$KEY" ]; then
    echo "  ⚠️  VLLM_API_KEY is not set in .env; cannot run the auth checks."
    exit 2
fi

CHAT_BODY="{\"model\":\"${MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: SAFE\"}],\"max_tokens\":4}"
expect 401 "GET  /v1/models without key"                "${URL}/v1/models"
expect 401 "POST /v1/chat/completions without key"      -X POST "${URL}/v1/chat/completions" -H 'Content-Type: application/json' -d "$CHAT_BODY"
expect 401 "GET  /v1/models with wrong key"             -H "Authorization: Bearer wrong-key" "${URL}/v1/models"
expect 200 "GET  /v1/models with VLLM_API_KEY"          -H "Authorization: Bearer ${KEY}" "${URL}/v1/models"

for ip in $(hostname -I 2>/dev/null); do
    case "$ip" in *:*) target="[${ip}]" ;; *) target="$ip" ;; esac
    got=$(code "http://${target}:${PORT}/v1/models")
    if [ "$got" = "000" ]; then ok "unreachable via ${ip}"; else bad "reachable via ${ip} -> HTTP ${got}"; fi
done

if [ "$failed" -eq 0 ]; then
    echo "RESULT: PASS - vLLM is loopback-only and requires the API key."
else
    echo "RESULT: FAIL - see ❌ lines above."
fi
exit "$failed"
