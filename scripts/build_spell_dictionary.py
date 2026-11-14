#!/usr/bin/env python3
"""Build the corpus spelling dictionary used for topic-word correction.

Counts, for every word, the number of distinct theses whose TITLE contains it (titles are clean
metadata, unlike OCR'd body text, so OCR junk never enters the dictionary). Output: one "word count"
per line, the format symspellpy loads. Re-run after ingesting new theses:

    python scripts/build_spell_dictionary.py            # -> models/spell/thesis_title_terms.txt
"""
import re
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "models" / "spell" / "thesis_title_terms.txt"


def main():
    from gateway.cache.redis_semantic_cache import RedisSemanticCache
    r = RedisSemanticCache.get_instance().sync_client
    t0 = time.time()
    res = r.execute_command("FT.AGGREGATE", "rag_documents_idx", "*", "LOAD", "1", "@title", "GROUPBY", "1", "@title",
                            "WITHCURSOR", "COUNT", "5000", "TIMEOUT", "300000", "DIALECT", "2")
    titles = []
    while True:
        page, cursor = res if isinstance(res, (list, tuple)) else (res, 0)
        rows = page.get(b"results", page.get("results", [])) if isinstance(page, dict) else page[1:]
        for row in rows:
            attrs = row.get(b"extra_attributes", row.get("extra_attributes", row)) if isinstance(row, dict) else dict(zip(row[::2], row[1::2]))
            t = attrs.get(b"title") or attrs.get("title") or b""
            titles.append(t.decode("utf-8", "ignore") if isinstance(t, bytes) else str(t))
        if not cursor:
            break
        res = r.execute_command("FT.CURSOR", "READ", "rag_documents_idx", cursor, "COUNT", "5000")
    df = Counter()
    for t in titles:
        df.update({w for w in re.findall(r"[a-z][a-z0-9]+", t.lower()) if len(w) >= 3})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w") as f:
        for w, n in sorted(df.items(), key=lambda x: (-x[1], x[0])):
            f.write(f"{w} {n}\n")
    print(f"{len(titles)} distinct titles, {len(df)} title words -> {OUT} ({time.time() - t0:.1f}s)")


if __name__ == "__main__":
    main()
