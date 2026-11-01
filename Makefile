.PHONY: help setup dev redis-up redis-down redis-status up down logs ingest ingest-pdfs query rag-answer serve-slm eval test clean

PYTHON ?= python3
VENV ?= .venv

help:
	@echo "Available commands:"
	@echo "  make setup        - Create virtualenv and install dependencies"
	@echo "  make dev          - Run Gateway service locally with live reload"
	@echo "  make redis-up     - Start Redis Stack daemon"
	@echo "  make redis-down   - Stop Redis Stack daemon"
	@echo "  make redis-status - Check Redis loading / persistence status"
	@echo "  make up           - Start full docker-compose stack (Redis, SLM, vLLM, Gateway)"
	@echo "  make down         - Stop all docker services"
	@echo "  make logs         - Tail docker service logs"
	@echo "  make ingest-pdfs  - Extract, clean, chunk, embed, and index all PDFs in data/raw_pdfs/"
	@echo "  make query Q=\"...\" - Perform semantic search query against the Redis vector index"
	@echo "  make rag-answer Q=\"...\" - Answer query using retrieved context + Qwen 2.5 3B on CPU"
	@echo "  make serve-slm    - Start Qwen 2.5 3B CPU SLM FastAPI server on port 8082"
	@echo "  make eval         - Run offline benchmarks (alpha-sweep, ragas, load tests)"
	@echo "  make test         - Run integration & security test suites"
	@echo "  make clean        - Remove caches and build artifacts"

setup:
	$(PYTHON) -m venv $(VENV)
	./$(VENV)/bin/pip install --upgrade pip
	./$(VENV)/bin/pip install -r requirements.txt
	@echo "Setup complete. Activate with: source $(VENV)/bin/activate"

dev:
	uvicorn gateway.main:app --host 0.0.0.0 --port 8080 --reload

redis-up:
	./redis-stack-server-7.2.0-v9/bin/redis-stack-server
	@echo "Redis Stack daemon started."

redis-down:
	@REDISCLI_AUTH='Redis*2703!' ./redis-stack-server-7.2.0-v9/bin/redis-cli shutdown || true
	@echo "Redis Stack stopped."

redis-status:
	@REDISCLI_AUTH='Redis*2703!' ./redis-stack-server-7.2.0-v9/bin/redis-cli info Persistence | grep -E "loading:|loading_loaded_perc:|rdb_last_bgsave_status:" || echo "Redis is not running."

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

rag-answer:
	$(PYTHON) -m pipeline.rag_chain "$(Q)"

serve-slm:
	$(PYTHON) -m pipeline.serve_cpu_slm --port 8082

eval:
	$(PYTHON) -m evaluation.benchmarks.alpha_sweep
	$(PYTHON) -m evaluation.benchmarks.ragas_eval

test:
	PYTHONPATH=. $(PYTHON) tests/test_injection_fence.py
	PYTHONPATH=. $(PYTHON) tests/test_rate_limiting.py
	PYTHONPATH=. $(PYTHON) tests/test_cache_hits.py

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	rm -rf .pytest_cache .coverage
