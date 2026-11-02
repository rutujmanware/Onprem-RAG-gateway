import os
import sys
import json
import random
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader

# Prioritize local workspace packages in sys.path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
PACKAGES_DIR = os.path.join(BASE_DIR, "packages")
if os.path.exists(PACKAGES_DIR) and PACKAGES_DIR not in sys.path:
    sys.path.insert(0, PACKAGES_DIR)

# Import local router components directly
sys.path.insert(0, os.path.dirname(__file__))
from model import RouterMLP
from features import extract_hybrid_features

from sentence_transformers import SentenceTransformer
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, confusion_matrix, accuracy_score, f1_score

class RouterDataset(Dataset):
    def __init__(self, X, y):
        self.X = torch.FloatTensor(X)
        self.y = torch.FloatTensor(y)

    def __len__(self):
        return len(self.y)

    def __getitem__(self, idx):
        return self.X[idx], self.y[idx]

def get_best_device():
    if torch.cuda.is_available():
        for gpu_id in [5, 6, 7, 0, 1, 2, 3, 4]:
            if gpu_id < torch.cuda.device_count():
                return torch.device(f"cuda:{gpu_id}")
        return torch.device("cuda:0")
    return torch.device("cpu")

def load_dataset(dataset_path):
    print(f"Loading data from {dataset_path}...")
    queries = []
    labels = []
    
    with open(dataset_path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            q = row.get("query", "").strip()
            lbl = row.get("label", "").strip().lower()
            if q and lbl in ["easy", "hard"]:
                queries.append(q)
                labels.append(0.0 if lbl == "easy" else 1.0)
                
    labels = np.array(labels, dtype=np.float32)
    print(f"Loaded {len(queries)} samples. Class distribution: Easy={int(np.sum(labels == 0))}, Hard={int(np.sum(labels == 1))}")
    return queries, labels

def train():
    device = get_best_device()
    print(f"Target execution device: {device}")
    
    dataset_path = os.path.join(BASE_DIR, "data/router_multichunk_training_dataset.jsonl")
    queries, y = load_dataset(dataset_path)
    
    # 1. Feature Extraction (Dense 384-d + Lexical 16-d = 400-d)
    cache_path = os.path.join(BASE_DIR, "data/hybrid_query_features_102k.pt")
    if os.path.exists(cache_path):
        print(f"Loading precomputed hybrid features from {cache_path}...")
        X = torch.load(cache_path, map_location="cpu").numpy()
    else:
        print("1/2: Generating dense text embeddings using all-MiniLM-L6-v2 on GPU...")
        local_model_path = os.path.join(BASE_DIR, "models/embeddings/all-MiniLM-L6-v2")
        model_source = local_model_path if os.path.exists(local_model_path) else "all-MiniLM-L6-v2"
        embedder = SentenceTransformer(model_source, device=device)
        
        dense_embs = embedder.encode(
            queries,
            batch_size=512,
            show_progress_bar=True,
            normalize_embeddings=True,
            convert_to_numpy=True
        )

        print("2/2: Extracting lexical and structural features strictly from user queries...")
        hybrid_list = []
        for q, emb in zip(queries, dense_embs):
            feat = extract_hybrid_features(q, emb)
            hybrid_list.append(feat)

        X = np.array(hybrid_list, dtype=np.float32)
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        torch.save(torch.from_numpy(X), cache_path)
        print(f"Saved computed hybrid features to {cache_path} (Shape: {X.shape})")

    # 2. Train / Validation Split (Stratified 80/20)
    X_train, X_val, y_train, y_val = train_test_split(
        X, y, test_size=0.20, random_state=42, stratify=y
    )
    print(f"Train size: {len(X_train)} | Val size: {len(X_val)}")

    train_dataset = RouterDataset(X_train, y_train)
    val_dataset = RouterDataset(X_val, y_val)

    train_loader = DataLoader(train_dataset, batch_size=256, shuffle=True, drop_last=False)
    val_loader = DataLoader(val_dataset, batch_size=512, shuffle=False)

    # 3. Initialize Network (400-d input)
    net = RouterMLP(input_dim=400).to(device)
    criterion = nn.BCEWithLogitsLoss()
    optimizer = torch.optim.AdamW(net.parameters(), lr=0.001, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode='min', factor=0.5, patience=2)

    epochs = 20
    patience = 5
    best_val_f1 = 0.0
    best_val_loss = float('inf')
    epochs_no_improve = 0

    best_model_paths = [
        os.path.join(os.path.dirname(__file__), "best_router_model.pth")
    ]

    print("\nStarting Training & Validation Loop...")
    for epoch in range(epochs):
        net.train()
        total_train_loss = 0.0
        for batch_X, batch_y in train_loader:
            batch_X, batch_y = batch_X.to(device), batch_y.to(device)
            optimizer.zero_grad()
            logits = net(batch_X)
            loss = criterion(logits, batch_y)
            loss.backward()
            optimizer.step()
            total_train_loss += loss.item() * len(batch_y)

        avg_train_loss = total_train_loss / len(train_dataset)

        # Validation
        net.eval()
        total_val_loss = 0.0
        val_probs = []
        val_targets = []

        with torch.no_grad():
            for batch_X, batch_y in val_loader:
                batch_X, batch_y = batch_X.to(device), batch_y.to(device)
                logits = net(batch_X)
                loss = criterion(logits, batch_y)
                total_val_loss += loss.item() * len(batch_y)

                probs = torch.sigmoid(logits).cpu().numpy()
                val_probs.extend(probs)
                val_targets.extend(batch_y.cpu().numpy())

        avg_val_loss = total_val_loss / len(val_dataset)
        val_probs = np.array(val_probs)
        val_targets = np.array(val_targets)
        val_preds = (val_probs >= 0.5).astype(int)

        acc = accuracy_score(val_targets, val_preds)
        macro_f1 = f1_score(val_targets, val_preds, average='macro')
        scheduler.step(avg_val_loss)

        print(f"Epoch {epoch+1:02d}/{epochs} | Train Loss: {avg_train_loss:.4f} | Val Loss: {avg_val_loss:.4f} | Val Acc: {acc*100:.2f}% | Macro F1: {macro_f1:.4f}")

        if macro_f1 > best_val_f1 or (macro_f1 == best_val_f1 and avg_val_loss < best_val_loss):
            best_val_f1 = macro_f1
            best_val_loss = avg_val_loss
            epochs_no_improve = 0
            for p in best_model_paths:
                torch.save(net.state_dict(), p)
            print(f"  >>> Checkpoint saved! New best Macro F1: {best_val_f1:.4f}")
        else:
            epochs_no_improve += 1
            if epochs_no_improve >= patience:
                print(f"Early stopping triggered at epoch {epoch+1}.")
                break

    # Load best checkpoint for final evaluation
    print(f"\nTraining Complete! Loading best checkpoint (Macro F1 = {best_val_f1:.4f}) for evaluation...")
    net.load_state_dict(torch.load(best_model_paths[0], map_location=device))
    net.eval()

    val_probs = []
    with torch.no_grad():
        for batch_X, batch_y in val_loader:
            batch_X = batch_X.to(device)
            logits = net(batch_X)
            val_probs.extend(torch.sigmoid(logits).cpu().numpy())
    val_probs = np.array(val_probs)
    val_preds = (val_probs >= 0.5).astype(int)

    print("\n" + "="*50)
    print("DETAILED VALIDATION METRICS (Threshold = 0.5):")
    print("="*50)
    print(classification_report(y_val, val_preds, target_names=["Easy (CPU SLM)", "Hard (GPU vLLM)"], digits=4))
    print("Confusion Matrix:")
    cm = confusion_matrix(y_val, val_preds)
    print(f"  [TN={cm[0,0]} (Easy correctly routed), FP={cm[0,1]} (Easy mistakenly sent to GPU)]")
    print(f"  [FN={cm[1,0]} (Hard mistakenly sent to CPU), TP={cm[1,1]} (Hard correctly routed)]")

    # Threshold Sweep
    print("\n" + "-"*50)
    print("THRESHOLD SWEEP ON VALIDATION SET:")
    print("-"*50)
    print("Thresh | Acc (%) | Macro F1 | Easy Recall | Hard Recall | GPU Egress %")
    for t in [0.30, 0.40, 0.50, 0.60, 0.65, 0.70]:
        t_preds = (val_probs >= t).astype(int)
        t_acc = accuracy_score(y_val, t_preds) * 100
        t_f1 = f1_score(y_val, t_preds, average='macro')
        easy_rec = np.sum((y_val == 0) & (t_preds == 0)) / np.sum(y_val == 0) * 100
        hard_rec = np.sum((y_val == 1) & (t_preds == 1)) / np.sum(y_val == 1) * 100
        gpu_egress = np.mean(t_preds == 1) * 100
        print(f" {t:.2f}  |  {t_acc:.2f}%  |  {t_f1:.4f}  |   {easy_rec:.1f}%    |   {hard_rec:.1f}%    |   {gpu_egress:.1f}%")

    # Evaluate on Golden Eval Set
    golden_path = os.path.join(BASE_DIR, "evaluation/datasets/golden_eval_set.json")
    if os.path.exists(golden_path):
        print("\n" + "="*50)
        print("EVALUATION ON GOLDEN BENCHMARK SET:")
        print("="*50)
        with open(golden_path, "r", encoding="utf-8") as f:
            golden_data = json.load(f)
        
        embedder = SentenceTransformer(local_model_path if os.path.exists(local_model_path) else "all-MiniLM-L6-v2", device=device)
        for sample in golden_data:
            q = sample["query"]
            emb = embedder.encode(q, normalize_embeddings=True)
            feat = extract_hybrid_features(q, emb)
            with torch.no_grad():
                inp = torch.FloatTensor(feat).unsqueeze(0).to(device)
                logit = net(inp)
                prob = torch.sigmoid(logit).item()
            tier = "GPU vLLM" if prob >= 0.5 else "CPU SLM"
            expected = sample.get("expected_tier", "unknown")
            print(f"Query: \"{q[:65]}...\"")
            print(f"  -> Predicted Prob(Hard): {prob:.4f} | Route: {tier} | Expected: {expected}")

if __name__ == "__main__":
    train()
