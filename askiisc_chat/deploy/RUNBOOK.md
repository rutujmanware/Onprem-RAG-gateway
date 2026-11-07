# AskIISc - host on the GPU server (auth-agnostic interim: network gate only)
Assumes Ubuntu/Debian with sudo, NVIDIA driver installed, ports bound to loopback except the gateway.
Replace <PLACEHOLDERS>. Run on the SERVER unless marked LAPTOP.

## 0. Know your box
    lsb_release -a; nvidia-smi; lscpu | grep -E "Model name|^CPU\(s\)|Thread|Core"; free -h; df -h /opt
    python3 --version; ip -4 addr | grep inet
    curl -sI --max-time 5 https://pypi.org | head -1        # no reply = server is offline: use the offline notes
vLLM wants Python 3.10-3.12. The 7B settings assume 24 GB VRAM and an RTX 4090; other GPUs need different flags.

## 1. User and folders
    sudo useradd --system --create-home --home-dir /opt/askiisc --shell /usr/sbin/nologin askiisc
    sudo mkdir -p /opt/askiisc/{app,models,redis-data,redis-cache} /etc/askiisc /var/lib/askiisc
    sudo chown -R askiisc:askiisc /opt/askiisc /var/lib/askiisc
    sudo apt-get update && sudo apt-get install -y python3-venv python3-pip build-essential cmake git unzip rsync

## 2. Copy artefacts from the laptops (LAPTOP)
    scp askiisc_chat.zip router.joblib <you>@<server>:/tmp/
    rsync -avP <path>/Qwen2.5-7B-Instruct/        <you>@<server>:/tmp/models/Qwen2.5-7B-Instruct/
    rsync -avP <path>/qwen2.5-1.5b-instruct-q4_k_m.gguf <you>@<server>:/tmp/models/
    rsync -avP <path>/bge-small-en-v1.5/          <you>@<server>:/tmp/models/bge-small-en-v1.5/   # your REAL embedding model
    redis-cli SAVE && redis-cli CONFIG GET dir      # then scp <dir>/dump.rdb <you>@<server>:/tmp/
Server:
    sudo unzip -o /tmp/askiisc_chat.zip -d /opt/askiisc/ && sudo rsync -a /opt/askiisc/askiisc_chat/ /opt/askiisc/app/ && sudo rm -rf /opt/askiisc/askiisc_chat
    sudo rsync -a /tmp/models/ /opt/askiisc/models/ && sudo cp /tmp/router.joblib /opt/askiisc/app/models/router.joblib
    sudo chown -R askiisc:askiisc /opt/askiisc

## 3. Redis Stack in RAM (plain Redis has NO vector search)
    export REDIS_PW=$(openssl rand -hex 24); echo $REDIS_PW      # save it; goes into REDIS_URL
    # Native bare-metal user-space binary:
    wget https://packages.redis.io/redis-stack/redis-stack-server-7.2.0-v9.jammy.x86_64.tar.gz
    tar -xzf redis-stack-server-7.2.0-v9.jammy.x86_64.tar.gz -C /opt/askiisc/
    ln -sf /opt/askiisc/redis-stack-server-7.2.0-v9.jammy.x86_64 /opt/askiisc/redis-stack

    # Start Redis Stack native daemon:
    /opt/askiisc/redis-stack/bin/redis-stack-server \
      --port 6379 \
      --requirepass $REDIS_PW \
      --dir /opt/askiisc/redis-data \
      --maxmemory 48gb \
      --maxmemory-policy noeviction \
      --daemonize yes

Load the data (laptop dump):
    redis-cli -p 6379 -a $REDIS_PW shutdown
    sudo cp /tmp/dump.rdb /opt/askiisc/redis-data/dump.rdb
    sudo chown askiisc:askiisc /opt/askiisc/redis-data/dump.rdb
    /opt/askiisc/redis-stack/bin/redis-stack-server --port 6379 --requirepass $REDIS_PW --dir /opt/askiisc/redis-data --daemonize yes
    redis-cli -a $REDIS_PW DBSIZE ; redis-cli -a $REDIS_PW FT._LIST
Do NOT enable --appendonly before loading: Redis then ignores dump.rdb and starts empty.
Alternative: keep your ingestion script and point it at the server through a tunnel: `ssh -L 6379:127.0.0.1:6379 <you>@<server>`.

## 4. Gateway venv (CPU only)
    sudo -u askiisc python3 -m venv /opt/askiisc/venv-gw
    sudo -u askiisc /opt/askiisc/venv-gw/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu
    sudo -u askiisc /opt/askiisc/venv-gw/bin/pip install -r /opt/askiisc/app/requirements.txt
Offline: LAPTOP `pip download -d wheels -r requirements.txt` (same OS + Python), copy `wheels/`, server `pip install --no-index --find-links wheels -r requirements.txt`.

## 5. Config file
    sudo cp /opt/askiisc/app/deploy/askiisc.env.example /etc/askiisc/askiisc.env
    sudo chown root:askiisc /etc/askiisc/askiisc.env && sudo chmod 640 /etc/askiisc/askiisc.env
    sudo nano /etc/askiisc/askiisc.env       # fill every <PLACEHOLDER>; LLM_KEY==VLLM_API_KEY, SLM_KEY==LLAMA_API_KEY
Lines must be KEY=value with NO trailing comments (systemd would put them in the value).

## 6. Verify the index and the embedding contract (the step that catches silent errors)
    sudo -u askiisc bash -c 'set -a; . /etc/askiisc/askiisc.env; set +a; cd /opt/askiisc/app; export PYTHONPATH=.
      PY=/opt/askiisc/venv-gw/bin/python
      $PY deploy/redis_index.py create --dry-run                 # shows the FT.CREATE it would run
      $PY deploy/redis_index.py create --prefix "chunk:"        # ONLY if FT._LIST above was empty; use your real key prefix
      $PY deploy/redis_index.py backfill --access public         # ONLY if ACCESS_TIERS=1 and chunks lack the access field
      $PY deploy/redis_index.py check --prefix "chunk:" --verify-embedder'
Expect: num_docs > 0, vector dim OK, self-KNN distance ~0, embedder-vs-stored cosine > 0.98. A MISMATCH means EMBED_MODEL/EMBED_QUERY_PREFIX/F_* differ from what built the index: fix BEFORE going further.
Do not load embargoed theses while ACCESS_TIERS=0.

## 7. SLM on CPU (llama.cpp)
    sudo -u askiisc git clone https://github.com/ggml-org/llama.cpp /opt/askiisc/llama.cpp     # offline: copy a source tarball
    cd /opt/askiisc/llama.cpp && sudo -u askiisc cmake -B build && sudo -u askiisc cmake --build build --config Release -j"$(nproc)" --target llama-server
Foreground test:
    sudo -u askiisc bash -c 'set -a; . /etc/askiisc/askiisc.env; set +a
      /opt/askiisc/llama.cpp/build/bin/llama-server -m $SLM_MODEL_PATH --alias $SLM_MODEL --host 127.0.0.1 --port 8082 -c $SLM_CTX --parallel 2 -t $SLM_THREADS'
Another shell:
    curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8082/v1/models                       # 401 = key enforced
    curl -s http://127.0.0.1:8082/v1/chat/completions -H "Authorization: Bearer <SLM_KEY>" -H 'Content-Type: application/json' \
      -d '{"model":"Qwen/Qwen2.5-3B-Instruct","messages":[{"role":"user","content":"Say hi"}],"logprobs":true,"max_tokens":8}'
If the reply has no "logprobs", the escalate-on-low-confidence feature silently never triggers: set CASCADE=0 or upgrade llama.cpp. Ctrl-C when done.

## 8. 7B on GPU (vLLM)
    sudo -u askiisc python3 -m venv /opt/askiisc/venv-vllm && sudo -u askiisc /opt/askiisc/venv-vllm/bin/pip install vllm
    sudo -u askiisc /opt/askiisc/venv-vllm/bin/python -c "import torch; print(torch.cuda.is_available())"      # must print True
Foreground test (first load takes minutes; wait for "Application startup complete"):
    sudo -u askiisc bash -c 'set -a; . /etc/askiisc/askiisc.env; set +a; CUDA_VISIBLE_DEVICES=0 VLLM_NO_USAGE_STATS=1 /opt/askiisc/venv-vllm/bin/vllm serve $LLM_MODEL_PATH --served-model-name $LLM_MODEL --quantization fp8 --kv-cache-dtype fp8 --max-model-len 32768 --gpu-memory-utilization 0.92 --max-num-seqs 32 --enable-prefix-caching --host 127.0.0.1 --port 8000'
    curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/v1/models                       # 401
    curl -s http://127.0.0.1:8000/v1/models -H "Authorization: Bearer <LLM_KEY>" | head -c 300
OOM / won't start: lower --max-model-len (16384) or --gpu-memory-utilization (0.85). Ctrl-C when done.

## 9. Run as services
    sudo cp /opt/askiisc/app/deploy/systemd/*.service /etc/systemd/system/ && sudo systemctl daemon-reload
    sudo systemctl enable --now askiisc-slm askiisc-vllm
    journalctl -u askiisc-vllm -f              # until startup complete, then Ctrl-C
    sudo systemctl enable --now askiisc-gateway
    systemctl --no-pager status askiisc-slm askiisc-vllm askiisc-gateway | grep -E "●|Active"

## 10. Firewall (SSH first, or you lock yourself out)
    sudo ufw allow OpenSSH            # tighten to your admin IP later
    sudo ufw allow from <ALLOWED_CIDR> to any port 9000 proto tcp
    sudo ufw default deny incoming && sudo ufw enable
Redis/vLLM/llama.cpp listen on 127.0.0.1 only (internal loopback only, never exposed directly to external networks).

## 11. Acceptance test (from a campus laptop)
    curl -s http://<BIND_HOST>:9000/health          # redis/slm/llm all true, router_model_loaded true
Browser http://<BIND_HOST>:9000 and ask: (1) an easy factual question -> SLM badge; (2) a "compare / why / explain" question -> 7B badge; (3) a question not in any thesis -> "could not find"; (4) from a phone on guest Wi-Fi -> must fail (403); (5) kill the 7B (`sudo systemctl stop askiisc-vllm`), ask a hard question -> fallback to SLM, then start it again.
    tail -n 5 /var/lib/askiisc/traces.jsonl ; nvidia-smi ; journalctl -u askiisc-gateway -n 50

## Troubleshooting
| Symptom | Cause |
|---|---|
| everyone gets 403 | ALLOWED_CIDRS wrong/unset (default loopback only) or you are behind a proxy (TRUSTED_PROXIES) |
| answers say "could not find" / 0 sources | wrong INDEX_NAME/F_* names; embedder mismatch (step 6); ACCESS_TIERS=1 with untagged chunks |
| SLM answers take minutes / time out | prompt too long for CPU: lower SLM_TOP_K, raise SLM_THREADS, raise SLM_TIMEOUT |
| vLLM "CUDA out of memory" | something else holds the GPU (gateway must have CUDA_VISIBLE_DEVICES=-1), or lower --gpu-memory-utilization |
| gateway starts then every request errors | env file has trailing comments or unfilled <PLACEHOLDER> |
