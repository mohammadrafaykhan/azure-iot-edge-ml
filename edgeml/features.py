"""Window features: the model's input, computed the same way in training and on the device."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from edgeml.readings import Reading

WINDOW_SIZE = 10  # readings per window (10 s at the default 1 Hz)

FEATURE_NAMES = (
    "vibration_rms_mean",
    "vibration_rms_std",
    "crest_factor",
    "temperature_mean",
    "temperature_slope",  # °C per minute
    "current_mean",
    "rpm_mean",
    "current_per_krpm",
)


def window_features(window: Sequence[Reading]) -> np.ndarray:
    if len(window) < 2:
        raise ValueError("a window needs at least two readings")
    vibration = np.array([r.vibration_rms for r in window], dtype=np.float64)
    peak = np.array([r.vibration_peak for r in window], dtype=np.float64)
    temperature = np.array([r.temperature for r in window], dtype=np.float64)
    current = np.array([r.current for r in window], dtype=np.float64)
    rpm = np.array([r.rpm for r in window], dtype=np.float64)
    seconds = np.array([(r.timestamp - window[0].timestamp).total_seconds() for r in window])
    slope_per_second = np.polyfit(seconds, temperature, 1)[0] if np.ptp(seconds) > 0 else 0.0
    rpm_mean = rpm.mean()
    return np.array(
        [
            vibration.mean(),
            vibration.std(),
            peak.max() / max(vibration.mean(), 1e-6),
            temperature.mean(),
            slope_per_second * 60.0,
            current.mean(),
            rpm_mean,
            current.mean() / max(rpm_mean / 1000.0, 1e-6),
        ],
        dtype=np.float32,
    )


def windows(readings: Sequence[Reading], size: int = WINDOW_SIZE) -> np.ndarray:
    """Features of consecutive, non-overlapping windows; a trailing partial window is dropped."""
    rows = [window_features(readings[i : i + size]) for i in range(0, len(readings) - size + 1, size)]
    return np.array(rows, dtype=np.float32).reshape(-1, len(FEATURE_NAMES))
