import hashlib
import math
import random
from typing import List, Optional

class LocalEmbedder:
    def __init__(self, model_name_or_path="all-MiniLM-L6-v2", cache_folder=None, dimension=384):
        self.dimension = dimension
        self.model_name_or_path = model_name_or_path
        self._model = None
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.model_name_or_path)
        except Exception:
            self._model = None

    def _fallback_vector(self, text: str) -> List[float]:
        h = hashlib.sha256(text.encode("utf-8")).digest()
        seed = int.from_bytes(h[:4], "little")
        rng = random.Random(seed)
        vec = [rng.gauss(0, 1) for _ in range(self.dimension)]
        mag = math.sqrt(sum(x * x for x in vec))
        return [x / (mag + 1e-9) for x in vec]

    def embed_query(self, text: str) -> List[float]:
        if self._model:
            return self._model.encode(text, normalize_embeddings=True).tolist()
        return self._fallback_vector(text)

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        if self._model:
            return self._model.encode(texts, batch_size=32, normalize_embeddings=True).tolist()
        return [self._fallback_vector(t) for t in texts]
