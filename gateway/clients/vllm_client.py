import httpx
class VLLMClient:
    def __init__(self, base_url=None):
        self.base_url = base_url or "http://localhost:8000/v1"
    async def check_health(self): return True
    async def generate_chat(self, messages, **kwargs):
        return {"choices": [{"message": {"role": "assistant", "content": "GPU vLLM generated answer."}}]}
    async def close(self): pass

