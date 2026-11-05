# AskIISc on a shared department GPU server - NO sudo
Everything lives in `$HOME/askiisc`, runs as you, uses high ports. Use this instead of RUNBOOK.md (which needs root/docker/systemd/ufw).
Process manager: `deploy/user/askiisc.sh`.  Settings: `~/askiisc/askiisc.env` (template: `deploy/user/askiisc.env.example`).

## 0. Look before you leap (shared machine!)
    whoami; hostname -I; python3 --version; df -h $HOME; which gcc cmake tmux crontab
    nvidia-smi                      # how much memory is FREE on which GPU? others may be using it
    ss -ltn | awk '{print $4}' | sort -u     # ports already taken (pick free high ports)
Ask the department admin for: (1) permission to host this service, (2) inbound TCP on your BIND_PORT from campus ranges only, (3) which GPU/how much memory you may use. You cannot do firewalling without root; the in-app IP gate (ALLOWED_CIDRS) is your only network control, so it must be set.

## 1. Folders and files
    mkdir -p ~/askiisc/{app,models,data,logs,run} && chmod 700 ~/askiisc
    # from the laptop:  scp askiisc_chat.zip router.joblib <you>@<server>:~/   (+ rsync models, see below)
    cd ~ && unzip -o askiisc_chat.zip -d ~/askiisc/ && rsync -a ~/askiisc/askiisc_chat/ ~/askiisc/app/ && rm -rf ~/askiisc/askiisc_chat
    cp ~/router.joblib ~/askiisc/app/models/router.joblib
    # laptop -> server models (embedder = YOUR real embedding model):
    rsync -avP Qwen2.5-7B-Instruct/ <you>@<server>:~/askiisc/models/Qwen2.5-7B-Instruct/
    rsync -avP qwen2.5-1.5b-instruct-q4_k_m.gguf <you>@<server>:~/askiisc/models/
    rsync -avP bge-small-en-v1.5/ <you>@<server>:~/askiisc/models/bge-small-en-v1.5/

## 2. Python environments (no root)
    python3 -m venv ~/askiisc/venv-gw        # if "ensurepip not available": install Miniforge into $HOME (bash Miniforge3-*.sh -b -p $HOME/miniforge3) and use its python
    ~/askiisc/venv-gw/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu
    ~/askiisc/venv-gw/bin/pip install -r ~/askiisc/app/requirements.txt
    python3 -m venv ~/askiisc/venv-vllm && ~/askiisc/venv-vllm/bin/pip install vllm       # vLLM wants Python 3.10-3.12
    ~/askiisc/venv-vllm/bin/python -c "import torch; print(torch.cuda.is_available())"     # True
Offline server: on the laptop `pip download -d wheels ...` (same OS+Python), copy, `pip install --no-index --find-links wheels ...`.

## 3. Redis path + password as environment variables (your teammate's Redis)
Get from the teammate (NOT over group chat/email in plain text; hand it over in person or via a one-time secret link): the `redis-cli`/redis-stack install folder, host+port, password, key prefix, index name.
Check how it is exposed:
    ss -ltn | grep -E ':6379|<their port>'      # must show 127.0.0.1:<port>. If it shows 0.0.0.0 or *, anyone on campus can reach it: tell them to set `bind 127.0.0.1` and `requirepass`.
Create a private secrets file (you cannot set a true machine-wide variable without root; each teammate does this for themselves):
    umask 077
    cat > ~/.askiisc_secrets <<'EOT'
    export REDIS_HOME=/home/<teammate>/<their-redis-folder>
    export PATH="$REDIS_HOME/bin:$PATH"
    export REDISCLI_AUTH='<password>'
    EOT
    chmod 600 ~/.askiisc_secrets
    echo '[ -f ~/.askiisc_secrets ] && . ~/.askiisc_secrets' >> ~/.bashrc && source ~/.bashrc
    redis-cli -h 127.0.0.1 -p <port> PING             # PONG    (REDISCLI_AUTH keeps the password out of history and `ps`)
    redis-cli -h 127.0.0.1 -p <port> MODULE LIST      # must list "search" if you want Redis vector search
If `REDIS_HOME/bin` is unreadable, the teammate must `chmod o+x` each folder on that path (or copy the binaries to you). Never `chmod -R 777`.
For the app, the same password goes into `~/askiisc/askiisc.env` as a URL (that file is chmod 600 and is what the process manager reads):
    REDIS_URL=redis://:<URL-ENCODED-PASSWORD>@127.0.0.1:<port>/0
    python3 -c "import urllib.parse,getpass;print(urllib.parse.quote(getpass.getpass(),safe=''))"     # encodes @ : / # etc.
Risks of sharing the teammate's Redis: if THEIR instance has an eviction policy (allkeys-lru) your chunks can be deleted under memory pressure; if THEY stop/restart it your app loses its store; RediSearch indexes only exist in database 0; use your own key prefix so you never collide. This is why I recommend step 4B.

## 4. Choose how the app reads chunks
**4A. Use the teammate's Redis Stack directly** (only if MODULE LIST shows `search` and the index exists)
    set in askiisc.env: RETRIEVER_BACKEND=redis, REDIS_URL=..., INDEX_NAME=<their index>, F_* = their field names
    cd ~/askiisc/app && set -a && . ~/askiisc/askiisc.env && set +a && PYTHONPATH=. ~/askiisc/venv-gw/bin/python deploy/redis_index.py check --prefix "<their prefix>" --verify-embedder
**4B. (recommended) Take a private copy: no dependency on anyone's Redis, no module needed**
    cd ~/askiisc/app && set -a && . ~/askiisc/askiisc.env && set +a
    PYTHONPATH=. ~/askiisc/venv-gw/bin/python deploy/export_chunks.py --prefix "<their prefix>" --out ~/askiisc/data/chunks.npz --default-access public
    PYTHONPATH=. ~/askiisc/venv-gw/bin/python deploy/check_chunks.py ~/askiisc/data/chunks.npz --verify-embedder
    set in askiisc.env: RETRIEVER_BACKEND=npz, CHUNKS_FILE=$HOME/askiisc/data/chunks.npz
Needs RETRIEVER_BACKEND unset/redis only while exporting. Expect `embedder-vs-stored cosine > 0.98`. MISMATCH = wrong EMBED_MODEL/EMBED_QUERY_PREFIX/EMBED_DIM: fix before anything else. (No Redis at all? Export on the laptop with the same script and `scp` chunks.npz.) The file is plain data: `chmod 600` it, and re-export when theses change.
Do not export embargoed/restricted theses while ACCESS_TIERS=0.

## 5. Small model on CPU (llama.cpp, no root)
Prebuilt: download a `llama-*-bin-ubuntu-x64.zip` release on the laptop, copy it, `unzip` into `~/askiisc/llama.cpp/`, set `LLAMA_BIN` to the `llama-server` inside (the zip's .so files sit beside it; if it can't find them: `export LD_LIBRARY_PATH=~/askiisc/llama.cpp`).
Or build if gcc+cmake exist: `git clone https://github.com/ggml-org/llama.cpp ~/askiisc/llama.cpp && cd ~/askiisc/llama.cpp && cmake -B build && cmake --build build --config Release -j4 --target llama-server` (LLAMA_BIN=.../build/bin/llama-server). `-j4`, not all cores: it is a shared machine.
Use only the cores you are allotted for SLM_THREADS.

## 6. GPU sharing
    nvidia-smi --query-gpu=index,memory.total,memory.used --format=csv
vLLM refuses to start if the free memory is below `VLLM_GPU_UTIL x total`. Example: 24 GB card, 6 GB used by others -> free 18 GB -> VLLM_GPU_UTIL=0.70 and lower VLLM_MAX_LEN (e.g. 16384). If someone later grabs memory, vLLM can crash with out-of-memory: that is the cost of a shared GPU. FP8 flags assume an RTX 4090-class card.

## 7. Fill the env file and start
    cp ~/askiisc/app/deploy/user/askiisc.env.example ~/askiisc/askiisc.env && chmod 600 ~/askiisc/askiisc.env && nano ~/askiisc/askiisc.env
    openssl rand -hex 24      # run 3x: LLM_KEY(=VLLM_API_KEY), SLM_KEY(=LLAMA_API_KEY), SESSION_SECRET
    ~/askiisc/app/deploy/user/askiisc.sh start all      # slm -> vllm (waits until ready) -> gateway
    ~/askiisc/app/deploy/user/askiisc.sh status
    ~/askiisc/app/deploy/user/askiisc.sh logs vllm
Keys are mandatory here: other users on the server can reach 127.0.0.1 ports. Ports clashing with someone else? The script refuses to start and tells you to change them.

## 8. Keep it alive without root
    crontab -e      # add:
    PATH=/usr/local/bin:/usr/bin:/bin
    @reboot   $HOME/askiisc/app/deploy/user/askiisc.sh start >> $HOME/askiisc/logs/boot.log 2>&1
    */5 * * * * $HOME/askiisc/app/deploy/user/askiisc.sh ensure
`ensure` restarts only services that died. If `crontab` is refused, use `tmux new -s askiisc` and run start inside it (survives logout, not a reboot: tell the admin).

## 9. Acceptance test (from a campus laptop)
    curl -s http://<BIND_HOST>:<BIND_PORT>/health
Browser: easy question -> SLM badge; "compare/why" question -> 7B badge; unknown topic -> "could not find"; guest Wi-Fi -> 403; stop vllm (`askiisc.sh stop vllm`) then a hard question -> falls back to SLM. `tail ~/askiisc/logs/traces.jsonl`.

## Troubleshooting
| Symptom | Cause |
|---|---|
| everyone 403 | ALLOWED_CIDRS unset/wrong, or admin has not opened the port (then connection just times out) |
| "unfilled <PLACEHOLDER>" | env file still has <...> |
| port already in use | someone else owns it: change *_PORT and the matching *_URL |
| vLLM dies at start | not enough FREE GPU memory: lower VLLM_GPU_UTIL / VLLM_MAX_LEN, or pick another GPU_ID |
| 0 sources / "could not find" | wrong prefix/index/F_* names, embedder mismatch (step 4), untagged chunks with ACCESS_TIERS=1 |
| SLM minutes per answer | lower SLM_TOP_K, raise SLM_THREADS (within your allotment), raise SLM_TIMEOUT |
