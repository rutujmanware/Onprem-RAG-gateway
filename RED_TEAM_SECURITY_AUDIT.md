# Red Team Security Audit & Vulnerability Assessment Report
**Project:** AskIISc On-Premise RAG Gateway  
**Date:** November 6, 2026  
**Assessment Type:** Source Code Vulnerability Review & Architectural Threat Modeling  
**Target Scope:** `gateway/`, `askiisc_chat/`, `pipeline/`, and orchestration configs  

---

## Executive Summary

A comprehensive white-box code audit and red team assessment was conducted on the AskIISc On-Premise RAG Gateway repository. The audit focused on perimeter security, authentication and authorization integrity, concurrency and admission controls, retrieval injection vectors, and session management.

The evaluation identified **8 distinct security findings**:
* **2 Critical Severity (P0):** Immediate authentication bypass and cryptographic session forgery.
* **2 High Severity (P1):** Unrestricted rate limit bypass on web chat and inactive prompt injection defenses.
* **3 Medium Severity (P2):** Concurrency race conditions in OTP brute-force limits, memory DoS via unbounded queries, and global semantic cache poisoning.
* **1 Low Severity (P3):** Timing analysis on admin credentials and insecure transport cookies.

---

## Vulnerability Summary Matrix

| ID | Severity | Category | Vulnerability Title | Affected Component |
| :---: | :---: | :---: | :--- | :--- |
| **VULN-01** | **CRITICAL** | Auth Bypass | IP Spoofing via Untrusted `X-Forwarded-For` Header | [`gateway/main.py`](gateway/main.py) |
| **VULN-02** | **CRITICAL** | Cryptography | Hardcoded Fallback Session Secret (Cookie Forgery) | [`gateway/main.py`](gateway/main.py) |
| **VULN-03** | **HIGH** | DoS / Rate Limit | Loopback Exemption Bypasses Rate Limiter for Chat UI | [`gateway/queue/admission.py`](gateway/queue/admission.py) |
| **VULN-04** | **HIGH** | LLM Security | Inactive Prompt Injection Fence (Mock Only in Tests) | [`tests/test_injection_fence.py`](tests/test_injection_fence.py) |
| **VULN-05** | **MEDIUM** | Race Condition | TOCTOU Concurrency Flaw in Distributed OTP Verification | [`askiisc_chat/askiisc/auth.py`](askiisc_chat/askiisc/auth.py) |
| **VULN-06** | **MEDIUM** | Resource Exhaustion | Denial of Service via Unbounded `top_k` and Query Text | [`gateway/main.py`](gateway/main.py) & [`pipeline/retriever.py`](pipeline/retriever.py) |
| **VULN-07** | **MEDIUM** | Data Integrity | Unpartitioned Semantic Cache Poisoning Across Users | [`gateway/cache/redis_semantic_cache.py`](gateway/cache/redis_semantic_cache.py) |
| **VULN-08** | **LOW** | Information Leak | Timing Analysis on `ADMIN_KEY` & Cleartext Cookies | [`gateway/main.py`](gateway/main.py) |

---

## Detailed Vulnerability Analysis & Remediations

### VULN-01: Authentication Bypass via Untrusted `X-Forwarded-For` Header (CWE-290)
* **Severity:** **CRITICAL** (CVSS 9.8)
* **Location:** [`gateway/main.py:92-96`](gateway/main.py#L92-L96)
* **Description:**  
  The access guard function `require_internal_or_auth()` controls access to internal endpoints (`/query`, `/v1/chat/completions`, `/cache/clear`, `/cache/stats`, `/queue/stats`). It inspects the `X-Forwarded-For` header and grants full administrative access if the first address in the header is a loopback IP:
  ```python
  # gateway/main.py
  if xff:
      client_ip = xff.split(",")[0].strip()
      if client_ip in ("127.0.0.1", "::1", "localhost"):
          return True  # Full access granted without authentication!
  ```
* **Threat Scenario:**  
  `X-Forwarded-For` is completely client-controllable. Any external, unauthenticated actor on the internet can supply `-H "X-Forwarded-For: 127.0.0.1"`. The gateway incorrectly treats the request as local loopback traffic, bypassing login completely and allowing unauthorized callers to wipe the Redis semantic cache (`/cache/clear`) or run unlimited compute jobs on the A100 GPU.
* **Remediation:**  
  Do not trust `X-Forwarded-For` unless the immediate socket peer (`request.client.host`) matches a strictly configured list of trusted campus reverse proxies (e.g., local Nginx).
  ```python
  TRUSTED_PROXIES = {"127.0.0.1", "::1"}

  def require_internal_or_auth(request: Request):
      peer = request.client.host if request.client else ""
      if peer in ("127.0.0.1", "::1", "testclient"):
          return True
      # Only inspect X-Forwarded-For if peer is explicitly verified as trusted proxy
      if peer in TRUSTED_PROXIES:
          xff = request.headers.get("x-forwarded-for", "")
          # Check proxy-appended right-most entry, not client-supplied left-most
  ```

---

### VULN-02: Hardcoded Fallback Session Secret (Cookie Forgery) (CWE-798)
* **Severity:** **CRITICAL** (CVSS 9.1)
* **Location:** [`gateway/main.py:115`](gateway/main.py#L115)
* **Description:**  
  Starlette's `SessionMiddleware` signs and encrypts user session cookies (`askiisc_session`) using a shared secret. The gateway provides a static, hardcoded fallback string if `SESSION_SECRET` is unset in the environment:
  ```python
  SessionMiddleware,
  secret_key=os.getenv("SESSION_SECRET", "e7b4c91a3f8d2e5b6a7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a"),
  ```
* **Threat Scenario:**  
  If the application is deployed without `SESSION_SECRET` explicitly set, the known key is used. An attacker can use `itsdangerous.URLSafeTimedSerializer` with this static key to sign an arbitrary session cookie containing `{"user": {"email": "victim@iisc.ac.in", "exp": 9999999999}}`. When presented to the gateway, the forged cookie passes validation, achieving total identity impersonation without completing OTP verification.
* **Remediation:**  
  Remove hardcoded fallback strings. Terminate execution immediately on startup if `SESSION_SECRET` is missing or matches known test placeholders:
  ```python
  session_secret = os.getenv("SESSION_SECRET")
  if not session_secret or len(session_secret) < 32 or "e7b4c91a" in session_secret:
      raise RuntimeError("FATAL: Secure SESSION_SECRET environment variable must be configured.")
  ```

---

### VULN-03: Complete Rate Limit Bypass for Web Chat Traffic (CWE-770)
* **Severity:** **HIGH** (CVSS 7.5)
* **Location:** [`askiisc_chat/askiisc/gateway_backend.py:83`](askiisc_chat/askiisc/gateway_backend.py#L83) & [`gateway/queue/admission.py:46`](gateway/queue/admission.py#L46)
* **Description:**  
  The web frontend proxies user chat queries to the gateway via an HTTP client call (`cl.stream("POST", f"{C.GATEWAY_URL}/query", ...)`). Because this call originates locally from loopback without forwarding session cookies or user authentication headers, `request.client.host` evaluates to `127.0.0.1`. In [`gateway/queue/admission.py`](gateway/queue/admission.py):
  ```python
  # AdmissionController.acquire()
  if client_id in ("127.0.0.1", "localhost", "internal", "::1"):
      return True  # Rate limiter is completely bypassed!
  ```
* **Threat Scenario:**  
  The token-bucket rate limiter (`capacity=60`, `refill_rate=20/s`) is completely deactivated for all web chat users. A single authenticated user can run an automated script against `/chat`, submitting thousands of queries simultaneously. The gateway accepts every query, saturating the 64 CPU threads and exhausting A100 GPU memory, causing a total Denial of Service for all other campus users.
* **Remediation:**  
  Pass authenticated user identity across the internal proxy via a cryptographically signed internal header (e.g., `X-Internal-User: <email>`) and enforce token-bucket rate limits per user account rather than per loopback IP.

---

### VULN-04: Inactive Prompt Injection Defense Fence (CWE-1021 / CWE-74)
* **Severity:** **HIGH** (CVSS 7.3)
* **Location:** [`tests/test_injection_fence.py:3`](tests/test_injection_fence.py#L3) vs [`pipeline/gpu_llm_runner.py:196`](pipeline/gpu_llm_runner.py#L196)
* **Description:**  
  Project documentation states that an active Prompt Injection and Jailbreak Fence rejects adversarial inputs before they reach the model. However, inspection of the codebase reveals that `sanitize_and_fence_prompt()` exists **exclusively as a unit test mock** in `tests/test_injection_fence.py`. Neither [`gateway/main.py`](gateway/main.py) nor [`pipeline/rag_chain.py`](pipeline/rag_chain.py) ever imports or executes prompt fencing.
* **Threat Scenario:**  
  1. **Direct Injection:** Adversarial user prompts (e.g. system override signatures, delimiter escapes) pass directly into the LLM context.
  2. **Indirect Injection:** Malicious text within indexed PDF chunks is formatted directly into `user_content` in [`GPULLMEngine.build_rag_messages()`](pipeline/gpu_llm_runner.py#L196) without data boundary tags, allowing untrusted corpus text to instruct the LLM to ignore system directives.
* **Remediation:**  
  1. Migrate prompt sanitation into gateway request processing middleware.
  2. Apply strict XML/Markdown fencing around retrieved chunk context (e.g., `<document_context>...</document_context>`) and instruct the system prompt to treat context strictly as passive data.

---

### VULN-05: TOCTOU Concurrency Race Condition in Redis OTP Verification (CWE-362)
* **Severity:** **MEDIUM** (CVSS 5.9)
* **Location:** [`askiisc_chat/askiisc/auth.py:236-265`](askiisc_chat/askiisc/auth.py#L236-L265)
* **Description:**  
  When validating user-submitted 6-digit one-time passcodes, the Redis-backed `OtpStore.verify()` method executes separate non-atomic commands:
  ```python
  raw = r.get(f"auth:otp:{email}")               # 1. Read attempt count
  # ... check logic ...
  data["tries"] = data.get("tries", 0) + 1       # 2. Increment in Python
  r.set(f"auth:otp:{email}", json.dumps(data))   # 3. Write back to Redis
  ```
* **Threat Scenario:**  
  Because checking and incrementing `tries` is not atomic, an attacker can fire 100 concurrent HTTP requests to `/auth/otp/verify` in parallel. All 100 threads read `data["tries"] = 0` before any `SET` operation finishes. This allows an attacker to bypass the 5-try brute-force lockout window and test hundreds of 6-digit combinations simultaneously.
* **Remediation:**  
  Execute attempt counting atomically using Redis `HINCRBY` or an atomic Lua script:
  ```lua
  -- Atomic OTP check and increment script
  local current = redis.call('HINCRBY', KEYS[1], 'tries', 1)
  if current > 5 then
      redis.call('DEL', KEYS[1])
      return -1 -- Locked out
  end
  return current
  ```

---

### VULN-06: Denial of Service via Unbounded `top_k` and Query Allocation (CWE-400)
* **Severity:** **MEDIUM** (CVSS 5.3)
* **Location:** [`gateway/main.py:134`](gateway/main.py#L134) & [`pipeline/retriever.py:560`](pipeline/retriever.py#L560)
* **Description:**  
  The `DirectQueryRequest` model defines `top_k: Optional[int] = None` and `query: str` without minimum, maximum, or length constraints. In [`pipeline/retriever.py`](pipeline/retriever.py#L560), the value is formatted directly into RediSearch:
  ```python
  search_query = f"({base_filter})=>[KNN {k} @vector $vec EF_RUNTIME {self.ef_runtime} AS score]"
  ```
* **Threat Scenario:**  
  A caller can submit `{"query": "...", "top_k": 500000}`. RediSearch will attempt to calculate cosine distances, rank, and allocate memory for 500,000 document records in Redis RAM and serialize megabytes of chunk data over the internal socket. This blocks Redis single-threaded execution, causing request timeouts across the entire platform.
* **Remediation:**  
  Enforce validation constraints at the API schema boundary:
  ```python
  class DirectQueryRequest(BaseModel):
      query: str = Field(..., min_length=2, max_length=1500)
      top_k: Optional[int] = Field(default=None, ge=1, le=50)
  ```

---

### VULN-07: Unpartitioned Semantic Cache Poisoning Across Users (CWE-444 / CWE-693)
* **Severity:** **MEDIUM** (CVSS 5.3)
* **Location:** [`gateway/cache/redis_semantic_cache.py:408-450`](gateway/cache/redis_semantic_cache.py#L408-L450)
* **Description:**  
  The semantic vector cache stores generated model answers under a global key prefix (`semcache:*`) with a 7-day TTL (`604,800s`). Any subsequent user whose query vector has a cosine similarity $\ge 0.90$ with a stored prompt receives the cached answer without rerunning retrieval or inference.
* **Threat Scenario:**  
  An attacker submits an adversarial query designed to elicit an inaccurate answer or a hallucination. Once generated, this response is indexed in the global cache. All future campus users who ask semantically similar questions receive the attacker-manipulated response directly from cache for the next 7 days.
* **Remediation:**  
  1. Reduce default cache TTL to 24 hours.
  2. Implement an administrative cache invalidation endpoint (`POST /cache/purge`) that takes a query string and purges all semantic matches $\ge 0.85$.
  3. Store response provenance metadata (model version, timestamp) and display a "Verified" or "Cached" badge in the UI.

---

### VULN-08: Non-Constant-Time `ADMIN_KEY` Comparison & Plaintext Cookies (CWE-208 / CWE-319)
* **Severity:** **LOW** (CVSS 3.7)
* **Location:** [`gateway/main.py:107, 116`](gateway/main.py#L107)
* **Description:**  
  1. In `require_internal_or_auth()`, the admin key is verified using Python's standard `==` string equality operator (`if auth_hdr == ADMIN_KEY:`), which terminates on the first non-matching byte, leaking timing information.
  2. `SessionMiddleware` sets `https_only=os.getenv("COOKIE_SECURE", "0") == "1"`, which defaults to `False` (`0`), transmitting session cookies over unencrypted HTTP.
* **Threat Scenario:**  
  1. Attackers on low-latency LAN connections can use statistical timing analysis to guess `ADMIN_KEY` byte by byte.
  2. Session cookies transmitted over unencrypted HTTP can be intercepted by eavesdroppers on the campus network.
* **Remediation:**  
  1. Use `secrets.compare_digest(auth_hdr, ADMIN_KEY)` for all secret comparisons.
  2. Enforce `COOKIE_SECURE=1` (`Secure` flag on cookies) whenever the gateway is deployed behind HTTPS/TLS.

---

## Remediation Checklist & Verification Commands

- [ ] **VULN-01 Fix:** Update `require_internal_or_auth()` in `gateway/main.py` to reject spoofed `X-Forwarded-For` loopback claims.
- [ ] **VULN-02 Fix:** Enforce mandatory non-default `SESSION_SECRET` on server initialization.
- [ ] **VULN-03 Fix:** Forward authenticated user headers across `stream_via_gateway()` so token-bucket rate limits apply to all chat users.
- [ ] **VULN-04 Fix:** Wire `sanitize_and_fence_prompt()` as active middleware in `gateway/main.py` and `pipeline/rag_chain.py`.
- [ ] **VULN-05 Fix:** Replace non-atomic Redis OTP verification with atomic Lua transactions.
- [ ] **VULN-06 Fix:** Add Pydantic validation bounds (`max_length=1500`, `le=50`) on query requests.
- [ ] **VULN-07 Fix:** Implement administrative semantic cache purging and reduce default TTL.
- [ ] **VULN-08 Fix:** Use `secrets.compare_digest` for admin keys and set `COOKIE_SECURE=1`.

#### Automated Security Regression Test Command
```bash
PYTHONPATH=. python3 -m pytest tests/test_single_port_security.py tests/test_admission_queue.py
```

