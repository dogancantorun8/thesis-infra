#!/usr/bin/env python3
"""
KFP Component 2: train_lstm_op
===============================

Trains an LSTM regressor on the preprocessed C-MAPSS data and logs the run
to MLflow. Mirrors the training logic of Notebook 03 (Cells 4-8) in a CLI
form suitable for invocation from a KFP container component.

Usage:
    python train_lstm.py \
        --data-dir /tmp/data \
        --epochs 30 \
        --batch-size 64 \
        --learning-rate 1e-3 \
        --mlflow-tracking-uri http://mlflow.mlops.svc.cluster.local:5000 \
        --output-file /tmp/train_output.json

Output (written to --output-file as JSON):
    {
      "mlflow_run_id": "abcdef1234",
      "val_rmse":      13.54,
      "model_uri":     "runs:/abcdef1234/model"
    }

Reproducibility:
    Multi-seed training (EC#25 fix): trains N=3 models with seeds [42, 123, 456]
and selects the model with the lowest val_rmse. This guards against the
lucky-seed sensitivity observed when porting Notebook 03 to a script
execution context (same data + hyperparameters + seed produced 3x worse
RMSE due to different RNG consumption order between notebook cells and
monolithic script).
"""

import argparse
import copy
import json
import logging
import os
import sys
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import TensorDataset, DataLoader

import mlflow
import mlflow.pytorch

# src/ must be on PYTHONPATH (set in Dockerfile)
from src.model import LSTMRegressor, count_parameters

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("train_lstm")

SEED = 42        # legacy single-seed default (kept for backward compat)
SEEDS = [42, 123, 456]   # Multi-seed: train N models, pick best by val_rmse
MODEL_NAME = "cmapss-rul"
EXPERIMENT_NAME = "cmapss-baseline"


def train_with_seed(
    seed,
    args,
    X_train_t,
    y_train_t,
    X_val_t,
    y_val_t,
    input_size,
    device,
):
    """Train one model with a given seed. Returns (best_rmse, best_state, best_epoch, history)."""
    torch.manual_seed(seed)
    np.random.seed(seed)

    # Build model + loaders (seed-dependent: weight init + shuffle order)
    model = LSTMRegressor(
        input_size=input_size,
        hidden_size=args.hidden_size,
        num_layers=args.num_layers,
        dropout=args.dropout,
    ).to(device)

    train_ds = TensorDataset(X_train_t, y_train_t)
    val_ds = TensorDataset(X_val_t, y_val_t)
    train_loader = DataLoader(train_ds, batch_size=args.batch_size, shuffle=True)
    val_loader = DataLoader(val_ds, batch_size=args.batch_size, shuffle=False)

    criterion = nn.MSELoss()
    optimizer = torch.optim.Adam(model.parameters(), lr=args.learning_rate)

    # Scheduler
    if args.scheduler == "cosine":
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.epochs)
    else:
        scheduler = None

    best_val_rmse = float("inf")
    best_epoch = 0
    best_state = None
    no_improve = 0
    history = []

    for epoch in range(1, args.epochs + 1):
        train_loss = train_one_epoch(model, train_loader, optimizer, criterion, device)
        val_mse, val_rmse, _, _ = evaluate_epoch(model, val_loader, criterion, device)
        current_lr = optimizer.param_groups[0]["lr"]

        history.append({
            "epoch": epoch,
            "train_loss": train_loss,
            "val_rmse": val_rmse,
            "lr": current_lr,
        })

        if val_rmse < best_val_rmse - args.min_improvement:
            best_val_rmse = val_rmse
            best_epoch = epoch
            best_state = copy.deepcopy(model.state_dict())
            no_improve = 0
        else:
            no_improve += 1

        if scheduler is not None:
            scheduler.step()

        if epoch == 1 or epoch % 5 == 0 or epoch == args.epochs:
            log.info("[seed=%d] Epoch %3d/%d  train_loss=%.4f  val_rmse=%.4f  lr=%.6f",
                     seed, epoch, args.epochs, train_loss, val_rmse, current_lr)

        if args.early_stopping_patience > 0 and no_improve >= args.early_stopping_patience:
            log.info("[seed=%d] Early stopping at epoch %d (best=%.4f@e%d)",
                     seed, epoch, best_val_rmse, best_epoch)
            break

    return best_val_rmse, best_state, best_epoch, history


def train_one_epoch(model, loader, optimizer, criterion, device):
    """Single training epoch — mirrors Notebook 03 Cell 5."""
    model.train()
    total_loss = 0.0
    n_samples = 0
    for X_batch, y_batch in loader:
        X_batch = X_batch.to(device)
        y_batch = y_batch.to(device)
        optimizer.zero_grad()
        y_pred = model(X_batch).squeeze(-1)
        loss = criterion(y_pred, y_batch)
        loss.backward()
        optimizer.step()
        total_loss += loss.item() * X_batch.size(0)
        n_samples += X_batch.size(0)
    return total_loss / n_samples


def evaluate_epoch(model, loader, criterion, device):
    """Single validation pass — mirrors Notebook 03 Cell 5."""
    model.eval()
    total_loss = 0.0
    n_samples = 0
    preds, truths = [], []
    with torch.no_grad():
        for X_batch, y_batch in loader:
            X_batch = X_batch.to(device)
            y_batch = y_batch.to(device)
            y_pred = model(X_batch).squeeze(-1)
            loss = criterion(y_pred, y_batch)
            total_loss += loss.item() * X_batch.size(0)
            n_samples += X_batch.size(0)
            preds.append(y_pred.cpu().numpy())
            truths.append(y_batch.cpu().numpy())
    mse = total_loss / n_samples
    rmse = float(np.sqrt(mse))
    return mse, rmse, np.concatenate(preds), np.concatenate(truths)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", required=True, help="Directory containing X_train.npy, etc.")
    parser.add_argument("--epochs", type=int, default=60)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--learning-rate", type=float, default=1e-3)
    parser.add_argument("--hidden-size", type=int, default=64)
    parser.add_argument("--num-layers", type=int, default=2)
    parser.add_argument("--dropout", type=float, default=0.2)
    parser.add_argument(
        "--mlflow-tracking-uri",
        default=os.environ.get(
            "MLFLOW_TRACKING_URI",
            "http://mlflow.mlops.svc.cluster.local:5000",
        ),
    )
    # EC#25 fix — scheduler + early stopping for robust convergence
    parser.add_argument(
        "--scheduler",
        choices=["none", "cosine"],
        default="cosine",
        help="LR scheduler (cosine annealing helps escape bad local minima)",
    )
    parser.add_argument(
        "--early-stopping-patience",
        type=int,
        default=7,
        help="Stop if val_rmse does not improve for N epochs (0 = disabled)",
    )
    parser.add_argument(
        "--min-improvement",
        type=float,
        default=0.01,
        help="Minimum val_rmse drop to count as improvement (early stopping)",
    )
    parser.add_argument("--output-file", required=True, help="Path to write JSON output for KFP")
    args = parser.parse_args()

    # Reproducibility — multi-seed mode (EC#25 fix)
    log.info("Multi-seed training: %d seeds (%s)", len(SEEDS), SEEDS)

    log.info("PyTorch     : %s", torch.__version__)
    log.info("MLflow      : %s", mlflow.__version__)
    log.info("Device      : CPU")
    log.info("Random seed : %d", SEED)

    # ──────────────────────────────────────────────────────
    # Load data (from load_data_op output)
    # ──────────────────────────────────────────────────────
    data_dir = Path(args.data_dir)
    log.info("Loading tensors from %s", data_dir)
    X_train = np.load(data_dir / "X_train.npy")
    y_train = np.load(data_dir / "y_train.npy")
    X_val = np.load(data_dir / "X_val.npy")
    y_val = np.load(data_dir / "y_val.npy")
    log.info("  X_train: %s   y_train: %s", X_train.shape, y_train.shape)
    log.info("  X_val:   %s   y_val:   %s", X_val.shape, y_val.shape)

    X_train_t = torch.from_numpy(X_train).float()
    y_train_t = torch.from_numpy(y_train).float()
    X_val_t = torch.from_numpy(X_val).float()
    y_val_t = torch.from_numpy(y_val).float()

    train_ds = TensorDataset(X_train_t, y_train_t)
    val_ds = TensorDataset(X_val_t, y_val_t)
    train_loader = DataLoader(train_ds, batch_size=args.batch_size, shuffle=True)
    val_loader = DataLoader(val_ds, batch_size=args.batch_size, shuffle=False)

    # ──────────────────────────────────────────────────────
    # Build model
    # ──────────────────────────────────────────────────────
    input_size = X_train.shape[2]
    log.info("Building LSTMRegressor: input_size=%d, hidden=%d, layers=%d",
             input_size, args.hidden_size, args.num_layers)
    model = LSTMRegressor(
        input_size=input_size,
        hidden_size=args.hidden_size,
        num_layers=args.num_layers,
        dropout=args.dropout,
    )
    n_params = count_parameters(model)
    log.info("Model parameters: %d", n_params)

    criterion = nn.MSELoss()
    optimizer = torch.optim.Adam(model.parameters(), lr=args.learning_rate)
    device = torch.device("cpu")
    model.to(device)

    # ──────────────────────────────────────────────────────
    # MLflow tracking — log the entire run
    # ──────────────────────────────────────────────────────
    mlflow.set_tracking_uri(args.mlflow_tracking_uri)
    mlflow.set_experiment(EXPERIMENT_NAME)

    log.info("MLflow tracking URI: %s", args.mlflow_tracking_uri)
    log.info("MLflow experiment:   %s", EXPERIMENT_NAME)

    with mlflow.start_run() as run:
        mlflow.log_params({
            "epochs": args.epochs,
            "batch_size": args.batch_size,
            "learning_rate": args.learning_rate,
            "hidden_size": args.hidden_size,
            "num_layers": args.num_layers,
            "dropout": args.dropout,
            "input_size": input_size,
            "seeds": str(SEEDS),
            "num_seeds": len(SEEDS),
            "n_parameters": n_params,
            "n_train_samples": X_train.shape[0],
            "n_val_samples": X_val.shape[0],
            "trigger": "kfp_pipeline",
        })

        # EC#25 fix v2: Multi-seed training — pick best of N
        mlflow.log_params({
            "scheduler": args.scheduler,
            "early_stopping_patience": args.early_stopping_patience,
        })

        seed_results = []
        for seed in SEEDS:
            log.info("=" * 60)
            log.info("Training with seed=%d", seed)
            log.info("=" * 60)

            best_rmse, best_state, best_epoch, hist = train_with_seed(
                seed, args, X_train_t, y_train_t, X_val_t, y_val_t, input_size, device
            )

            log.info("[seed=%d] Final best_val_rmse=%.4f at epoch %d",
                     seed, best_rmse, best_epoch)

            # Log per-seed metrics (visible in MLflow UI)
            mlflow.log_metric(f"val_rmse_seed_{seed}", best_rmse)
            mlflow.log_metric(f"best_epoch_seed_{seed}", best_epoch)

            seed_results.append({
                "seed": seed,
                "best_rmse": best_rmse,
                "best_state": best_state,
                "best_epoch": best_epoch,
            })

        # Pick the best seed by val_rmse
        winner = min(seed_results, key=lambda r: r["best_rmse"])
        log.info("=" * 60)
        log.info("WINNER: seed=%d  val_rmse=%.4f  best_epoch=%d",
                 winner["seed"], winner["best_rmse"], winner["best_epoch"])
        log.info("=" * 60)

        # Log winner info
        mlflow.log_param("best_seed", winner["seed"])
        mlflow.log_metric("best_val_rmse", winner["best_rmse"])
        mlflow.log_metric("best_epoch", winner["best_epoch"])

        # Rebuild the winning model and restore its best state for logging
        torch.manual_seed(winner["seed"])
        np.random.seed(winner["seed"])
        model = LSTMRegressor(
            input_size=input_size,
            hidden_size=args.hidden_size,
            num_layers=args.num_layers,
            dropout=args.dropout,
        ).to(device)
        model.load_state_dict(winner["best_state"])
        log.info("Restored winning model (seed=%d, val_rmse=%.4f)",
                 winner["seed"], winner["best_rmse"])

        # Final evaluation
        final_mse, final_rmse, preds, truths = evaluate_epoch(
            model, val_loader, criterion, device
        )

        log.info("Final val_rmse: %.4f  (best across all seeds: %.4f)",
                 final_rmse, winner["best_rmse"])

        mlflow.log_metric("final_val_rmse", final_rmse)
        mlflow.log_metric("final_val_mse", final_mse)

        # Log model with explicit input example (required for registry signature)
        input_example = X_train_t[:1].numpy()
        mlflow.pytorch.log_model(
            pytorch_model=model,
            artifact_path="model",
            registered_model_name=MODEL_NAME,
            input_example=input_example,
        )

        run_id = run.info.run_id
        model_uri = f"runs:/{run_id}/model"

        log.info("MLflow run_id: %s", run_id)
        log.info("Model URI:     %s", model_uri)

    # ──────────────────────────────────────────────────────
    # Write KFP component output
    # ──────────────────────────────────────────────────────
    output = {
        "mlflow_run_id": run_id,
        "val_rmse": float(final_rmse),
        "best_val_rmse": float(winner["best_rmse"]),
        "best_seed": int(winner["seed"]),
        "model_uri": model_uri,
    }
    output_path = Path(args.output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2))
    log.info("Wrote component output: %s", output_path)
    log.info("Output payload: %s", output)

    return 0


if __name__ == "__main__":
    sys.exit(main())
