# baseline-refresh: regenerates drift detection baseline for the
# current MLflow @production model and updates the cluster ConfigMap.
#
# Built with nerdctl + buildkit (containerd-native, no Docker daemon).
# Same toolchain as files/fastapi/Dockerfile and files/evidently/Dockerfile.

FROM python:3.12-slim

# Install kubectl (for ConfigMap apply) + curl
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && curl -fsSLo /usr/local/bin/kubectl \
        https://dl.k8s.io/release/v1.30.3/bin/linux/amd64/kubectl \
    && chmod +x /usr/local/bin/kubectl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps first for layer caching
COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Refresh script
COPY app/refresh.py /app/refresh.py

# Model serialization references src.model.LSTMRegressor; bundle it into
# the image so MLflow can resolve the import path at load time.
# Same pattern as files/fastapi/Dockerfile.
COPY src/ /app/src/

# Defaults (overridable in K8s Job spec)
ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    MLFLOW_TRACKING_URI=http://mlflow.mlops.svc.cluster.local:5000 \
    MLFLOW_S3_ENDPOINT_URL=http://minio.minio.svc.cluster.local:9000 \
    MODEL_NAME=cmapss-rul \
    MODEL_ALIAS=production \
    X_TRAIN_PATH=/data/X_train.npy \
    S3_BUCKET=thesis-data \
    S3_KEY=processed/X_train.npy \
    AWS_DEFAULT_REGION=us-east-1 \
    CONFIGMAP_NAME=evidently-baseline \
    CONFIGMAP_NAMESPACE=monitoring \
    HOST_DISK_PATH=/mnt/host-data/baseline.json

ENTRYPOINT ["python", "/app/refresh.py"]
