# files/drift-webhook/Dockerfile
# Multi-stage build for the drift webhook server.
# Final image is ~300 MB (python:3.12-slim base + FastAPI + KFP SDK).
#
# Used by: Deployment drift-webhook in mlops namespace
#
# Build command (from repo root):
#   nerdctl --address /run/k3s/containerd/containerd.sock --namespace k8s.io \
#     build -t thesis/drift-webhook:0.1.0 -f files/drift-webhook/Dockerfile .

# ---------------- Builder stage ----------------
FROM python:3.12-slim AS builder

WORKDIR /build

# Install pip wheels into a venv for clean copy in the final stage
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY app/requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ---------------- Runtime stage ----------------
FROM python:3.12-slim

# Run as non-root for safety
RUN useradd --create-home --shell /bin/bash app
USER app
WORKDIR /home/app

# Copy the prebuilt venv from the builder stage
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy the app code
COPY --chown=app:app app/ ./app/
ENV PYTHONPATH=/home/app:${PYTHONPATH}

# Default env (overridden by Kubernetes manifest)
ENV PYTHONUNBUFFERED=1 \
    KFP_HOST=http://ml-pipeline.kubeflow.svc.cluster.local:8888 \
    PIPELINE_NAME=cmapss-rul-retraining \
    KFP_NAMESPACE=kubeflow \
    EXPERIMENT_NAME=cmapss-retraining-tests \
    DEBOUNCE_SECONDS=300 \
    RETRAIN_EPOCHS=30 \
    RETRAIN_THRESHOLD=0.05 \
    RETRAIN_BATCH_SIZE=64 \
    RETRAIN_LEARNING_RATE=0.001

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
