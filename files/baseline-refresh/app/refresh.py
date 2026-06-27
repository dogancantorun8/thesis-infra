"""
baseline-refresh: regenerate drift detection baseline for the current
MLflow @production model version, then update the cluster ConfigMap.

Triggered by:
  - Notebook 03 last cell after model promotion (synchronous trigger)
  - Manual operator action after alias swap
  - KFP retraining pipeline (Step 4)

Idempotent: if ConfigMap baseline version already matches MLflow alias,
the script exits 0 without rewriting (no-op, fast).

Exit codes:
  0  success (or no-op if already up-to-date)
  1  could not reach MLflow / could not load model
  2  could not load X_train tensor from PVC
  3  could not apply updated ConfigMap
"""

import datetime
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("baseline-refresh")

MLFLOW_TRACKING_URI = os.environ.get(
    "MLFLOW_TRACKING_URI",
    "http://mlflow.mlops.svc.cluster.local:5000",
)
MODEL_NAME = os.environ.get("MODEL_NAME", "cmapss-rul")
MODEL_ALIAS = os.environ.get("MODEL_ALIAS", "production")
X_TRAIN_PATH = os.environ.get("X_TRAIN_PATH", "/data/X_train.npy")
CONFIGMAP_NAME = os.environ.get("CONFIGMAP_NAME", "evidently-baseline")
CONFIGMAP_NAMESPACE = os.environ.get("CONFIGMAP_NAMESPACE", "monitoring")

# Host disk path (optional) — if set, the baseline JSON is also written
# to this path on the host filesystem (via hostPath volume mount). This
# keeps the local dev copy (data/drift/baseline.json) in sync with the
# cluster ConfigMap. Set to empty to disable host-write.
HOST_DISK_PATH = os.environ.get("HOST_DISK_PATH", "/mnt/host-data/baseline.json")

# MinIO / S3 access for X_train tensor
S3_ENDPOINT_URL = os.environ.get("MLFLOW_S3_ENDPOINT_URL", "http://minio.minio.svc.cluster.local:9000")
S3_BUCKET = os.environ.get("S3_BUCKET", "thesis-data")
S3_KEY = os.environ.get("S3_KEY", "processed/X_train.npy")

BUCKET_EDGES = [0, 25, 50, 75, 100, 125, 150, 175, 200, 250, 300, 400]


def get_current_configmap_version() -> str | None:
    try:
        result = subprocess.run(
            [
                "kubectl", "get", "configmap", CONFIGMAP_NAME,
                "-n", CONFIGMAP_NAMESPACE,
                "-o", "jsonpath={.data.baseline\\.json}",
            ],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return str(json.loads(result.stdout).get("model_version", ""))
    except Exception as e:
        log.warning(f"Could not read current ConfigMap: {e}")
        return None


def build_baseline(model, X_train: np.ndarray, model_version: str) -> dict:
    n = len(X_train)
    log.info(f"Running inference on {n} training samples...")
    batch_size = 256
    all_preds = []
    for i in range(0, n, batch_size):
        batch = X_train[i:i+batch_size]
        preds = np.asarray(model.predict(batch)).reshape(-1)
        all_preds.append(preds)
        if i % 5120 == 0 or i + batch_size >= n:
            pct = min(i + batch_size, n) * 100.0 / n
            log.info(f"  Processed {min(i+batch_size, n)}/{n} ({pct:.1f}%)")
    predictions = np.concatenate(all_preds)

    log.info("")
    log.info("=" * 60)
    log.info("Baseline summary")
    log.info("=" * 60)
    log.info(f"  Mean RUL:   {predictions.mean():.2f}")
    log.info(f"  Median:     {np.median(predictions):.2f}")
    log.info(f"  Q25-Q75:    [{np.percentile(predictions, 25):.2f}, "
             f"{np.percentile(predictions, 75):.2f}]")

    counts, _ = np.histogram(predictions, bins=BUCKET_EDGES + [float("inf")])
    return {
        "model_version": model_version,
        "model_uri": f"models:/{MODEL_NAME}@{MODEL_ALIAS}",
        "training_set_size": int(n),
        "prediction_stats": {
            "mean": float(predictions.mean()),
            "std": float(predictions.std()),
            "min": float(predictions.min()),
            "max": float(predictions.max()),
            "median": float(np.median(predictions)),
            "q25": float(np.percentile(predictions, 25)),
            "q75": float(np.percentile(predictions, 75)),
        },
        "histogram": {"buckets": BUCKET_EDGES, "counts": counts.tolist()},
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "generator": "baseline-refresh-job",
    }


def apply_configmap(baseline_json: dict) -> bool:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(baseline_json, f, indent=2)
        tmp_path = f.name
    try:
        result = subprocess.run(
            ["kubectl", "create", "configmap", CONFIGMAP_NAME,
             "-n", CONFIGMAP_NAMESPACE,
             "--from-file", f"baseline.json={tmp_path}",
             "--dry-run=client", "-o", "yaml"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            log.error(f"YAML gen failed: {result.stderr}")
            return False
        apply = subprocess.run(
            ["kubectl", "apply", "-f", "-"],
            input=result.stdout, capture_output=True, text=True, timeout=10,
        )
        if apply.returncode != 0:
            log.error(f"apply failed: {apply.stderr}")
            return False
        log.info(f"  {apply.stdout.strip()}")
        return True
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def main() -> int:
    log.info("=" * 60)
    log.info("baseline-refresh starting")
    log.info("=" * 60)
    log.info(f"  MLflow URI:   {MLFLOW_TRACKING_URI}")
    log.info(f"  Model:        {MODEL_NAME}@{MODEL_ALIAS}")
    log.info(f"  X_train path: {X_TRAIN_PATH}")
    log.info(f"  ConfigMap:    {CONFIGMAP_NAMESPACE}/{CONFIGMAP_NAME}")
    log.info("")

    try:
        import mlflow
        from mlflow.tracking import MlflowClient
    except Exception as e:
        log.error(f"MLflow import failed: {e}")
        return 1

    mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)
    client = MlflowClient(tracking_uri=MLFLOW_TRACKING_URI)

    try:
        mv = client.get_model_version_by_alias(MODEL_NAME, MODEL_ALIAS)
        target_version = str(mv.version)
        log.info(f"MLflow @{MODEL_ALIAS} alias: v{target_version}")
    except Exception as e:
        log.error(f"Could not read MLflow alias: {e}")
        return 1

    current_version = get_current_configmap_version()
    log.info(f"Current baseline:           v{current_version or '(none)'}")
    log.info("")

    if current_version == target_version:
        log.info("✓ Already up-to-date — no refresh needed")
        log.info("=" * 60)
        return 0

    log.info(f"→ Refresh: v{current_version} → v{target_version}")
    log.info("")

    # Load X_train tensor — try local path first (for backward compatibility
    # when running outside k8s), then fall back to S3/MinIO download.
    X_train = None
    if Path(X_TRAIN_PATH).exists():
        try:
            X_train = np.load(X_TRAIN_PATH)
            log.info(f"Loaded X_train from local file: shape={X_train.shape}")
        except Exception as e:
            log.warning(f"Local file load failed: {e}; trying S3")

    if X_train is None:
        log.info(f"Downloading X_train from S3: s3://{S3_BUCKET}/{S3_KEY}")
        try:
            import boto3
            import io
            s3 = boto3.client(
                "s3",
                endpoint_url=S3_ENDPOINT_URL,
                aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
                aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
                region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
            )
            obj = s3.get_object(Bucket=S3_BUCKET, Key=S3_KEY)
            X_train = np.load(io.BytesIO(obj["Body"].read()))
            log.info(f"Loaded X_train from S3: shape={X_train.shape}")
        except Exception as e:
            log.error(f"Could not load X_train from S3: {e}")
            return 2

    try:
        model_uri = f"models:/{MODEL_NAME}@{MODEL_ALIAS}"
        log.info(f"Loading model: {model_uri}")
        model = mlflow.pyfunc.load_model(model_uri)
        log.info("Model loaded successfully")
        log.info("")
    except Exception as e:
        log.error(f"Could not load model: {e}")
        return 1

    baseline = build_baseline(model, X_train, target_version)
    log.info("")
    log.info("Applying updated ConfigMap to cluster...")
    if not apply_configmap(baseline):
        return 3

    # Write to host disk if hostPath volume is mounted. This keeps the
    # local dev copy (data/drift/baseline.json) synchronized with the
    # cluster ConfigMap, so Notebook 04's pre-flight check sees the
    # same version that Evidently CronJob does.
    if HOST_DISK_PATH:
        host_path = Path(HOST_DISK_PATH)
        if host_path.parent.exists():
            try:
                with host_path.open("w") as f:
                    json.dump(baseline, f, indent=2)
                log.info(f"✓ Also wrote to host disk: {HOST_DISK_PATH}")
            except Exception as e:
                log.warning(f"Could not write to host disk ({HOST_DISK_PATH}): {e}")
                log.warning("  ConfigMap is updated; only the local dev copy lags.")
        else:
            log.info(f"  HOST_DISK_PATH parent does not exist ({host_path.parent}); skipping host-write")

    log.info("")
    log.info("=" * 60)
    log.info(f"✓ Baseline refreshed: v{current_version or '(none)'} → v{target_version}")
    log.info("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
