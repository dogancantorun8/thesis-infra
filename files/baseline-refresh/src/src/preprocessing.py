"""
C-MAPSS preprocessing pipeline.

Used by:
  - notebooks/02_preprocessing.ipynb  (interactive exploration)
  - notebooks/03_baseline_lstm.ipynb  (training input)
  - FastAPI inference service         (incoming feature normalization)
  - Evidently drift CronJob           (production-time preprocessing)

Design decisions (justified in notebooks/01_eda.ipynb):
  - Drop 8 constant features (std < 10^-3 in FD001)
  - Cap RUL at 125 cycles (Heimes 2008, Zheng et al. 2017)
  - Min-Max normalization [0, 1]
  - Engine-based train/val split (no temporal leakage)
  - Sequence window of 30 cycles for LSTM input
"""

from pathlib import Path
from typing import List, Tuple

import numpy as np
import pandas as pd
from sklearn.preprocessing import MinMaxScaler


# ----------------------------------------------------------------------
# Constants from EDA findings (notebook 01)
# ----------------------------------------------------------------------

C_MAPSS_COLUMNS = [
    'unit_number', 'time_in_cycles',
    'op_setting_1', 'op_setting_2', 'op_setting_3',
] + [f'sensor_{i:02d}' for i in range(1, 22)]

# Features dropped because they are constant in FD001 (EDA notebook, cell 9).
CONSTANT_FEATURES = [
    'op_setting_2', 'op_setting_3',
    'sensor_01', 'sensor_05', 'sensor_10',
    'sensor_16', 'sensor_18', 'sensor_19',
]

# RUL is capped at this value during training (EDA notebook, cell 12).
RUL_CAP = 125

# LSTM input sequence length.
DEFAULT_WINDOW = 30


# ----------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------

def load_cmapss(file_path: Path) -> pd.DataFrame:
    """Load a raw C-MAPSS file into a DataFrame with the standard schema."""
    df = pd.read_csv(
        file_path,
        sep=r'\s+',
        header=None,
        names=C_MAPSS_COLUMNS,
    )
    return df


# ----------------------------------------------------------------------
# Feature engineering
# ----------------------------------------------------------------------

def drop_constant_features(df: pd.DataFrame) -> pd.DataFrame:
    """Drop the features identified as constant during EDA."""
    return df.drop(columns=CONSTANT_FEATURES, errors='ignore')


def compute_rul(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add a 'RUL' column derived from time_in_cycles per engine.
    RUL = max_cycle_for_engine - current_cycle
    """
    max_cycle = df.groupby('unit_number')['time_in_cycles'].transform('max')
    df = df.copy()
    df['RUL'] = max_cycle - df['time_in_cycles']
    return df


def piecewise_rul(df: pd.DataFrame, cap: int = RUL_CAP) -> pd.DataFrame:
    """Apply piecewise-linear RUL: cap large values at `cap`."""
    df = df.copy()
    df['RUL'] = df['RUL'].clip(upper=cap)
    return df


# ----------------------------------------------------------------------
# Normalization
# ----------------------------------------------------------------------

def get_feature_columns(df: pd.DataFrame) -> List[str]:
    """Return the columns that go into the model (sensors + op_settings)."""
    excluded = {'unit_number', 'time_in_cycles', 'RUL'}
    return [c for c in df.columns if c not in excluded]


def fit_scaler(df: pd.DataFrame) -> MinMaxScaler:
    """Fit a Min-Max scaler on training data only."""
    feature_cols = get_feature_columns(df)
    scaler = MinMaxScaler(feature_range=(0, 1))
    scaler.fit(df[feature_cols].values)
    return scaler


def apply_scaler(df: pd.DataFrame, scaler: MinMaxScaler) -> pd.DataFrame:
    """Apply a fitted scaler — used for both training and validation."""
    feature_cols = get_feature_columns(df)
    df = df.copy()
    df[feature_cols] = scaler.transform(df[feature_cols].values)
    return df


# ----------------------------------------------------------------------
# Sequence creation (LSTM input shape)
# ----------------------------------------------------------------------

def create_sequences(
    df: pd.DataFrame,
    window: int = DEFAULT_WINDOW,
    feature_cols: List[str] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Convert a DataFrame into LSTM-ready sequences.

    Returns:
        X: shape (n_sequences, window, n_features)
        y: shape (n_sequences,) — RUL at the end of each sequence

    Engines with fewer than `window` cycles are skipped.
    """
    if feature_cols is None:
        feature_cols = get_feature_columns(df)

    X_list, y_list = [], []

    for engine_id, engine_df in df.groupby('unit_number'):
        engine_df = engine_df.sort_values('time_in_cycles').reset_index(drop=True)
        n = len(engine_df)

        if n < window:
            continue

        features = engine_df[feature_cols].values   # shape (n, n_features)
        targets = engine_df['RUL'].values           # shape (n,)

        # Sliding window with stride=1
        for i in range(n - window + 1):
            X_list.append(features[i:i + window])
            y_list.append(targets[i + window - 1])  # RUL at end of window

    X = np.stack(X_list, axis=0).astype(np.float32)
    y = np.array(y_list, dtype=np.float32)
    return X, y


# ----------------------------------------------------------------------
# Train/val split (engine-based, NO temporal leakage)
# ----------------------------------------------------------------------

def engine_based_split(
    df: pd.DataFrame,
    val_fraction: float = 0.2,
    random_seed: int = 42,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Split engines (not individual cycles) into train/val.
    Ensures the same engine cannot appear in both splits.
    """
    engine_ids = df['unit_number'].unique()
    rng = np.random.default_rng(random_seed)
    rng.shuffle(engine_ids)

    n_val = int(len(engine_ids) * val_fraction)
    val_ids = set(engine_ids[:n_val])
    train_ids = set(engine_ids[n_val:])

    df_train = df[df['unit_number'].isin(train_ids)].copy()
    df_val = df[df['unit_number'].isin(val_ids)].copy()

    return df_train, df_val


# ----------------------------------------------------------------------
# End-to-end pipeline (convenience function)
# ----------------------------------------------------------------------

def build_training_set(
    raw_file: Path,
    window: int = DEFAULT_WINDOW,
    val_fraction: float = 0.2,
    random_seed: int = 42,
) -> dict:
    """
    Run the full preprocessing pipeline on a raw C-MAPSS file.

    Returns a dict with: X_train, y_train, X_val, y_val, scaler, feature_cols.
    """
    # 1. Load
    df = load_cmapss(raw_file)

    # 2. Drop constants, compute RUL, cap RUL
    df = drop_constant_features(df)
    df = compute_rul(df)
    df = piecewise_rul(df, cap=RUL_CAP)

    # 3. Engine-based train/val split
    df_train_raw, df_val_raw = engine_based_split(df, val_fraction, random_seed)

    # 4. Fit scaler on train ONLY, apply to both
    scaler = fit_scaler(df_train_raw)
    df_train = apply_scaler(df_train_raw, scaler)
    df_val = apply_scaler(df_val_raw, scaler)

    # 5. Create sequences for LSTM
    feature_cols = get_feature_columns(df_train)
    X_train, y_train = create_sequences(df_train, window=window, feature_cols=feature_cols)
    X_val, y_val = create_sequences(df_val, window=window, feature_cols=feature_cols)

    return {
        'X_train': X_train,
        'y_train': y_train,
        'X_val': X_val,
        'y_val': y_val,
        'scaler': scaler,
        'feature_cols': feature_cols,
        'window': window,
    }
