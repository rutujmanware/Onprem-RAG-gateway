"""Stand-in for llama-server / vllm used to test deploy/user/askiisc.sh. Usage: stub_server.py NAME KEYENV PORT"""
import json, os, sys
import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
name, keyenv, port = sys.argv[1], sys.argv[2], int(sys.argv[3]); key = os.environ[keyenv]
app = FastAPI()
def auth(a):
    if a != f"Bearer {key}": raise HTTPException(401)
@app.get("/v1/models")
async def models(authorization: str = Header(None)): auth(authorization); return {"data": []}
@app.post("/v1/chat/completions")
async def chat(body: dict, authorization: str = Header(None)):
    auth(authorization)
    async def g():
        for w in [f"{name}-answer", " ok"]:
            yield "data: " + json.dumps({"choices": [{"delta": {"content": w}, "logprobs": {"content": [{"logprob": -0.05}]}}]}) + "\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(g(), media_type="text/event-stream")
uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")
