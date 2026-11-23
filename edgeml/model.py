"""The model card that travels with every ONNX model: provenance, threshold, and feature statistics."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from edgeml.features import FEATURE_NAMES

MODEL_FILE = "model.onnx"
CARD_FILE = "model.json"


class ModelError(Exception):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class ModelCard:
    version: str
    sha256: str
    threshold: float  # anomalous when the decision score is below this
    features: tuple[str, ...]
    feature_mean: tuple[float, ...]
    feature_std: tuple[float, ...]
    metrics: dict[str, Any] = field(default_factory=dict)
    training: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "sha256": self.sha256,
            "threshold": self.threshold,
            "features": list(self.features),
            "featureMean": list(self.feature_mean),
            "featureStd": list(self.feature_std),
            "metrics": self.metrics,
            "training": self.training,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModelCard:
        try:
            card = cls(
                version=str(data["version"]),
                sha256=str(data["sha256"]),
                threshold=float(data["threshold"]),
                features=tuple(data["features"]),
                feature_mean=tuple(float(v) for v in data["featureMean"]),
                feature_std=tuple(float(v) for v in data["featureStd"]),
                metrics=dict(data.get("metrics", {})),
                training=dict(data.get("training", {})),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelError(f"invalid model card: {exc}") from exc
        if card.features != FEATURE_NAMES:
            raise ModelError(f"model expects features {card.features}, this build computes {FEATURE_NAMES}")
        if not len(card.feature_mean) == len(card.feature_std) == len(FEATURE_NAMES):
            raise ModelError("feature statistics do not match the feature list")
        return card

    def explain(self, features: np.ndarray, top: int = 3) -> list[dict[str, float | str]]:
        """The features furthest from what the model saw in training, as z-scores."""
        mean = np.asarray(self.feature_mean)
        std = np.maximum(np.asarray(self.feature_std), 1e-6)
        z = (np.asarray(features, dtype=np.float64) - mean) / std
        order = np.argsort(-np.abs(z))[:top]
        return [
            {"feature": self.features[i], "value": round(float(features[i]), 3), "z": round(float(z[i]), 1)}
            for i in order
        ]


def save(directory: Path, onnx_bytes: bytes, card: ModelCard) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / MODEL_FILE).write_bytes(onnx_bytes)
    (directory / CARD_FILE).write_text(json.dumps(card.to_dict(), indent=2) + "\n", encoding="utf-8")


def load(directory: Path) -> tuple[bytes, ModelCard]:
    """Read a model and its card, refusing a model whose bytes do not match the card."""
    try:
        onnx_bytes = (directory / MODEL_FILE).read_bytes()
        card = ModelCard.from_dict(json.loads((directory / CARD_FILE).read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise ModelError(f"cannot load model from {directory}: {exc}") from exc
    if sha256(onnx_bytes) != card.sha256:
        raise ModelError("model.onnx does not match the sha256 in model.json")
    return onnx_bytes, card
