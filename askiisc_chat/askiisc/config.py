"""All deployment-specific settings. Override via environment variables.
Everything here must point at INTRANET hosts only (no external egress)."""
import os

try:
    from dotenv import load_dotenv
    _cfg_dir = os.path.dirname(os.path.abspath(__file__))
    _chat_dir = os.path.dirname(_cfg_dir)
    _root_dir = os.path.dirname(_chat_dir)
    for _env_file in [
        os.path.join(_chat_dir, "smtp.env"),
        os.path.join(_chat_dir, "app.env"),
        os.path.join(_root_dir, "smtp.env"),
        os.path.join(_root_dir, ".env"),
    ]:
        if os.path.exists(_env_file):
            load_dotenv(_env_file, override=False)
except ImportError:
    pass

def _f(name, default):
    return type(default)(os.environ.get(name, default))

# --- Redis (RediSearch vector index; needs Redis Stack / redis-search module) ---
REDIS_URL        = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
INDEX_NAME       = os.environ.get("INDEX_NAME", "idx:chunks")
F_TEXT           = os.environ.get("F_TEXT", "text")          # chunk text field
F_VECTOR         = os.environ.get("F_VECTOR", "embedding")   # vector field (FLOAT32 blob)
F_THESIS_ID      = os.environ.get("F_THESIS_ID", "thesis_id")  # TAG field
F_TITLE          = os.environ.get("F_TITLE", "title")
F_CHAPTER        = os.environ.get("F_CHAPTER", "chapter")
F_VERSION        = os.environ.get("F_VERSION", "version")
F_ACCESS         = os.environ.get("F_ACCESS", "access")      # TAG: "public" | "restricted"  (YOU must backfill this on every chunk)
TOP_K            = _f("TOP_K", 6)

# --- Embedding: MUST be the same model that built the Redis index ---
EMBED_BACKEND    = os.environ.get("EMBED_BACKEND", "sentence-transformers")  # | openai | hash
EMBED_MODEL      = os.environ.get("EMBED_MODEL", "BAAI/bge-small-en-v1.5")
EMBED_URL        = os.environ.get("EMBED_URL", "http://localhost:8001/v1")   # for backend=openai
EMBED_DIM        = _f("EMBED_DIM", 384)
EMBED_QUERY_PREFIX = os.environ.get("EMBED_QUERY_PREFIX", "")  # e.g. bge: "Represent this sentence for searching relevant passages: "

# --- Models (OpenAI-compatible /v1/chat/completions: vLLM, llama.cpp server, Ollama) ---
SLM_URL          = os.environ.get("SLM_URL", "http://localhost:8002/v1")
SLM_MODEL        = os.environ.get("SLM_MODEL", "Qwen/Qwen2.5-1.5B-Instruct")
SLM_KEY          = os.environ.get("SLM_KEY", "intranet-token")
LLM_URL          = os.environ.get("LLM_URL", "http://localhost:8000/v1")
LLM_MODEL        = os.environ.get("LLM_MODEL", "Qwen/Qwen2.5-7B-Instruct")
LLM_KEY          = os.environ.get("LLM_KEY", "intranet-token")
MAX_TOKENS       = _f("MAX_TOKENS", 2048)
TEMPERATURE      = _f("TEMPERATURE", 0.2)
REQUEST_TIMEOUT  = _f("REQUEST_TIMEOUT", 120.0)

# --- Router ---
ROUTER_PATH      = os.environ.get("ROUTER_PATH", "models/router.joblib")
ALPHA            = _f("ALPHA", 0.5)        # P(hard) >= ALPHA -> 7B. Overrides value stored in the model file if set.
ALPHA_FROM_ENV   = "ALPHA" in os.environ
CASCADE          = os.environ.get("CASCADE", "1") == "1"   # FR-10: escalate low-confidence SLM answers
CASCADE_MIN_CONF = _f("CASCADE_MIN_CONF", 0.60)           # mean token prob threshold
MAX_HISTORY_TURNS = _f("MAX_HISTORY_TURNS", 4)

# --- Server ---
BIND_HOST        = os.environ.get("BIND_HOST", "127.0.0.1")  # set to the intranet IP in deployment
BIND_PORT        = _f("BIND_PORT", 9000)
TRACE_FILE       = os.environ.get("TRACE_FILE", "traces.jsonl")

# --- Access control ---
# Network gate (fail-closed: default only allows loopback). Get real CIDRs from the IISc network team.
ALLOWED_CIDRS    = [x.strip() for x in os.environ.get("ALLOWED_CIDRS", "127.0.0.0/8").split(",") if x.strip()]
TRUSTED_PROXIES  = [x.strip() for x in os.environ.get("TRUSTED_PROXIES", "").split(",") if x.strip()]
# Microsoft Entra ID (IISc Microsoft 365 tenant) - OIDC authorization-code flow + PKCE, single-tenant app registration
ENTRA_TENANT_ID  = os.environ.get("ENTRA_TENANT_ID", "")
ENTRA_CLIENT_ID  = os.environ.get("ENTRA_CLIENT_ID", "")
ENTRA_CLIENT_SECRET = os.environ.get("ENTRA_CLIENT_SECRET", "")
PUBLIC_BASE_URL  = os.environ.get("PUBLIC_BASE_URL", "https://askiisc.example.iisc.ac.in")  # must match the registered redirect URI
ALLOWED_EMAIL_DOMAINS = [x.strip().lower() for x in os.environ.get("ALLOWED_EMAIL_DOMAINS", "iisc.ac.in").split(",") if x.strip()]
SENSITIVE_GROUP_ID = os.environ.get("SENSITIVE_GROUP_ID", "")  # optional Entra group object id; blank = any tenant member
SESSION_SECRET   = os.environ.get("SESSION_SECRET", "")          # required: long random string
SESSION_TTL      = _f("SESSION_TTL", 8 * 3600)
COOKIE_SECURE    = os.environ.get("COOKIE_SECURE", "1") == "1"
SENSITIVE_DIST   = _f("SENSITIVE_DIST", 0.30)  # cosine distance under which a restricted chunk counts as "the question is about restricted info" - TUNE ON YOUR DATA
REFUSAL_MSG      = os.environ.get("REFUSAL_MSG", "This question relates to restricted information that cannot be answered without signing in with a valid IISc account.")

# --- Sign-in mode: "otp" (email one-time code) or "entra" (Microsoft SSO) ---
AUTH_MODE        = os.environ.get("AUTH_MODE", "otp")
REQUIRE_LOGIN    = os.environ.get("REQUIRE_LOGIN", "1") == "1"   # 1 = no chat/theses access at all without signing in
ALLOW_SUBDOMAINS = os.environ.get("ALLOW_SUBDOMAINS", "0") == "1"  # accept dept subdomains like csa.iisc.ac.in? CHECK what your real mail addresses look like
OTP_TTL          = _f("OTP_TTL", 180)              # seconds a code stays valid (default: 180s = 3 minutes)
OTP_MAX_TRIES    = _f("OTP_MAX_TRIES", 5)          # wrong guesses before the code is destroyed
OTP_RESEND_SECS  = _f("OTP_RESEND_SECS", 60)       # min gap between codes per address
OTP_MAX_PER_HOUR = _f("OTP_MAX_PER_HOUR", 10)       # codes per address per hour (stops mail-bombing a victim)
OTP_MAX_PER_IP_HOUR = _f("OTP_MAX_PER_IP_HOUR", 1000)
# --- Outgoing mail: use IISc's INTERNAL relay. smtp.office365.com would be external egress and needs a mailbox credential.
SMTP_HOST        = os.environ.get("SMTP_HOST", "")
SMTP_PORT        = _f("SMTP_PORT", 587)
SMTP_STARTTLS    = os.environ.get("SMTP_STARTTLS", "1") == "1"
SMTP_SSL         = os.environ.get("SMTP_SSL", "0") == "1"
SMTP_USER        = os.environ.get("SMTP_USER", "")
SMTP_PASS        = os.environ.get("SMTP_PASS", "")
SMTP_FROM        = os.environ.get("SMTP_FROM", "askiisc-noreply@iisc.ac.in")
SMTP_TIMEOUT     = _f("SMTP_TIMEOUT", 15.0)
# DEMO ONLY: when SMTP_HOST is empty and this is 1, the one-time code is written to the SERVER LOG instead of being mailed.
# The operator reads it from the log and types it in. It proves nothing about mailbox ownership - never enable for real users.
OTP_DEV_LOG      = os.environ.get("OTP_DEV_LOG", "0") == "1"

# --- Deployment-mode switches (auth is decided separately) ---
ACCESS_TIERS     = os.environ.get("ACCESS_TIERS", "1") == "1"   # 0 = no public/restricted split: everyone allowed through the network gate sees ALL chunks.
                                                              # Only use 0 while the index holds NO restricted/embargoed theses.
SLM_TOP_K        = _f("SLM_TOP_K", 2)      # chunks given to the CPU SLM (prefill on CPU is slow); the 7B gets TOP_K
SLM_TIMEOUT      = _f("SLM_TIMEOUT", 300.0)  # CPU prefill + generation can take minutes

# --- Retrieval backend ---
RETRIEVER_BACKEND = os.environ.get("RETRIEVER_BACKEND", "redis")   # "redis" (Redis Stack/RediSearch) | "npz" (in-RAM numpy index loaded from CHUNKS_FILE; no Redis needed)
CHUNKS_FILE       = os.environ.get("CHUNKS_FILE", "data/chunks.npz")

# --- Answering backend ---
# "own"     = this app embeds the question, searches Redis and calls the SLM/LLM servers itself (needs the Redis field names below to match).
# "gateway" = forward the question to the team gateway (gateway/main.py POST /query), which does routing, retrieval and generation.
BACKEND          = os.environ.get("BACKEND", "own")
GATEWAY_URL      = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8080")
GATEWAY_TIMEOUT  = _f("GATEWAY_TIMEOUT", 600.0)     # CPU SLM prefill + generation can take minutes
GATEWAY_DEBUG    = os.environ.get("GATEWAY_DEBUG", "0") == "1"   # log the first raw events from the gateway (use on first runs)
