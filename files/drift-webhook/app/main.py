"""
drift-webhook — Alertmanager → KFP retraining trigger.

Closed-loop MLOps glue: receives ModelDriftDetected alerts from
Alertmanager, validates them, and submits a Kubeflow Pipelines run
to retrain the LSTM. Designed to run as a Deployment in mlops ns.

Endpoints:
    POST /webhook/drift     — Alertmanager webhook target
    GET  /health            — Liveness/readiness probe
    GET  /metrics           — Prometheus scrape endpoint

Environment variables (all optional; defaults are cluster-local):
    KFP_HOST                http://ml-pipeline.kubeflow.svc.cluster.local:8888
    PIPELINE_NAME           cmapss-rul-retraining
    KFP_NAMESPACE           kubeflow
    DEBOUNCE_SECONDS        300
    RETRAIN_EPOCHS          30
    RETRAIN_THRESHOLD       0.05
    RETRAIN_BATCH_SIZE      64
    RETRAIN_LEARNING_RATE   0.001
"""

import logging
import os
import time
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from pydantic import BaseModel, Field
from kfp import Client as KfpClient
from kubernetes import client as k8s_client, config as k8s_config

# ─── Configuration ────────────────────────────────────────────────────
KFP_HOST = os.getenv("KFP_HOST", "http://ml-pipeline.kubeflow.svc.cluster.local:8888")
PIPELINE_NAME = os.getenv("PIPELINE_NAME", "cmapss-rul-retraining")
KFP_NAMESPACE = os.getenv("KFP_NAMESPACE", "kubeflow")
EXPERIMENT_NAME = os.getenv("EXPERIMENT_NAME", "cmapss-retraining-tests")
DEBOUNCE_SECONDS = int(os.getenv("DEBOUNCE_SECONDS", "300"))

RETRAIN_PARAMS = {
    "epochs":           int(os.getenv("RETRAIN_EPOCHS", "30")),
    "threshold_factor": float(os.getenv("RETRAIN_THRESHOLD", "0.05")),
    "batch_size":       int(os.getenv("RETRAIN_BATCH_SIZE", "64")),
    "learning_rate":    float(os.getenv("RETRAIN_LEARNING_RATE", "0.001")),
}

# ─── Logging ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("drift-webhook")

# ─── Prometheus metrics ───────────────────────────────────────────────
events_total = Counter(
    "drift_webhook_events_total",
    "Webhook events handled, by result",
    ["result"],
)
kfp_runs_total = Counter(
    "drift_webhook_kfp_runs_total",
    "KFP pipeline runs successfully submitted",
)
request_duration = Histogram(
    "drift_webhook_request_duration_seconds",
    "Webhook request handling duration",
)

# ─── In-memory debounce store (fingerprint → epoch seconds) ──────────
recent_alerts: dict = {}

# ─── Pipeline ID cache (resolved once at startup) ────────────────────
_pipeline_id_cache: Optional[str] = None


# ─── Pydantic models for Alertmanager v4 payload ─────────────────────
class AlertmanagerAlert(BaseModel):
    status: str = Field(..., description="firing or resolved")
    labels: dict = Field(default_factory=dict)
    annotations: dict = Field(default_factory=dict)
    startsAt: Optional[str] = None
    fingerprint: Optional[str] = None


class AlertmanagerPayload(BaseModel):
    """Minimal Alertmanager v4 webhook payload schema."""
    version: str = "4"
    status: str = Field(..., description="firing or resolved")
    receiver: str = ""
    groupLabels: dict = Field(default_factory=dict)
    commonLabels: dict = Field(default_factory=dict)
    commonAnnotations: dict = Field(default_factory=dict)
    alerts: list[AlertmanagerAlert] = Field(default_factory=list)


# ─── Helpers ──────────────────────────────────────────────────────────
def resolve_pipeline_id(client: KfpClient) -> Optional[str]:
    """Look up pipeline_id by display name. Cached after first call."""
    global _pipeline_id_cache
    if _pipeline_id_cache:
        return _pipeline_id_cache

    try:
        pipelines = client.list_pipelines(page_size=100)
        for p in (pipelines.pipelines or []):
            if p.display_name == PIPELINE_NAME:
                _pipeline_id_cache = p.pipeline_id
                log.info("Resolved pipeline_id: %s (name=%s)",
                         _pipeline_id_cache, PIPELINE_NAME)
                return _pipeline_id_cache
        log.error("Pipeline '%s' not found in KFP", PIPELINE_NAME)
        return None
    except Exception as e:
        log.error("Failed to list KFP pipelines: %s", e)
        return None


def is_workflow_already_running() -> bool:
    """Check Argo Workflow CRD in kubeflow ns for any Running cmapss-rul-* workflow."""
    try:
        # In-cluster config (Pod has serviceaccount mounted)
        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            k8s_config.load_kube_config()

        api = k8s_client.CustomObjectsApi()
        result = api.list_namespaced_custom_object(
            group="argoproj.io",
            version="v1alpha1",
            namespace=KFP_NAMESPACE,
            plural="workflows",
        )
        for wf in result.get("items", []):
            name = wf.get("metadata", {}).get("name", "")
            phase = wf.get("status", {}).get("phase", "")
            if name.startswith("cmapss-rul-retraining-") and phase == "Running":
                log.info("Found running workflow: %s", name)
                return True
        return False
    except Exception as e:
        log.warning("Could not check workflow status (%s) — assuming not running", e)
        return False


def is_duplicate_alert(fingerprint: str) -> bool:
    """Return True if same fingerprint fired within DEBOUNCE_SECONDS."""
    now = time.time()

    # Garbage-collect old entries (keep the dict small)
    expired = [k for k, ts in recent_alerts.items() if now - ts > DEBOUNCE_SECONDS]
    for k in expired:
        del recent_alerts[k]

    if fingerprint in recent_alerts:
        age = now - recent_alerts[fingerprint]
        log.info("Duplicate alert (fingerprint=%s, age=%.0fs)", fingerprint, age)
        return True

    recent_alerts[fingerprint] = now
    return False


def get_kfp_client() -> Optional[KfpClient]:
    """Construct a KFP client; returns None on connection failure."""
    try:
        return KfpClient(host=KFP_HOST)
    except Exception as e:
        log.error("Failed to construct KFP client (host=%s): %s", KFP_HOST, e)
        return None


# ─── FastAPI app ──────────────────────────────────────────────────────
app = FastAPI(
    title="drift-webhook",
    description="Alertmanager → KFP retraining trigger",
    version="0.1.0",
)


@app.get("/health")
def health():
    """Liveness probe. Also reports KFP reachability (best-effort)."""
    client = get_kfp_client()
    kfp_reachable = False
    if client is not None:
        try:
            # list_experiments is a lightweight call
            client.list_experiments(page_size=1)
            kfp_reachable = True
        except Exception:
            pass

    return JSONResponse({
        "status": "healthy",
        "kfp_reachable": kfp_reachable,
        "pipeline_name": PIPELINE_NAME,
    })


@app.get("/metrics")
def metrics():
    """Prometheus scrape endpoint. Returns raw text exposition format."""
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )


@app.post("/webhook/drift")
async def webhook_drift(request: Request):
    """
    Alertmanager webhook target. Validates payload, applies idempotency
    guards, and submits a KFP pipeline run.
    """
    with request_duration.time():
        # Parse payload
        try:
            raw = await request.json()
            payload = AlertmanagerPayload(**raw)
        except Exception as e:
            log.error("Invalid payload: %s", e)
            events_total.labels(result="failed").inc()
            raise HTTPException(status_code=422, detail=f"Invalid payload: {e}")

        # Validate alert
        if payload.status != "firing":
            log.info("Ignoring non-firing payload (status=%s)", payload.status)
            events_total.labels(result="skipped").inc()
            return {"decision": "skipped", "reason": "status != firing"}

        alertname = payload.commonLabels.get("alertname", "")
        if alertname != "ModelDriftDetected":
            log.info("Ignoring non-drift alert: %s", alertname)
            events_total.labels(result="skipped").inc()
            return {"decision": "skipped", "reason": f"alertname={alertname}"}

        # Idempotency 1: workflow already running
        if is_workflow_already_running():
            log.info("Skipping — a cmapss-rul-retraining workflow is already Running")
            events_total.labels(result="skipped").inc()
            return {"decision": "skipped", "reason": "workflow_already_running"}

        # Idempotency 2: debounce duplicate alerts
        fingerprint = ""
        if payload.alerts:
            fingerprint = payload.alerts[0].fingerprint or ""
        if not fingerprint:
            # Synthesize from groupLabels if Alertmanager didn't provide one
            fingerprint = str(sorted(payload.groupLabels.items()))

        if is_duplicate_alert(fingerprint):
            events_total.labels(result="skipped").inc()
            return {"decision": "skipped", "reason": "debounced_duplicate"}

        # Submit KFP run
        client = get_kfp_client()
        if client is None:
            events_total.labels(result="failed").inc()
            raise HTTPException(status_code=503, detail="KFP client unavailable")

        pipeline_id = resolve_pipeline_id(client)
        if pipeline_id is None:
            events_total.labels(result="failed").inc()
            raise HTTPException(
                status_code=503,
                detail=f"Pipeline '{PIPELINE_NAME}' not found in KFP",
            )

        # Pick latest pipeline version
        try:
            versions = client.list_pipeline_versions(pipeline_id)
            latest = sorted(
                versions.pipeline_versions,
                key=lambda v: v.created_at,
                reverse=True,
            )[0]
        except Exception as e:
            log.error("Failed to list pipeline versions: %s", e)
            events_total.labels(result="failed").inc()
            raise HTTPException(status_code=503, detail="Could not list versions")

        # Pick experiment
        try:
            experiment = client.get_experiment(experiment_name=EXPERIMENT_NAME)
        except Exception:
            experiment = client.create_experiment(name=EXPERIMENT_NAME)

        # Submit
        ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        baseline_version = payload.commonLabels.get("baseline_version", "unknown")
        run_name = f"drift-triggered-baseline-v{baseline_version}-{ts}"

        try:
            run = client.run_pipeline(
                experiment_id=experiment.experiment_id,
                job_name=run_name,
                pipeline_id=pipeline_id,
                version_id=latest.pipeline_version_id,
                params=RETRAIN_PARAMS,
                enable_caching=False,
            )
            log.info("Submitted KFP run: id=%s name=%s", run.run_id, run_name)
            events_total.labels(result="submitted").inc()
            kfp_runs_total.inc()

            return {
                "decision": "submitted",
                "run_id": run.run_id,
                "run_name": run_name,
                "pipeline_version": latest.display_name,
                "baseline_version_in_alert": baseline_version,
            }
        except Exception as e:
            log.error("Failed to submit KFP run: %s", e)
            events_total.labels(result="failed").inc()
            raise HTTPException(status_code=503, detail=f"KFP submit failed: {e}")
