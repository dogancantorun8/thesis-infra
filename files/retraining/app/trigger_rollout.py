#!/usr/bin/env python3
"""
KFP Component 4: trigger_rollout_op
=====================================

Synchronizes downstream consumers of @production after a model promotion.
Mirrors the 3-step logic of Notebook 03 Cell 10:

    Step 1/2: Trigger baseline-refresh Job
              → updates evidently-baseline ConfigMap + host disk
              → ~7 sec (or ~1 sec no-op if already up-to-date)

    Step 2/2: FastAPI rolling restart
              → pod re-resolves @production alias → loads new model
              → ~20-30 sec total

Note: Notebook 03 Cell 10 had a third step (port-forward refresh) for the
notebook dev session. In the KFP pipeline that step is unnecessary — we are
running inside the cluster, no port-forwards involved. Therefore this
component executes only Steps 1 and 2.

Skip behavior:
    If --alias-assigned is not "production", this component is a no-op.
    Rationale: staging models should not trigger a cluster-wide rollout.

Usage:
    python trigger_rollout.py \
        --new-version 57 \
        --alias-assigned production \
        --output-file /tmp/rollout_output.json

Output (JSON):
    {
      "skipped":              false,
      "baseline_refresh_ok":  true,
      "fastapi_rollout_ok":   true,
      "new_version":          "57",
      "duration_sec":         32.7
    }
"""

import argparse
import json
import logging
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("trigger_rollout")

PROD_ALIAS = "production"
MONITORING_NS = "monitoring"
MLOPS_NS = "mlops"


def trigger_baseline_refresh() -> bool:
    """Step 1 — trigger baseline-refresh Job. Returns True on success."""
    job_name = f"baseline-refresh-{int(time.time())}"
    log.info("Step 1/2: Triggering baseline-refresh Job")
    log.info("  Job name: %s", job_name)

    result = subprocess.run(
        ["kubectl", "create", "job",
         "--from=cronjob/baseline-refresh",
         "-n", MONITORING_NS, job_name],
        capture_output=True, text=True, timeout=15,
    )

    if result.returncode != 0:
        log.warning("  Could not trigger baseline-refresh: %s", result.stderr.strip())
        log.warning("  This is non-fatal — Step 2 will still run.")
        return False

    log.info("  %s", result.stdout.strip())
    log.info("  Waiting for completion (max 90 sec)...")

    wait_result = subprocess.run(
        ["kubectl", "wait", "--for=condition=complete",
         "--timeout=90s", f"job/{job_name}", "-n", MONITORING_NS],
        capture_output=True, text=True, timeout=100,
    )

    if wait_result.returncode != 0:
        log.warning("  Did not complete in 90s — may still be running")
        return False

    # Try to extract summary line from logs
    logs = subprocess.run(
        ["kubectl", "logs", "-n", MONITORING_NS,
         "-l", f"job-name={job_name}", "--tail=10"],
        capture_output=True, text=True, timeout=10,
    )
    summary = None
    for line in logs.stdout.splitlines():
        if "Already up-to-date" in line or "Baseline refreshed" in line:
            summary = line.split("] ", 1)[-1] if "] " in line else line
            break
    log.info("  OK  %s", summary.strip() if summary else "Job completed")

    # Cleanup
    subprocess.run(
        ["kubectl", "delete", "job", job_name, "-n", MONITORING_NS,
         "--ignore-not-found", "--wait=false"],
        capture_output=True, timeout=5,
    )
    return True


def trigger_fastapi_rollout() -> bool:
    """Step 2 — rolling restart of FastAPI deployment. Returns True on success."""
    log.info("Step 2/2: FastAPI rolling restart")
    restart_start = datetime.now(timezone.utc)

    restart = subprocess.run(
        ["kubectl", "rollout", "restart", "deployment/fastapi", "-n", MLOPS_NS],
        capture_output=True, text=True, timeout=10,
    )
    if restart.returncode != 0:
        log.warning("  Rollout restart failed: %s", restart.stderr.strip())
        return False

    log.info("  %s", restart.stdout.strip())
    log.info("  Waiting for rollout to complete (max 180 sec)...")

    status = subprocess.run(
        ["kubectl", "rollout", "status",
         "deployment/fastapi", "-n", MLOPS_NS, "--timeout=180s"],
        capture_output=True, text=True, timeout=200,
    )
    if status.returncode != 0:
        log.warning("  Rollout did not complete: %s", status.stderr.strip())
        return False

    duration = (datetime.now(timezone.utc) - restart_start).total_seconds()
    log.info("  OK  %s", status.stdout.strip())
    log.info("  Rollout duration: %.1f sec", duration)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--new-version", required=True)
    parser.add_argument("--alias-assigned", required=True,
                        help="The alias assigned by register_model_op (production or staging)")
    parser.add_argument("--output-file", required=True)
    args = parser.parse_args()

    sync_start = datetime.now(timezone.utc)
    log.info("Post-promotion sync starting: %s", sync_start.isoformat())
    log.info("  new_version:    %s", args.new_version)
    log.info("  alias_assigned: %s", args.alias_assigned)

    # ──────────────────────────────────────────────────────
    # Skip if not promoted to production
    # ──────────────────────────────────────────────────────
    if args.alias_assigned != PROD_ALIAS:
        log.info("Alias is '%s' (not '%s') — skipping rollout", args.alias_assigned, PROD_ALIAS)
        output = {
            "skipped": True,
            "skip_reason": f"alias_assigned={args.alias_assigned}, not production",
            "baseline_refresh_ok": None,
            "fastapi_rollout_ok": None,
            "new_version": args.new_version,
            "duration_sec": 0.0,
        }
        Path(args.output_file).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output_file).write_text(json.dumps(output, indent=2))
        return 0

    # ──────────────────────────────────────────────────────
    # Execute sync steps
    # ──────────────────────────────────────────────────────
    baseline_ok = trigger_baseline_refresh()
    fastapi_ok = trigger_fastapi_rollout()

    duration = (datetime.now(timezone.utc) - sync_start).total_seconds()

    log.info("=" * 60)
    log.info("Post-promotion sync complete in %.1f sec", duration)
    log.info("  Step 1 (baseline-refresh):  %s", "OK" if baseline_ok else "WARN")
    log.info("  Step 2 (FastAPI rollout):   %s", "OK" if fastapi_ok else "WARN")
    log.info("=" * 60)

    if baseline_ok and fastapi_ok:
        log.info("Cluster fully synchronized:")
        log.info("  - Evidently baseline reflects @production model v%s", args.new_version)
        log.info("  - FastAPI serving @production v%s", args.new_version)
    else:
        log.warning("Some steps did not complete — system may be partially synced")

    # ──────────────────────────────────────────────────────
    # Output
    # ──────────────────────────────────────────────────────
    output = {
        "skipped": False,
        "baseline_refresh_ok": baseline_ok,
        "fastapi_rollout_ok": fastapi_ok,
        "new_version": args.new_version,
        "duration_sec": duration,
    }
    Path(args.output_file).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output_file).write_text(json.dumps(output, indent=2))
    log.info("Wrote component output: %s", args.output_file)

    # Non-zero exit if any step failed (KFP will mark pipeline as failed)
    return 0 if (baseline_ok and fastapi_ok) else 4


if __name__ == "__main__":
    sys.exit(main())
