"""Redis safety around the semantic cache index.

2026-11-14: Redis deadlocked inside FT.INFO (IndexSpec_VectorIndexSize -> TieredHNSWIndex::info ->
pthread_rwlock_wrlock) in this RediSearch build. The code must never call FT.INFO, must not send
FT.ALTER on every initialisation (once per process; "Duplicate field" means present), and must not use KEYS (blocking full-keyspace walk over 2.26 M keys).
"""

import re
from pathlib import Path

import pytest

import gateway.cache.redis_semantic_cache as m
from gateway.cache.redis_semantic_cache import RedisSemanticCache as C

ROOT = Path(__file__).resolve().parent.parent


class _Fake:
    index_name = "test_idx"
    _ALTER_OWNER = C._ALTER_OWNER
    _owner_alter_result = C._owner_alter_result

    def __init__(self, has_owner, error=None):
        self.calls, self.has_owner, self.error = [], has_owner, error

    def execute(self, *cmd):
        self.calls.append(cmd)
        if self.error:
            raise Exception(self.error)
        if cmd[0] == "FT.ALTER" and self.has_owner:
            raise Exception("Duplicate field in schema - owner")  # exact Redis Stack 7.2.0-v9 text
        self.has_owner = True
        return "OK"


@pytest.fixture(autouse=True)
def _reset():
    m._OWNER_FIELD_READY.discard("test_idx")
    yield
    m._OWNER_FIELD_READY.discard("test_idx")


def test_existing_field_is_detected_by_duplicate_error():
    f = _Fake(True)
    C._ensure_owner_field(f, f.execute)
    assert f.calls == [("FT.ALTER", "test_idx", "SCHEMA", "ADD", "owner", "TAG")]
    assert "test_idx" in m._OWNER_FIELD_READY


def test_missing_field_is_added_once_then_never_again_in_process():
    f = _Fake(False)
    C._ensure_owner_field(f, f.execute)
    assert [c[0] for c in f.calls] == ["FT.ALTER"] and f.has_owner
    g = _Fake(True)
    C._ensure_owner_field(g, g.execute)
    assert g.calls == [], "a second cache object in the same process must not touch the schema"


def test_loading_error_is_retried_later():
    f = _Fake(False, error="LOADING Redis is loading the dataset in memory")
    C._ensure_owner_field(f, f.execute)
    assert "test_idx" not in m._OWNER_FIELD_READY, "must retry on the next cache object"


def test_schema_is_not_probed_with_a_search():
    """2026-11-14: a search on a field missing from the schema returns 0 results, not an error, so a
    search probe reported the field as present and FT.ALTER was never sent (semantic hits all missed)."""
    src = (ROOT / "gateway/cache/redis_semantic_cache.py").read_text()
    body = src[src.index("def _ensure_owner_field"):src.index("async def ainit_index")]
    assert "FT.SEARCH" not in body


CODE_DIRS = ["gateway", "pipeline", "scripts", "askiisc_chat", "tests"]


def _code_lines():
    for d in CODE_DIRS:
        for p in (ROOT / d).rglob("*.py"):
            if p.name == Path(__file__).name:
                continue
            for n, line in enumerate(p.read_text(errors="ignore").splitlines(), 1):
                stripped = line.split("#", 1)[0]
                yield p.relative_to(ROOT), n, stripped


def test_no_ft_info_calls_anywhere():
    bad = [(str(p), n) for p, n, s in _code_lines()
           if re.search(r"""["']FT\.INFO["']|\.ft\([^)]*\)\.info\(""", s)]
    assert not bad, f"FT.INFO deadlocks this RediSearch build: {bad}"


def test_no_blocking_keys_calls():
    bad = [(str(p), n) for p, n, s in _code_lines() if re.search(r"""\.keys\(\s*f?["']""", s) or re.search(r"""["']KEYS["']""", s)]
    assert not bad, f"use scan_iter instead of KEYS: {bad}"
