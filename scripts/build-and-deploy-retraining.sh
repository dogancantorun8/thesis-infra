#!/usr/bin/env bash
#
# scripts/build-and-deploy-retraining.sh
# ──────────────────────────────────────────────────────────────────
# One-shot build + deploy for the KFP retraining pipeline.
#
# Stages:
#   1. Resolve target image tag (auto-bump or --tag)
#   2. nerdctl build → thesis/retraining:<tag>
#   3. Update IMAGE in kfp/retraining_pipeline.py (sed)
#   4. Compile pipeline YAML + upload as new version
#      (delegates to kfp/compile.sh upload)
#   5. (optional) Submit a test run
#   6. (optional) Run the pipeline-setup test
#
# Usage:
#   ./scripts/build-and-deploy-retraining.sh                # auto-bump patch
#   ./scripts/build-and-deploy-retraining.sh --tag 0.5.0    # explicit tag
#   ./scripts/build-and-deploy-retraining.sh --run          # also start test run
#   ./scripts/build-and-deploy-retraining.sh --no-test      # skip post-deploy test
#   ./scripts/build-and-deploy-retraining.sh --tag 0.3.0 --run --epochs 30
#
# Flags:
#   --tag VER          Explicit image tag (skips auto-bump)
#   --run              Submit a test run after upload
#   --no-test          Skip the post-deploy pipeline-setup test
#   --epochs N         Override epochs (default 30, only when --run)
#   --threshold F      Override threshold_factor (default 0.001 = safe)
#   --help, -h         Show this message

set -uo pipefail

# ──────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────
REPO_ROOT="/root/thesis-infra"
VENV_PY="${REPO_ROOT}/.venv/bin/python"
PIPELINE_DEF="${REPO_ROOT}/kfp/retraining_pipeline.py"
COMPILE_SH="${REPO_ROOT}/kfp/compile.sh"
DOCKERFILE="${REPO_ROOT}/files/retraining/Dockerfile"
SETUP_TEST="${REPO_ROOT}/tests/04-ml/test-kfp-retraining-pipeline.sh"
IMAGE_NAME="thesis/retraining"
NERDCTL_ADDR="/run/k3s/containerd/containerd.sock"
NERDCTL_NS="k8s.io"

# Color helpers
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# ──────────────────────────────────────────────────────────────────
# Defaults
# ──────────────────────────────────────────────────────────────────
EXPLICIT_TAG=""
DO_RUN=false
DO_TEST=true
EPOCHS=30
THRESHOLD=0.001

# ──────────────────────────────────────────────────────────────────
# Parse args
# ──────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case $1 in
        --tag)         EXPLICIT_TAG="$2"; shift 2 ;;
        --run)         DO_RUN=true; shift ;;
        --no-test)     DO_TEST=false; shift ;;
        --epochs)      EPOCHS="$2"; shift 2 ;;
        --threshold)   THRESHOLD="$2"; shift 2 ;;
        --help|-h)
            grep '^#' "$0" | sed 's/^# //' | sed 's/^#//'
            exit 0
            ;;
        *)
            echo -e "${RED}Unknown flag: $1${NC}"
            exit 1
            ;;
    esac
done

# ──────────────────────────────────────────────────────────────────
# Stage 1: Resolve target tag
# ──────────────────────────────────────────────────────────────────
section() { echo ""; echo -e "${BLUE}━━━ $1 ━━━${NC}"; }
info()    { echo -e "  ${BLUE}INFO${NC}  $1"; }
ok()      { echo -e "  ${GREEN}OK${NC}    $1"; }
fail()    { echo -e "  ${RED}FAIL${NC}  $1"; }

section "Stage 1: Resolve target tag"

# Current tag from pipeline.py
CURRENT_TAG=$(grep '^IMAGE = ' "$PIPELINE_DEF" \
    | sed -E 's|^IMAGE = "[^:]+:([^"]+)".*|\1|')
info "Current tag in pipeline.py: $CURRENT_TAG"

if [[ -n "$EXPLICIT_TAG" ]]; then
    TARGET_TAG="$EXPLICIT_TAG"
    info "Using explicit tag: $TARGET_TAG"
else
    # Auto-bump patch version (0.2.0 → 0.3.0)
    if [[ "$CURRENT_TAG" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
        MAJOR="${BASH_REMATCH[1]}"
        MINOR="${BASH_REMATCH[2]}"
        PATCH="${BASH_REMATCH[3]}"
        NEW_MINOR=$((MINOR + 1))
        TARGET_TAG="${MAJOR}.${NEW_MINOR}.0"
        info "Auto-bumped minor: $CURRENT_TAG → $TARGET_TAG"
    else
        fail "Cannot parse current tag '$CURRENT_TAG' for auto-bump"
        fail "Use --tag VER to specify explicitly"
        exit 1
    fi
fi

# Check if same tag — warn (sometimes intentional, like patches)
if [[ "$TARGET_TAG" == "$CURRENT_TAG" ]]; then
    echo -e "  ${YELLOW}WARN${NC}  Target tag == current tag ($TARGET_TAG)."
    echo -e "        Build will overwrite. Continue? (y/N)"
    read -r CONFIRM
    [[ "$CONFIRM" != "y" ]] && exit 0
fi

FULL_IMAGE="${IMAGE_NAME}:${TARGET_TAG}"
info "Target image: $FULL_IMAGE"

# ──────────────────────────────────────────────────────────────────
# Stage 2: Container build
# ──────────────────────────────────────────────────────────────────
section "Stage 2: Container build"

info "Building $FULL_IMAGE (this may use cache from previous builds)"
cd "$REPO_ROOT"

if time nerdctl --address "$NERDCTL_ADDR" --namespace "$NERDCTL_NS" \
    build -t "$FULL_IMAGE" -f "$DOCKERFILE" . 2>&1 | tail -8; then
    ok "Image built: $FULL_IMAGE"
else
    fail "Build failed"
    exit 1
fi

# Verify image exists
IMAGE_COUNT=$(nerdctl --address "$NERDCTL_ADDR" --namespace "$NERDCTL_NS" \
    images 2>/dev/null | grep -c "${IMAGE_NAME}.*${TARGET_TAG}")
if [[ "$IMAGE_COUNT" -ge 1 ]]; then
    ok "Image registered in containerd ($IMAGE_COUNT entry)"
else
    fail "Image not found after build"
    exit 1
fi

# ──────────────────────────────────────────────────────────────────
# Stage 3: Update IMAGE tag in pipeline.py
# ──────────────────────────────────────────────────────────────────
section "Stage 3: Update IMAGE tag in pipeline.py"

if [[ "$TARGET_TAG" == "$CURRENT_TAG" ]]; then
    ok "No tag change needed (already $TARGET_TAG)"
else
    sed -i "s|^IMAGE = \"${IMAGE_NAME}:${CURRENT_TAG}\"|IMAGE = \"${IMAGE_NAME}:${TARGET_TAG}\"|" \
        "$PIPELINE_DEF"
    NEW_LINE=$(grep '^IMAGE = ' "$PIPELINE_DEF")
    ok "Updated: $NEW_LINE"
fi

# ──────────────────────────────────────────────────────────────────
# Stage 4: Compile + upload via kfp/compile.sh
# ──────────────────────────────────────────────────────────────────
section "Stage 4: Compile + upload pipeline YAML"

if [[ ! -x "$COMPILE_SH" ]]; then
    chmod +x "$COMPILE_SH" 2>/dev/null || true
fi

if bash "$COMPILE_SH" upload 2>&1; then
    ok "Pipeline compiled + uploaded (new version)"
else
    fail "Compile/upload failed"
    exit 1
fi

# ──────────────────────────────────────────────────────────────────
# Stage 5: Run pipeline-setup test (unless --no-test)
# ──────────────────────────────────────────────────────────────────
if [[ "$DO_TEST" == "true" ]]; then
    section "Stage 5: Post-deploy pipeline-setup test"
    if bash "$SETUP_TEST" 2>&1 | tail -20; then
        ok "Setup test completed"
    else
        echo -e "  ${YELLOW}WARN${NC}  Setup test reported issues (review above)"
    fi
fi

# ──────────────────────────────────────────────────────────────────
# Stage 6: (optional) Submit test run
# ──────────────────────────────────────────────────────────────────
if [[ "$DO_RUN" == "true" ]]; then
    section "Stage 6: Submit test run"
    info "Params: epochs=$EPOCHS, threshold_factor=$THRESHOLD (safe mode)"

    pkill -f "kubectl.*port-forward.*ml-pipeline" 2>/dev/null || true
    sleep 1
    kubectl port-forward -n kubeflow svc/ml-pipeline 8888:8888 > /tmp/kfp-pf.log 2>&1 &
    sleep 3

    "$VENV_PY" - <<PYEOF
from kfp import Client
from datetime import datetime

client = Client(host="http://localhost:8888")
PIPELINE_ID = "48a01d41-cacf-4a28-8897-6c268d348aa3"

versions = client.list_pipeline_versions(PIPELINE_ID)
latest = sorted(versions.pipeline_versions, key=lambda v: v.created_at, reverse=True)[0]
print(f"  Using version: {latest.display_name}")

experiment = client.get_experiment(experiment_name="cmapss-retraining-tests")

RUN_NAME = f"deploy-${TARGET_TAG}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
run = client.run_pipeline(
    experiment_id=experiment.experiment_id,
    job_name=RUN_NAME,
    pipeline_id=PIPELINE_ID,
    version_id=latest.pipeline_version_id,
    params={
        "epochs": $EPOCHS,
        "batch_size": 64,
        "learning_rate": 0.001,
        "threshold_factor": $THRESHOLD,
    },
    enable_caching=False,
)

print(f"  Run name:  {RUN_NAME}")
print(f"  Run ID:    {run.run_id}")
print(f"  UI URL:    http://localhost:8080/#/runs/details/{run.run_id}")
PYEOF

    pkill -f "kubectl.*port-forward.*ml-pipeline" 2>/dev/null || true
    ok "Test run submitted (cluster-side execution: ~10-15 min for multi-seed)"
fi

# ──────────────────────────────────────────────────────────────────
# Summary
# ──────────────────────────────────────────────────────────────────
section "Summary"
ok "Image:    $FULL_IMAGE"
ok "Pipeline: cmapss-rul-retraining (new version uploaded)"
[[ "$DO_TEST" == "true" ]] && ok "Test:     Pipeline-setup test executed"
[[ "$DO_RUN" == "true" ]]  && ok "Run:      Test run submitted to cluster"

echo ""
echo -e "${GREEN}━━━ Deploy complete ━━━${NC}"
