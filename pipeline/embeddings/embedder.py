import hashlib
import logging
import math
import os
import random
import threading
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("pipeline.embedder")

# Prevent OpenMP and HuggingFace Tokenizers thread crashes on high-core-count machines
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# One loaded (tokenizer, model) per (model path, device) per process. The gateway used to build a new
# RAGQAPipeline (router + retriever, each with its own embedder) for every fresh request, reloading
# MiniLM from disk twice (~110 ms per request). All LocalEmbedder instances now share the weights.
_MODEL_CACHE: Dict[Tuple[str, str, Optional[str]], Tuple[object, object, threading.Lock]] = {}
_MODEL_CACHE_LOCK = threading.Lock()

# ONNX Runtime backend (NFR-1, semantic-cache p99). scripts/export_embedder_onnx.py exports the same
# MiniLM model to <model dir>/onnx/model.onnx and verifies it against PyTorch (cosine >= 0.99999), so
# the document index, the semantic cache and the router (all built with PyTorch vectors) are unaffected.
# Measured on this host: ~2x faster per query. EMBED_BACKEND=auto (default: ONNX when the file exists,
# else PyTorch) | onnx (required) | torch.
_ORT_SESSIONS: Dict[str, object] = {}


def _ort_session(path: str):
    with _MODEL_CACHE_LOCK:
        if path not in _ORT_SESSIONS:
            import onnxruntime as ort
            so = ort.SessionOptions()
            so.intra_op_num_threads = max(1, int(os.getenv("EMBED_NUM_THREADS", "8")))
            so.inter_op_num_threads = 1
            so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            # Idle threads sleep instead of spinning: 6 gateway workers share a busy host (latency cost < 1 ms).
            so.add_session_config_entry("session.intra_op.allow_spinning", os.getenv("EMBED_ORT_SPIN", "0"))
            _ORT_SESSIONS[path] = ort.InferenceSession(path, so, providers=["CPUExecutionProvider"])
        return _ORT_SESSIONS[path]


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
        self._tok_lock = threading.Lock()

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
            # NFR-1: torch defaults to one thread per core (128 here). With 6 gateway workers embedding
            # concurrently that oversubscribes the CPU and inflates semantic-cache p99. OMP_NUM_THREADS set
            # above is ignored once torch was imported earlier (the router imports it first), so set it here.
            embed_threads = int(os.getenv("EMBED_NUM_THREADS", "16"))
            if embed_threads > 0 and device == "cpu":
                torch.set_num_threads(embed_threads)
            key = (self.hf_name, str(self.device), cache_folder)
            with _MODEL_CACHE_LOCK:
                if key not in _MODEL_CACHE:
                    tok = AutoTokenizer.from_pretrained(self.hf_name, cache_dir=cache_folder)
                    model = AutoModel.from_pretrained(self.hf_name, cache_dir=cache_folder).to(self.device)
                    model.eval()
                    # Fast (Rust) tokenizers are not safe for concurrent calls ("Already borrowed"),
                    # so tokenization is serialized per shared tokenizer (~0.1 ms); inference is not.
                    _MODEL_CACHE[key] = (tok, model, threading.Lock())
                self._tokenizer, self._model, self._tok_lock = _MODEL_CACHE[key]
        except Exception:
            # Priority 2: Fallback to sentence_transformers if available
            try:
                from sentence_transformers import SentenceTransformer
                self._st_model = SentenceTransformer(self.hf_name, device=self.device)
            except Exception:
                self._st_model = None

        self._ort = None
        backend = os.getenv("EMBED_BACKEND", "auto").lower()
        onnx_path = os.path.join(self.hf_name, "onnx", "model.onnx") if os.path.isdir(self.hf_name) else ""
        if backend in ("auto", "onnx") and str(device) == "cpu" and self._tokenizer is not None:
            if onnx_path and os.path.exists(onnx_path):
                try:
                    self._ort = _ort_session(onnx_path)
                    self._ort_inputs = [i.name for i in self._ort.get_inputs()]
                except Exception as exc:
                    if backend == "onnx":
                        raise
                    logger.warning("ONNX embedder unavailable (%s); using PyTorch", exc)
        if backend == "onnx" and self._ort is None:
            raise RuntimeError(f"EMBED_BACKEND=onnx but no usable ONNX model for {self.hf_name}: "
                               "run scripts/export_embedder_onnx.py")
        self.backend = "onnx" if self._ort is not None else ("torch" if self._model is not None else "fallback")

    def _ort_embed(self, texts: List[str]):
        import numpy as np
        with self._tok_lock:
            enc = self._tokenizer(texts, padding=True, truncation=True, max_length=512, return_tensors="np")
        feeds = {}
        for name in self._ort_inputs:
            arr = enc.get(name)
            feeds[name] = (arr if arr is not None else np.zeros_like(enc["input_ids"])).astype(np.int64)
        hidden = self._ort.run(None, feeds)[0]
        mask = enc["attention_mask"][..., None].astype(np.float32)
        pooled = (hidden * mask).sum(1) / np.clip(mask.sum(1), 1e-9, None)
        return pooled / np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12, None)

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
        if self._ort is not None:
            return self._ort_embed([text])[0].tolist()
        if self._model and self._tokenizer:
            with self._tok_lock:
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
        if self._ort is not None and texts:
            out: List[List[float]] = []
            for i in range(0, len(texts), 32):
                out.extend(self._ort_embed(texts[i:i + 32]).tolist())
            return out
        if self._model and self._tokenizer and texts:
            all_vecs = []
            batch_size = 32
            for i in range(0, len(texts), batch_size):
                batch = texts[i : i + batch_size]
                with self._tok_lock:
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
