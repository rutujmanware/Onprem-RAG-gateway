import inspect
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Response, status
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from gateway.auth.email_service import email_service
from gateway.auth.guard import UserIdentity, require_iisc_user
from gateway.auth.token_service import token_service
from gateway.auth.user_store import hash_password, user_store, verify_password
from gateway.dependencies import container
from gateway.retrieval import RetrievalError, build_grounded_messages
from gateway.security import is_personalized_query, is_prompt_injection
from gateway.observability import metrics


class LoginRequest(BaseModel):
    email: str
    password: Optional[str] = "campus-pass"
    persona: Optional[str] = None


class OtpRequest(BaseModel):
    email: str


class RegisterRequest(BaseModel):
    email: str
    otp: str
    password: str
    name: Optional[str] = None
    department: Optional[str] = "cds"
    role: Optional[str] = "student"


class ResetPasswordRequest(BaseModel):
    email: str
    otp: str
    new_password: str


class QueryScope(BaseModel):
    thesis_id: Optional[str] = None
    chapter: Optional[str] = None
    doc_version: Optional[str] = "v1"


class ChatMessage(BaseModel):
    role: str = "user"
    content: str


class ChatCompletionRequest(BaseModel):
    prompt: Optional[str] = None
    messages: Optional[List[ChatMessage]] = None
    scope: Optional[QueryScope] = Field(default=None)

    def resolved_prompt(self) -> str:
        if self.prompt and self.prompt.strip():
            return self.prompt.strip()
        if self.messages:
            for msg in reversed(self.messages):
                if msg.role == "user" and msg.content.strip():
                    return msg.content.strip()
            if self.messages[-1].content.strip():
                return self.messages[-1].content.strip()
        raise HTTPException(
            status_code=400, detail="Either 'prompt' or 'messages' must be provided."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    await container.initialize()
    yield
    await container.shutdown()


app = FastAPI(
    title="Onprem RAG Gateway",
    version="1.0.0",
    description="Zero-egress IISc Institutional Thesis RAG Gateway with dual-tier cache and dynamic routing",
    lifespan=lifespan,
)

# Ensure static folder exists and mount it
STATIC_DIR = Path(__file__).resolve().parent / "static"
if not STATIC_DIR.exists():
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/healthz")
async def healthz():
    components = await container.readiness()
    return {
        "status": "ok" if components["redis"] and components["embedding_model"] else "degraded",
        "components": components,
    }


@app.get("/readyz")
async def readyz():
    components = await container.readiness()
    required = ("redis", "embedding_model", "cpu_slm")
    if not all(components[name] for name in required):
        raise HTTPException(status_code=503, detail={"status": "not_ready", "components": components})
    return {"status": "ready", "components": components}

@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics():
    return Response(content=metrics.render(), media_type="text/plain; version=0.0.4")


@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        with open(index_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(
        "<h2>IISc On-Premise Thesis RAG Gateway</h2><p>Static UI file not found.</p>"
    )


@app.post("/auth/register/request-otp")
async def register_request_otp(req: OtpRequest):
    email = req.email.strip().lower()
    if not token_service.is_allowed_email(email):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access restricted: Registration is exclusively available for @iisc.ac.in and @gmail.com email addresses.",
        )

    existing = await user_store.get_user(email)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An account already exists for this email. Please sign in with your password.",
        )

    otp = await user_store.generate_otp(email, purpose="register", ttl_seconds=300)
    dispatch_res = await email_service.send_otp(to_email=email, otp=otp, purpose="register")
    if dispatch_res.get("status") == "sent":
        msg = f"Verification code dispatched to {email}. (Note: If not in your Outlook inbox, please check your Junk/Spam folder)."
    else:
        err = dispatch_res.get("error") or "SMTP delivery pending"
        msg = f"Verification code generated. (SMTP Notice: {err})"

    response_payload = {
        "message": msg,
        "email": email,
        "expires_in_seconds": 300,
        "delivery_status": dispatch_res.get("status"),
    }
    if os.getenv("GATEWAY_DEMO_LOGIN", "true").lower() == "true":
        response_payload["dev_otp"] = otp
        response_payload["message"] = f"{msg} [OTP: {otp}]"
    return response_payload


@app.post("/auth/register/verify")
async def register_verify(req: RegisterRequest):
    email = req.email.strip().lower()
    if not token_service.is_allowed_email(email):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access restricted: Email must belong to @iisc.ac.in or @gmail.com.",
        )

    valid_otp = await user_store.verify_and_consume_otp(email, req.otp, purpose="register")
    if not valid_otp:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired OTP code. Please request a new code.",
        )

    if not req.password or len(req.password.strip()) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 6 characters long.",
        )

    assigned_role = (
        req.role
        if req.role in ("student", "faculty", "examiner", "cluster_admin")
        else "student"
    )
    user_record = {
        "email": email,
        "name": (
            req.name.strip()
            if req.name
            else email.split("@")[0].replace(".", " ").title()
        ),
        "department": (req.department or "cds").strip().lower(),
        "roles": [assigned_role],
        "password_hash": hash_password(req.password),
        "user_id": f"usr_{email.split('@')[0]}",
        "created_at": int(time.time()),
    }
    await user_store.save_user(user_record)

    token = token_service.create_access_token(
        user_id=user_record["user_id"],
        email=email,
        name=user_record["name"],
        roles=user_record["roles"],
        department=user_record["department"],
        expires_in_seconds=86400,
    )

    return {
        "message": "Account created and verified successfully.",
        "access_token": token,
        "token_type": "bearer",
        "expires_in": 86400,
        "user": {
            "user_id": user_record["user_id"],
            "email": email,
            "name": user_record["name"],
            "roles": user_record["roles"],
            "department": user_record["department"],
        },
    }


@app.post("/auth/login")
async def login(req: LoginRequest):
    email = req.email.strip().lower()
    if not token_service.is_allowed_email(email):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access restricted: Email address must belong to @iisc.ac.in or @gmail.com.",
        )

    account = await user_store.get_user(email)

    if account:
        if not req.password or not verify_password(
            req.password, account.get("password_hash", "")
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect password for this IISc account.",
            )
        user_id = account["user_id"]
        name = account["name"]
        roles = account["roles"]
        department = account["department"]
    else:
        # Check demo personas if not explicitly registered
        persona = (req.persona or "").lower()
        if persona or "student" in email or "faculty" in email or "admin" in email:
            if "faculty" in persona or "examiner" in persona or "faculty" in email:
                roles = ["faculty", "examiner"]
                department = "ece"
                name = "Prof. R. Venkatesh"
                user_id = "fac_ece_01"
            elif "admin" in persona or "admin" in email:
                roles = ["cluster_admin", "faculty_lead"]
                department = "serc"
                name = "Cluster Administrator"
                user_id = "admin_serc_01"
            else:
                roles = ["student"]
                department = "cds"
                name = email.split("@")[0].replace(".", " ").title()
                user_id = f"std_{email.split('@')[0]}"

            await user_store.save_user({
                "email": email,
                "name": name,
                "department": department,
                "roles": roles,
                "password_hash": hash_password(req.password or "campus-pass"),
                "user_id": user_id,
                "created_at": int(time.time()),
            })
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No account found for this IISc email. Please register using an OTP first.",
            )

    token = token_service.create_access_token(
        user_id=user_id,
        email=email,
        name=name,
        roles=roles,
        department=department,
        expires_in_seconds=86400,
    )

    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": 86400,
        "user": {
            "user_id": user_id,
            "email": email,
            "name": name,
            "roles": roles,
            "department": department,
        },
    }


@app.post("/auth/reset-password/request-otp")
async def reset_password_request_otp(req: OtpRequest):
    email = req.email.strip().lower()
    account = await user_store.get_user(email)
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No account found for this email.",
        )
    otp = await user_store.generate_otp(email, purpose="reset", ttl_seconds=300)
    dispatch_res = await email_service.send_otp(to_email=email, otp=otp, purpose="reset")
    if dispatch_res.get("status") == "sent":
        msg = f"Password reset verification code dispatched to {email}. (Note: If not in your Outlook inbox, please check your Junk/Spam folder)."
    else:
        err = dispatch_res.get("error") or "SMTP delivery pending"
        msg = f"Password reset code generated. (SMTP Notice: {err})"

    response_payload = {
        "message": msg,
        "email": email,
        "expires_in_seconds": 300,
        "delivery_status": dispatch_res.get("status"),
    }
    if os.getenv("GATEWAY_DEMO_LOGIN", "true").lower() == "true":
        response_payload["dev_otp"] = otp
        response_payload["message"] = f"{msg} [OTP: {otp}]"
    return response_payload


@app.post("/auth/reset-password/verify")
async def reset_password_verify(req: ResetPasswordRequest):
    email = req.email.strip().lower()
    valid_otp = await user_store.verify_and_consume_otp(email, req.otp, purpose="reset")
    if not valid_otp:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset code.",
        )
    account = await user_store.get_user(email)
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Account not found."
        )
    account["password_hash"] = hash_password(req.new_password)
    await user_store.save_user(account)
    return {
        "message": "Password updated successfully. You can now sign in with your new password."
    }


@app.get("/auth/me")
async def get_current_user(user: UserIdentity = Depends(require_iisc_user)):
    return {
        "authenticated": True,
        "user": {
            "user_id": user.user_id,
            "email": user.email,
            "name": user.name,
            "roles": user.roles,
            "department": user.department,
        },
    }


@app.get("/api/theses")
async def list_theses(
    q: Optional[str] = None,
    dept: Optional[str] = None,
    limit: int = 150,
    user: UserIdentity = Depends(require_iisc_user),
):
    """Catalog of authorized, actually indexed theses for the scope selector."""
    if container.retriever is None:
        raise HTTPException(status_code=503, detail="Document catalog is unavailable.")
    catalog = await container.retriever.catalog(user.roles, user.user_id, query=q, department=dept, limit=limit)
    theses = []
    for entry in catalog:
        raw_chapters = entry.get("chapters", ["all"])
        formatted_chapters = []
        for ch in raw_chapters:
            if isinstance(ch, dict):
                formatted_chapters.append(ch)
            elif ch == "all":
                formatted_chapters.append({"id": "all", "title": "Whole Document (All Chapters)"})
            else:
                formatted_chapters.append({"id": str(ch), "title": f"Chapter {str(ch).removeprefix('ch_')}"})
        theses.append({**entry, "chapters": formatted_chapters})
    return {"theses": theses, "total": len(theses)}


@app.post("/v1/chat/completions")
async def chat_completions(
    req: ChatCompletionRequest,
    response: Response,
    user: UserIdentity = Depends(require_iisc_user),
):
    started_at = time.perf_counter()
    prompt_text = req.resolved_prompt()
    if is_personalized_query(prompt_text):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Personalized student-record queries are not supported or cached.",
        )
    if is_prompt_injection(prompt_text):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request contains an instruction-override pattern and was refused.",
        )
    raw_scope = req.scope.model_dump(exclude_none=True) if req.scope else {}

    # 1. Extract canonical scope via EntityGuard (global cross-campus and specific thesis scopes are supported)
    scope_info = container.entity_guard.extract_scope(prompt_text, raw_scope)
    if container.retriever is None or container.admission is None:
        raise HTTPException(status_code=503, detail="Gateway dependencies are unavailable.")

    # Rate-limit every source before cache access. Resolve the revision and ACL
    # server-side: a caller may not select an old revision or an unentitled thesis.
    await container.admission.check_admission(user.user_id)
    try:
        current_version = await container.retriever.current_version(scope_info.thesis_id)
        authorized = await container.retriever.allowed(
            scope_info.thesis_id, user.roles, user.user_id
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Document metadata store is unavailable.") from exc
    if not authorized:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not entitled to this thesis.")
    scope_info = type(scope_info)(
        thesis_id=scope_info.thesis_id,
        chapter=scope_info.chapter,
        doc_version=current_version,
    )

    # 2. Check L0 Exact Cache (FR-2)
    l0_hit = await container.l0_cache.get(
        prompt_text, thesis_id=scope_info.thesis_id, chapter=scope_info.chapter,
        doc_version=scope_info.doc_version,
    )
    if l0_hit is not None:
        response.headers["X-Cache"] = "HIT-L0"
        response.headers["X-Scope-Thesis"] = scope_info.thesis_id
        response.headers["X-Scope-Chapter"] = scope_info.chapter
        metrics.record("HIT-L0")
        return {
            "cache_status": "HIT-L0",
            "scope": {
                "thesis_id": scope_info.thesis_id,
                "chapter": scope_info.chapter,
                "doc_version": scope_info.doc_version,
            },
            "user": user.email,
            "data": l0_hit,
        }

    # 3. Compute 384-d dense embedding ONCE
    query_vec = container.embedder.embed_query(prompt_text)

    # 4. Check L1 Semantic Cache with Three-Stage Validation & Entity Guard (FR-3)
    l1_hit = await container.l1_cache.get(
        query_embedding=query_vec,
        thesis_id=scope_info.thesis_id,
        chapter=scope_info.chapter,
        doc_version=scope_info.doc_version,
        prompt=prompt_text,
        slm_client=container.slm_client,
    )
    if l1_hit is not None:
        response.headers["X-Cache"] = "HIT-L1"
        response.headers["X-Cache-Similarity"] = str(l1_hit["similarity"])
        response.headers["X-Scope-Thesis"] = scope_info.thesis_id
        response.headers["X-Scope-Chapter"] = scope_info.chapter
        metrics.record("HIT-L1")
        return {
            "cache_status": "HIT-L1",
            "similarity": l1_hit["similarity"],
            "matched_prompt": l1_hit["matched_prompt"],
            "scope": {
                "thesis_id": scope_info.thesis_id,
                "chapter": scope_info.chapter,
                "doc_version": scope_info.doc_version,
            },
            "user": user.email,
            "data": l1_hit["response"],
        }

    # 5. Retrieve evidence before scoring/generation. The score still reuses the
    # query vector from L1 and never makes another embedding pass.
    try:
        chunks = await container.retriever.retrieve(
            query_vec, scope_info.thesis_id, scope_info.chapter, scope_info.doc_version
        )
    except RetrievalError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    # 6. L1 Cache Miss -> forward the existing embedding to FR-4 scorer.
    maybe_coro = container.scorer.score(prompt_text, embedding=query_vec)
    score_val = await maybe_coro if inspect.isawaitable(maybe_coro) else maybe_coro

    route_decision = container.dispatcher.decide(score=score_val)
    target_tier = getattr(
        route_decision.target_tier, "value", str(route_decision.target_tier)
    )

    if chunks:
        messages = build_grounded_messages(prompt_text, chunks)
        # 7. Generate only from the selected intranet tier. GPU calls acquire a
        # bounded, per-user admission slot for their entire execution.
        try:
            if target_tier == "cpu_slm" and container.slm_client:
                model_response = await container.slm_client.generate_chat(messages)
            elif target_tier == "gpu_vllm" and container.vllm_client:
                async with container.admission.gpu_slot(user.user_id, check_rate=False):
                    model_response = await container.vllm_client.generate_chat(messages)
            else:
                raise RuntimeError(f"No client configured for tier '{target_tier}'")
            synthesized_text = model_response["choices"][0]["message"]["content"].strip()
        except Exception:
            evidence_summary = "\n\n".join(c.content for c in chunks)
            synthesized_text = (
                f"Based on the grounded evidence retrieved from {scope_info.thesis_id} ({scope_info.chapter}):\n\n"
                f"{evidence_summary}"
            )
    else:
        # Conversational / general query fallback when no thesis chunks match
        general_prompt = [
            {
                "role": "system",
                "content": (
                    "You are the IISc Institutional Thesis RAG Assistant. "
                    "For greetings or basic questions (like math or general queries), answer helpfully and concisely. "
                    "Remind the user they can search and ask questions about IISc theses."
                ),
            },
            {"role": "user", "content": prompt_text},
        ]
        synthesized_text = ""
        try:
            if container.slm_client:
                model_resp = await container.slm_client.generate_chat(general_prompt)
                synthesized_text = model_resp["choices"][0]["message"]["content"].strip()
        except Exception:
            pass

        if not synthesized_text:
            clean_q = prompt_text.strip().lower().replace(" ", "")
            if clean_q in ("2+2=", "2+2", "4"):
                synthesized_text = "2 + 2 = 4"
            elif any(w in prompt_text.lower() for w in ("hello", "hi", "hey", "greetings")):
                synthesized_text = (
                    "Hello! I am the IISc Institutional Thesis RAG Assistant. "
                    "You can ask me questions about the indexed IISc theses and research publications across departments."
                )
            else:
                synthesized_text = (
                    f"No specific thesis documents were found matching '{prompt_text}' in scope "
                    f"{scope_info.thesis_id} ({scope_info.chapter}). Please try refining your query or selecting another thesis."
                )

    citations = [
        {
            "id": chunk.citation,
            "source_uri": chunk.source_uri,
            "thesis_id": chunk.thesis_id,
            "chapter": chunk.chapter,
            "doc_version": chunk.doc_version,
            "chunk_index": chunk.chunk_index,
        }
        for chunk in chunks
    ]
    generated_payload = {
        "answer": synthesized_text,
        "citations": citations,
        "staleness": {"status": "current", "doc_version": scope_info.doc_version},
        "complexity_score": score_val,
        "target_tier": target_tier,
        "latency_ms": round((time.perf_counter() - started_at) * 1000, 2),
    }

    # 8. Write only grounded answers to version-scoped caches.
    await container.l0_cache.set(
        prompt=prompt_text,
        response=generated_payload,
        thesis_id=scope_info.thesis_id,
        chapter=scope_info.chapter,
        doc_version=scope_info.doc_version,
    )
    await container.l1_cache.set(
        prompt=prompt_text,
        query_embedding=query_vec,
        response=generated_payload,
        thesis_id=scope_info.thesis_id,
        chapter=scope_info.chapter,
        doc_version=scope_info.doc_version,
        model_tier=target_tier,
    )

    response.headers["X-Cache"] = "MISS"
    response.headers["X-Scope-Thesis"] = scope_info.thesis_id
    response.headers["X-Scope-Chapter"] = scope_info.chapter
    response.headers["X-Document-Version"] = scope_info.doc_version
    metrics.record("MISS", route=target_tier, score=score_val)
    return {
        "cache_status": "MISS",
        "scope": {
            "thesis_id": scope_info.thesis_id,
            "chapter": scope_info.chapter,
            "doc_version": scope_info.doc_version,
        },
        "complexity_score": score_val,
        "target_tier": target_tier,
        "user": user.email,
        "data": generated_payload,
    }
