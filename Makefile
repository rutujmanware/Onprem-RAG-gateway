.PHONY: help setup dev restart redis-up redis-down up down logs download-slm provision-embedding provision-llm ingest ingest-pdfs query train-router eval test clean

ifneq (,$(wildcard ./.env))
    include .env
    export
endif

PYTHON ?= $(shell if [ -f "/data/home/vivekkumar/miniconda/bin/python3" ]; then echo "/data/home/vivekkumar/miniconda/bin/python3"; elif command -v python3 >/dev/null 2>&1; then command -v python3; else echo "./.venv/bin/python"; fi)
VENV ?= .venv

help:
	@echo "Available commands:"
	@echo "  make setup        - Create virtualenv and install dependencies"
	@echo "  make dev          - Run Gateway service locally with live reload"
	@echo "  make restart      - Stop existing processes, start Redis, and restart Gateway"
	@echo "  make redis-up     - Start Redis Stack (Docker if permitted, or standalone binary)"
	@echo "  make redis-down   - Stop Redis Stack"
	@echo "  make up           - Start services (Docker if permitted, or standalone Redis + Gateway)"
	@echo "  make down         - Stop all running services (Docker and standalone)"
	@echo "  make logs         - Tail docker service logs"
	@echo "  make download-slm - Download Hugging Face SLM GGUF model for CPU inference"
	@echo "  make provision-embedding - Download the embedding model during provisioning"
	@echo "  make provision-llm - Download the Qwen 7B GPU model during provisioning"
	@echo "  make ingest-pdfs  - Extract, clean, chunk, embed, and index all PDFs in data/raw_pdfs/"
	@echo "  make query Q=\"...\" - Perform semantic search query against the Redis vector index"
	@echo "  make train-router - Train and export MLP complexity router to ONNX"
	@echo "  make eval         - Run offline benchmarks (alpha-sweep, ragas, load tests)"
	@echo "  make test         - Run integration & security test suites"
	@echo "  make clean        - Remove caches and build artifacts"


setup:
	python3 -m venv $(VENV)
	./$(VENV)/bin/pip install --upgrade pip
	./$(VENV)/bin/pip install -r requirements.txt
	@echo "Setup complete. Activate with: source $(VENV)/bin/activate"

dev:
	@fuser -k 8080/tcp 2>/dev/null || true
	@while fuser 8080/tcp >/dev/null 2>&1; do sleep 0.3; done
	$(PYTHON) -m uvicorn gateway.main:app --host 0.0.0.0 --port 8080 --reload

restart:
	@echo "Stopping existing gateway and Redis processes..."
	@fuser -k 8080/tcp 2>/dev/null || true
	@while fuser 8080/tcp >/dev/null 2>&1; do sleep 0.3; done
	@$(MAKE) redis-down
	@sleep 1
	@echo "Starting Redis..."
	@$(MAKE) redis-up
	@echo "Starting Gateway..."
	@$(MAKE) dev

redis-up:
	@if docker info >/dev/null 2>&1; then \
		docker compose up -d redis && echo "Redis Stack is up via Docker!"; \
	elif [ -f "./redis-stack-server-7.2.0-v9/bin/redis-server" ]; then \
		fuser -k 6379/tcp 2>/dev/null || true; \
		while fuser 6379/tcp >/dev/null 2>&1; do sleep 0.2; done; \
		./redis-stack-server-7.2.0-v9/bin/redis-server \
			--loadmodule ./redis-stack-server-7.2.0-v9/lib/redisearch.so \
			--loadmodule ./redis-stack-server-7.2.0-v9/lib/rejson.so \
			--port 6379 \
			--requirepass "Redis*2703!" \
			--pidfile ./redis.pid \
			--dir ./ \
			--logfile ./redis.log \
			--daemonize yes && echo "Redis Stack is up via standalone binary on port 6379!"; \
	else \
		echo "Error: Neither Docker nor standalone redis-server available."; exit 1; \
	fi

redis-down:
	@if docker info >/dev/null 2>&1; then \
		docker compose stop redis 2>/dev/null || true; \
	fi
	@if [ -f redis.pid ]; then kill $$(cat redis.pid) 2>/dev/null || true; rm -f redis.pid; fi
	@fuser -k 6379/tcp 2>/dev/null || true
	@echo "Redis stopped."

up:
	@if docker info >/dev/null 2>&1; then \
		docker compose up -d --build; \
	else \
		echo "Notice: Docker is not accessible for this user on this host."; \
		echo "Starting standalone Redis Stack and local Gateway..."; \
		$(MAKE) redis-up; \
		$(MAKE) dev; \
	fi

down:
	@if docker info >/dev/null 2>&1; then \
		docker compose down 2>/dev/null || true; \
	fi
	@echo "Stopping local Gateway and Redis..."
	@fuser -k 8080/tcp 2>/dev/null || true
	@$(MAKE) redis-down
	@echo "All services stopped."

download-slm:
	$(PYTHON) -m pipeline.download_slm

provision-embedding:
	$(PYTHON) -m pipeline.provision_embedding_model

provision-llm:
	$(PYTHON) -m pipeline.provision_llm

ingest-pdfs:
	$(PYTHON) -m pipeline.batch_ingest_pdfs --pdf-dir data/raw_pdfs

query:
	$(PYTHON) -m pipeline.query_vector_db "$(Q)"

train-router:
	$(PYTHON) train_router.py

eval:
	$(PYTHON) -m evaluation.benchmarks.alpha_sweep
	@echo "Run local response evaluation with: $(PYTHON) -m evaluation.benchmarks.ragas_eval --responses path/to/responses.json"

test:
	PYTHONPATH=. $(PYTHON) -m pytest -q tests/

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	rm -rf .pytest_cache .coverage
