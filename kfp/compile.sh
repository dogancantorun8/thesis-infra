#!/usr/bin/env bash
#
# kfp/compile.sh — One-stop pipeline lifecycle helper.
#
# Usage:
#   ./kfp/compile.sh compile     # Just compile to YAML
#   ./kfp/compile.sh upload      # Compile + upload to KFP
#   ./kfp/compile.sh run         # Compile + submit one-off test run
#   ./kfp/compile.sh all         # compile + upload + run
#
# Requires:
#   - /root/thesis-infra/.venv with kfp installed
#   - kubectl access to the kubeflow namespace (auto-managed port-forward)

set -euo pipefail

REPO_ROOT="/root/thesis-infra"
VENV_PY="${REPO_ROOT}/.venv/bin/python"
PIPELINE_DEF="${REPO_ROOT}/kfp/retraining_pipeline.py"
OUTPUT_YAML="${REPO_ROOT}/kfp/retraining_pipeline.yaml"

ACTION="${1:-compile}"

start_port_forward() {
    echo "[compile.sh] Starting port-forward to ml-pipeline..."
    pkill -f "kubectl.*port-forward.*ml-pipeline" 2>/dev/null || true
    sleep 1
    kubectl port-forward -n kubeflow svc/ml-pipeline 8888:8888 > /tmp/kfp-pf.log 2>&1 &
    PF_PID=$!
    sleep 3
    echo "[compile.sh] Port-forward PID: $PF_PID"
}

stop_port_forward() {
    pkill -f "kubectl.*port-forward.*ml-pipeline" 2>/dev/null || true
    echo "[compile.sh] Port-forward stopped"
}

trap stop_port_forward EXIT

case "$ACTION" in
    compile)
        echo "[compile.sh] Compiling pipeline only..."
        "$VENV_PY" "$PIPELINE_DEF" --output "$OUTPUT_YAML"
        ;;
    upload)
        start_port_forward
        echo "[compile.sh] Compiling + uploading..."
        "$VENV_PY" "$PIPELINE_DEF" --output "$OUTPUT_YAML" --upload \
            --kfp-host http://localhost:8888
        ;;
    run)
        start_port_forward
        echo "[compile.sh] Compiling + submitting one-off test run..."
        "$VENV_PY" "$PIPELINE_DEF" --output "$OUTPUT_YAML" --run \
            --kfp-host http://localhost:8888
        ;;
    all)
        start_port_forward
        echo "[compile.sh] Compile + upload + run..."
        "$VENV_PY" "$PIPELINE_DEF" --output "$OUTPUT_YAML" --upload --run \
            --kfp-host http://localhost:8888
        ;;
    *)
        echo "Usage: $0 {compile|upload|run|all}"
        exit 1
        ;;
esac

echo "[compile.sh] Done."
