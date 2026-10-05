"""Batch Ingestion & Vector Indexing for PDFs.

Scans downloaded PDFs (including DSpace metadata sidecars), runs layout-aware extraction,
cleaning, chunking, dense embedding generation, and bulk indexing into Redis Stack RediSearch.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import logging
import os
import sys
import time
from typing import List, Optional

from pipeline.crawlers.pdf_extractor import PDFExtractor
from pipeline.indexer import RedisDocumentIndexer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("batch_ingester")


class BatchPDFIngester:
    """Manages full directory ingestion of PDFs into Redis Stack vector store."""

    def __init__(
        self,
        pdf_dir: str = "data/raw_pdfs",
        redis_url: Optional[str] = None,
        index_name: str = "rag_documents_idx",
        vector_dim: int = 384,
    ):
        self.pdf_dir = pdf_dir
        self.redis_url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self.index_name = index_name
        self.vector_dim = vector_dim
        self.pdf_extractor = PDFExtractor()
        self.indexer = RedisDocumentIndexer(
            redis_url=self.redis_url,
            index_name=self.index_name,
            vector_dim=self.vector_dim,
        )

    def find_pdfs(self) -> List[str]:
        """Locates all PDF files in the target directory recursively."""
        pattern = os.path.join(self.pdf_dir, "**", "*.pdf")
        files = glob.glob(pattern, recursive=True)
        return sorted(files)

    def _load_sidecar_metadata(self, pdf_path: str) -> dict:
        """Loads JSON metadata sidecar if produced by dspace_crawler."""
        json_path = os.path.splitext(pdf_path)[0] + ".json"
        if os.path.exists(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    async def run(self) -> dict:
        """Processes and indexes all PDFs into the vector database."""
        start_time = time.time()
        pdf_files = self.find_pdfs()

        if not pdf_files:
            print(f"[WARN] No PDF files found in '{self.pdf_dir}'. Please run the downloader first.", flush=True)
            return {"total_pdfs": 0, "total_chunks": 0, "elapsed_sec": 0.0}

        print(f"[START] Found {len(pdf_files)} PDF file(s) in '{self.pdf_dir}'. Starting vector ingestion...", flush=True)

        total_chunks = 0
        successful_docs = 0

        for idx, pdf_path in enumerate(pdf_files, 1):
            filename = os.path.basename(pdf_path)
            doc_id = os.path.splitext(filename)[0]
            print(f"[{idx}/{len(pdf_files)}] Extracting: {filename}...", flush=True)

            sidecar = self._load_sidecar_metadata(pdf_path)

            extracted = self.pdf_extractor.extract(pdf_path)
            if not extracted or not extracted.full_text.strip():
                try:
                    with open(pdf_path, "r", encoding="utf-8", errors="ignore") as f:
                        raw_text = f.read()
                    metadata = {"source": filename, "extractor": "raw_fallback", **sidecar}
                except Exception as exc:
                    print(f"[ERR] Could not extract text from {filename}: {exc}", flush=True)
                    continue
            else:
                raw_text = extracted.full_text
                if sidecar.get("abstract"):
                    raw_text = f"Title: {sidecar.get('title', '')}\nAbstract: {sidecar['abstract']}\n\n" + raw_text

                metadata = {
                    "source": filename,
                    "title": sidecar.get("title") or extracted.title,
                    "pages": extracted.metadata.get("total_pages", 1),
                    **extracted.metadata,
                    **sidecar,
                }

            try:
                num_chunks = await self.indexer.index_document(
                    doc_id=doc_id,
                    raw_text=raw_text,
                    metadata=metadata,
                    is_paper=True,
                )
                total_chunks += num_chunks
                successful_docs += 1
                print(f"[OK] Indexed '{filename}' -> {num_chunks} vector chunks.", flush=True)
            except Exception as exc:
                print(f"[ERR] Failed to index {filename}: {exc}", flush=True)

        elapsed = time.time() - start_time
        summary = {
            "total_pdfs": len(pdf_files),
            "successful_docs": successful_docs,
            "total_chunks": total_chunks,
            "elapsed_sec": round(elapsed, 2),
            "index_name": self.index_name,
        }

        print("\n" + "=" * 60, flush=True)
        print("Ingestion Summary:", flush=True)
        print(f"  Processed PDFs   : {successful_docs} / {len(pdf_files)}", flush=True)
        print(f"  Total Chunks     : {total_chunks}", flush=True)
        print(f"  Total Time       : {elapsed:.2f} seconds", flush=True)
        print(f"  Vector Index     : {self.index_name}", flush=True)
        print("=" * 60 + "\n", flush=True)

        return summary


def main():
    parser = argparse.ArgumentParser(description="Batch index PDFs into Redis Stack vector search")
    parser.add_argument("--pdf-dir", default="data/raw_pdfs", help="Directory containing PDF files")
    parser.add_argument("--redis-url", default=os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    parser.add_argument("--index-name", default="rag_documents_idx")
    parser.add_argument("--vector-dim", type=int, default=384)

    args = parser.parse_args()
    ingester = BatchPDFIngester(
        pdf_dir=args.pdf_dir,
        redis_url=args.redis_url,
        index_name=args.index_name,
        vector_dim=args.vector_dim,
    )
    asyncio.run(ingester.run())


if __name__ == "__main__":
    main()
