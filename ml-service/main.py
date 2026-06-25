"""
ML Anomaly Detection Service
- Isolation Forest (primary model)
- Rule-based fallback
- Online training on first N samples
- Returns: anomaly_score, severity
"""

import os, json, logging, threading
from typing import Optional
import numpy as np
from fastapi import FastAPI
from pydantic import BaseModel
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler
import joblib

logging.basicConfig(level=logging.INFO, format="%(asctime)s [ML] %(message)s")
logger = logging.getLogger(__name__)

MODEL_PATH  = "/models/isolation_forest.pkl"
SCALER_PATH = "/models/scaler.pkl"
MIN_TRAIN   = 500      # min samples before model kicks in
RETRAIN_AT  = 5000     # retrain every N samples

os.makedirs("/models", exist_ok=True)

app = FastAPI(title="ML Anomaly Detection", version="1.0.0")

# ─── State ───────────────────────────────────────────────────────────────────
buffer: list[list[float]] = []
model:  Optional[IsolationForest] = None
scaler: Optional[StandardScaler] = None
lock   = threading.Lock()
total_seen = 0

FEATURES = ["cpu_usage", "memory_usage", "latency_ms", "error_rate"]


def _extract(m: dict) -> list[float]:
    return [m.get(f, 0.0) for f in FEATURES]


def _train(data: list[list[float]]):
    global model, scaler
    X = np.array(data)
    sc = StandardScaler()
    Xs = sc.fit_transform(X)
    clf = IsolationForest(n_estimators=200, contamination=0.05, random_state=42, n_jobs=-1)
    clf.fit(Xs)
    joblib.dump(clf, MODEL_PATH)
    joblib.dump(sc, SCALER_PATH)
    model  = clf
    scaler = sc
    logger.info(f"Model trained on {len(data)} samples")


def _score_ml(features: list[float]) -> float:
    """Return 0..1 anomaly score (higher = more anomalous)."""
    X = scaler.transform([features])
    raw = model.decision_function(X)[0]   # negative = anomaly
    # Normalise to 0..1: raw is roughly in [-0.5, 0.5]
    score = 1 - (raw + 0.5)
    return float(np.clip(score, 0.0, 1.0))


def _score_rule(features: list[float]) -> float:
    cpu, mem, lat, err = features
    s = 0.0
    if cpu > 80:  s += 0.4 * (cpu - 80) / 20
    if mem > 80:  s += 0.3 * (mem - 80) / 20
    if lat > 500: s += 0.2 * min(1, (lat - 500) / 2500)
    if err > 5:   s += 0.1 * min(1, err / 50)
    return min(1.0, s)


def _severity(score: float) -> str:
    if score >= 0.65: return "critical"
    if score >= 0.40: return "warning"
    return "normal"


# ─── Schemas ──────────────────────────────────────────────────────────────────
class MetricItem(BaseModel):
    server_id: str
    cpu_usage: float
    memory_usage: float
    latency_ms: float
    error_rate: float
    timestamp: Optional[str] = None
    region: Optional[str] = None
    service: Optional[str] = None

class DetectRequest(BaseModel):
    metrics: list[MetricItem]


# ─── Load existing model if present ──────────────────────────────────────────
try:
    model  = joblib.load(MODEL_PATH)
    scaler = joblib.load(SCALER_PATH)
    logger.info("Loaded existing model from disk")
except Exception:
    logger.info("No existing model; will train after warm-up")


# ─── Routes ───────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "model_ready": model is not None, "samples_seen": total_seen}


@app.post("/detect")
def detect(req: DetectRequest):
    global buffer, total_seen

    results = []
    new_rows = []

    for m in req.metrics:
        feat = _extract(m.model_dump())
        new_rows.append(feat)

        if model is not None:
            score = _score_ml(feat)
        else:
            score = _score_rule(feat)

        results.append({"anomaly_score": round(score, 4), "severity": _severity(score)})

    # Accumulate for training
    with lock:
        buffer.extend(new_rows)
        total_seen += len(new_rows)

        if total_seen == MIN_TRAIN or (total_seen % RETRAIN_AT == 0 and total_seen > MIN_TRAIN):
            train_data = buffer[-10000:]  # cap buffer
            t = threading.Thread(target=_train, args=(train_data,), daemon=True)
            t.start()

    return {"results": results, "model_used": "isolation_forest" if model else "rule_based"}


@app.get("/model/stats")
def model_stats():
    return {
        "samples_seen": total_seen,
        "buffer_size": len(buffer),
        "model_ready": model is not None,
        "min_train_threshold": MIN_TRAIN,
    }
