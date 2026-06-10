"""
LSTM-based RUL regressor for C-MAPSS turbofan data.

Used by:
  - notebooks/03_baseline_lstm.ipynb   (training)
  - FastAPI inference service          (loaded from MLflow Registry)
  - Champion-challenger evaluation     (in retraining pipeline)

Design:
  - 2 stacked LSTM layers (hidden_size=64)
  - Dropout between LSTM layers (0.2)
  - Final FC layer maps last-timestep hidden state to a single RUL value
  - Input shape: (batch, window=30, features=16)
  - Output shape: (batch,)  -- predicted RUL
"""

import torch
import torch.nn as nn


class LSTMRegressor(nn.Module):
    """
    Stacked LSTM that maps a window of sensor readings to a scalar RUL.

    Architecture:
        input (B, T, F)
            → LSTM layer 1 (hidden=64)
            → Dropout(0.2)
            → LSTM layer 2 (hidden=64)
            → take last-timestep hidden state
            → Linear(64 → 1)
            → output (B,)
    """

    def __init__(
        self,
        input_size: int = 16,
        hidden_size: int = 64,
        num_layers: int = 2,
        dropout: float = 0.2,
    ):
        super().__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.num_layers = num_layers

        self.lstm = nn.LSTM(
            input_size=input_size,
            hidden_size=hidden_size,
            num_layers=num_layers,
            batch_first=True,         # input shape (B, T, F)
            dropout=dropout if num_layers > 1 else 0.0,
        )

        self.fc = nn.Linear(hidden_size, 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Forward pass.

        Args:
            x: shape (batch_size, window_size, input_size)

        Returns:
            shape (batch_size,)  -- predicted RUL per sample
        """
        # LSTM output: (B, T, hidden_size), (h_n, c_n)
        lstm_out, _ = self.lstm(x)

        # Take the last timestep output: (B, hidden_size)
        last_step = lstm_out[:, -1, :]

        # Map to single RUL: (B, 1) → squeeze to (B,)
        rul = self.fc(last_step).squeeze(-1)
        return rul


def count_parameters(model: nn.Module) -> int:
    """Return the total number of trainable parameters in a model."""
    return sum(p.numel() for p in model.parameters() if p.requires_grad)


if __name__ == "__main__":
    # Quick sanity check
    model = LSTMRegressor()
    dummy_input = torch.randn(8, 30, 16)  # batch=8, window=30, features=16
    output = model(dummy_input)
    print(f"Input shape:  {tuple(dummy_input.shape)}")
    print(f"Output shape: {tuple(output.shape)}")
    print(f"Total trainable parameters: {count_parameters(model):,}")
