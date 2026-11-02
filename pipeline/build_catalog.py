"""Script to scan all indexed theses in Redis and generate doc:catalog metadata keys."""

import asyncio
import json
import logging
import time
from redis.asyncio import Redis

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("build_catalog")


async def build_catalog(redis_url: str = "redis://:Redis*2703!@localhost:6379/0"):
    r = Redis.from_url(redis_url)
    t0 = time.time()
    logger.info("Scanning chunk_0 keys to discover all indexed theses...")

    chunk0_keys = []
    async for key in r.scan_iter(match="doc:chunk:*_chunk_0", count=5000):
        chunk0_keys.append(key)

    total_theses = len(chunk0_keys)
    logger.info(f"Discovered {total_theses} indexed theses in {time.time() - t0:.2f}s. Extracting metadata...")

    # Fetch metadata in batches of 500
    batch_size = 500
    catalog_records = []
    for i in range(0, total_theses, batch_size):
        batch = chunk0_keys[i : i + batch_size]
        p = r.pipeline(transaction=False)
        for k in batch:
            p.hmget(k, "doc_id", "title", "author", "department", "date", "degree")
        rows = await p.execute()
        for k, row in zip(batch, rows):
            if not row or not row[0]:
                continue
            doc_id = row[0].decode("utf-8", errors="replace")
            title = row[1].decode("utf-8", errors="replace") if row[1] else doc_id
            author = row[2].decode("utf-8", errors="replace") if row[2] else ""
            dept_raw = row[3].decode("utf-8", errors="replace") if row[3] else "IISc"
            dept = dept_raw.split("/")[0].strip()
            date = row[4].decode("utf-8", errors="replace") if row[4] else ""
            year = date[:4] if len(date) >= 4 else "2024"

            # Clean up author if duplicated like "Singh, Namrata, Singh, Namrata"
            author_parts = [a.strip() for a in author.split(",") if a.strip()]
            if len(author_parts) >= 4 and author_parts[:2] == author_parts[2:4]:
                clean_author = ", ".join(author_parts[:2])
            else:
                clean_author = author

            record = {
                "thesis_id": doc_id,
                "title": title,
                "author": clean_author,
                "department": dept,
                "year": year,
                "chapters": [{"id": "all", "title": "Whole Document"}],
            }
            catalog_records.append((doc_id, record))

    logger.info(f"Extracted metadata for {len(catalog_records)} theses. Writing doc:catalog:* to Redis...")

    # Write to Redis in batches
    access_policy = json.dumps({
        "roles": ["student", "faculty", "examiner", "cluster_admin", "faculty_lead"],
        "users": [],
    })
    write_pipe = r.pipeline(transaction=False)
    for idx, (doc_id, rec) in enumerate(catalog_records):
        write_pipe.set(f"doc:catalog:{doc_id}", json.dumps(rec))
        write_pipe.set(f"doc:version:{doc_id}:current", "v1")
        write_pipe.set(f"doc:access:{doc_id}", access_policy)
        if (idx + 1) % 500 == 0:
            await write_pipe.execute()
            write_pipe = r.pipeline(transaction=False)

    if len(write_pipe.command_stack) > 0:
        await write_pipe.execute()

    total_time = time.time() - t0
    logger.info(f"Successfully generated catalog for {len(catalog_records)} theses in {total_time:.2f}s!")
    await r.aclose()


if __name__ == "__main__":
    asyncio.run(build_catalog())

