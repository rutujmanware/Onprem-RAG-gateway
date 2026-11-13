.PHONY: help setup download-models dev prod-up prod-down prod-status prod-restart prod-restart-vllm prod-down-vllm vllm-check chat-up chat-down chat-restart chat-status chat-logs chat-code test-smtp cache-stats cache-clear otp-clear queue-stats redis-up redis-down redis-status redis-watch up down logs ingest-pdfs query ask rag-answer serve-slm serve-gpu serve-router generate-dataset eval load-test load-test-cold test test-intensive clean

-include .env
export

CONDA_ENV_PYTHON := $(if $(CONDA_PREFIX),$(CONDA_PREFIX)/bin/python,/data2/home/ritujmanware/miniconda3/envs/rag_env/bin/python)
PYTHON ?= $(if $(wildcard $(CONDA_ENV_PYTHON)),$(CONDA_ENV_PYTHON),python3)
VENV ?= .venv
REDIS_PASSWORD ?= $(shell grep -E '^REDIS_PASSWORD=' .env 2>/dev/null | cut -d '=' -f2-)
MODEL_7B_PATH ?= models/llm_7b/qwen2.5-7b-instruct

help:
	@echo "Available commands:"
	@echo "  make setup          - Create virtualenv and install dependencies"
	@echo "  make download-models - Download AI models to models/ (Embeddings, CPU SLM, GPU 7B)"
	@echo "  make dev            - Run Gateway service locally on port 8080 with live reload"
	@echo "  make prod-up        - Start production stack (Gateway 6 workers, CPU SLM 6x16, vLLM, Redis)"
	@echo "  make prod-status    - Display status and health of all production services"
	@echo "  make prod-restart   - Restart production stack with optimized high-concurrency settings"
	@echo "  make prod-restart-vllm - Same, but also restart vLLM via the hardened launcher (slow reload)"
	@echo "  make prod-down      - Gracefully terminate all background services (preserves Redis data and vLLM)"
	@echo "  make prod-down-vllm - Same, but also stop the vLLM GPU engine"
	@echo "  make vllm-check     - Verify vLLM is loopback-only and requires VLLM_API_KEY (red-team fix)"
	@echo "  make chat-restart   - Restart Unified Frontend Gateway on port 8080 (6 workers)"
	@echo "  make cache-stats    - View semantic cache item count and vector index status"
	@echo "  make cache-clear    - Clear semantic cache (keeps 2.26M chunk vector index intact)"
	@echo "  make otp-clear [EMAIL=...] - Clear OTP rate limits (for a specific user or all)"
	@echo "  make queue-stats    - View admission queue depth, active inferences, and shedding telemetry"
	@echo "  make redis-up       - Start Redis Stack daemon"
	@echo "  make redis-down     - Stop Redis Stack daemon"
	@echo "  make redis-status   - Check Redis loading / persistence status (one-shot)"
	@echo "  make redis-watch    - Live interactive monitor of 12GB dataset loading with progress bar & ETA"
	@echo "  make up             - Start production on-prem stack (Redis, SLM, vLLM, Gateway)"
	@echo "  make down           - Stop all background compute services"
	@echo "  make logs           - Tail gateway service logs"
	@echo "  make ingest-pdfs    - Extract, clean, chunk, embed, and index all PDFs in data/raw_pdfs/"
	@echo "  make query Q=\"...\"  - Perform semantic search query against the Redis vector index"
	@echo "  make ask Q=\"...\"    - Query the running Gateway with clean formatted answer output"
	@echo "  make rag-answer Q=\"...\" - Answer query using retrieved context + dual-tier routing"
	@echo "  make serve-slm      - Start Qwen 2.5 3B CPU SLM server (6 workers x 16 threads = 96 cores)"
	@echo "  make serve-gpu      - Start Qwen 2.5 7B vLLM on 127.0.0.1:8000 (GPU = VLLM_GPU, needs VLLM_API_KEY)"
	@echo "  make serve-router   - Start MLP Router microservice on port 8090"
	@echo "  make generate-dataset - Generate synthetic multi-chunk dataset with scripts/dataset_generator.py"
	@echo "  make eval           - Run offline router complexity & threshold evaluation"
	@echo "  make load-test      - Run mixed concurrency load test against running gateway"
	@echo "  make load-test-cold - Run 100 uncached cold requests load test (0% cache hits) under C=10"
	@echo "  make test           - Run integration, security & admission queue test suites"
	@echo "  make clean          - Remove caches and build artifacts"

setup:
	$(PYTHON) -m venv $(VENV)
	./$(VENV)/bin/pip install --upgrade pip
	./$(VENV)/bin/pip install -r requirements.txt
	@echo "Setup complete. Activate with: source $(VENV)/bin/activate"

download-models:
	$(PYTHON) scripts/download_models.py

dev:
	uvicorn gateway.main:app --host 0.0.0.0 --port 8080 --reload

prod-up:
	$(PYTHON) scripts/prod_stack.py start

prod-down:
	$(PYTHON) scripts/prod_stack.py stop

prod-down-vllm:
	$(PYTHON) scripts/prod_stack.py stop --with-vllm

prod-status:
	$(PYTHON) scripts/prod_stack.py status

prod-restart:
	$(PYTHON) scripts/prod_stack.py restart

# Also restarts the vLLM GPU engine (slow model reload). Needed after vLLM launch/security changes.
prod-restart-vllm:
	$(PYTHON) scripts/prod_stack.py restart --with-vllm
	@echo "vLLM is reloading the model; once ready, verify with: make vllm-check"

# Red-team fix check: loopback-only bind + API key enforced on /v1/*.
vllm-check:
	@bash scripts/check_vllm_security.sh

chat-up:
	$(PYTHON) scripts/prod_stack.py start
	@echo "Unified AskIISc Frontend & Gateway running on port 8080 (6 workers)."

chat-down:
	$(PYTHON) scripts/prod_stack.py stop
	@echo "Frontend on port 8080 stopped."

chat-restart:
	$(PYTHON) scripts/prod_stack.py restart
	@echo "Unified AskIISc Frontend & Gateway restarted on port 8080 (6 workers)."

chat-status:
	@curl -s http://127.0.0.1:8080/health && echo " -> Gateway (Port 8080) UP" || echo "Gateway (Port 8080) STOPPED"

chat-logs:
	tail -n 80 -f logs/gateway.log

chat-code:
	cd askiisc_chat && ./app.sh code

test-smtp:
	$(PYTHON) scripts/test_smtp.py $(TO)

cache-stats:
	@curl -s http://127.0.0.1:8080/cache/stats || echo "Gateway is not running on 8080"

cache-clear:
	@curl -s -X POST http://127.0.0.1:8080/cache/clear
	@echo " -> Semantic cache cleared successfully. Document vector index (2.26M chunks) is untouched."

otp-clear:
	@$(PYTHON) scripts/clear_otp_limits.py $(if $(EMAIL),$(EMAIL),--all)

queue-stats:
	@curl -s http://127.0.0.1:8080/queue/stats || echo "Gateway is not running on 8080"

redis-up:
	./redis-stack-server-7.2.0-v9/bin/redis-stack-server
	@echo "Redis Stack daemon started."

redis-down:
	@REDISCLI_AUTH="$(REDIS_PASSWORD)" ./redis-stack-server-7.2.0-v9/bin/redis-cli shutdown 2>/dev/null || ./redis-stack-server-7.2.0-v9/bin/redis-cli shutdown 2>/dev/null || true
	@echo "Redis Stack stopped."

redis-status:
	@REDISCLI_AUTH="$(REDIS_PASSWORD)" ./redis-stack-server-7.2.0-v9/bin/redis-cli info Persistence 2>/dev/null | grep -E "loading:|loading_loaded_perc:|loading_eta_seconds:|rdb_last_bgsave_status:" || echo "Redis is not running."

redis-watch:
	@$(PYTHON) scripts/track_redis.py

up: prod-up

down: prod-down

logs:
	tail -n 100 -f logs/gateway.log

ingest-pdfs:
	$(PYTHON) -m pipeline.batch_ingest_pdfs --pdf-dir data/raw_pdfs

query:
	$(PYTHON) -m pipeline.retriever "$(Q)"

ask:
	$(PYTHON) scripts/query.py "$(Q)"

rag-answer:
	$(PYTHON) -m pipeline.rag_chain "$(Q)"

serve-slm:
	$(PYTHON) -m pipeline.serve_cpu_slm --port 8082 --workers 6 --threads 16

# Always launch vLLM through scripts/serve_vllm.py: stock vLLM 0.6.3 ignores --host and binds 0.0.0.0.
serve-gpu:
	CUDA_VISIBLE_DEVICES=$(or $(VLLM_GPU),0) $(PYTHON) scripts/serve_vllm.py \
		--model $(MODEL_7B_PATH) \
		--served-model-name "Qwen/Qwen2.5-7B-Instruct" \
		--host 127.0.0.1 \
		--port 8000 \
		--gpu-memory-utilization 0.45 \
		--max-model-len 8192 \
		--dtype bfloat16

serve-router:
	$(PYTHON) -m gateway.router.mlp_router.serve_router --port 8090

generate-dataset:
	$(PYTHON) scripts/dataset_generator.py

eval:
	$(PYTHON) scripts/eval_router_threshold.py

load-test:
	PYTHONPATH=. $(PYTHON) -m scripts.load_test

load-test-cold:
	PYTHONPATH=. $(PYTHON) scripts/run_100_uncached_load_test.py

test:
	PYTHONPATH=. $(PYTHON) tests/test_admission_queue.py
	PYTHONPATH=. $(PYTHON) -m pytest tests/test_single_port_security.py
	PYTHONPATH=. $(PYTHON) -m pytest tests/test_vllm_launcher.py
	PYTHONPATH=. $(PYTHON) -m pytest tests/test_cache_intent_guard.py tests/test_cache_intent_guard_extensive.py
	PYTHONPATH=. $(PYTHON) tests/test_injection_fence.py
	PYTHONPATH=. $(PYTHON) tests/test_rate_limiting.py
	PYTHONPATH=. $(PYTHON) tests/test_cache_hits.py
	PYTHONPATH=. $(PYTHON) -m unittest tests/test_cache_insufficient_data.py
	PYTHONPATH=. $(PYTHON) -m unittest tests/test_dynamic_retrieval.py
	PYTHONPATH=. $(PYTHON) -m unittest tests/test_query_metadata_extractor.py

test-intensive:
	PYTHONPATH=. $(PYTHON) tests/test_intensive_full_stack.py

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	rm -rf .pytest_cache .coverage
