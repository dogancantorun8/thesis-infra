#!/bin/bash
# scripts/observability/healthcheck.sh
# =====================================
# Comprehensive health check for the thesis-infra MLOps stack.
#
# Six layers verified:
#   1. infra     — node Ready, namespaces, pods Running, PVCs Bound
#   2. storage   — MinIO buckets + PostgreSQL databases reachable
#   3. resources — kubectl top nodes (CPU% / MEMORY%)
#   4. ports     — kubectl port-forward tunnels listening locally
#   5. services  — HTTP /health endpoints responding
#   6. model     — FastAPI / MLflow alias / disk baseline / cluster baseline agree
#
# Usage:
#   ./scripts/observability/healthcheck.sh              # full report (all 6 layers)
#   ./scripts/observability/healthcheck.sh infra        # only infra layer
#   ./scripts/observability/healthcheck.sh storage      # only storage layer
#   ./scripts/observability/healthcheck.sh resources    # only resource usage
#   ./scripts/observability/healthcheck.sh ports        # only port-forward layer
#   ./scripts/observability/healthcheck.sh services     # only service HTTP responses
#   ./scripts/observability/healthcheck.sh model        # only model-version consistency
#   ./scripts/observability/healthcheck.sh --quiet      # CI mode: exit 0 on success, 1 on failure
#
# Exit codes:
#   0  all checks passed
#   1  one or more checks failed
#   2  invalid usage

set +e

# ---------- Colors ----------
if [ -t 1 ]; then
  GREEN='\033[0;32m'
  RED='\033[0;31m'
  YELLOW='\033[1;33m'
  BLUE='\033[0;34m'
  NC='\033[0m'
else
  GREEN=''; RED=''; YELLOW=''; BLUE=''; NC=''
fi

export KUBECONFIG=${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}

CHECKS_PASSED=0
CHECKS_FAILED=0
QUIET=0

pass() {
  CHECKS_PASSED=$((CHECKS_PASSED + 1))
  [ "$QUIET" = "0" ] && echo -e "  ${GREEN}OK${NC}    $1"
}

fail() {
  CHECKS_FAILED=$((CHECKS_FAILED + 1))
  [ "$QUIET" = "0" ] && echo -e "  ${RED}FAIL${NC}  $1"
  [ -n "${2:-}" ] && [ "$QUIET" = "0" ] && echo -e "        ${RED}↳${NC} $2"
}

warn() {
  [ "$QUIET" = "0" ] && echo -e "  ${YELLOW}WARN${NC}  $1"
  [ -n "${2:-}" ] && [ "$QUIET" = "0" ] && echo -e "        ${YELLOW}↳${NC} $2"
}

info() {
  [ "$QUIET" = "0" ] && echo -e "  ${BLUE}INFO${NC}  $1"
}

section() {
  [ "$QUIET" = "0" ] && echo "" && echo "─── $1 ───"
}

# ──────────────────────────────────────────────────────────────────
# LAYER 1: Infrastructure (node, namespaces, pods, PVCs)
# ──────────────────────────────────────────────────────────────────
check_infra() {
  section "1. Infrastructure (node, namespaces, pods, PVCs)"

  # Node status
  local node_status
  node_status=$(kubectl get nodes --no-headers 2>/dev/null | awk '{print $2}' | head -1)
  if [ "$node_status" = "Ready" ]; then
    pass "Node mlops-master    Ready"
  else
    fail "Node not Ready"     "status: ${node_status:-unknown}"
  fi

  # Namespaces and pods
  for ns in kube-system minio mlops kubeflow monitoring; do
    if kubectl get ns "$ns" >/dev/null 2>&1; then
      local total not_ready
      total=$(kubectl get pods -n "$ns" --no-headers 2>/dev/null | wc -l)
      not_ready=$(kubectl get pods -n "$ns" --no-headers 2>/dev/null | grep -v -E 'Running|Completed' | wc -l)

      if [ "$total" -eq 0 ]; then
        warn "ns/$ns"             "exists but has no pods"
      elif [ "$not_ready" -eq 0 ]; then
        pass "ns/$ns               $total/$total pods healthy"
      else
        fail "ns/$ns               $not_ready of $total pods NOT ready"
      fi
    else
      warn "ns/$ns"             "namespace does not exist"
    fi
  done

  # PVCs
  local pvc_total pvc_pending
  pvc_total=$(kubectl get pvc -A --no-headers 2>/dev/null | wc -l)
  pvc_pending=$(kubectl get pvc -A --no-headers 2>/dev/null | grep -v Bound | wc -l)
  if [ "$pvc_total" -eq 0 ]; then
    warn "PVCs"                 "no PVCs found"
  elif [ "$pvc_pending" -eq 0 ]; then
    pass "PVCs                  all $pvc_total Bound"
  else
    fail "PVCs"                 "$pvc_pending of $pvc_total not Bound"
  fi
}

# ──────────────────────────────────────────────────────────────────
# LAYER 2: Storage (MinIO buckets, PostgreSQL databases)
# ──────────────────────────────────────────────────────────────────
check_storage() {
  section "2. Storage (MinIO buckets, PostgreSQL databases)"

  # MinIO
  if kubectl get deploy/minio -n minio >/dev/null 2>&1; then
    if kubectl exec -n minio deploy/minio -- mc alias set local http://localhost:9000 \
         "$(kubectl get secret -n minio minio -o jsonpath='{.data.rootUser}' | base64 -d)" \
         "$(kubectl get secret -n minio minio -o jsonpath='{.data.rootPassword}' | base64 -d)" \
         >/dev/null 2>&1; then
      local buckets
      buckets=$(kubectl exec -n minio deploy/minio -- mc ls local/ 2>/dev/null \
                  | awk '{print $NF}' | tr -d '/' | sort | tr '\n' ' ')
      if echo "$buckets" | grep -q "thesis-data" \
         && echo "$buckets" | grep -q "thesis-mlflow" \
         && echo "$buckets" | grep -q "thesis-models"; then
        pass "MinIO                 3 buckets present: $buckets"
      else
        fail "MinIO"               "expected buckets missing. Found: $buckets"
      fi
    else
      fail "MinIO"                 "mc alias failed"
    fi
  else
    warn "MinIO"                  "not deployed"
  fi

  # PostgreSQL
  if kubectl get pod postgres-0 -n mlops >/dev/null 2>&1; then
    local pg_dbs
    pg_dbs=$(kubectl exec -n mlops postgres-0 -- psql -U postgres -tAc \
              "SELECT datname FROM pg_database WHERE datname IN ('mlflow','kfp');" 2>/dev/null \
              | tr '\n' ' ')
    if echo "$pg_dbs" | grep -q "mlflow" && echo "$pg_dbs" | grep -q "kfp"; then
      pass "PostgreSQL            both databases present: $pg_dbs"
    else
      fail "PostgreSQL"           "mlflow/kfp databases missing. Found: $pg_dbs"
    fi
  else
    warn "PostgreSQL"             "not deployed"
  fi
}

# ──────────────────────────────────────────────────────────────────
# LAYER 3: Resource usage (CPU/RAM)
# ──────────────────────────────────────────────────────────────────
check_resources() {
  section "3. Resource usage"

  local top_output header data
  top_output=$(kubectl top nodes 2>/dev/null)
  if [ -z "$top_output" ]; then
    warn "kubectl top nodes"     "metrics-server not ready"
    return
  fi

  header=$(echo "$top_output" | head -1)
  data=$(echo "$top_output" | tail -1)
  local cpu_pct_col mem_pct_col
  cpu_pct_col=$(echo "$header" | awk '{for(i=1;i<=NF;i++) if($i=="CPU%") print i}')
  mem_pct_col=$(echo "$header" | awk '{for(i=1;i<=NF;i++) if($i=="MEMORY%") print i}')

  if [ -z "$cpu_pct_col" ] || [ -z "$mem_pct_col" ]; then
    warn "kubectl top nodes"     "CPU%/MEMORY% columns not found"
    return
  fi

  local node_cpu node_mem
  node_cpu=$(echo "$data" | awk -v c="$cpu_pct_col" '{print $c}' | tr -d '%')
  node_mem=$(echo "$data" | awk -v c="$mem_pct_col" '{print $c}' | tr -d '%')

  if [ -z "$node_mem" ] || ! [ "$node_mem" -eq "$node_mem" ] 2>/dev/null; then
    warn "resource usage"        "could not parse memory percentage"
    return
  fi

  if [ "$node_mem" -gt 85 ]; then
    fail "Node memory            ${node_mem}% (CPU: ${node_cpu}%)"   "HIGH — consider workload reduction"
  elif [ "$node_mem" -gt 70 ]; then
    warn "Node memory            ${node_mem}% (CPU: ${node_cpu}%)"   "moderate — monitor"
  else
    pass "Node memory            ${node_mem}% (CPU: ${node_cpu}%)"
  fi
}

# ──────────────────────────────────────────────────────────────────
# Service registry (used by ports + services layers)
# ──────────────────────────────────────────────────────────────────
SERVICES=(
  "FastAPI         |8000|/                          |model_version"
  "MLflow          |5000|/health                    |"
  "MinIO Console   |9001|/                          |"
  "MinIO S3 API    |9000|/minio/health/live         |"
  "Grafana         |3000|/api/health                |ok"
  "Prometheus      |9090|/-/healthy                 |"
  "Alertmanager    |9093|/-/healthy                 |"
  "KFP UI          |8080|/                          |"
  "Jupyter Lab     |8889|/api                       |"
)

# ──────────────────────────────────────────────────────────────────
# LAYER 4: Port-forward tunnels
# ──────────────────────────────────────────────────────────────────
check_ports() {
  section "4. Port-forward layer (local tunnels)"

  for entry in "${SERVICES[@]}"; do
    IFS='|' read -r name port path pattern <<< "$entry"
    name_trim="${name// /}"
    port_trim="${port// /}"

    if ss -tln 2>/dev/null | grep -q ":${port_trim} "; then
      pass "${name}    port ${port_trim} listening"
    else
      fail "${name}    port ${port_trim} NOT listening" \
           "run: ./scripts/observability/port-forward-all.sh restart"
    fi
  done
}

# ──────────────────────────────────────────────────────────────────
# LAYER 5: Service HTTP health
# ──────────────────────────────────────────────────────────────────
check_services() {
  section "5. Service responses (HTTP health endpoints)"

  for entry in "${SERVICES[@]}"; do
    IFS='|' read -r name port path pattern <<< "$entry"
    name_trim="${name// /}"
    port_trim="${port// /}"
    path_trim="${path// /}"
    pattern_trim="${pattern// /}"

    # Use HTTP status code to determine liveness:
    #   2xx  = healthy
    #   3xx  = redirect (alive — e.g. Jupyter auth redirect)
    #   401  = unauthorized (alive but token required)
    #   403  = forbidden (alive but auth required, e.g. MinIO S3)
    #   5xx / 000 / timeout = down
    local http_code response
    http_code=$(curl -s -o /tmp/.hc-body -m 5 -w "%{http_code}" \
                  "http://localhost:${port_trim}${path_trim}" 2>/dev/null)
    response=$(cat /tmp/.hc-body 2>/dev/null || echo "")
    rm -f /tmp/.hc-body

    if [ -z "$http_code" ] || [ "$http_code" = "000" ]; then
      fail "${name}"             "no response (connection refused or timeout)"
    elif [ "$http_code" = "401" ] || [ "$http_code" = "403" ]; then
      pass "${name}    HTTP ${http_code} — auth-protected, alive"
    elif [ "$http_code" -ge 300 ] && [ "$http_code" -lt 400 ]; then
      pass "${name}    HTTP ${http_code} — redirect, alive"
    elif [ "$http_code" -ge 200 ] && [ "$http_code" -lt 300 ]; then
      if [ -n "$pattern_trim" ] && [ -n "$response" ] && ! echo "$response" | grep -q "$pattern_trim"; then
        warn "${name}"           "HTTP ${http_code} but expected '${pattern_trim}' in body"
      else
        pass "${name}    HTTP ${http_code}"
      fi
    else
      fail "${name}"             "HTTP ${http_code} from http://localhost:${port_trim}${path_trim}"
    fi
  done
}

# ──────────────────────────────────────────────────────────────────
# LAYER 6: Model state consistency
# ──────────────────────────────────────────────────────────────────
check_model_consistency() {
  section "6. Model state consistency"

  local fastapi_v mlflow_v disk_v cluster_v

  # FastAPI version
  fastapi_v=$(curl -sS -m 5 http://localhost:8000/ 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('model_version','?'))" 2>/dev/null)
  if [ -z "$fastapi_v" ] || [ "$fastapi_v" = "?" ]; then
    fail "FastAPI model_version" "could not query / endpoint"
    return
  fi
  pass "FastAPI model_version       v${fastapi_v}"

  # MLflow @production alias
  mlflow_v=$(curl -sS -m 5 \
    "http://localhost:5000/api/2.0/mlflow/registered-models/alias?name=cmapss-rul&alias=production" 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('model_version',{}).get('version','?'))" 2>/dev/null)
  if [ -z "$mlflow_v" ] || [ "$mlflow_v" = "?" ]; then
    fail "MLflow @production alias" "could not query MLflow API"
    return
  fi
  pass "MLflow @production alias    v${mlflow_v}"

  # Disk baseline
  disk_v=$(python3 -c "
import json
try:
    print(json.load(open('/root/thesis-infra/data/drift/baseline.json')).get('model_version','?'))
except Exception:
    print('?')
" 2>/dev/null)
  if [ "$disk_v" = "?" ] || [ -z "$disk_v" ]; then
    fail "Drift baseline (disk)" "could not read data/drift/baseline.json"
    return
  fi
  pass "Drift baseline (disk)       v${disk_v}"

  # Cluster ConfigMap baseline
  cluster_v=$(kubectl get configmap evidently-baseline -n monitoring \
    -o jsonpath='{.data.baseline\.json}' 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('model_version','?'))" 2>/dev/null)
  if [ "$cluster_v" = "?" ] || [ -z "$cluster_v" ]; then
    fail "Drift baseline (cluster)" "could not read ConfigMap evidently-baseline"
    return
  fi
  pass "Drift baseline (cluster)    v${cluster_v}"

  # All four agree?
  if [ "$fastapi_v" = "$mlflow_v" ] && [ "$mlflow_v" = "$disk_v" ] && [ "$disk_v" = "$cluster_v" ]; then
    pass "All four agree              v${fastapi_v}"
  else
    fail "Version mismatch" \
         "FastAPI=v${fastapi_v}, MLflow=v${mlflow_v}, disk=v${disk_v}, cluster=v${cluster_v}"
    info "Fix: ansible-playbook playbooks/12-evidently.yml --ask-vault-pass"
  fi
}

# ──────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────
usage() {
  cat <<USAGE
Usage: $0 [layer] [--quiet]

Layers:
  infra      Node, namespaces, pods, PVCs
  storage    MinIO buckets, PostgreSQL databases
  resources  CPU/MEMORY% on the node
  ports      Port-forward tunnels (listening locally)
  services   HTTP /health endpoints
  model      FastAPI / MLflow alias / disk baseline / cluster baseline agree

  (default)  All six layers

Flags:
  --quiet    Suppress output; exit 0 on success, 1 on any failure

Exit codes:
  0  all checks passed
  1  one or more checks failed
  2  invalid usage
USAGE
}

MODE="${1:-all}"
[ "${2:-}" = "--quiet" ] && QUIET=1
[ "${1:-}" = "--quiet" ] && QUIET=1 && MODE="all"

[ "$QUIET" = "0" ] && {
  echo "=============================================="
  echo "  thesis-infra healthcheck"
  echo "  $(date)"
  echo "=============================================="
}

case "$MODE" in
  infra)     check_infra ;;
  storage)   check_storage ;;
  resources) check_resources ;;
  ports)     check_ports ;;
  services)  check_services ;;
  model)     check_model_consistency ;;
  all|--quiet)
    check_infra
    check_storage
    check_resources
    check_ports
    check_services
    check_model_consistency
    ;;
  -h|--help)
    usage
    exit 2
    ;;
  *)
    echo "Unknown layer: $MODE"
    usage
    exit 2
    ;;
esac

# Summary
[ "$QUIET" = "0" ] && echo ""
[ "$QUIET" = "0" ] && echo "=============================================="
if [ "$CHECKS_FAILED" = "0" ]; then
  [ "$QUIET" = "0" ] && echo -e "  ${GREEN}OK${NC}  All checks passed (${CHECKS_PASSED})"
  exit 0
else
  [ "$QUIET" = "0" ] && echo -e "  ${RED}FAIL${NC}  ${CHECKS_FAILED} failed, ${CHECKS_PASSED} passed"
  exit 1
fi
