"""Train Hybrid MLP Router for Onprem-RAG-gateway.

Trains an MLP on router_training_dataset.csv and router_multichunk_training_dataset.csv
to classify queries as Easy (CPU SLM) vs Hard (GPU LLM).
Exports the trained model to ONNX for sub-millisecond production inference.
"""

import io
import os
import re
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader
from sklearn.model_selection import GroupShuffleSplit
from sklearn.metrics import precision_recall_curve, roc_auc_score
from sentence_transformers import SentenceTransformer

# =====================================================================
# 1. Configuration (Aligned with Onprem-RAG-gateway)
# =====================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SINGLE_CSV_PATH = os.path.join(BASE_DIR, "router_training_dataset.csv")
MULTI_CSV_PATH = os.path.join(BASE_DIR, "router_multichunk_training_dataset.csv")
ONNX_EXPORT_PATH = os.path.join(BASE_DIR, "gateway", "router", "models", "scorer.onnx")
EMBEDDING_CACHE_PATH = os.path.join(BASE_DIR, "cached_minilm_embeddings_196k.npy")

# Pre-retrieval routing produces 384 (embedding) + 6 (query text scalars) = 390 features
USE_POST_RETRIEVAL_FEATURES = False

EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"  # Matches pipeline/embeddings/embedder.py
EMBED_DIM = 384
BATCH_SIZE = 256
LR = 1e-3
WEIGHT_DECAY = 1e-4
EPOCHS = 25
POS_WEIGHT_VAL = 1.15  # Slight boost toward hard-query recall
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# =====================================================================
# 2. Null-Byte Safe Loader & Vectorized Feature Extractor
# =====================================================================
COMPLEX_KEYWORDS = re.compile(
    r"\b(compare|contrast|synthesize|difference|versus|vs|why|derive|"
    r"exception|waiver|eligibility|multi|across|both|implications)\b",
    re.IGNORECASE,
)

def load_clean_csv(filepath: str, default_is_multichunk: int = 0) -> pd.DataFrame:
    with open(filepath, "rb") as f:
        clean_text = f.read().replace(b"\x00", b"").decode("utf-8", errors="ignore")
    df = pd.read_csv(io.StringIO(clean_text))

    # Properly map 'query_type' from router_multichunk_training_dataset.csv
    if "query_type" in df.columns:
        df["is_cross_document"] = (df["query_type"] == "cross_document").astype(np.float32)
    elif "is_cross_document" not in df.columns:
        df["is_cross_document"] = float(default_is_multichunk)

    if "num_chunks" not in df.columns:
        df["num_chunks"] = 1.0 if default_is_multichunk == 0 else 4.0
    if "num_docs" not in df.columns:
        df["num_docs"] = 1.0 if default_is_multichunk == 0 else 2.0
    return df

def extract_query_scalars_single(query: str) -> list:
    """Single-query helper (used identically in training and gateway inference)."""
    q = query or ""
    words = q.split()
    num_words = len(words)
    num_chars = len(q)
    return [
        min(num_words, 60) / 60.0,
        min(num_chars, 400) / 400.0,
        (num_chars / max(num_words, 1)) / 10.0,
        min(q.count(",") + q.count(";") + q.count("?"), 5) / 5.0,
        1.0 if COMPLEX_KEYWORDS.search(q) else 0.0,
        1.0 if any(c.isdigit() for c in q) else 0.0,
    ]

def extract_scalar_features(df: pd.DataFrame, include_retrieval: bool = False) -> np.ndarray:
    queries = df["query"].fillna("").astype(str).tolist()
    text_feats = np.array([extract_query_scalars_single(q) for q in queries], dtype=np.float32)

    if not include_retrieval:
        return text_feats

    num_chunks_arr = (df["num_chunks"].fillna(1.0).clip(upper=10.0) / 10.0).values.astype(np.float32)
    num_docs_arr = (df["num_docs"].fillna(1.0).clip(upper=5.0) / 5.0).values.astype(np.float32)
    is_cross_arr = df["is_cross_document"].fillna(0.0).values.astype(np.float32)
    retrieval_feats = np.column_stack([num_chunks_arr, num_docs_arr, is_cross_arr])
    return np.hstack([text_feats, retrieval_feats])

# =====================================================================
# 3. Hybrid MLP Architecture
# =====================================================================
class ComplexityRouterMLP(nn.Module):
    def __init__(self, embed_dim: int = 384, scalar_dim: int = 6):
        super().__init__()
        self.embed_dim = embed_dim
        self.scalar_dim = scalar_dim
        self.scalar_norm = nn.BatchNorm1d(scalar_dim)

        self.net = nn.Sequential(
            nn.Linear(embed_dim + scalar_dim, 128),
            nn.LayerNorm(128),
            nn.GELU(),
            nn.Dropout(0.2),
            nn.Linear(128, 64),
            nn.LayerNorm(64),
            nn.GELU(),
            nn.Dropout(0.1),
            nn.Linear(64, 1),
        )

    def forward_logits(self, x: torch.Tensor) -> torch.Tensor:
        emb = x[:, : self.embed_dim]
        scalars = self.scalar_norm(x[:, self.embed_dim :])
        combined = torch.cat([emb, scalars], dim=1)
        return self.net(combined).squeeze(-1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return torch.sigmoid(self.forward_logits(x))

class RouterDataset(Dataset):
    def __init__(self, features: np.ndarray, labels: np.ndarray):
        self.X = torch.tensor(features, dtype=torch.float32)
        self.y = torch.tensor(labels, dtype=torch.float32)

    def __len__(self):
        return len(self.y)

    def __getitem__(self, idx):
        return self.X[idx], self.y[idx]

# =====================================================================
# 4. Training, Subgroup Recall Audit & ONNX Export
# =====================================================================
def main():
    print(f"[Device] Using {DEVICE}")
    df_single = load_clean_csv(SINGLE_CSV_PATH, default_is_multichunk=0)
    df_multi = load_clean_csv(MULTI_CSV_PATH, default_is_multichunk=1)
    df = pd.concat([df_single, df_multi], ignore_index=True)
    print(f"Loaded {len(df):,} total rows ({len(df_single):,} single, {len(df_multi):,} multichunk).")

    # Binary target mapping (0 = Easy/SLM, 1 = Hard/7B GPU)
    label_col = next(c for c in ["label", "complexity", "is_hard", "target"] if c in df.columns)
    if pd.api.types.is_numeric_dtype(df[label_col]):
        y_all = (df[label_col].astype(float) > 0.5).astype(np.float32).values
    else:
        y_all = (
            df[label_col]
            .astype(str)
            .str.strip()
            .str.lower()
            .isin(["hard", "complex", "1", "1.0", "true", "gpu"])
            .astype(np.float32)
            .values
        )

    # Deterministic group IDs to avoid topic leakage
    if "source_text" in df.columns:
        group_series = df["source_text"].fillna(df["query"]).astype(str).str[:80]
    else:
        group_series = df["query"].astype(str).str[:80]
    groups = pd.factorize(group_series)[0]

    # Disk-cached all-MiniLM-L6-v2 embeddings
    queries = df["query"].fillna("").astype(str).tolist()
    if os.path.exists(EMBEDDING_CACHE_PATH):
        print(f"Loading cached embeddings from {EMBEDDING_CACHE_PATH}...")
        embeddings = np.load(EMBEDDING_CACHE_PATH)
    else:
        print(f"Encoding {len(queries):,} queries with {EMBEDDING_MODEL_NAME}...")
        embedder = SentenceTransformer(EMBEDDING_MODEL_NAME, device=str(DEVICE))
        embeddings = embedder.encode(
            queries, batch_size=256, normalize_embeddings=True, show_progress_bar=True
        ).astype(np.float32)
        np.save(EMBEDDING_CACHE_PATH, embeddings)

    scalars = extract_scalar_features(df, include_retrieval=USE_POST_RETRIEVAL_FEATURES)
    X_all = np.hstack([embeddings, scalars])
    scalar_dim = scalars.shape[1]
    print(f"Feature matrix shape: {X_all.shape} (384 emb + {scalar_dim} scalars)")

    # Document-grouped Train/Val split
    gss = GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=42)
    train_idx, val_idx = next(gss.split(X_all, y_all, groups=groups))

    X_train, y_train = X_all[train_idx], y_all[train_idx]
    X_val, y_val = X_all[val_idx], y_all[val_idx]
    val_is_cross = df["is_cross_document"].values[val_idx]

    train_loader = DataLoader(RouterDataset(X_train, y_train), batch_size=BATCH_SIZE, shuffle=True)
    val_loader = DataLoader(RouterDataset(X_val, y_val), batch_size=BATCH_SIZE, shuffle=False)

    # Initialize model & weighted loss
    model = ComplexityRouterMLP(embed_dim=EMBED_DIM, scalar_dim=scalar_dim).to(DEVICE)
    pos_weight = torch.tensor([POS_WEIGHT_VAL], device=DEVICE)
    criterion = nn.BCEWithLogitsLoss(pos_weight=pos_weight)
    optimizer = torch.optim.AdamW(model.parameters(), lr=LR, weight_decay=WEIGHT_DECAY)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=EPOCHS)

    best_auc = 0.0
    best_state = {k: v.cpu() for k, v in model.state_dict().items()}

    for epoch in range(1, EPOCHS + 1):
        model.train()
        train_loss = 0.0
        for bx, by in train_loader:
            bx, by = bx.to(DEVICE), by.to(DEVICE)
            optimizer.zero_grad()
            loss = criterion(model.forward_logits(bx), by)
            loss.backward()
            optimizer.step()
            train_loss += loss.item() * len(by)
        scheduler.step()

        model.eval()
        val_preds = []
        with torch.no_grad():
            for bx, _ in val_loader:
                val_preds.extend(model(bx.to(DEVICE)).cpu().numpy())

        val_auc = roc_auc_score(y_val, val_preds)
        if val_auc > best_auc:
            best_auc = val_auc
            best_state = {k: v.cpu() for k, v in model.state_dict().items()}

        if epoch % 5 == 0 or epoch == EPOCHS:
            print(f"Epoch {epoch:02d} | Train Loss: {train_loss/len(y_train):.4f} | Val ROC-AUC: {val_auc:.4f}")

    model.load_state_dict(best_state)
    model.to("cpu").eval()

    # Alpha Calibration & Cross-Document Recall Audit
    with torch.no_grad():
        val_probs = model(torch.tensor(X_val, dtype=torch.float32)).cpu().numpy()

    _, recalls, thresholds = precision_recall_curve(y_val, val_probs)
    valid_idxs = np.where(recalls[:-1] >= 0.92)[0]
    recommended_alpha = float(thresholds[valid_idxs[-1]]) if len(valid_idxs) > 0 else 0.60

    cross_mask = (val_is_cross == 1) & (y_val == 1)
    cross_recall = (val_probs[cross_mask] >= recommended_alpha).mean() * 100 if cross_mask.any() else 0.0

    print(f"\n[Calibration] Best Val ROC-AUC: {best_auc:.4f}")
    print(f"[Calibration] Recommended alpha (α) for >=92% Hard Recall: {recommended_alpha:.3f}")
    print(f"[Audit] Cross-Document Hard Query Recall at α={recommended_alpha:.3f}: {cross_recall:.1f}%")

    # Export to ONNX
    os.makedirs(os.path.dirname(ONNX_EXPORT_PATH), exist_ok=True)
    dummy_input = torch.randn(1, EMBED_DIM + scalar_dim, dtype=torch.float32)
    export_kwargs = dict(
        export_params=True,
        opset_version=17,
        do_constant_folding=True,
        input_names=["input_features"],
        output_names=["complexity_score"],
        dynamic_axes={"input_features": {0: "batch_size"}, "complexity_score": {0: "batch_size"}},
    )
    try:
        # Prefer legacy TorchScript exporter (dynamo=False) which only requires 'onnx' and not 'onnxscript'
        torch.onnx.export(model, dummy_input, ONNX_EXPORT_PATH, dynamo=False, **export_kwargs)
    except TypeError:
        # Older PyTorch versions without 'dynamo' kwarg
        torch.onnx.export(model, dummy_input, ONNX_EXPORT_PATH, **export_kwargs)
    except ModuleNotFoundError as e:
        print(f"\n[Export Error] Missing dependency for ONNX export: {e}")
        print("Please install: pip install onnx onnxscript\n")
        raise
    print(f"[Export] Saved 390-d ONNX router model to {ONNX_EXPORT_PATH}")

if __name__ == "__main__":
    main()

