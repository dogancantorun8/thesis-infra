#!/usr/bin/env python3
"""
KFP Component 1: load_data_op (DVC-aware)
==========================================

Pulls preprocessed training tensors from the MinIO bucket `thesis-data` using
DVC, then copies the four required `.npy` files to the KFP output directory.

Why DVC pull (instead of direct boto3 download)?
   The retraining pipeline always uses the exact, DVC-tracked version of the
   training data — addressed by content hash (md5), not by mutable bucket path.
   This makes every retraining reproducible: 6 months from now, if you need to
   reproduce a model trained today, the same data will be pulled, byte-for-byte.

This is the core "data reproducibility" argument of the thesis.

Container layout assumed (built by files/retraining/Dockerfile):
    /workspace/
    ├── .dvc/config        ← cluster-aware (endpoint: minio.minio.svc.cluster.local)
    └── data/
        └── processed.dvc  ← DVC pointer file (referencing md5 hashes on MinIO)

Credentials:
    AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY must be set in the environment.
    The KFP pipeline runtime injects these from the `minio-credentials` Secret
    (copied from mlops namespace to kubeflow namespace during cluster setup).

Usage:
    python load_data.py --output-dir /tmp/data

Outputs (written to --output-dir):
    X_train.npy   (shape: [n_samples, sequence_length=30, n_features=16])
    y_train.npy   (shape: [n_samples])
    X_val.npy
    y_val.npy
"""

import argparse
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("load_data")

WORKSPACE = Path("/workspace")  # DVC repo root inside the container
DVC_POINTER = "data/processed.dvc"
REQUIRED_FILES = ["X_train.npy", "y_train.npy", "X_val.npy", "y_val.npy"]


def check_credentials() -> bool:
    """Verify MinIO credentials are present in environment."""
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        if var not in os.environ:
            log.error("%s is not set — DVC cannot authenticate to MinIO", var)
            return False
    log.info("MinIO credentials present (AWS_ACCESS_KEY_ID = %s...)",
             os.environ["AWS_ACCESS_KEY_ID"][:8])
    return True


def run_dvc_pull() -> bool:
    """Execute `dvc pull` against the cluster MinIO endpoint."""
    log.info("Working directory: %s", WORKSPACE)
    log.info("DVC pointer:       %s", DVC_POINTER)

    # Show DVC + remote config for debug
    subprocess.run(
        ["dvc", "version"],
        cwd=WORKSPACE, check=False,
    )
    subprocess.run(
        ["dvc", "remote", "list"],
        cwd=WORKSPACE, check=False,
    )

    log.info("Running: dvc pull %s --force", DVC_POINTER)
    result = subprocess.run(
        ["dvc", "pull", DVC_POINTER, "--force", "--verbose"],
        cwd=WORKSPACE,
        capture_output=True, text=True, timeout=300,
    )

    if result.returncode != 0:
        log.error("dvc pull failed (rc=%d)", result.returncode)
        log.error("STDOUT:\n%s", result.stdout)
        log.error("STDERR:\n%s", result.stderr)
        return False

    log.info("dvc pull stdout:\n%s", result.stdout)
    if result.stderr:
        log.debug("dvc pull stderr:\n%s", result.stderr)
    return True


def verify_pulled_files() -> bool:
    """Confirm all required .npy files exist in /workspace/data/processed/."""
    pulled_dir = WORKSPACE / "data" / "processed"
    log.info("Checking pulled files in: %s", pulled_dir)

    if not pulled_dir.exists():
        log.error("Directory not created by dvc pull: %s", pulled_dir)
        return False

    for filename in REQUIRED_FILES:
        path = pulled_dir / filename
        if not path.exists():
            log.error("Required file missing after dvc pull: %s", path)
            return False
        size_mb = path.stat().st_size / (1024 * 1024)
        log.info("  OK  %s (%.2f MB)", filename, size_mb)

    return True


def copy_to_output(output_dir: Path) -> bool:
    """Copy the four required .npy files to the KFP output directory."""
    pulled_dir = WORKSPACE / "data" / "processed"
    output_dir.mkdir(parents=True, exist_ok=True)

    for filename in REQUIRED_FILES:
        src = pulled_dir / filename
        dst = output_dir / filename
        shutil.copy(src, dst)
        size_mb = dst.stat().st_size / (1024 * 1024)
        log.info("  Copied %s → %s (%.2f MB)", filename, dst, size_mb)

    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Local directory to write .npy files into (consumed by train_lstm_op)",
    )
    args = parser.parse_args()

    # ─── 1. Credentials ──────────────────────────────────────────────
    if not check_credentials():
        return 1

    # ─── 2. DVC pull ──────────────────────────────────────────────────
    if not run_dvc_pull():
        return 2

    # ─── 3. Verify files arrived ──────────────────────────────────────
    if not verify_pulled_files():
        return 3

    # ─── 4. Copy to KFP output directory ──────────────────────────────
    output_dir = Path(args.output_dir)
    if not copy_to_output(output_dir):
        return 4

    log.info("All required files staged in %s", output_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
