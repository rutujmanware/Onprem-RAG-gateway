"""Train the easy/hard router.
Input CSV columns: query,label[,thesis_id]  (label 1=hard, 0=easy; labels from your LLM-distillation step)
Usage: python train_router.py labels.csv --out models/router.joblib [--alpha 0.5]
Uses the SAME embedder + feature function as serving. Splits by thesis_id if present (no leakage)."""
import argparse, csv, joblib, numpy as np
from sklearn.neural_network import MLPClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GroupShuffleSplit, train_test_split
from sklearn.metrics import classification_report, precision_recall_curve
from askiisc.embedder import embed_sync
from askiisc.router import featurize
from askiisc import config as C

ap = argparse.ArgumentParser(); ap.add_argument("csv"); ap.add_argument("--out", default=C.ROUTER_PATH)
ap.add_argument("--alpha", type=float, default=None, help="threshold; default picks best-F1 on test")
a = ap.parse_args()
rows = list(csv.DictReader(open(a.csv, encoding="utf-8")))
X = np.stack([featurize(r["query"], embed_sync(r["query"])) for r in rows]); y = np.array([int(r["label"]) for r in rows])
groups = [r.get("thesis_id") or str(i) for i, r in enumerate(rows)]
if len(set(groups)) > 1 and len(set(groups)) < len(rows):
    tr, te = next(GroupShuffleSplit(test_size=0.2, random_state=0).split(X, y, groups))
else:
    tr, te = train_test_split(np.arange(len(y)), test_size=0.2, stratify=y, random_state=0)
base = LogisticRegression(max_iter=2000, class_weight="balanced").fit(X[tr], y[tr])
mlp = MLPClassifier(hidden_layer_sizes=(64, 32), early_stopping=True, max_iter=500, random_state=0).fit(X[tr], y[tr])
for name, m in (("LogReg baseline", base), ("MLP", mlp)):
    print(f"== {name}"); print(classification_report(y[te], m.predict(X[te]), digits=3))
p = mlp.predict_proba(X[te])[:, 1]
prec, rec, thr = precision_recall_curve(y[te], p)
f1 = 2 * prec * rec / np.maximum(prec + rec, 1e-9)
alpha = a.alpha if a.alpha is not None else float(thr[max(0, min(np.argmax(f1[:-1]), len(thr) - 1))])
print(f"alpha = {alpha:.3f}  (P(hard) >= alpha -> 7B). Prefer erring toward 7B: lower alpha if hard-recall matters more than cost.")
joblib.dump({"model": mlp, "alpha": alpha, "features": "emb+cheap", "embed_dim": int(X.shape[1] - 7), "hard_label": 1}, a.out)
print("saved", a.out)
