"""DEFECT-42: /ready reports whether the gateway can answer; /health stays a liveness check.

Reported: /health answered {"status":"ok"} while Redis was down and every query failed. /ready returns 503 unless
Redis with the thesis index is reachable and at least one model tier is up; details only for loopback/admin.
"""

import pytest
from fastapi.testclient import TestClient

import gateway.main as gm
from gateway.dependencies import get_semantic_cache, get_slm_client, get_vllm_client

EXTERNAL = {"X-Forwarded-For": "203.0.113.7"}


class Redis:
    def __init__(self, ok):
        self.ok = ok

    async def execute_command(self, *a):
        if not self.ok:
            raise ConnectionError("down")
        return [b"rag_documents_idx", b"semantic_cache_idx"]


class Cache:
    def __init__(self, ok):
        self.async_client = Redis(ok)


class Model:
    def __init__(self, ok):
        self.ok = ok

    async def check_health(self):
        return self.ok


@pytest.fixture
def setup(monkeypatch):
    import pipeline.embeddings.embedder as emb

    class Emb:            # embedding model present (DEFECT-48 check)
        _model = object()
    monkeypatch.setattr(emb, "LocalEmbedder", Emb)
    over = gm.app.dependency_overrides
    saved = dict(over)

    def make(redis=True, cpu=True, gpu=True):
        over.update({get_semantic_cache: lambda: Cache(redis), get_slm_client: lambda: Model(cpu),
                     get_vllm_client: lambda: Model(gpu)})
        return TestClient(gm.app)
    yield make
    over.clear()
    over.update(saved)


@pytest.mark.parametrize("redis,cpu,gpu,status", [
    (True, True, True, 200), (True, True, False, 200), (True, False, True, 200),
    (False, True, True, 503), (True, False, False, 503), (False, False, False, 503)])
def test_ready_reflects_dependencies(setup, redis, cpu, gpu, status):
    c = setup(redis, cpu, gpu)
    r = c.get("/ready")
    assert r.status_code == status
    assert r.json()["checks"] == {"redis_thesis_index": redis, "embedding_model": True, "cpu_model": cpu, "gpu_model": gpu}


def test_outsiders_see_only_the_status(setup):
    r = setup(redis=False).get("/ready", headers=EXTERNAL)
    assert r.status_code == 503 and r.json() == {"status": "not_ready"}


def test_health_stays_a_liveness_check(setup):
    assert setup(redis=False, cpu=False, gpu=False).get("/health").json() == {"status": "ok"}


def test_missing_embedding_model_is_not_ready(setup, monkeypatch):
    import pipeline.embeddings.embedder as emb

    class NoEmb:
        _model = None
    c = setup()
    monkeypatch.setattr(emb, "LocalEmbedder", NoEmb)
    r = c.get("/ready")
    assert r.status_code == 503 and r.json()["checks"]["embedding_model"] is False
