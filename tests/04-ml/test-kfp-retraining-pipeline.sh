#!/bin/bash
# tests/04-ml/test-kfp-retraining-pipeline.sh
# Verifies the KFP retraining pipeline setup is complete and consistent.
#
# Replaces the manual "browser open the KFP UI and check the DAG" step.
# Runs entirely in-cluster — no port-forward, no browser, no Python.
#
# Preconditions:
#   - KFP Standalone deployed (Playbook 06)
#   - Retraining pipeline uploaded (Playbook 14 — TODO, or manual upload)
#   - thesis/retraining:<tag> image present in k8s.io containerd namespace
#   - mlops/minio-credentials Secret exists (Playbook 04)
#
# Checks performed:
#   1. KFP API responsive (sanity)
#   2. Pipeline 'cmapss-rul-retraining' uploaded
#   3. Pipeline YAML has 8 components (4 main + 4 helper)
#   4. Pipeline YAML has Secret inject (platforms.kubernetes.secretAsEnv)
#   5. minio-credentials Secret present in kubeflow namespace
#   6. pipeline-runner RBAC: monitoring (jobs) + mlops (deployments)
#   7. Container image referenced by pipeline.py present in containerd

set -uo pipefail
source "$(dirname "$0")/../_lib.sh"

test_header "KFP retraining pipeline setup"

# ---------- Preconditions ----------

if ! kubectl get deployment ml-pipeline -n kubeflow >/dev/null 2>&1; then
  skip "KFP not deployed yet (Playbook 06 not run)"
  exit 0
fi

# ---------- Check 1: KFP API responsive ----------

KFP_POD=$(kubectl get pod -n kubeflow -l app=ml-pipeline \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)

if [ -z "$KFP_POD" ]; then
  fail "Could not locate ml-pipeline API pod"
  exit 1
fi

info "Using KFP API pod: $KFP_POD"

HEALTH=$(kubectl exec -n kubeflow "$KFP_POD" -- \
  wget -qO- http://localhost:8888/apis/v1beta1/healthz 2>/dev/null || echo "fail")

if echo "$HEALTH" | grep -q "multi_user"; then
  pass "KFP API server responds to /healthz"
else
  fail "KFP API server not reachable" "got: $HEALTH"
  exit 1
fi

# ---------- Check 2: Pipeline uploaded ----------

PIPELINES_JSON=$(kubectl exec -n kubeflow "$KFP_POD" -- \
  wget -qO- "http://localhost:8888/apis/v2beta1/pipelines" 2>/dev/null || echo "")

if echo "$PIPELINES_JSON" | grep -q '"display_name":"cmapss-rul-retraining"'; then
  pass "Pipeline 'cmapss-rul-retraining' uploaded"

  # Extract pipeline ID for downstream checks
  PIPELINE_ID=$(echo "$PIPELINES_JSON" \
    | tr ',' '\n' \
    | grep -B1 '"display_name":"cmapss-rul-retraining"' \
    | grep '"pipeline_id"' \
    | head -1 \
    | sed -E 's/.*"pipeline_id":"([^"]+)".*/\1/')
  info "Pipeline ID: $PIPELINE_ID"
else
  fail "Pipeline 'cmapss-rul-retraining' not found in KFP" \
       "run: python kfp/retraining_pipeline.py --upload (or Playbook 14 when ready)"
fi

# ---------- Check 3: Pipeline has 8 components ----------

if [ -f "kfp/retraining_pipeline.yaml" ]; then
  COMPONENT_COUNT=$(grep -c "componentRef:" kfp/retraining_pipeline.yaml 2>/dev/null || echo 0)
  if [ "$COMPONENT_COUNT" = "8" ]; then
    pass "Compiled YAML has 8 components (4 main + 4 helper)"
  else
    fail "Compiled YAML has $COMPONENT_COUNT components" "expected 8"
  fi
else
  skip "kfp/retraining_pipeline.yaml not present in repo (re-compile to regenerate)"
fi

# ---------- Check 4: YAML Secret inject ----------

if [ -f "kfp/retraining_pipeline.yaml" ]; then
  SECRET_INJECT_COUNT=$(grep -c "secretName: minio-credentials" \
    kfp/retraining_pipeline.yaml 2>/dev/null || echo 0)
  if [ "$SECRET_INJECT_COUNT" = "3" ]; then
    pass "YAML has 3 secretAsEnv blocks (load_data, train_lstm, register_model)"
  else
    fail "YAML has $SECRET_INJECT_COUNT secretAsEnv blocks" "expected 3"
  fi
else
  skip "kfp/retraining_pipeline.yaml not in repo"
fi

# ---------- Check 5: minio-credentials Secret in kubeflow namespace ----------

if kubectl get secret minio-credentials -n kubeflow >/dev/null 2>&1; then
  pass "Secret 'minio-credentials' present in kubeflow namespace"

  # Verify keys
  SECRET_KEYS=$(kubectl get secret minio-credentials -n kubeflow \
    -o jsonpath='{.data}' 2>/dev/null \
    | grep -oE 'access_key|secret_key' | sort -u | tr '\n' ',' | sed 's/,$//')
  if [ "$SECRET_KEYS" = "access_key,secret_key" ]; then
    pass "Secret has both required keys (access_key, secret_key)"
  else
    fail "Secret keys mismatch" "got: $SECRET_KEYS, expected: access_key,secret_key"
  fi
else
  fail "Secret 'minio-credentials' missing in kubeflow namespace" \
       "copy from mlops: kubectl get secret minio-credentials -n mlops -o yaml | sed 's/namespace: mlops/namespace: kubeflow/' | kubectl apply -f -"
fi

# ---------- Check 6: pipeline-runner RBAC ----------

# 6a: monitoring namespace — create Jobs
CAN_CREATE_JOBS=$(kubectl auth can-i create jobs \
  --as=system:serviceaccount:kubeflow:pipeline-runner \
  -n monitoring 2>/dev/null)

if [ "$CAN_CREATE_JOBS" = "yes" ]; then
  pass "pipeline-runner can create Jobs in monitoring namespace"
else
  fail "pipeline-runner cannot create Jobs in monitoring namespace" \
       "missing Role + RoleBinding (files/retraining/k8s/rbac.yaml)"
fi

# 6b: mlops namespace — patch deployments
CAN_PATCH_DEPLOY=$(kubectl auth can-i patch deployment/fastapi \
  --as=system:serviceaccount:kubeflow:pipeline-runner \
  -n mlops 2>/dev/null)

if [ "$CAN_PATCH_DEPLOY" = "yes" ]; then
  pass "pipeline-runner can patch deployments in mlops namespace"
else
  fail "pipeline-runner cannot patch deployments in mlops namespace" \
       "missing Role + RoleBinding (files/retraining/k8s/rbac.yaml)"
fi

# 6c: mlops namespace — list pods (for rollout status)
CAN_LIST_PODS=$(kubectl auth can-i list pods \
  --as=system:serviceaccount:kubeflow:pipeline-runner \
  -n mlops 2>/dev/null)

if [ "$CAN_LIST_PODS" = "yes" ]; then
  pass "pipeline-runner can list pods in mlops namespace"
else
  fail "pipeline-runner cannot list pods in mlops namespace"
fi

# ---------- Check 7: Container image present ----------

# Dynamically resolve the image tag from kfp/retraining_pipeline.py instead of
# hard-coding a version. This way the test stays correct as the pipeline image
# is bumped (0.1.0 -> 0.2.0 -> 0.3.0 ...) without test code edits.
PIPELINE_IMAGE=$(grep '^IMAGE = ' kfp/retraining_pipeline.py 2>/dev/null \
  | sed -E 's|^IMAGE = "([^"]+)".*|\1|')

if [ -z "$PIPELINE_IMAGE" ]; then
  fail "Could not parse IMAGE tag from kfp/retraining_pipeline.py" \
       "expected a line like: IMAGE = \"thesis/retraining:0.x.y\""
else
  info "Pipeline references image: $PIPELINE_IMAGE"

  # k3s containerd uses /run/k3s/containerd/containerd.sock
  # Image must be in the k8s.io namespace (KFP pulls from there)
  IMAGE_FOUND=$(nerdctl --address /run/k3s/containerd/containerd.sock \
    --namespace k8s.io \
    images 2>/dev/null \
    | grep -cE "thesis/retraining[[:space:]]+${PIPELINE_IMAGE#*:}[[:space:]]" \
    || echo 0)

  if [ "$IMAGE_FOUND" -gt 0 ]; then
    pass "Image $PIPELINE_IMAGE present in containerd"
  else
    fail "Image $PIPELINE_IMAGE not found in containerd" \
         "build: ./scripts/build-and-deploy-retraining.sh --tag ${PIPELINE_IMAGE#*:}"
  fi
fi

# ---------- Summary ----------

echo ""
info "Setup verification complete. If all checks PASSed, the pipeline is ready"
info "for a manual or webhook-triggered run. See: kfp/retraining_pipeline.py"
