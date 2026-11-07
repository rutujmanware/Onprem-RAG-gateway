from typing import Any, Dict, List, Optional

class TextChunk:
    def __init__(self, chunk_id: str, doc_id: str, content: str, chunk_index: int, metadata=None):
        self.chunk_id = chunk_id
        self.doc_id = doc_id
        self.content = content
        self.chunk_index = chunk_index
        self.metadata = metadata or {}

class RecursiveCharacterChunker:
    def __init__(self, chunk_size=512, chunk_overlap=64):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def chunk_document(self, doc_id: str, text: str, metadata=None) -> List[TextChunk]:
        chunks = []
        start = 0
        idx = 0
        text_len = len(text)
        while start < text_len:
            end = min(start + self.chunk_size, text_len)
            chunk_content = text[start:end].strip()
            if chunk_content:
                chunks.append(TextChunk(f"{doc_id}_chunk_{idx}", doc_id, chunk_content, idx, metadata))
                idx += 1
            if end >= text_len: break
            start += self.chunk_size - self.chunk_overlap
        return chunks
