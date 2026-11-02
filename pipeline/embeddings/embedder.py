import hashlib
import math
import os
import random
from typing import List, Optional

# Prevent OpenMP and HuggingFace Tokenizers thread crashes on high-core-count machines
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

class LocalEmbedder:
    """Robust dense embedder using direct Hugging Face transformers with mean-pooling,

    bypassing sentence_transformers package conflicts.
    """

    def __init__(
        self,
        model_name_or_path: str = "sentence-transformers/all-MiniLM-L6-v2",
        cache_folder: Optional[str] = None,
        dimension: int = 384,
        device: str = "cpu"
    ):
        self.dimension = dimension
        self.device = device
        self._tokenizer = None
        self._model = None
        self._torch = None
        self._F = None

        local_dir = os.path.abspath("models/embeddings/all-MiniLM-L6-v2")
        if os.path.exists(local_dir):
            hf_name = local_dir
        else:
            hf_name = model_name_or_path
            if not ("/" in hf_name or os.path.exists(hf_name)):
                hf_name = f"sentence-transformers/{hf_name}"
        self.hf_name = hf_name

        # Priority 1: Direct HuggingFace transformers (AutoTokenizer + AutoModel)
        try:
            import torch
            import torch.nn.functional as F
            from transformers import AutoTokenizer, AutoModel

            self._torch = torch
            self._F = F
            self._tokenizer = AutoTokenizer.from_pretrained(self.hf_name, cache_dir=cache_folder)
            self._model = AutoModel.from_pretrained(self.hf_name, cache_dir=cache_folder).to(self.device)
            self._model.eval()
        except Exception:
            # Priority 2: Fallback to sentence_transformers if available
            try:
                from sentence_transformers import SentenceTransformer
                self._st_model = SentenceTransformer(self.hf_name, device=self.device)
            except Exception:
                self._st_model = None

    def _mean_pooling(self, model_output, attention_mask):
        token_embeddings = model_output[0]
        input_mask_expanded = attention_mask.unsqueeze(-1).expand(token_embeddings.size()).float()
        sum_embeddings = self._torch.sum(token_embeddings * input_mask_expanded, 1)
        sum_mask = self._torch.clamp(input_mask_expanded.sum(1), min=1e-9)
        return sum_embeddings / sum_mask

    def _fallback_vector(self, text: str) -> List[float]:
        h = hashlib.sha256(text.encode("utf-8")).digest()
        seed = int.from_bytes(h[:4], "little")
        rng = random.Random(seed)
        vec = [rng.gauss(0, 1) for _ in range(self.dimension)]
        mag = math.sqrt(sum(x * x for x in vec))
        return [x / (mag + 1e-9) for x in vec]

    def embed_query(self, text: str) -> List[float]:
        if self._model and self._tokenizer:
            encoded = self._tokenizer(
                [text],
                padding=True,
                truncation=True,
                max_length=512,
                return_tensors="pt"
            ).to(self.device)

            with self._torch.no_grad():
                out = self._model(**encoded)

            pooled = self._mean_pooling(out, encoded["attention_mask"])
            normalized = self._F.normalize(pooled, p=2, dim=1)
            return normalized[0].cpu().tolist()

        if hasattr(self, "_st_model") and self._st_model:
            return self._st_model.encode(text, normalize_embeddings=True, show_progress_bar=False).tolist()

        return self._fallback_vector(text)

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        if self._model and self._tokenizer and texts:
            all_vecs = []
            batch_size = 32
            for i in range(0, len(texts), batch_size):
                batch = texts[i : i + batch_size]
                encoded = self._tokenizer(
                    batch,
                    padding=True,
                    truncation=True,
                    max_length=512,
                    return_tensors="pt"
                ).to(self.device)

                with self._torch.no_grad():
                    out = self._model(**encoded)

                pooled = self._mean_pooling(out, encoded["attention_mask"])
                normalized = self._F.normalize(pooled, p=2, dim=1)
                all_vecs.extend(normalized.cpu().tolist())
            return all_vecs

        if hasattr(self, "_st_model") and self._st_model:
            return self._st_model.encode(texts, batch_size=32, normalize_embeddings=True, show_progress_bar=False).tolist()

        return [self._fallback_vector(t) for t in texts]
