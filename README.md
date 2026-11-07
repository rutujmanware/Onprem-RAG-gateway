# On-Premise Institutional RAG Gateway

Institutional, zero-egress RAG routing and admission gateway with neural complexity routing, CPU/GPU dynamic dispatch, admission control, and a Redis Stack vector database.

> 📖 **Master Engineering & Progress Log:** See [PROJECT_PROGRESS_AND_ARCHITECTURE.md](PROJECT_PROGRESS_AND_ARCHITECTURE.md) for full project history, architecture modules, technical challenges & solutions, CPU SLM optimizations, and living roadmap.

## 📌 Current Project Status (Building Phase)
We are currently in the **Data Ingestion and Vectorization Phase**. 
- Over **12,900+ research papers** have been successfully scraped from DSpace.
- These PDFs have been processed, embedded, and indexed into exactly **2.26 million vector chunks**.
- The entire vector index is stored in a compressed backup file (`dump_final.rdb`), which inflates to ~43 GB of RAM when loaded into Redis.


---

## 🛠️ 1. Initial Setup (For Teammates)

If you are joining the project and starting fresh on the server, follow these steps to set up your environment:

### Create Virtual Environment & Install Requirements
We use a `Makefile` to handle dependencies. Run this once:
```bash
make setup
```

### Activate the Environment
You must activate the Python environment every time you open a new terminal before running scripts:
```bash
source .venv/bin/activate
```

### Download Required AI Models
Automatically provision and download all required local offline models (Dense Embedder, 3B CPU SLM GGUF, and 7B GPU weights) into their designated locations under `models/`:
```bash
make download-models
```
*(You can also download specific models individually: `python scripts/download_models.py --embeddings`, `--slm`, or `--gpu-llm`).*

### Configure the Environment Variables
1. Copy the template to create your local `.env` file:
   ```bash
   cp .env.example .env
   ```
2. Open `.env` and configure your Redis credentials. Make sure you use the team's password (e.g. `<YOUR_REDIS_PASSWORD>`):
   ```env
   REDIS_PASSWORD=<YOUR_REDIS_PASSWORD>
   REDIS_URL=redis://:<YOUR_REDIS_PASSWORD>@localhost:6379/0
   ```

*(Note: If you run scripts interactively, ensure the `.env` variables are loaded into your terminal session by running: `set -a && source .env && set +a`)*

---

## 🗄️ 2. Database Initialization (Redis Stack)

We are running a standalone, user-space binary of Redis Stack on the server (no `sudo` or Docker required). 

### How to Load the Existing Database
Because the database is massive (~43 GB in RAM), we keep it shut down when not in use to free up server resources. To load the 12 GB `dump_final.rdb` backup back into Redis:

1. **Move the backup into the active Redis directory:**
   ```bash
   cp ./redis_server_data/dump_final.rdb ./redis-stack-server-7.2.0-v9/var/db/redis-stack/dump.rdb
   ```

2. **Start the Redis Server in the background:**
   ```bash
   ./redis-stack-server-7.2.0-v9/bin/redis-stack-server \
       --dir ./redis-stack-server-7.2.0-v9/var/db/redis-stack \
       --requirepass "<YOUR_REDIS_PASSWORD>" \
       --daemonize yes
   ```

3. **Check Loading Status:**
   Redis takes a few moments to decompress the 12GB file back into 43GB of RAM. Watch its progress:
   ```bash
   watch -n 1 "redis-cli info Persistence | grep loading"
   ```
   *(Wait until it says `loading:0` before running any scripts or queries).*

### Safely Shutting Down Redis
To free up the 43 GB of server RAM when you are done working for the day:
```bash
redis-cli shutdown nosave
```

---

## 🕷️ 3. Resuming the Scraper

If you want to continue scraping DSpace where we left off, you do not need to figure out page offsets! The scraper script has **built-in resume support**. It checks the `data/raw_pdfs/` directory for existing JSON metadata and instantly skips papers we already have.

**To resume scraping:**
1. Open a `tmux` session so the script doesn't die when you close your laptop.
2. Ensure your `.env` is loaded: `set -a && source .env && set +a`
3. Run the script:
   ```bash
   python3 pipeline/crawlers/stream_dspace.py
   ```

**Pro-Tip on Concurrency:**
The script has a `--concurrency` flag (default `1`). You can increase this to `3` or `5` to download and index multiple PDFs in parallel, but do not set it too high to avoid crashing the server's RAM or getting IP banned.

---

## 📊 4. Useful Commands

**Check Total Documents Scraped:**
```bash
find data/ -name "*.json" | wc -l
```

**Check Redis Database Size (Number of vector chunks):**
```bash
redis-cli dbsize
```

**Check Redis RAM Usage:**
```bash
redis-cli info memory | grep used_memory_human
```

**Lock Down Your Folder Permissions (Security):**
Make sure others on the shared server cannot read your `.env` password or database backup!
```bash
chmod 700 /home/user
```

---

## 📂 5. Project Directory Structure

```
onprem-rag-gateway/
├── configs/            # YAML configuration files (gateway, SLM, vLLM)
├── data/               # Vector features, augmentations, and historical archives
├── docs/               # Technical specs & CPU SLM optimization guides
├── gateway/            # FastAPI gateway, auth, cache, rate limits, and MLP router
├── logs/               # Application, cluster, and dataset generation logs
├── models/             # Local embedding models, 7B GPU weights, and 3B SLM GGUFs
├── pipeline/           # Hybrid retriever, CPU SLM, GPU LLM, and RAG chain
├── scripts/            # Synthetic dataset generator and Slurm cluster batch scripts
├── tests/              # Automated unit, cache, rate limit, and security tests
├── Makefile            # Central operational commands and service runbooks
├── PROJECT_PROGRESS_AND_ARCHITECTURE.md  # Master engineering changelog
└── README.md           # Quickstart and overview documentation
```

For advanced performance tuning and CPU SLM benchmarks, see [docs/OPTIMIZATION_GUIDE.md](docs/OPTIMIZATION_GUIDE.md).
For microservice port topologies and routing matrices, see [PROJECT_PROGRESS_AND_ARCHITECTURE.md](PROJECT_PROGRESS_AND_ARCHITECTURE.md).

---

## 💬 6. Querying the Gateway

Once the stack is running, you can query the system using any of the following methods:

### Method A: One-Liner CLI Query (Formatted Output)
```bash
make ask Q="What is vermiculite and what are its primary uses?"
```
*(Or via python directly)*:
```bash
python scripts/query.py "Compare stability of fluid flows with magnetic field interactions."
```

### Method B: Interactive Terminal Chat Loop
Launch an interactive Q&A session:
```bash
python scripts/query.py
```

### Method C: Standard HTTP `curl`
```bash
curl -s -X POST http://127.0.0.1:8080/query \
  -H "Content-Type: application/json" \
  -d '{"query": "What is vermiculite and what are its primary uses?"}' | jq -r .answer
```
