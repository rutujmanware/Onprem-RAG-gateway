from typing import List, Optional

class LocalEmbedder:
    def __init__(self, model_name_or_path="all-MiniLM-L6-v2", cache_folder=None, dimension=384):
        self.dimension = dimension
        self.model_name_or_path = model_name_or_path
        self._model = None
        self._load_error = None
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.model_name_or_path, cache_folder=cache_folder, local_files_only=True)
        except Exception as exc:
            self._load_error = exc
            self._model = None

    def _require_model(self):
        if self._model is None:
            raise RuntimeError(
                f"Local embedding model '{self.model_name_or_path}' is unavailable; "
                "provision it in the on-prem model mirror before starting the gateway."
            ) from self._load_error

    def embed_query(self, text: str) -> List[float]:
        self._require_model()
        return self._model.encode(text, normalize_embeddings=True).tolist()

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        self._require_model()
        return self._model.encode(texts, batch_size=32, normalize_embeddings=True).tolist()
