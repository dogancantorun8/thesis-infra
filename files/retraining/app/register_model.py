#!/usr/bin/env python3
"""
KFP Component 3: register_model_op
====================================

Promotes the just-trained model version to the `@production` alias if (and
only if) it improves on the current champion by at least `--threshold-factor`.

Mirrors the logic of Notebook 03 Cell 9 (alias swap) and Engineering
Challenge #9 (champion-challenger gating).

Decision rule:
    promote if new_val_rmse < champion_val_rmse * threshold_factor
    (default threshold_factor = 0.95 — require 5% improvement)

If no champion exists (first promotion), unconditional promote.

Usage:
    python register_model.py \
        --mlflow-run-id <run_id from train_lstm.py output> \
        --val-rmse 13.54 \
        --threshold-factor 0.95 \
        --mlflow-tracking-uri http://mlflow.mlops.svc.cluster.local:5000 \
        --output-file /tmp/register_output.json

Output (JSON):
    {
      "new_version":     "57",
      "alias_assigned":  "production",   // or "staging" if not promoted
      "champion_version":"56",            // previous @production
      "champion_rmse":   13.91,
      "challenger_rmse": 13.54,
      "promoted":        true
    }
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import mlflow
from mlflow.tracking import MlflowClient

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("register_model")

MODEL_NAME = "cmapss-rul"
PROD_ALIAS = "production"
STAGING_ALIAS = "staging"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--mlflow-run-id", required=True)
    parser.add_argument("--val-rmse", type=float, required=True,
                        help="Validation RMSE of the challenger (just-trained model)")
    parser.add_argument("--threshold-factor", type=float, default=0.95,
                        help="Promote only if val_rmse < champion_rmse * threshold (default: 0.95)")
    parser.add_argument(
        "--mlflow-tracking-uri",
        default=os.environ.get(
            "MLFLOW_TRACKING_URI",
            "http://mlflow.mlops.svc.cluster.local:5000",
        ),
    )
    parser.add_argument("--output-file", required=True)
    parser.add_argument("--force-promote", action="store_true",
                        help="Skip champion-challenger gate (for testing)")
    args = parser.parse_args()

    mlflow.set_tracking_uri(args.mlflow_tracking_uri)
    client = MlflowClient()

    # ──────────────────────────────────────────────────────
    # 1. Find the most-recently-registered version
    # ──────────────────────────────────────────────────────
    log.info("Searching for newly-registered version of '%s'...", MODEL_NAME)
    all_versions = client.search_model_versions(f"name='{MODEL_NAME}'")
    if not all_versions:
        log.error("No registered versions of '%s' found", MODEL_NAME)
        return 2

    matching = [v for v in all_versions if v.run_id == args.mlflow_run_id]
    if not matching:
        log.error("No version found for run_id=%s — train_lstm_op may not have completed registration",
                  args.mlflow_run_id)
        return 3

    new_version = matching[0]
    log.info("New version found:")
    log.info("  Number:   %s", new_version.version)
    log.info("  Run id:   %s", new_version.run_id)
    log.info("  Source:   %s", new_version.source)

    # ──────────────────────────────────────────────────────
    # 2. Identify current champion (if any)
    # ──────────────────────────────────────────────────────
    champion_version = None
    champion_rmse = None
    try:
        champion = client.get_model_version_by_alias(MODEL_NAME, PROD_ALIAS)
        champion_version = champion.version
        # Retrieve champion's val_rmse from its run
        champion_run = client.get_run(champion.run_id)
        champion_rmse = champion_run.data.metrics.get("final_val_rmse")
        if champion_rmse is None:
            # Fall back to best_val_rmse if final not logged
            champion_rmse = champion_run.data.metrics.get("best_val_rmse")
        log.info("Current champion:")
        log.info("  Version:  %s", champion_version)
        log.info("  val_rmse: %s", champion_rmse)
    except Exception:
        log.info("No current @production alias — this is the first promotion")

    # ──────────────────────────────────────────────────────
    # 3. Champion-challenger decision (EC#9 logic)
    # ──────────────────────────────────────────────────────
    challenger_rmse = args.val_rmse
    promoted = False
    decision_reason = ""

    if args.force_promote:
        promoted = True
        decision_reason = "force-promote flag set"
    elif champion_version is None:
        promoted = True
        decision_reason = "first promotion (no previous champion)"
    elif champion_rmse is None:
        # Champion exists but RMSE metric missing — be conservative, skip promote
        promoted = False
        decision_reason = "champion exists but RMSE metric missing — refusing to promote"
    else:
        gate = champion_rmse * args.threshold_factor
        if challenger_rmse < gate:
            promoted = True
            decision_reason = (
                f"challenger {challenger_rmse:.4f} < champion {champion_rmse:.4f} "
                f"× {args.threshold_factor} = {gate:.4f}"
            )
        else:
            promoted = False
            decision_reason = (
                f"challenger {challenger_rmse:.4f} >= champion {champion_rmse:.4f} "
                f"× {args.threshold_factor} = {gate:.4f}"
            )

    log.info("Decision: %s", "PROMOTE" if promoted else "REJECT")
    log.info("  Reason: %s", decision_reason)

    # ──────────────────────────────────────────────────────
    # 4. Assign alias
    # ──────────────────────────────────────────────────────
    alias_assigned = PROD_ALIAS if promoted else STAGING_ALIAS
    log.info("Setting alias '%s' on version %s", alias_assigned, new_version.version)

    client.set_registered_model_alias(
        name=MODEL_NAME,
        alias=alias_assigned,
        version=new_version.version,
    )
    log.info("Alias set successfully")

    # ──────────────────────────────────────────────────────
    # 5. Final registry audit
    # ──────────────────────────────────────────────────────
    log.info("Full registry state for '%s':", MODEL_NAME)
    all_versions = client.search_model_versions(f"name='{MODEL_NAME}'")
    all_versions = sorted(all_versions, key=lambda v: int(v.version))
    for v in all_versions[-5:]:  # show last 5 only
        aliases = list(v.aliases) if v.aliases else []
        tag = f"  [aliases: {', '.join(aliases)}]" if aliases else ""
        log.info("  Version %s%s", v.version, tag)

    # ──────────────────────────────────────────────────────
    # 6. Write KFP component output
    # ──────────────────────────────────────────────────────
    output = {
        "new_version": new_version.version,
        "alias_assigned": alias_assigned,
        "champion_version": champion_version,
        "champion_rmse": float(champion_rmse) if champion_rmse is not None else None,
        "challenger_rmse": float(challenger_rmse),
        "threshold_factor": args.threshold_factor,
        "promoted": promoted,
        "decision_reason": decision_reason,
    }
    output_path = Path(args.output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2))
    log.info("Wrote component output: %s", output_path)
    log.info("Output payload: %s", output)

    return 0


if __name__ == "__main__":
    sys.exit(main())
