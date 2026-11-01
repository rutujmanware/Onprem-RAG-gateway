from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from gateway.dependencies import (
    container, get_l0_cache, get_l1_cache, get_complexity_scorer,
    get_dispatcher, get_admission_controller, get_slm_client, get_vllm_client
)
from gateway.auth.guard import UserIdentity, require_iisc_user

app = FastAPI(title="On-Premise RAG Gateway")

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatCompletionRequest(BaseModel):
    messages: List[ChatMessage]
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = 512

@app.get("/healthz")
async def healthz(): return {"status": "ok"}

@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest, user: UserIdentity = Depends(require_iisc_user)):
    return {
        "id": "chatcmpl-local",
        "choices": [{"message": {"role": "assistant", "content": "Gateway response."}}],
        "user": user.email
    }
