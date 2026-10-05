import asyncio
from gateway.cache.l0_exact import L0ExactCache

async def test_l0():
    c = L0ExactCache()
    await c.set("hi", {"resp": "hello"})
    res = await c.get("hi")
    assert res["resp"] == "hello"

if __name__ == "__main__":
    asyncio.run(test_l0())
    print("Cache hit tests passed.")
