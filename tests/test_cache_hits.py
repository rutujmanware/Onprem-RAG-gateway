import re
import os
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import numpy as np
import pytest
from fastapi.testclient import TestClient

from gateway.dependencies import container
from gateway.main import app
from gateway.auth.token_service import token_service
from gateway.retrieval import RetrievedChunk
from gateway.cache.l1_semantic import L1CacheError, L1SemanticCache


class FakeRediSearchPipeline:
    def __init__(self, store: "FakeRedisStackServer"):
        self.store = store
        self.ops = []

    def hset(self, key: str, mapping: Dict[bytes, bytes]):
        self.ops.append(("hset", key, mapping))
        return self

    def expire(self, key: str, ttl: int):
        self.ops.append(("expire", key, ttl))
        return self

    async def execute(self):
        for op in self.ops:
            if op[0] == "hset":
                _, key, mapping = op
                self.store.hashes[key] = dict(mapping)
        self.ops.clear()
        return True


class FakeRedisStackServer:
    def __init__(self):
        self.hashes: Dict[str, Dict[bytes, bytes]] = {}
        self.kv: Dict[str, bytes] = {}
        self.indices: set = set()

    def pipeline(self, transaction: bool = False):
        return FakeRediSearchPipeline(self)

    async def get(self, key: str) -> Optional[bytes]:
        return self.kv.get(key)

    async def setex(self, key: str, ttl: int, value: bytes) -> None:
        self.kv[key] = value

    async def delete(self, *keys: str) -> int:
        removed = 0
        for k in keys:
            if self.hashes.pop(k, None) is not None:
                removed += 1
        return removed

    @staticmethod
    def _unescape_tag(val: str) -> str:
        return re.sub(r"\\(.)", r"\1", val)

    async def execute_command(self, *args) -> Any:
        cmd = str(args[0]).upper()
        if cmd == "FT.INFO":
            idx = args[1]
            if idx not in self.indices:
                raise RuntimeError("Unknown Index name")
            return [b"index_name", idx.encode()]

        if cmd == "FT.CREATE":
            idx = args[1]
            self.indices.add(idx)
            return b"OK"

        if cmd == "FT.SEARCH":
            query_str = str(args[2])
            thesis_match = re.search(r"@thesis_id:\{([^}]+)\}", query_str)
            chapter_match = re.search(r"@chapter:\{([^}]+)\}", query_str)
            version_match = re.search(r"@doc_version:\{([^}]+)\}", query_str)

            req_thesis = self._unescape_tag(thesis_match.group(1)) if thesis_match else None
            req_chapter = self._unescape_tag(chapter_match.group(1)) if chapter_match else None
            req_version = self._unescape_tag(version_match.group(1)) if version_match else None

            if "NOCONTENT" in args:
                matched_keys = []
                for k, h in self.hashes.items():
                    if req_thesis and h.get(b"thesis_id", b"").decode() != req_thesis:
                        continue
                    if req_version and h.get(b"doc_version", b"").decode() != req_version:
                        continue
                    matched_keys.append(k.encode())
                return [len(matched_keys), *matched_keys]

            args_list = list(args)
            vec_idx = args_list.index("vec") + 1
            q_vec = np.frombuffer(args_list[vec_idx], dtype=np.float32)
            q_norm = np.linalg.norm(q_vec)
            if q_norm > 0:
                q_vec = q_vec / q_norm

            best_key = None
            best_dist = 999.0
            best_hash = None

            for k, h in self.hashes.items():
                if req_thesis and h.get(b"thesis_id", b"").decode() != req_thesis:
                    continue
                if req_chapter and h.get(b"chapter", b"").decode() != req_chapter:
                    continue
                if req_version and h.get(b"doc_version", b"").decode() != req_version:
                    continue

                cand_vec = np.frombuffer(h[b"vector"], dtype=np.float32)
                c_norm = np.linalg.norm(cand_vec)
                if c_norm > 0:
                    cand_vec = cand_vec / c_norm

                cos_sim = float(np.dot(q_vec, cand_vec))
                cos_dist = 1.0 - cos_sim
                if cos_dist < best_dist:
                    best_dist = cos_dist
                    best_key = k
                    best_hash = h

            if best_hash is None:
                return [0]

            fields = [
                b"prompt", best_hash[b"prompt"],
                b"response", best_hash[b"response"],
                b"thesis_id", best_hash[b"thesis_id"],
                b"chapter", best_hash[b"chapter"],
                b"doc_version", best_hash[b"doc_version"],
                b"score", f"{best_dist:.6f}".encode("utf-8"),
            ]
            if b"model_tier" in best_hash:
                fields.extend([b"model_tier", best_hash[b"model_tier"]])
            if b"language" in best_hash:
                fields.extend([b"language", best_hash[b"language"]])
            if b"conversation_context" in best_hash:
                fields.extend([b"conversation_context", best_hash[b"conversation_context"]])
            return [1, best_key.encode("utf-8"), fields]

        raise NotImplementedError(f"Unsupported command: {cmd}")


def _deterministic_semantic_embed(prompt: str) -> List[float]:
    vec = np.zeros(384, dtype=np.float32)
    lower = prompt.lower()
    if "estimator" in lower:
        vec[0:64] = 1.0
    if "proposed" in lower or "explain" in lower or "design" in lower:
        vec[64:128] = 0.85
    rng = np.random.default_rng(sum(ord(c) for c in prompt))
    vec += rng.normal(0, 0.03, size=384).astype(np.float32)
    norm = np.linalg.norm(vec)
    return (vec / norm).tolist()


@pytest.fixture()
def test_client(monkeypatch):
    fake_redis = FakeRedisStackServer()
    container.redis_client = fake_redis

    class AuthorizedRetriever:
        async def current_version(self, thesis_id: str, fallback: str = "v1") -> str:
            return "v1"

        async def allowed(self, thesis_id: str, roles, user_id: str) -> bool:
            return True

        async def retrieve(self, embedding, thesis_id: str, chapter: str, doc_version: str):
            return [RetrievedChunk(
                key="doc:chunk:test:v1:0",
                content="The proposed estimator is described in the indexed thesis evidence.",
                thesis_id=thesis_id,
                chapter=chapter,
                doc_version=doc_version,
                source_uri="test://thesis",
                chunk_index=0,
                score=0.01,
            )]

    class LocalSLM:
        async def generate_chat(self, messages, **kwargs):
            return {"choices": [{"message": {"role": "assistant", "content": "Grounded test answer [SOURCE]."}}]}

    monkeypatch.setenv("GATEWAY_AUTH_STRICT", "true")
    with TestClient(app) as client:
        container.redis_client = fake_redis
        container.l0_cache.redis = fake_redis
        container.l1_cache.redis = fake_redis
        container.embedder.embed_query = _deterministic_semantic_embed
        container.retriever = AuthorizedRetriever()
        container.slm_client = LocalSLM()
        token = token_service.create_access_token(
            user_id="test_student", email="test_student@iisc.ac.in", roles=["student"]
        )
        client.headers.update({"Authorization": f"Bearer {token}"})
        yield client

    container.redis_client = None


def test_fr3_semantic_cache_and_entity_guard_lifecycle(test_client: TestClient):
    # T1: Base Cache Populate
    r1 = test_client.post(
        "/v1/chat/completions",
        json={
            "prompt": "What estimator is proposed in Chapter 3?",
            "scope": {"thesis_id": "thesis_A", "chapter": "ch_3"},
        },
    )
    assert r1.status_code == 200
    assert r1.headers["X-Cache"] == "MISS"

    # T2: Paraphrase Hit (Same thesis_A + Same ch_3 + cosine similarity >= 0.88)
    r2 = test_client.post(
        "/v1/chat/completions",
        json={
            "prompt": "Can you explain the Chapter 3 estimator design?",
            "scope": {"thesis_id": "thesis_A", "chapter": "ch_3"},
        },
    )
    assert r2.status_code == 200
    assert r2.headers["X-Cache"] == "HIT-L1"
    body2 = r2.json()
    assert body2["cache_status"] == "HIT-L1"
    assert body2["similarity"] >= 0.88
    assert body2["matched_prompt"] == "What estimator is proposed in Chapter 3?"

    # T3: Cross-Thesis Rejection (Same question + Different thesis_B -> Cache MISS)
    r3 = test_client.post(
        "/v1/chat/completions",
        json={
            "prompt": "Can you explain the Chapter 3 estimator design?",
            "scope": {"thesis_id": "thesis_B", "chapter": "ch_3"},
        },
    )
    assert r3.status_code == 200
    assert r3.headers["X-Cache"] == "MISS"
    assert r3.json()["cache_status"] == "MISS"

    # T4: Cross-Chapter Rejection (Same thesis_A + Different ch_5 -> Cache MISS)
    r4 = test_client.post(
        "/v1/chat/completions",
        json={
            "prompt": "Can you explain the Chapter 5 estimator design?",
            "scope": {"thesis_id": "thesis_A", "chapter": "ch_5"},
        },
    )
    assert r4.status_code == 200
    assert r4.headers["X-Cache"] == "MISS"
    assert r4.json()["cache_status"] == "MISS"


def test_t5_fr4_zero_reembedding_on_cache_miss(test_client: TestClient):
    # T5: Verify embed_query is called exactly ONCE on a cache miss, and reused by FR-4
    with patch.object(
        container.embedder, "embed_query", wraps=_deterministic_semantic_embed
    ) as spy_embed:
        resp = test_client.post(
            "/v1/chat/completions",
            json={
                "prompt": "How is convergence proven in Chapter IV?",
                "scope": {"thesis_id": "thesis_C"},
            },
        )
        assert resp.status_code == 200
        assert resp.headers["X-Cache"] == "MISS"
        assert resp.headers["X-Scope-Chapter"] == "ch_4"
        assert spy_embed.call_count == 1


@pytest.mark.asyncio
async def test_l1_versions_are_isolated_and_invalidated():
    redis = FakeRedisStackServer()
    cache = L1SemanticCache(redis_client=redis, vector_dimension=384, ttl_seconds=60)
    embedding = np.zeros(384, dtype=np.float32)
    embedding[0] = 1.0

    key_v1 = await cache.set(
        "What does the thesis conclude?", embedding, {"answer": "old"},
        "thesis_a", "ch_1", doc_version="v1",
    )
    key_v2 = await cache.set(
        "What does the thesis conclude?", embedding, {"answer": "new"},
        "thesis_a", "ch_1", doc_version="v2",
    )
    assert key_v1 != key_v2

    v1_hit = await cache.get(embedding, "thesis_a", "ch_1", doc_version="v1")
    v2_hit = await cache.get(embedding, "thesis_a", "ch_1", doc_version="v2")
    assert v1_hit["response"]["answer"] == "old"
    assert v2_hit["response"]["answer"] == "new"

    assert await cache.invalidate_version("thesis_a", "v1") == 1
    assert await cache.get(embedding, "thesis_a", "ch_1", doc_version="v1") is None
    assert await cache.get(embedding, "thesis_a", "ch_1", doc_version="v2") is not None


@pytest.mark.asyncio
async def test_l1_rejects_wrong_dimension_and_zero_vector():
    cache = L1SemanticCache(redis_client=FakeRedisStackServer(), vector_dimension=384)
    with pytest.raises(L1CacheError, match="dimension"):
        await cache.set("q", [1.0, 2.0], {}, "thesis_a", "ch_1")
    with pytest.raises(L1CacheError, match="zero vector"):
        await cache.set("q", [0.0] * 384, {}, "thesis_a", "ch_1")


if __name__ == "__main__":
    fake_redis = FakeRedisStackServer()
    container.redis_client = fake_redis

    with TestClient(app) as client:
        container.redis_client = fake_redis
        container.l0_cache.redis = fake_redis
        container.l1_cache.redis = fake_redis
        container.embedder.embed_query = _deterministic_semantic_embed
        test_fr3_semantic_cache_and_entity_guard_lifecycle(client)
        test_t5_fr4_zero_reembedding_on_cache_miss(client)

    container.redis_client = None
    print("All L1 semantic cache & entity guard acceptance tests (T1-T5) passed successfully!")
