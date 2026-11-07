#!/usr/bin/env bash
# End-to-end test of the rootless deployment: fake HOME, fake llama-server/vllm, real gateway, npz retriever.
set -u; cd "$(dirname "$0")/.."; SRC="$PWD"; T="$(mktemp -d)"; export HOME="$T"; H="$T/askiisc"
mkdir -p "$H"/{app,models,data,llama.cpp,venv-gw/bin,venv-vllm/bin}; cp -r "$SRC/askiisc" "$SRC/static" "$H/app/"; mkdir -p "$H/app/models"
ln -s "$(command -v python3)" "$H/venv-gw/bin/python"
cat > "$H/llama.cpp/llama-server" <<EOS
#!/usr/bin/env bash
while [ \$# -gt 0 ]; do [ "\$1" = --port ] && P="\$2"; shift; done; exec python3 "$SRC/tests/stub_server.py" SLM LLAMA_API_KEY "\$P"
EOS
cat > "$H/venv-vllm/bin/vllm" <<EOS
#!/usr/bin/env bash
while [ \$# -gt 0 ]; do [ "\$1" = --port ] && P="\$2"; shift; done; exec python3 "$SRC/tests/stub_server.py" LLM VLLM_API_KEY "\$P"
EOS
chmod +x "$H/llama.cpp/llama-server" "$H/venv-vllm/bin/vllm"
read -r P1 P2 P3 < <(python3 -c "
import socket
s=[socket.socket() for _ in range(3)]
[x.bind(('127.0.0.1',0)) for x in s]; print(*[x.getsockname()[1] for x in s])")
(cd "$SRC" && PYTHONPATH=. python3 - <<PY
import json, numpy as np
from askiisc.embedder import hash_embed
rows=[("c0","T1","public","attention mechanism in transformers"),("c1","T1","restricted","embargoed patent claims on the sensor")]
np.savez_compressed("$H/data/chunks.npz", vectors=np.stack([hash_embed(r[3],64) for r in rows]),
  meta_json=np.frombuffer(json.dumps([{"id":r[0],"text":r[3],"thesis_id":r[1],"access":r[2],"title":"Th","chapter":"1","version":"1"} for r in rows]).encode(),dtype=np.uint8))
PY
)
cat > "$H/askiisc.env" <<EOE
BIND_HOST=127.0.0.1
BIND_PORT=$P3
ALLOWED_CIDRS=127.0.0.0/8
REQUIRE_LOGIN=0
ACCESS_TIERS=1
SESSION_SECRET=abc
RETRIEVER_BACKEND=npz
CHUNKS_FILE=\$HOME/askiisc/data/chunks.npz
EMBED_BACKEND=hash
EMBED_DIM=64
LLM_PORT=$P1
LLM_URL=http://127.0.0.1:$P1/v1
LLM_MODEL=Qwen/Qwen2.5-7B-Instruct
LLM_MODEL_PATH=\$HOME/askiisc/models/x
LLM_KEY=k1k1
VLLM_API_KEY=k1k1
GPU_ID=0
VLLM_GPU_UTIL=0.5
VLLM_MAX_LEN=4096
SLM_PORT=$P2
SLM_URL=http://127.0.0.1:$P2/v1
SLM_MODEL=Qwen/Qwen2.5-1.5B-Instruct
SLM_MODEL_PATH=\$HOME/askiisc/models/y
SLM_KEY=k2k2
LLAMA_API_KEY=k2k2
LLAMA_BIN=\$HOME/askiisc/llama.cpp/llama-server
SLM_CTX=4096
SLM_THREADS=1
ROUTER_PATH=\$HOME/askiisc/app/models/none.joblib
TRACE_FILE=\$HOME/askiisc/logs/traces.jsonl
EOE
S="$SRC/deploy/user/askiisc.sh"; ok=0; fail() { echo "FAIL: $*"; ok=1; }
"$S" start all >"$T/start.out" 2>&1 || { cat "$T/start.out"; fail "start all"; }
grep -q "gateway /health: {\"redis\":true,\"slm\":true,\"llm\":true" "$T/start.out" || { cat "$T/start.out"; fail "health not all true"; }
R=$(curl -s -N -X POST "http://127.0.0.1:$P3/chat" -H 'Content-Type: application/json' -d '{"message":"attention mechanism in transformers"}')
echo "$R" | grep -q "event: done" && echo "$R" | grep -q '"citations"' || fail "chat did not complete: $R"
R2=$(curl -s -N -X POST "http://127.0.0.1:$P3/chat" -H 'Content-Type: application/json' -d '{"message":"embargoed patent claims on the sensor"}')
echo "$R2" | grep -q "event: refused" || fail "restricted question not refused: $R2"
echo "$R$R2" | grep -q "embargoed patent claims on the sensor" && echo "$R" | grep -q "embargoed patent" && fail "restricted text leaked in public answer"
[ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$P1/v1/models)" = 401 ] || fail "vllm stub accepted keyless request"
"$S" start slm | grep -q "already running" || fail "start not idempotent"
kill -9 "$(cat "$H/run/vllm.pid")" 2>/dev/null; sleep 1
"$S" status | grep -q "vllm: running" || { fail "ensure did not restart vllm"; tail -n 8 "$H/logs/ensure.log"; tail -n 5 "$H/logs/vllm.log"; }
"$S" stop all >/dev/null; sleep 1; "$S" status | grep -c STOPPED | grep -q 3 || fail "stop all left something running"
sed -i 's/^LLM_KEY=k1k1/LLM_KEY=<hex1>/' "$H/askiisc.env"; "$S" status 2>&1 | grep -q "unfilled <PLACEHOLDER>" || fail "placeholder guard"
"$S" stop all >/dev/null 2>&1 || true
[ $ok = 0 ] && echo "ALL DEPLOY-SCRIPT CHECKS PASSED" || echo "SOME CHECKS FAILED"; exit $ok
