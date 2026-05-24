#!/usr/bin/env python3
"""
Drift detection script — runs as a Kubernetes CronJob (hourly).

Workflow:
  1. Load baseline.json (mounted as ConfigMap at /etc/baseline/baseline.json)
  2. Query Prometheus for the last 1 hour of rul_prediction_value histograms
  3. Compute PSI (Population Stability Index) between baseline and production
  4. Compute KS-test (Kolmogorov-Smirnov) — non-parametric distribution test
  5. Push drift metrics to Prometheus Pushgateway
  6. If PSI > 0.2 OR KS p-value < 0.05 → fire Alertmanager webhook

Environment variables (set by Kubernetes CronJob manifest):
  PROMETHEUS_URL        — http://prometheus-kube-prometheus-prometheus.monitoring.svc.cluster.local:9090
  PUSHGATEWAY_URL       — http://prometheus-pushgateway.monitoring.svc.cluster.local:9091
  ALERTMANAGER_URL      — http://prometheus-kube-prometheus-alertmanager.monitoring.svc.cluster.local:9093
  BASELINE_PATH         — /etc/baseline/baseline.json (default)
  WINDOW                — 1h (default — Prometheus PromQL duration)
  PSI_THRESHOLD         — 0.2 (default — alert above this)
  KS_PVALUE_THRESHOLD   — 0.05 (default — alert below this)

Outputs:
  - Console logs (kubectl logs ...)
  - Pushgateway metrics (drift_psi, drift_ks_statistic, drift_ks_pvalue,
    drift_detected, drift_baseline_median, drift_production_median)
  - Alertmanager webhook (if drift detected)
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import requests
from scipy import stats

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("drift-check")


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
PROMETHEUS_URL = os.getenv(
    "PROMETHEUS_URL",
    "http://prometheus-kube-prometheus-prometheus.monitoring.svc.cluster.local:9090",
)
PUSHGATEWAY_URL = os.getenv(
    "PUSHGATEWAY_URL",
    "http://prometheus-pushgateway.monitoring.svc.cluster.local:9091",
)
ALERTMANAGER_URL = os.getenv(
    "ALERTMANAGER_URL",
    "http://prometheus-kube-prometheus-alertmanager.monitoring.svc.cluster.local:9093",
)
BASELINE_PATH = os.getenv("BASELINE_PATH", "/etc/baseline/baseline.json")
WINDOW = os.getenv("WINDOW", "1h")
PSI_THRESHOLD = float(os.getenv("PSI_THRESHOLD", "0.2"))
KS_PVALUE_THRESHOLD = float(os.getenv("KS_PVALUE_THRESHOLD", "0.05"))
JOB_NAME = "evidently_drift_check"


# ----------------------------------------------------------------------
# Step 1: Load baseline
# ----------------------------------------------------------------------
def load_baseline(path: str) -> dict:
    """Load training-set prediction baseline JSON."""
    log.info(f"Loading baseline from {path}")
    p = Path(path)
    if not p.exists():
        log.error(f"Baseline not found at {path}")
        sys.exit(1)
    with p.open() as f:
        baseline = json.load(f)
    log.info(
        f"Baseline: model v{baseline['model_version']}, "
        f"training_size={baseline['training_set_size']}, "
        f"median={baseline['prediction_stats']['median']:.2f}"
    )
    return baseline


# ----------------------------------------------------------------------
# Step 2: Query Prometheus for production histogram
# ----------------------------------------------------------------------
def query_prometheus(query: str) -> list:
    """Run a Prometheus instant query, return result list."""
    url = f"{PROMETHEUS_URL}/api/v1/query"
    log.debug(f"Querying: {query}")
    try:
        resp = requests.get(url, params={"query": query}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "success":
            log.error(f"Prometheus query failed: {data}")
            return []
        return data["data"]["result"]
    except Exception as e:
        log.error(f"Prometheus query error: {e}")
        return []


def fetch_production_histogram(buckets: list) -> dict:
    """
    Fetch production prediction counts per bucket over the WINDOW.

    Returns dict mapping bucket_le -> count delta over the window.
    Strategy: query `increase(rul_prediction_value_bucket[WINDOW])` —
    this gives the count of new predictions in each bucket during
    the window.
    """
    log.info(f"Fetching production histogram over last {WINDOW}")
    query = f'increase(rul_prediction_value_bucket[{WINDOW}])'
    results = query_prometheus(query)

    if not results:
        log.warning("No production data available — service may be idle")
        return {}

    # Aggregate counts per bucket (sum across pod instances)
    bucket_counts = {}
    for entry in results:
        le = entry["metric"].get("le", "")
        count = float(entry["value"][1])
        bucket_counts[le] = bucket_counts.get(le, 0) + count

    log.info(f"Found {len(bucket_counts)} bucket entries in production")
    return bucket_counts


def cumulative_to_per_bucket(
    cumulative: dict, buckets: list
) -> np.ndarray:
    """
    Convert Prometheus cumulative bucket counts to per-bucket counts.

    Prometheus histograms are cumulative: bucket le="50" counts everything
    up to 50, le="75" counts everything up to 75 (includes le="50"), etc.
    Drift comparison needs per-bucket (non-cumulative) counts.

    `buckets` defines N+1 edges, so we return N per-bucket counts to
    align with baseline (which uses np.histogram(..., bins=buckets+[inf])
    yielding len(buckets) elements).

    For consistency with the baseline computation (which used
    np.histogram with bins = buckets + [+inf], producing len(buckets)
    counts), we return len(buckets) - 1 deltas (intervals between
    consecutive edges).

    NOTE: We deliberately skip the +Inf overflow bucket — if any
    predictions land there (RUL > 500 cycles, unusual for C-MAPSS),
    they are dropped from the drift calculation. Production data should
    rarely if ever land in this region.
    """
    per_bucket = []
    prev = 0.0
    # iterate over consecutive edge pairs to get N-1 intervals
    # but baseline has N counts (because np.histogram with bins=buckets+[inf]
    # produces N counts). So we mirror that: return N counts where the
    # last bucket is [buckets[-2], buckets[-1]).
    n_buckets = len(buckets) - 1   # 13 edges -> 12 intervals
    for i in range(n_buckets):
        # Prometheus key for "le=buckets[i+1]" — count up to this edge
        upper_edge = buckets[i + 1]
        key = str(float(upper_edge))
        cum = cumulative.get(key, 0.0)
        per_bucket.append(max(cum - prev, 0))
        prev = cum
    return np.array(per_bucket)


# ----------------------------------------------------------------------
# Step 3: Compute PSI (Population Stability Index)
# ----------------------------------------------------------------------
def compute_psi(baseline_counts: np.ndarray, production_counts: np.ndarray) -> float:
    """
    PSI = sum((p_prod - p_base) * ln(p_prod / p_base)) over all buckets.

    Convention:
      PSI < 0.1     : no drift
      0.1 < PSI < 0.2 : moderate drift (monitor)
      PSI > 0.2     : significant drift (alert)
    """
    # Normalize to probabilities; add small epsilon to avoid log(0)
    eps = 1e-6
    p_base = baseline_counts / max(baseline_counts.sum(), 1)
    p_prod = production_counts / max(production_counts.sum(), 1)
    p_base = np.where(p_base == 0, eps, p_base)
    p_prod = np.where(p_prod == 0, eps, p_prod)

    psi_per_bucket = (p_prod - p_base) * np.log(p_prod / p_base)
    return float(np.sum(psi_per_bucket))


# ----------------------------------------------------------------------
# Step 4: KS-test using histogram-based reconstruction
# ----------------------------------------------------------------------
def reconstruct_samples_from_histogram(
    buckets: list, counts: np.ndarray, max_per_bucket: int = 500
) -> np.ndarray:
    """
    Reconstruct approximate samples from a histogram for KS-test.
    Each bucket [low, high) gets `count` samples at the midpoint.
    """
    samples = []
    bucket_edges = list(buckets) + [buckets[-1] + (buckets[-1] - buckets[-2])]
    for i, count in enumerate(counts):
        if count <= 0:
            continue
        low = bucket_edges[i]
        high = bucket_edges[i + 1] if i + 1 < len(bucket_edges) else low + 25
        midpoint = (low + high) / 2.0
        n = min(int(count), max_per_bucket)
        samples.extend([midpoint] * n)
    return np.array(samples) if samples else np.array([0.0])


def ks_from_histograms(
    baseline_counts: np.ndarray,
    production_counts: np.ndarray,
) -> tuple:
    """
    Two-sample Kolmogorov-Smirnov test computed directly from histogram
    bucket counts — no sample reconstruction.

    KS statistic = max |CDF_baseline(x) - CDF_prod(x)| evaluated at each
    bucket boundary. P-value derived from the two-sample KS distribution
    (scipy.stats.kstwo) using effective sample size en = sqrt(n1*n2/(n1+n2)).

    This replaces the previous implementation that reconstructed samples
    at bucket midpoints and called scipy.stats.ks_2samp on them. That
    approach was systematically biased toward p-value ~ 0 because the
    reconstructed samples were discrete (all replicas of a few midpoints),
    violating the continuity assumption of the two-sample KS test.

    See Engineering Challenge 16 in thesis documentation.

    Args:
        baseline_counts: Per-bucket counts from baseline histogram.
        production_counts: Per-bucket counts from production histogram.

    Returns:
        (ks_statistic, p_value) — both floats in [0, 1].
    """
    n1 = int(np.sum(baseline_counts))
    n2 = int(np.sum(production_counts))

    # Edge case: no data on either side — cannot compute, assume no drift
    if n1 == 0 or n2 == 0:
        return 0.0, 1.0

    # Empirical CDFs from histogram counts (normalized cumulative sums).
    # Both CDFs are evaluated at the same bucket boundaries, so we can
    # compare them point-wise.
    cdf_baseline = np.cumsum(baseline_counts) / n1
    cdf_prod = np.cumsum(production_counts) / n2

    # KS statistic: max absolute difference between the two empirical CDFs
    ks_stat = float(np.max(np.abs(cdf_baseline - cdf_prod)))

    # P-value: effective sample size, then survival function of the
    # two-sample KS distribution under the null hypothesis.
    # NOTE: scipy.stats.kstwo.sf() expects an integer-like second argument
    # (sample count). Float en produces NaN p-values, so we round to int.
    en = np.sqrt(n1 * n2 / (n1 + n2))
    en_int = int(round(en))
    if en_int < 1:
        # Sample size too small for meaningful KS distribution
        return ks_stat, 1.0
    p_value = float(stats.kstwo.sf(ks_stat, en_int))

    # Guard against numerical edge cases (very rare)
    if not np.isfinite(p_value):
        p_value = 1.0

    return ks_stat, p_value


def compute_ks_test(
    baseline_counts: np.ndarray,
    production_counts: np.ndarray,
    buckets: list,
) -> tuple:
    """
    Kolmogorov-Smirnov test between baseline and production distributions.
    Returns (statistic, p_value).

    Delegates to ks_from_histograms(), which compares empirical CDFs
    directly without sample reconstruction. The `buckets` argument is
    kept in the signature for backward compatibility but is unused by
    the new implementation (bucket alignment is implicit since both
    histograms share the same buckets).
    """
    return ks_from_histograms(baseline_counts, production_counts)


# ----------------------------------------------------------------------
# Step 5: Push metrics to Prometheus Pushgateway
# ----------------------------------------------------------------------
def push_metrics(metrics: dict) -> bool:
    """Push drift metrics to Prometheus Pushgateway."""
    payload = "\n".join(
        f"# HELP {name} {help_text}\n# TYPE {name} gauge\n{name} {value}"
        for name, (value, help_text) in metrics.items()
    ) + "\n"

    url = f"{PUSHGATEWAY_URL}/metrics/job/{JOB_NAME}"
    try:
        resp = requests.post(
            url,
            data=payload,
            headers={"Content-Type": "text/plain"},
            timeout=10,
        )
        resp.raise_for_status()
        log.info(f"✓ Metrics pushed to {url}")
        return True
    except Exception as e:
        log.error(f"Failed to push metrics: {e}")
        return False


# ----------------------------------------------------------------------
# Step 6: Fire Alertmanager webhook (only if drift detected)
# ----------------------------------------------------------------------
def fire_alert(psi: float, ks_pvalue: float, baseline_version: str) -> bool:
    """Send a drift detection alert to Alertmanager."""
    alert = [
        {
            "labels": {
                "alertname": "ModelDriftDetected",
                "severity": "warning",
                "service": "fastapi",
                "model_name": "cmapss-rul",
                "baseline_version": baseline_version,
            },
            "annotations": {
                "summary": f"Model drift detected (PSI={psi:.3f}, KS p-value={ks_pvalue:.4f})",
                "description": (
                    f"Production prediction distribution has drifted from "
                    f"the training baseline. PSI={psi:.3f} (threshold {PSI_THRESHOLD}), "
                    f"KS p-value={ks_pvalue:.4f} (threshold {KS_PVALUE_THRESHOLD}). "
                    f"Consider retraining the model."
                ),
            },
            "startsAt": datetime.now(timezone.utc).isoformat(),
        }
    ]

    url = f"{ALERTMANAGER_URL}/api/v2/alerts"
    try:
        resp = requests.post(url, json=alert, timeout=10)
        resp.raise_for_status()
        log.info(f"✓ Alert fired to Alertmanager")
        return True
    except Exception as e:
        log.error(f"Failed to fire alert: {e}")
        return False


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def main():
    log.info("=" * 60)
    log.info("Drift detection run starting")
    log.info("=" * 60)

    # Step 1: Load baseline
    baseline = load_baseline(BASELINE_PATH)
    buckets = baseline["histogram"]["buckets"]
    baseline_counts = np.array(baseline["histogram"]["counts"], dtype=float)

    # Step 2: Fetch production histogram
    production_cumulative = fetch_production_histogram(buckets)
    if not production_cumulative:
        log.warning("No production data — exiting without drift computation")
        push_metrics({
            "drift_check_no_data": (1, "1 if no production data available"),
        })
        return 0
    production_counts = cumulative_to_per_bucket(production_cumulative, buckets)

    log.info(f"Baseline counts: {baseline_counts.tolist()}")
    log.info(f"Production counts: {production_counts.tolist()}")

    # EC#18: Bucket alignment + empty-traffic guard.
    # Prometheus `increase()` over a 1h window may return one fewer
    # bucket than baseline when traffic is sparse. Two fixes:
    #   (a) Pad production_counts with zeros to match baseline length.
    #   (b) If production has zero total traffic, skip PSI/KS and emit
    #       a clean "no signal" state.
    if len(production_counts) < len(baseline_counts):
        n_missing = len(baseline_counts) - len(production_counts)
        log.warning(
            f"Production histogram has {n_missing} fewer buckets than baseline; "
            f"padding with zeros"
        )
        production_counts = np.concatenate(
            [production_counts, np.zeros(n_missing)]
        )
    elif len(production_counts) > len(baseline_counts):
        log.warning("Production histogram is longer than baseline; truncating")
        production_counts = production_counts[: len(baseline_counts)]

    if production_counts.sum() == 0:
        log.info("No production traffic in 1h window — skipping drift evaluation")
        psi = 0.0
        ks_stat = 0.0
        ks_pvalue = 1.0
        drift_detected = False
        log.info(f"PSI = {psi:.4f} (no signal)")
        log.info(f"KS statistic = {ks_stat:.4f}, p-value = {ks_pvalue:.4f} (no signal)")
        log.info(f"Drift detected: {drift_detected} (no traffic)")
        push_metrics({
            "drift_psi": (psi, "Population Stability Index"),
            "drift_ks_statistic": (ks_stat, "KS-test statistic"),
            "drift_ks_pvalue": (ks_pvalue, "KS-test p-value"),
            "drift_detected": (int(drift_detected), "1 if drift detected, 0 otherwise"),
            "drift_check_no_data": (0, "1 if no production data available"),
        })
        log.info("✓ Metrics pushed (zero-traffic state)")
        log.info("=" * 60)
        log.info(f"Drift check complete — PSI={psi:.4f}, drift={drift_detected}")
        log.info("=" * 60)
        return 0
    # Step 3: PSI
    psi = compute_psi(baseline_counts, production_counts)
    log.info(f"PSI = {psi:.4f} (threshold: {PSI_THRESHOLD})")

    # Step 4: KS-test
    ks_stat, ks_pvalue = compute_ks_test(
        baseline_counts, production_counts, buckets
    )
    log.info(
        f"KS statistic = {ks_stat:.4f}, p-value = {ks_pvalue:.4f} "
        f"(threshold p < {KS_PVALUE_THRESHOLD})"
    )

    # Step 5: Decision
    drift_detected = (psi > PSI_THRESHOLD) or (ks_pvalue < KS_PVALUE_THRESHOLD)
    log.info(f"Drift detected: {drift_detected}")

    # Compute medians for visibility
    baseline_median = baseline["prediction_stats"]["median"]
    prod_total = production_counts.sum()
    if prod_total > 0:
        cumulative = np.cumsum(production_counts)
        median_idx = np.searchsorted(cumulative, prod_total / 2)
        production_median = float(buckets[min(median_idx, len(buckets) - 1)])
    else:
        production_median = 0.0

    # Step 6: Push metrics
    push_metrics({
        "drift_psi": (psi, "Population Stability Index"),
        "drift_ks_statistic": (ks_stat, "KS-test statistic"),
        "drift_ks_pvalue": (ks_pvalue, "KS-test p-value"),
        "drift_detected": (int(drift_detected), "1 if drift detected, 0 otherwise"),
        "drift_baseline_median": (baseline_median, "Median of baseline prediction distribution"),
        "drift_production_median": (production_median, "Approx median of production prediction distribution"),
        "drift_check_no_data": (0, "1 if no production data available"),
    })

    # Step 7: Fire alert if needed
    if drift_detected:
        fire_alert(psi, ks_pvalue, baseline["model_version"])

    log.info("=" * 60)
    log.info(f"Drift check complete — PSI={psi:.4f}, drift={drift_detected}")
    log.info("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
