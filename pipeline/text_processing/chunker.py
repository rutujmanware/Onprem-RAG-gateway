import re
from typing import Any, Dict, List, Optional

class TextChunk:
    def __init__(self, chunk_id: str, doc_id: str, content: str, chunk_index: int, metadata=None):
        self.chunk_id = chunk_id
        self.doc_id = doc_id
        self.content = content
        self.chunk_index = chunk_index
        self.metadata = metadata or {}

class RecursiveCharacterChunker:
    """Token-approximate chunker; whitespace/punctuation tokens are deterministic.

    It avoids a runtime tokenizer dependency while keeping the project-required
    1,200–2,000 token chunks and 150-token overlap.
    """
    def __init__(self, chunk_size=1600, chunk_overlap=150):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def chunk_document(self, doc_id: str, text: str, metadata=None) -> List[TextChunk]:
        chunks = []
        tokens = re.findall(r"\S+", text)
        start = 0
        idx = 0
        while start < len(tokens):
            end = min(start + self.chunk_size, len(tokens))
            chunk_content = " ".join(tokens[start:end]).strip()
            if chunk_content:
                # Preserve a deterministic chapter tag for retrieval/cache scope.
                prefix = " ".join(tokens[:start])
                current_start = " ".join(tokens[start:start + min(40, end - start)])
                headings = list(re.finditer(r"\b(?:chapter|ch\.)\s+([0-9]+|[ivxlcdm]+)\b", prefix, re.I))
                headings += list(re.finditer(r"\b(?:chapter|ch\.)\s+([0-9]+|[ivxlcdm]+)\b", current_start, re.I))
                chunk_meta = dict(metadata or {})
                if headings:
                    chapter_number = headings[-1].group(1).lower()
                    chunk_meta["chapter"] = f"ch_{chapter_number}"
                else:
                    chunk_meta.setdefault("chapter", "all")
                chunks.append(TextChunk(f"{doc_id}_chunk_{idx}", doc_id, chunk_content, idx, chunk_meta))
                idx += 1
            if end >= len(tokens): break
            start += self.chunk_size - self.chunk_overlap
        return chunks
