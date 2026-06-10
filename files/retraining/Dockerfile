# Container image for KFP pipeline components (retraining, evaluation, registration, rollout)
# Build context: /root/thesis-infra (repo root) — required for DVC pointer file access.
#
# Used by:
#   load_data_op       → app/load_data.py (DVC pull from MinIO)
#   train_lstm_op      → app/train_lstm.py
#   register_model_op  → app/register_model.py
#   trigger_rollout_op → app/trigger_rollout.py
#
# Image tag: thesis/retraining:0.1.0
#
# Build command (from repo root):
#   nerdctl build -t thesis/retraining:0.1.0 -f files/retraining/Dockerfile .

FROM python:3.12-slim

# ─── System dependencies ──────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        git \
    && rm -rf /var/lib/apt/lists/*
# Note: git is required by DVC (it inspects repo state even with no_scm=true).
# git is a thin dependency (~30 MB) and well worth the reproducibility argument.

# ─── kubectl (for trigger_rollout_op) ──────────────────────────────────
RUN curl -fsSL -o /usr/local/bin/kubectl \
        "https://dl.k8s.io/release/v1.30.5/bin/linux/amd64/kubectl" \
    && chmod +x /usr/local/bin/kubectl \
    && kubectl version --client

# ─── Python dependencies (heavy layer, cached separately) ─────────────
WORKDIR /app
COPY files/retraining/app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# ─── DVC repo skeleton ─────────────────────────────────────────────────
# We assemble a minimal DVC repo at /workspace inside the container so that
# `dvc pull` works without needing the full thesis-infra repository.
#
# Layout:
#   /workspace/
#   ├── .dvc/
#   │   └── config             ← cluster-aware version (svc.cluster.local endpoint)
#   └── data/
#       └── processed.dvc      ← DVC pointer file (small, ~200 B)
#
# Credentials come from environment variables (AWS_ACCESS_KEY_ID,
# AWS_SECRET_ACCESS_KEY) set by the KFP pipeline runtime — no secrets in image.

RUN mkdir -p /workspace/.dvc /workspace/data

# Cluster-aware DVC config (replaces repo's localhost:9000 endpoint)
COPY files/retraining/dvc-config-cluster /workspace/.dvc/config

# DVC pointer file (200 B, refers to dvc/files/md5/<hash> on MinIO)
COPY data/processed.dvc /workspace/data/processed.dvc

# Initialize DVC repo state (git is required by DVC's init checks)
RUN cd /workspace \
    && git init --quiet \
    && git config user.email "kfp@thesis-infra.local" \
    && git config user.name "kfp-pipeline"

# ─── Application code ──────────────────────────────────────────────────
COPY files/retraining/app/src/         /app/src/
COPY files/retraining/app/load_data.py       /app/load_data.py
COPY files/retraining/app/train_lstm.py      /app/train_lstm.py
COPY files/retraining/app/register_model.py  /app/register_model.py
COPY files/retraining/app/trigger_rollout.py /app/trigger_rollout.py

# PYTHONPATH for `from src.model import LSTMRegressor`
ENV PYTHONPATH=/app

# No CMD or ENTRYPOINT — KFP component invokes scripts directly via args.
# Example:
#   python /app/load_data.py --output-dir /tmp/data
