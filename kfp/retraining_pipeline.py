#!/usr/bin/env python3
"""
KFP Pipeline: cmapss-rul-retraining
====================================

Drift-triggered LSTM retraining pipeline. Four container components:

    load_data_op       → fetch X_train, y_train, X_val, y_val from MinIO (via DVC pull)
    train_lstm_op      → train LSTM, log to MLflow, register version
    register_model_op  → champion-challenger gate + alias swap
    trigger_rollout_op → baseline-refresh job + FastAPI rolling restart

Each component runs in `thesis/retraining:0.1.0` (see files/retraining/Dockerfile).

MinIO credentials:
    The first three components (load, train, register) inject `minio-credentials`
    Secret as AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY environment variables.
    The Secret must exist in the `kubeflow` namespace (copied from `mlops` by
    cluster pre-flight setup).

    trigger_rollout_op uses kubectl with the pipeline-runner SA's token
    (auto-mounted), so it does not need MinIO credentials.

Compile + upload:
    python kfp_pipeline.py            → produces retraining_pipeline.yaml
    python kfp_pipeline.py --upload   → also uploads to KFP

KFP SDK version: 2.10.1
kfp-kubernetes:  1.4.0
"""

import argparse
import os
from datetime import datetime
from pathlib import Path

from kfp import dsl, compiler
from kfp import kubernetes  # ← NEW: for Secret env var injection
from kfp.dsl import Input, Output, Dataset, Artifact

# ──────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────
PIPELINE_NAME = "cmapss-rul-retraining"
PIPELINE_DESCRIPTION = (
    "Drift-triggered LSTM retraining + champion-challenger + auto-rollout. "
    "Replaces manual Notebook 03 invocation (closes the closed-loop)."
)
IMAGE = "thesis/retraining:0.5.0"

# In-cluster service endpoints
DEFAULT_S3_ENDPOINT = "http://minio.minio.svc.cluster.local:9000"
DEFAULT_S3_BUCKET = "thesis-data"
DEFAULT_MLFLOW_URI = "http://mlflow.mlops.svc.cluster.local:5000"

# Secret reference (must be present in kubeflow namespace)
# Copied from mlops/minio-credentials during cluster pre-flight setup
MINIO_SECRET_NAME = "minio-credentials"
MINIO_SECRET_KEY_MAPPING = {
    "access_key": "AWS_ACCESS_KEY_ID",
    "secret_key": "AWS_SECRET_ACCESS_KEY",
}

# Plain env vars for MLflow client boto3 (EC#23 fix)
# Without MLFLOW_S3_ENDPOINT_URL, boto3 falls back to AWS S3 default
# (s3.amazonaws.com) and rejects MinIO credentials with "InvalidAccessKeyId".
MLFLOW_S3_ENV_VARS = {
    "MLFLOW_S3_ENDPOINT_URL": "http://minio.minio.svc.cluster.local:9000",
    "AWS_DEFAULT_REGION": "us-east-1",
}


# ──────────────────────────────────────────────────────────
# Component 1: load_data
# Output: directory of .npy files (X_train, y_train, X_val, y_val)
# ──────────────────────────────────────────────────────────
@dsl.container_component
def load_data_op(
    data_output: Output[Dataset],
    s3_endpoint: str = DEFAULT_S3_ENDPOINT,
    s3_bucket: str = DEFAULT_S3_BUCKET,
):
    return dsl.ContainerSpec(
        image=IMAGE,
        command=["python", "/app/load_data.py"],
        args=[
            "--output-dir", data_output.path,
        ],
    )


# ──────────────────────────────────────────────────────────
# Component 2: train_lstm
# Input: directory of .npy files
# Output: artifact containing train_output.json
# ──────────────────────────────────────────────────────────
@dsl.container_component
def train_lstm_op(
    data_input: Input[Dataset],
    train_output: Output[Artifact],
    epochs: int = 30,
    batch_size: int = 64,
    learning_rate: float = 0.001,
    mlflow_tracking_uri: str = DEFAULT_MLFLOW_URI,
):
    return dsl.ContainerSpec(
        image=IMAGE,
        command=["python", "/app/train_lstm.py"],
        args=[
            "--data-dir", data_input.path,
            "--epochs", epochs,
            "--batch-size", batch_size,
            "--learning-rate", learning_rate,
            "--mlflow-tracking-uri", mlflow_tracking_uri,
            "--output-file", train_output.path,
        ],
    )


# ──────────────────────────────────────────────────────────
# Component 3: register_model
# Input: mlflow_run_id (str), val_rmse (float)
# Output: artifact containing register_output.json
# ──────────────────────────────────────────────────────────
@dsl.container_component
def register_model_op(
    mlflow_run_id: str,
    val_rmse: float,
    register_output: Output[Artifact],
    threshold_factor: float = 0.95,
    mlflow_tracking_uri: str = DEFAULT_MLFLOW_URI,
):
    return dsl.ContainerSpec(
        image=IMAGE,
        command=["python", "/app/register_model.py"],
        args=[
            "--mlflow-run-id", mlflow_run_id,
            "--val-rmse", val_rmse,
            "--threshold-factor", threshold_factor,
            "--mlflow-tracking-uri", mlflow_tracking_uri,
            "--output-file", register_output.path,
        ],
    )


# ──────────────────────────────────────────────────────────
# Component 4: trigger_rollout
# Input: new_version (str), alias_assigned (str)
# Output: artifact containing rollout_output.json
# Uses kubectl with pipeline-runner SA token (no MinIO needed)
# ──────────────────────────────────────────────────────────
@dsl.container_component
def trigger_rollout_op(
    new_version: str,
    alias_assigned: str,
    rollout_output: Output[Artifact],
):
    return dsl.ContainerSpec(
        image=IMAGE,
        command=["python", "/app/trigger_rollout.py"],
        args=[
            "--new-version", new_version,
            "--alias-assigned", alias_assigned,
            "--output-file", rollout_output.path,
        ],
    )


# ──────────────────────────────────────────────────────────
# Helper components — extract fields from JSON outputs
# Python function components (lightweight, single output)
# ──────────────────────────────────────────────────────────
@dsl.component(base_image="python:3.12-slim")
def extract_run_id_op(train_output: Input[Artifact]) -> str:
    """Extract mlflow_run_id from train_lstm_op's JSON output."""
    import json
    with open(train_output.path) as f:
        data = json.load(f)
    return str(data["mlflow_run_id"])


@dsl.component(base_image="python:3.12-slim")
def extract_val_rmse_op(train_output: Input[Artifact]) -> float:
    """Extract val_rmse from train_lstm_op's JSON output."""
    import json
    with open(train_output.path) as f:
        data = json.load(f)
    return float(data["val_rmse"])


@dsl.component(base_image="python:3.12-slim")
def extract_new_version_op(register_output: Input[Artifact]) -> str:
    """Extract new_version from register_model_op's JSON output."""
    import json
    with open(register_output.path) as f:
        data = json.load(f)
    return str(data["new_version"])


@dsl.component(base_image="python:3.12-slim")
def extract_alias_op(register_output: Input[Artifact]) -> str:
    """Extract alias_assigned from register_model_op's JSON output."""
    import json
    with open(register_output.path) as f:
        data = json.load(f)
    return str(data["alias_assigned"])


# ──────────────────────────────────────────────────────────
# Helper: inject MinIO credentials into a component task
# ──────────────────────────────────────────────────────────
def _inject_minio_secret(task):
    """Inject minio-credentials Secret + MLflow S3 endpoint env vars (EC#23)."""
    kubernetes.use_secret_as_env(
        task=task,
        secret_name=MINIO_SECRET_NAME,
        secret_key_to_env=MINIO_SECRET_KEY_MAPPING,
    )
    # EC#23: MLflow client boto3 needs MLFLOW_S3_ENDPOINT_URL or it falls
    # back to AWS S3 default, causing "InvalidAccessKeyId" errors.
    for env_name, env_value in MLFLOW_S3_ENV_VARS.items():
        task.set_env_variable(name=env_name, value=env_value)
    return task


# ──────────────────────────────────────────────────────────
# Pipeline DAG
# ──────────────────────────────────────────────────────────
@dsl.pipeline(
    name=PIPELINE_NAME,
    description=PIPELINE_DESCRIPTION,
)
def retraining_pipeline(
    epochs: int = 30,
    batch_size: int = 64,
    learning_rate: float = 0.001,
    threshold_factor: float = 0.95,
    s3_endpoint: str = DEFAULT_S3_ENDPOINT,
    s3_bucket: str = DEFAULT_S3_BUCKET,
    mlflow_tracking_uri: str = DEFAULT_MLFLOW_URI,
):
    # ─── Step 1: fetch training data from MinIO (DVC pull) ────────
    load = load_data_op(
        s3_endpoint=s3_endpoint,
        s3_bucket=s3_bucket,
    )
    _inject_minio_secret(load)  # ← needs MinIO for dvc pull

    # ─── Step 2: train the model ───────────────────────────────────
    train = train_lstm_op(
        data_input=load.outputs["data_output"],
        epochs=epochs,
        batch_size=batch_size,
        learning_rate=learning_rate,
        mlflow_tracking_uri=mlflow_tracking_uri,
    )
    _inject_minio_secret(train)  # ← MLflow uses MinIO as artifact backend

    # ─── Step 2.5: extract train fields for next component ─────────
    run_id = extract_run_id_op(train_output=train.outputs["train_output"])
    rmse = extract_val_rmse_op(train_output=train.outputs["train_output"])

    # ─── Step 3: champion-challenger gate + alias swap ─────────────
    register = register_model_op(
        mlflow_run_id=run_id.output,
        val_rmse=rmse.output,
        threshold_factor=threshold_factor,
        mlflow_tracking_uri=mlflow_tracking_uri,
    )
    _inject_minio_secret(register)  # ← MLflow operations need MinIO creds

    # ─── Step 3.5: extract register fields for next component ──────
    new_version = extract_new_version_op(
        register_output=register.outputs["register_output"]
    )
    alias_assigned = extract_alias_op(
        register_output=register.outputs["register_output"]
    )

    # ─── Step 4: sync downstream consumers (kubectl, no MinIO needed) ──
    rollout = trigger_rollout_op(
        new_version=new_version.output,
        alias_assigned=alias_assigned.output,
    )
    # NOTE: trigger_rollout_op does NOT inject MinIO Secret.
    # It uses kubectl with the pipeline-runner SA token (auto-mounted).


# ──────────────────────────────────────────────────────────
# Entrypoint
# ──────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--output",
        default="retraining_pipeline.yaml",
        help="Output path for compiled pipeline YAML",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="After compiling, upload the pipeline to KFP at --kfp-host",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="After compiling, submit a one-off run (for testing)",
    )
    parser.add_argument(
        "--kfp-host",
        default=os.environ.get("KFP_HOST", "http://localhost:8888"),
        help="KFP API endpoint (default: http://localhost:8888 via port-forward)",
    )
    args = parser.parse_args()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Compiling pipeline: {PIPELINE_NAME}")
    compiler.Compiler().compile(
        pipeline_func=retraining_pipeline,
        package_path=str(output_path),
    )
    print(f"  Wrote: {output_path}")
    print(f"  Size:  {output_path.stat().st_size:,} bytes")

    if args.upload or args.run:
        from kfp import Client

        client = Client(host=args.kfp_host)

        if args.upload:
            # EC#26 fix: idempotent upload — works for both first deploy
            # (creates pipeline) and iterative redeploy (uploads new version).
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            print(f"\nUploading pipeline to {args.kfp_host}")

            # Look up existing pipeline by display name
            existing = None
            try:
                pipelines = client.list_pipelines(page_size=100)
                for p in (pipelines.pipelines or []):
                    if p.display_name == PIPELINE_NAME:
                        existing = p
                        break
            except Exception as e:
                print(f"  WARN: could not list pipelines ({e}); will try create")

            if existing is None:
                # First-time deploy: create the pipeline
                uploaded = client.upload_pipeline(
                    pipeline_package_path=str(output_path),
                    pipeline_name=PIPELINE_NAME,
                    description=PIPELINE_DESCRIPTION,
                )
                print(f"  Created pipeline:")
                print(f"    Pipeline ID: {uploaded.pipeline_id}")
                print(f"    Name:        {PIPELINE_NAME}")
            else:
                # Iterative redeploy: upload as a new version
                version_name = f"v-{ts}"
                new_version = client.upload_pipeline_version(
                    pipeline_package_path=str(output_path),
                    pipeline_version_name=version_name,
                    pipeline_id=existing.pipeline_id,
                    description=PIPELINE_DESCRIPTION,
                )
                print(f"  Uploaded new version:")
                print(f"    Pipeline ID:  {existing.pipeline_id}")
                print(f"    Version ID:   {new_version.pipeline_version_id}")
                print(f"    Version name: {version_name}")

        if args.run:
            print(f"\nSubmitting one-off test run to {args.kfp_host}")
            run = client.create_run_from_pipeline_func(
                pipeline_func=retraining_pipeline,
                run_name=f"test-run-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
                experiment_name="cmapss-retraining-tests",
                enable_caching=False,
            )
            print(f"  Run ID:  {run.run_id}")
            print(f"  Run URL: {args.kfp_host.replace(':8888', ':8080')}/#/runs/details/{run.run_id}")


if __name__ == "__main__":
    main()
