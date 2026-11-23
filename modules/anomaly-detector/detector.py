"""Edge inference: windows per press, model scores, debounced alerts, and periodic summaries.

Raw readings never leave the device. Upstream traffic is one alert per state change and one
compact summary per interval, so the cloud sees what matters at a small fraction of the volume.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Protocol

import numpy as np

from edgeml.debounce import Debouncer
from edgeml.features import WINDOW_SIZE, window_features
from edgeml.model import ModelCard
from edgeml.readings import Reading, iso

ALERT_SCHEMA = "edge-ml/alert/1"
SUMMARY_SCHEMA = "edge-ml/summary/1"


class Scorer(Protocol):
    def score(self, features: np.ndarray) -> float: ...


class OnnxScorer:
    """Scores one feature vector with ONNX Runtime (decision score: negative means anomalous)."""

    def __init__(self, onnx_bytes: bytes) -> None:
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        self._session = ort.InferenceSession(onnx_bytes, options, providers=["CPUExecutionProvider"])
        self._input = self._session.get_inputs()[0].name

    def score(self, features: np.ndarray) -> float:
        _, scores = self._session.run(None, {self._input: features.reshape(1, -1).astype(np.float32)})
        return float(np.asarray(scores).ravel()[0])


@dataclass(frozen=True)
class DetectorConfig:
    threshold: float | None = None  # None: the model card's threshold
    raise_after: int = 3
    clear_after: int = 6
    summary_seconds: int = 60

    def apply(self, desired: dict[str, Any]) -> tuple[DetectorConfig, list[str]]:
        """Apply a desired-properties patch; invalid values are skipped and reported."""
        config, errors = self, []
        if "threshold" in desired:
            value = desired["threshold"]
            if value is None or (isinstance(value, int | float) and not isinstance(value, bool)):
                config = replace(config, threshold=None if value is None else float(value))
            else:
                errors.append("threshold must be a number or null")
        for key, attr, low, high in (
            ("raiseAfterWindows", "raise_after", 1, 60),
            ("clearAfterWindows", "clear_after", 1, 360),
            ("summarySeconds", "summary_seconds", 10, 3600),
        ):
            if key in desired:
                value = desired[key]
                if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
                    config = replace(config, **{attr: value})
                else:
                    errors.append(f"{key} must be an integer from {low} to {high}")
        return config, errors

    def to_reported(self, card: ModelCard) -> dict[str, Any]:
        return {
            "threshold": self.threshold if self.threshold is not None else card.threshold,
            "thresholdSource": "desired" if self.threshold is not None else "model",
            "raiseAfterWindows": self.raise_after,
            "clearAfterWindows": self.clear_after,
            "summarySeconds": self.summary_seconds,
        }


@dataclass
class AssetState:
    debouncer: Debouncer
    buffer: list[Reading] = field(default_factory=list)
    last_sequence: int = -1
    last_time: datetime | None = None
    windows: int = 0
    anomalous: int = 0
    score_sum: float = 0.0
    min_score: float | None = None
    since: datetime | None = None  # when the current alert was raised


class Detector:
    def __init__(
        self,
        card: ModelCard,
        scorer: Scorer,
        config: DetectorConfig | None = None,
        perf_counter: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.card = card
        self.config = config or DetectorConfig()
        self._scorer = scorer
        self._perf = perf_counter
        self._assets: dict[str, AssetState] = {}
        self._latency_ms: deque[float] = deque(maxlen=2000)
        self.readings_in = 0
        self.duplicates = 0
        self.windows_total = 0
        self.alerts_out = 0
        self.summaries_out = 0
        self._interval_readings = 0

    @property
    def threshold(self) -> float:
        return self.config.threshold if self.config.threshold is not None else self.card.threshold

    def reconfigure(self, config: DetectorConfig) -> None:
        self.config = config
        for state in self._assets.values():
            state.debouncer.raise_after = config.raise_after
            state.debouncer.clear_after = config.clear_after

    def _state(self, asset_id: str) -> AssetState:
        state = self._assets.get(asset_id)
        if state is None:
            state = AssetState(Debouncer(self.config.raise_after, self.config.clear_after))
            self._assets[asset_id] = state
        return state

    def add(self, reading: Reading) -> list[dict[str, Any]]:
        """Consume one reading; return the alerts it triggers (usually none)."""
        state = self._state(reading.asset_id)
        if reading.sequence <= state.last_sequence:
            if state.last_time is not None and reading.timestamp > state.last_time:
                state.buffer.clear()  # the sensor restarted and its sequence began again
            else:
                self.duplicates += 1  # at-least-once delivery between modules; already counted
                return []
        state.last_sequence = reading.sequence
        state.last_time = reading.timestamp
        self.readings_in += 1
        self._interval_readings += 1
        state.buffer.append(reading)
        if len(state.buffer) < WINDOW_SIZE:
            return []
        window, state.buffer = state.buffer, []
        return self._evaluate(state, window)

    def _evaluate(self, state: AssetState, window: list[Reading]) -> list[dict[str, Any]]:
        features = window_features(window)
        started = self._perf()
        score = self._scorer.score(features)
        self._latency_ms.append((self._perf() - started) * 1000)
        anomalous = score < self.threshold

        self.windows_total += 1
        state.windows += 1
        state.anomalous += int(anomalous)
        state.score_sum += score
        state.min_score = score if state.min_score is None else min(state.min_score, score)

        transition = state.debouncer.update(anomalous)
        if transition is None:
            return []
        end = window[-1].timestamp
        alert: dict[str, Any] = {
            "schema": ALERT_SCHEMA,
            "assetId": window[-1].asset_id,
            "state": transition,
            "at": iso(end),
            "score": round(score, 4),
            "threshold": round(self.threshold, 4),
            "modelVersion": self.card.version,
        }
        if transition == "raised":
            state.since = end
            alert["explanation"] = self.card.explain(features)
        else:
            alert["since"] = iso(state.since) if state.since else None
            state.since = None
        self.alerts_out += 1
        return [alert]

    def latency(self) -> dict[str, float]:
        if not self._latency_ms:
            return {"p50": 0.0, "p95": 0.0}
        values = np.asarray(self._latency_ms)
        return {"p50": round(float(np.percentile(values, 50)), 3), "p95": round(float(np.percentile(values, 95)), 3)}

    def summary(self, start: datetime, end: datetime) -> dict[str, Any]:
        """Per-press figures for the interval since the previous summary; counters then reset."""
        assets = {}
        for asset_id, state in sorted(self._assets.items()):
            assets[asset_id] = {
                "windows": state.windows,
                "anomalous": state.anomalous,
                "meanScore": round(state.score_sum / state.windows, 3) if state.windows else None,
                "minScore": round(state.min_score, 3) if state.min_score is not None else None,
                "alert": state.debouncer.active,
            }
            state.windows = state.anomalous = 0
            state.score_sum, state.min_score = 0.0, None
        readings, self._interval_readings = self._interval_readings, 0
        self.summaries_out += 1
        return {
            "schema": SUMMARY_SCHEMA,
            "from": iso(start),
            "to": iso(end),
            "modelVersion": self.card.version,
            "readings": readings,
            "latencyMs": self.latency(),
            "assets": assets,
        }

    def statistics(self) -> dict[str, Any]:
        upstream = self.alerts_out + self.summaries_out
        return {
            "readingsIn": self.readings_in,
            "duplicatesIgnored": self.duplicates,
            "windows": self.windows_total,
            "alertsOut": self.alerts_out,
            "summariesOut": self.summaries_out,
            "upstreamReduction": round(self.readings_in / upstream, 1) if upstream else None,
            "latencyMs": self.latency(),
            "activeAlerts": sorted(a for a, s in self._assets.items() if s.debouncer.active),
        }
