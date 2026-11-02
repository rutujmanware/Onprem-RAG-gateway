import os
import sys
import torch
import numpy as np

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Import local router components directly
sys.path.insert(0, os.path.dirname(__file__))
from model import RouterMLP
from features import extract_hybrid_features

# Priority: Use LocalEmbedder from pipeline to bypass sentence_transformers/huggingface_hub package conflicts
try:
    from pipeline.embeddings.embedder import LocalEmbedder
    _USE_LOCAL_EMBEDDER = True
except Exception:
    from sentence_transformers import SentenceTransformer
    _USE_LOCAL_EMBEDDER = False

class QueryRouter:
    """
    Production-ready Institutional Query Router:
    Takes strictly a single user query string: `predict(query_text)`.
    Performs zero database calls, zero retrieval lookups.
    Computes a 400-d hybrid representation (384-d semantic + 16-d lexical/intent)
    and predicts whether the query should route to CPU SLM or GPU vLLM.
    """
    def __init__(self, model_path=None, threshold=0.5, device=None):
        if device is None:
            self.device = torch.device('cuda:5' if torch.cuda.is_available() and torch.cuda.device_count() > 5 else ('cuda:0' if torch.cuda.is_available() else 'cpu'))
        else:
            self.device = torch.device(device)
            
        self.threshold = threshold

        # 1. Load Embedder Offline (direct Transformers mean-pooling)
        local_embed_dir = os.path.join(BASE_DIR, "models/embeddings/all-MiniLM-L6-v2")
        model_source = local_embed_dir if os.path.exists(local_embed_dir) else "all-MiniLM-L6-v2"
        dev_str = str(self.device)
        
        if _USE_LOCAL_EMBEDDER:
            self.embedder = LocalEmbedder(model_name_or_path=model_source, device=dev_str)
        else:
            self.embedder = SentenceTransformer(model_source, device=self.device)

        # 2. Locate and load model checkpoint
        if model_path is None:
            candidates = [
                os.path.join(os.path.dirname(__file__), "best_router_model.pth"),
                os.path.join(BASE_DIR, "best_router_model.pth")
            ]
            for c in candidates:
                if os.path.exists(c):
                    model_path = c
                    break
                    
        if model_path is None or not os.path.exists(model_path):
            raise FileNotFoundError(f"Model checkpoint not found. Looked in: {candidates}")

        self.model = RouterMLP(input_dim=400)
        state_dict = torch.load(model_path, map_location=self.device)
        self.model.load_state_dict(state_dict)
        self.model.to(self.device)
        self.model.eval()

    def predict(self, query_text: str) -> dict:
        """
        Routes an incoming user query based strictly on the query text alone.
        """
        with torch.no_grad():
            # 1. Compute 384-d dense embedding
            if _USE_LOCAL_EMBEDDER:
                dense_emb = np.array(self.embedder.embed_query(query_text), dtype=np.float32)
            else:
                dense_emb = self.embedder.encode(query_text, normalize_embeddings=True)
                
            dense_emb = np.squeeze(np.asarray(dense_emb, dtype=np.float32))

            # 2. Extract 400-d hybrid feature vector (384-d dense + 16-d lexical/intent)
            hybrid_feat = extract_hybrid_features(query_text, dense_emb)
            input_tensor = torch.FloatTensor(hybrid_feat).unsqueeze(0).to(self.device)

            # 3. Model forward pass
            logit = self.model(input_tensor)
            prob = torch.sigmoid(logit).item()

            decision = "hard" if prob >= self.threshold else "easy"
            target_tier = "gpu_vllm" if prob >= self.threshold else "cpu_slm"

            return {
                "query": query_text,
                "route": decision,
                "target_tier": target_tier,
                "probability_hard": round(prob, 4),
                "threshold_applied": self.threshold
            }

if __name__ == "__main__":
    router = QueryRouter()

    test_queries = [
        "What is the capital of France?",
        "What are the prerequisites for the Cloud Computing course?",
        "Synthesize the trade-offs of PagedAttention vs standard KV caching in continuous batching.",
        "Compare the methodologies used in the study on polymer membrane with continuous glucose monitoring.",
        "Derive the mathematical relationship between learning rate and gradient variance in SGD.",
        "Where is the Central Library located on campus?",
        "List the 5 key features of modern distributed state machines.",
        "What is the contact email address for the admissions office?"
    ]

    print("\n--- Testing Upgraded Hybrid QueryRouter (Single-Query Input) ---")
    for q in test_queries:
        res = router.predict(q)
        print(f"Query: \"{res['query']}\"")
        print(f"  -> Route: {res['route'].upper()} ({res['target_tier']}) | Prob(Hard): {res['probability_hard']}\n")
