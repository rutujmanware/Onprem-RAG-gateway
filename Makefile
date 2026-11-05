.PHONY: help setup dev prod-up prod-down prod-status prod-restart redis-up redis-down redis-status redis-watch up down logs ingest-pdfs query ask rag-answer serve-slm serve-gpu serve-router generate-dataset eval test clean

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
	@echo "  make dev            - Run Gateway service locally on port 8080 with live reload"
	@echo "  make redis-up       - Start Redis Stack daemon"
	@echo "  make redis-down     - Stop Redis Stack daemon"
	@echo "  make redis-status   - Check Redis loading / persistence status (one-shot)"
	@echo "  make redis-watch    - Live interactive monitor of 12GB dataset loading with progress bar & ETA"
	@echo "  make up             - Start full docker-compose stack (Redis, SLM, vLLM, Gateway)"
	@echo "  make down           - Stop all docker services"
	@echo "  make logs           - Tail docker service logs"
	@echo "  make ingest-pdfs    - Extract, clean, chunk, embed, and index all PDFs in data/raw_pdfs/"
	@echo "  make query Q=\"...\"  - Perform semantic search query against the Redis vector index"
	@echo "  make ask Q=\"...\"    - Query the running Gateway with clean formatted answer output"
	@echo "  make rag-answer Q=\"...\" - Answer query using retrieved context + dual-tier routing"
	@echo "  make serve-slm      - Start Qwen 2.5 3B CPU SLM server on port 8082"
	@echo "  make serve-gpu      - Start Qwen 2.5 7B GPU LLM server on port 8000"
	@echo "  make serve-router   - Start MLP Router microservice on port 8090"
	@echo "  make generate-dataset - Generate synthetic multi-chunk dataset with scripts/dataset_generator.py"
	@echo "  make eval           - Run offline benchmarks (alpha-sweep, ragas, load tests)"
	@echo "  make test           - Run integration & security test suites"
	@echo "  make clean          - Remove caches and build artifacts"

setup:
	$(PYTHON) -m venv $(VENV)
	./$(VENV)/bin/pip install --upgrade pip
	./$(VENV)/bin/pip install -r requirements.txt
	@echo "Setup complete. Activate with: source $(VENV)/bin/activate"

dev:
	uvicorn gateway.main:app --host 0.0.0.0 --port 8080 --reload

prod-up:
	$(PYTHON) scripts/prod_stack.py start

prod-down:
	$(PYTHON) scripts/prod_stack.py stop

prod-status:
	$(PYTHON) scripts/prod_stack.py status

prod-restart:
	$(PYTHON) scripts/prod_stack.py restart

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

up:
	docker run -d --name onprem-rag-redis \
		-p 6379:6379 -p 8001:8001 \
		-v redis_data:/data \
		redis/redis-stack:7.2.0-v9 || docker start onprem-rag-redis

down:
	docker stop onprem-rag-redis || true

logs:
	docker logs -f onprem-rag-redis

ingest-pdfs:
	$(PYTHON) -m pipeline.batch_ingest_pdfs --pdf-dir data/raw_pdfs

query:
	$(PYTHON) -m pipeline.retriever "$(Q)"

ask:
	$(PYTHON) scripts/query.py "$(Q)"

rag-answer:
	$(PYTHON) -m pipeline.rag_chain "$(Q)"

serve-slm:
	$(PYTHON) -m pipeline.serve_cpu_slm --port 8082

serve-gpu:
	CUDA_VISIBLE_DEVICES=0 $(PYTHON) -m vllm.entrypoints.openai.api_server \
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
	$(PYTHON) -m evaluation.benchmarks.alpha_sweep
	$(PYTHON) -m evaluation.benchmarks.ragas_eval

test:
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
