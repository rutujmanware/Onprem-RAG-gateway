"""Regression test suite for VULN-05: TOCTOU Concurrency Race Condition in Distributed OTP Verification.
Verifies that concurrent verification requests cannot bypass the maximum attempt threshold (OTP_MAX_TRIES = 5).
"""
import concurrent.futures
import os
import time
import pytest

from askiisc_chat.askiisc import auth
from askiisc_chat.askiisc import config as C


def test_in_memory_otp_concurrency_lockout():
    """Verify that in-memory fallback OtpStore enforces atomic max tries under concurrent thread pressure."""
    simulated_clock = [1000.0]
    store = auth.OtpStore(clock=lambda: simulated_clock[0])
    email = "researcher@iisc.ac.in"
    ip = "10.0.0.1"

    code = store.issue(email, ip)
    assert code is not None

    def attempt_wrong(worker_id):
        wrong_code = f"{(worker_id + 99999) % 1000000:06d}"
        return store.verify(email, wrong_code)

    # 25 concurrent threads attempting wrong codes
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(attempt_wrong, range(25)))

    # All attempts must fail
    assert all(res is False for res in results)
    # The record must be purged after OTP_MAX_TRIES
    with store.lock:
        assert email not in store.rec

    # Verifying even with the correct code now must fail because it was locked out
    assert store.verify(email, code) is False


def test_redis_otp_concurrency_lockout():
    """Verify that Redis-backed OtpStore uses atomic Lua transactions preventing race conditions across workers."""
    r = auth.otp_store._get_redis()
    if r is None:
        pytest.skip("Redis server not reachable, skipping live Redis concurrency test")

    email = f"concurrency_audit_{int(time.time())}@iisc.ac.in"
    ip = "127.0.0.1"

    code = auth.otp_store.issue(email, ip)
    assert code is not None

    key = f"auth:otp:{email}"
    assert r.exists(key) == 1

    def attempt_wrong(worker_id):
        # Distinct wrong 6-digit codes
        wrong_code = f"{(worker_id + 88888) % 1000000:06d}"
        return auth.otp_store.verify(email, wrong_code)

    # 30 concurrent requests attempting wrong OTPs simultaneously
    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
        results = list(executor.map(attempt_wrong, range(30)))

    # All wrong attempts must return False
    assert all(res is False for res in results)

    # The Redis key must be burned / deleted because attempts reached OTP_MAX_TRIES (5)
    assert r.exists(key) == 0

    # Any subsequent verification (even with the original valid code) must be rejected
    assert auth.otp_store.verify(email, code) is False


def test_redis_otp_success_burns_key_immediately():
    """Verify that successful verification consumes and deletes the OTP immediately to prevent replay attacks."""
    r = auth.otp_store._get_redis()
    if r is None:
        pytest.skip("Redis server not reachable, skipping live Redis OTP test")

    email = f"single_use_{int(time.time())}@iisc.ac.in"
    ip = "127.0.0.1"

    code = auth.otp_store.issue(email, ip)
    assert code is not None

    key = f"auth:otp:{email}"
    assert r.exists(key) == 1

    # First attempt with correct code must succeed
    assert auth.otp_store.verify(email, code) is True

    # Key must be deleted immediately after single use
    assert r.exists(key) == 0

    # Second attempt with same code must fail (replay prevented)
    assert auth.otp_store.verify(email, code) is False

