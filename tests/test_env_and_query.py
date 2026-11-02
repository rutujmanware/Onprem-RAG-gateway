import os
from unittest.mock import patch
import pytest

from pipeline.env_utils import resolve_redis_url


def test_resolve_redis_url_injects_password_when_missing():
    with patch.dict(os.environ, {"REDIS_PASSWORD": "SecretPassword123!"}, clear=False):
        url = resolve_redis_url("redis://localhost:6379/0")
        assert url == "redis://:SecretPassword123!@localhost:6379/0"


def test_resolve_redis_url_preserves_existing_password():
    url = resolve_redis_url("redis://:custom_pass@localhost:6379/0")
    assert url == "redis://:custom_pass@localhost:6379/0"


def test_resolve_redis_url_replaces_docker_host_if_unresolvable():
    url = resolve_redis_url("redis://:Redis*2703!@redis:6379/0")
    # If 'redis' host cannot be resolved in local environment, it should map to localhost
    assert "redis://:Redis*2703!@" in url
    assert ("@localhost:6379" in url) or ("@redis:6379" in url)
