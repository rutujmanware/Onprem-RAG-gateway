import os
import json
import random
from collections import defaultdict
import pandas as pd
from vllm import LLM, SamplingParams
import redis

def generate():
    print("🚀 Connecting to Redis database...")
    # Read the REDIS_URL from environment (needs to be sourced from .env)
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    r = redis.from_url(redis_url, decode_responses=True)

    print("🔍 Fetching chunks from Redis and indexing by document...")
    # Scan chunk keys from Redis
    candidate_keys = []
    cursor = '0'
    while cursor != 0 and len(candidate_keys) < 250000:
        cursor, keys = r.scan(cursor=cursor, match="doc:chunk:*", count=10000)
        candidate_keys.extend(keys)

    print(f"📦 Total raw chunk keys fetched: {len(candidate_keys)}")

    # Group chunk keys by document ID: key format is doc:chunk:{doc_id}_chunk_{idx}
    docs_to_chunks = defaultdict(list)
    for key in candidate_keys:
        if "_chunk_" in key:
            base_id = key.rsplit("_chunk_", 1)[0]
            docs_to_chunks[base_id].append(key)

    doc_ids = list(docs_to_chunks.keys())
    # Sort chunks within each doc by chunk_index
    for doc in doc_ids:
        docs_to_chunks[doc].sort(
            key=lambda k: int(k.rsplit("_chunk_", 1)[1]) if k.rsplit("_chunk_", 1)[1].isdigit() else 0
        )

    print(f"📚 Indexed {len(doc_ids)} unique documents from Redis.")

    # Target number of dataset rows
    TOTAL_SAMPLES = 100000
    print(f"🎯 Sampling {TOTAL_SAMPLES} dynamic multi-chunk combinations (3 to 10 chunks, intra-doc and cross-doc)...")

    sample_tasks = []
    # 50% intra-document (single doc multi-chunk), 50% cross-document (2-4 docs)
    for _ in range(TOTAL_SAMPLES):
        is_cross_doc = random.random() < 0.5
        target_k = random.randint(3, 10)

        selected_chunk_keys = []
        if not is_cross_doc:
            # Pick a doc with at least 3 chunks
            for _ in range(5):
                candidate_doc = random.choice(doc_ids)
                chunks_available = docs_to_chunks[candidate_doc]
                if len(chunks_available) >= 3:
                    break
            else:
                candidate_doc = random.choice(doc_ids)
                chunks_available = docs_to_chunks[candidate_doc]

            k = min(target_k, len(chunks_available))
            if k < 3 and len(chunks_available) < 3:
                # Fallback to cross-doc if single document has fewer than 3 chunks
                is_cross_doc = True
            else:
                max_start = max(0, len(chunks_available) - k)
                start_idx = random.randint(0, max_start)
                selected_chunk_keys = [(candidate_doc, k_id) for k_id in chunks_available[start_idx : start_idx + k]]

        if is_cross_doc:
            # Pick between 2 and 4 different documents
            num_docs = random.randint(2, min(4, len(doc_ids)))
            chosen_docs = random.sample(doc_ids, num_docs)

            # Distribute target_k chunks among chosen_docs
            allocated_keys = []
            for d in chosen_docs:
                doc_chunks = docs_to_chunks[d]
                if doc_chunks:
                    allocated_keys.append((d, random.choice(doc_chunks)))

            # If we need more chunks to reach target_k, add more from chosen_docs
            while len(allocated_keys) < target_k:
                d = random.choice(chosen_docs)
                remaining = [k for k in docs_to_chunks[d] if (d, k) not in allocated_keys]
                if remaining:
                    allocated_keys.append((d, random.choice(remaining)))
                else:
                    break

            selected_chunk_keys = allocated_keys

        if len(selected_chunk_keys) >= 3:
            sample_tasks.append((is_cross_doc, selected_chunk_keys))

    print(f"✅ Prepared {len(sample_tasks)} chunk combinations. Fetching contents from Redis via pipeline...")

    # Fetch contents using Redis pipeline in batches for maximum speed
    prompts = []
    metadata_list = []
    BATCH_SIZE = 5000

    for i in range(0, len(sample_tasks), BATCH_SIZE):
        batch = sample_tasks[i : i + BATCH_SIZE]
        pipe = r.pipeline(transaction=False)
        for is_cross, chunk_tuples in batch:
            for doc_id, chunk_key in chunk_tuples:
                pipe.hmget(chunk_key, ["content", "title"])

        results = pipe.execute()
        res_idx = 0

        for is_cross, chunk_tuples in batch:
            chunk_items = []
            for doc_id, chunk_key in chunk_tuples:
                content, title = results[res_idx]
                res_idx += 1
                if content:
                    chunk_items.append({
                        "doc_id": doc_id,
                        "title": title or doc_id,
                        "content": content
                    })

            if len(chunk_items) >= 3:
                k = len(chunk_items)
                unique_docs = len(set(c["doc_id"] for c in chunk_items))

                if unique_docs > 1:
                    formatted_chunks = "\n\n".join([
                        f"[Document: {c['title']} | Chunk {idx+1}]:\n{c['content']}"
                        for idx, c in enumerate(chunk_items)
                    ])
                    doc_context_desc = f"{k} text chunks extracted across {unique_docs} different research documents"
                    dep_instruction = (
                        "The query MUST require cross-document comparison, synthesis, or connecting ideas "
                        "between the different documents provided."
                    )
                    query_type = "cross_document"
                else:
                    formatted_chunks = "\n\n".join([
                        f"[Chunk {idx+1}]:\n{c['content']}"
                        for idx, c in enumerate(chunk_items)
                    ])
                    doc_context_desc = f"{k} related text chunks extracted from an academic paper"
                    dep_instruction = (
                        "The query MUST require synthesizing information, multi-step logic, or tracking concepts "
                        "across MULTIPLE chunks. It must not be answerable from a single chunk alone."
                    )
                    query_type = "intra_document"

                prompt = f"""You are an expert AI dataset curator.
Read the following {doc_context_desc}.

{formatted_chunks}

Based on these {k} text chunks, generate ONE realistic query that a human user might ask.

CRITICAL REQUIREMENTS:
1. MULTI-CHUNK & MULTI-DOCUMENT DEPENDENCY:
   - {dep_instruction}
   - It must NOT be answerable by reading any single chunk alone.
2. DIVERSITY: Create varied question types (e.g., cross-paper comparison of methodologies or results, multi-hop reasoning across sections, holistic technical synthesis).
3. CLASSIFICATION: Classify the query's complexity into exactly one label:
   - "easy": Direct multi-point lookup, simple factual aggregation, or straightforward side-by-side comparison that a small 3B model could answer once given the context.
   - "hard": Deep domain reasoning, complex trade-off analysis, technical conflict resolution, or multi-step algorithmic deduction that strictly requires a 7B+ model.

Output ONLY valid JSON in this exact format, with no markdown formatting:
{{"query": "The generated question here", "label": "easy or hard"}}
"""
                prompts.append(prompt)
                metadata_list.append({
                    "num_chunks": k,
                    "num_docs": unique_docs,
                    "query_type": query_type
                })

    print(f"📦 Successfully constructed {len(prompts)} prompts. Loading the 72B LLM into the GPUs...")

    # Load Qwen2.5-72B-Instruct using vLLM across 4 GPUs
    # max_model_len=3072 safely supports up to 10 chunks (1200 tokens) + prompt + generation
    llm = LLM(
        model="Qwen/Qwen2.5-72B-Instruct-GPTQ-Int8",
        tensor_parallel_size=4,
        download_dir="/dev/shm/huggingface_cache",
        trust_remote_code=True,
        gpu_memory_utilization=0.8,
        max_model_len=3072,
        enforce_eager=True
    )

    print("🧠 Model loaded! Generating dataset queries with vLLM...")
    sampling_params = SamplingParams(temperature=0.1, max_tokens=100)
    outputs = llm.generate(prompts, sampling_params)

    print("💾 Generation finished! Parsing outputs...")
    dataset = []
    for output, meta in zip(outputs, metadata_list):
        generated_text = output.outputs[0].text.strip()
        try:
            data = json.loads(generated_text)
            if "query" in data and "label" in data:
                dataset.append({
                    "query": data["query"],
                    "label": data["label"].lower(),
                    "num_chunks": meta["num_chunks"],
                    "num_docs": meta["num_docs"],
                    "query_type": meta["query_type"]
                })
        except Exception:
            pass

    output_csv = "router_multichunk_training_dataset.csv"
    output_jsonl = "router_multichunk_training_dataset.jsonl"
    df = pd.DataFrame(dataset)
    df.to_csv(output_csv, index=False, escapechar="\\")
    df.to_json(output_jsonl, orient="records", lines=True)

    print(f"🎉 SUCCESS! Dataset saved to {output_csv} and {output_jsonl} with {len(df)} rows.")

if __name__ == "__main__":
    generate()
