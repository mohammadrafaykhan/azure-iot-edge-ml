"""Train, evaluate, and export the anomaly model. Runs in the first stage of the detector's image build.

    python -m ml.train --config ml/models/v1.json --out build/model

1. Simulate healthy operation for every press and cut it into feature windows.
2. Fit the configured detector on those windows (unsupervised: no fault data is used to train):
   ``isolationForest`` or ``oneClassSvm`` (standardized features, RBF kernel).
3. Pick the decision threshold on a separate healthy validation set for the target false-alarm rate.
4. Evaluate on a held-out healthy test set and on each fault, at full severity and as a slow ramp.
5. Export to ONNX, check it scores exactly like scikit-learn, and write the model card.

Everything is seeded, so the same config always produces the same model bytes and sha256.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from edgeml import model as model_store
from edgeml.debounce import RAISED, Debouncer
from edgeml.features import FEATURE_NAMES, WINDOW_SIZE, windows
from edgeml.physics import FAULT_MODES, Fault, simulate

PARITY_TOLERANCE = 1e-4
EVALUATION_HOURS = 3.0  # per press, for each of the validation and test sets
RAISE_AFTER = 3
ALGORITHMS = ("isolationForest", "oneClassSvm")


@dataclass(frozen=True)
class TrainConfig:
    version: str
    description: str
    algorithm: str
    training_hours: float
    assets: int
    params: dict[str, Any]
    false_alarm_rate: float
    seed: int

    @classmethod
    def load(cls, path: Path) -> TrainConfig:
        data = json.loads(path.read_text(encoding="utf-8"))
        config = cls(
            version=str(data["version"]),
            description=str(data.get("description", "")),
            algorithm=str(data["algorithm"]),
            training_hours=float(data["trainingHours"]),
            assets=int(data["assets"]),
            params=dict(data.get("params", {})),
            false_alarm_rate=float(data["falseAlarmRate"]),
            seed=int(data["seed"]),
        )
        if config.algorithm not in ALGORITHMS:
            raise ValueError(f"algorithm must be one of {', '.join(ALGORITHMS)}")
        if not 0 < config.false_alarm_rate < 0.5:
            raise ValueError("falseAlarmRate must be between 0 and 0.5")
        if config.training_hours <= 0 or config.assets < 1:
            raise ValueError("trainingHours and assets must be positive")
        return config


def build_estimator(config: TrainConfig, n_windows: int) -> Any:
    """An unfitted scikit-learn estimator whose ``decision_function`` is negative for anomalies."""
    params = config.params
    if config.algorithm == "isolationForest":
        from sklearn.ensemble import IsolationForest

        return IsolationForest(
            n_estimators=int(params.get("nEstimators", 100)),
            max_samples=min(int(params.get("maxSamples", 256)), n_windows),
            random_state=config.seed,
        )
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.svm import OneClassSVM

    gamma = params.get("gamma", "scale")
    return make_pipeline(
        StandardScaler(),
        OneClassSVM(nu=float(params.get("nu", 0.01)), gamma=gamma if gamma == "scale" else float(gamma)),
    )


def healthy_parts(hours: float, assets: int, seed: int) -> list[np.ndarray]:
    seconds = int(hours * 3600)
    return [windows(simulate(f"press-{i + 1:02d}", seconds, seed * 1000 + i)) for i in range(assets)]


def healthy(hours: float, assets: int, seed: int) -> np.ndarray:
    return np.vstack(healthy_parts(hours, assets, seed))


def faulty(mode: str, seconds: int, seed: int, ramp: float) -> np.ndarray:
    return windows(simulate("press-eval", seconds, seed, fault=Fault(mode, started_at=0.0, ramp_seconds=ramp)))


def false_raises_per_day(parts: list[np.ndarray], scores: list[np.ndarray], threshold: float) -> float:
    """Debounced alerts raised on healthy data, per press per day."""
    raises = 0
    for part_scores in scores:
        debouncer = Debouncer(raise_after=RAISE_AFTER)
        raises += sum(debouncer.update(bool(s < threshold)) == RAISED for s in part_scores)
    days = sum(len(p) for p in parts) * WINDOW_SIZE / 86400
    return round(raises / days, 2)


def seconds_to_detect(scores: np.ndarray, threshold: float) -> float | None:
    """Seconds from fault start to the end of the window that raises the alert (debounced)."""
    debouncer = Debouncer(raise_after=RAISE_AFTER)
    for index, score in enumerate(scores):
        if debouncer.update(bool(score < threshold)) == RAISED:
            return float((index + 1) * WINDOW_SIZE)
    return None


def to_onnx(model: Any, sample: np.ndarray) -> bytes:
    from skl2onnx import to_onnx as convert

    onx = convert(model, sample[:1], target_opset={"": 17, "ai.onnx.ml": 3})
    return onx.SerializeToString()


def onnx_scores(onnx_bytes: bytes, features: np.ndarray) -> np.ndarray:
    import onnxruntime as ort

    session = ort.InferenceSession(onnx_bytes, providers=["CPUExecutionProvider"])
    _, scores = session.run(None, {session.get_inputs()[0].name: features.astype(np.float32)})
    return np.asarray(scores, dtype=np.float64).ravel()


def train(config: TrainConfig) -> tuple[bytes, model_store.ModelCard]:
    import onnx
    import skl2onnx
    import sklearn

    seed = config.seed
    x_train = healthy(config.training_hours, config.assets, seed)
    x_validation = healthy(EVALUATION_HOURS, config.assets, seed + 1)
    test_parts = healthy_parts(EVALUATION_HOURS, config.assets, seed + 2)
    x_test = np.vstack(test_parts)

    estimator = build_estimator(config, len(x_train)).fit(x_train)
    threshold = float(np.quantile(estimator.decision_function(x_validation), config.false_alarm_rate))

    full = {mode: faulty(mode, 1200, seed + 10 + i, ramp=0.0) for i, mode in enumerate(FAULT_MODES)}
    ramps = {mode: faulty(mode, 900, seed + 20 + i, ramp=300.0) for i, mode in enumerate(FAULT_MODES)}

    onnx_bytes = to_onnx(estimator, x_train)
    everything = np.vstack([x_validation, x_test, *full.values(), *ramps.values()])
    drift = float(np.max(np.abs(onnx_scores(onnx_bytes, everything) - estimator.decision_function(everything))))
    if drift > PARITY_TOLERANCE:
        raise RuntimeError(f"ONNX scores differ from scikit-learn by {drift:.2e}")

    test_scores = estimator.decision_function(x_test)
    metrics = {
        "falseAlarmRate": {
            "target": config.false_alarm_rate,
            "test": round(float(np.mean(test_scores < threshold)), 4),
        },
        "falseAlertsPerPressDay": false_raises_per_day(
            test_parts, [estimator.decision_function(p) for p in test_parts], threshold
        ),
        "recallAtFullSeverity": {
            mode: round(float(np.mean(estimator.decision_function(x) < threshold)), 4) for mode, x in full.items()
        },
        "secondsToDetectFiveMinuteRamp": {
            mode: seconds_to_detect(estimator.decision_function(x), threshold) for mode, x in ramps.items()
        },
        "onnxMaxAbsDifference": float(f"{drift:.2e}"),
    }
    card = model_store.ModelCard(
        version=config.version,
        sha256=model_store.sha256(onnx_bytes),
        threshold=round(threshold, 6),
        features=FEATURE_NAMES,
        feature_mean=tuple(round(float(v), 5) for v in x_train.mean(axis=0)),
        feature_std=tuple(round(float(v), 5) for v in x_train.std(axis=0)),
        metrics=metrics,
        training={
            "description": config.description,
            "algorithm": config.algorithm,
            "params": config.params,
            "windows": len(x_train),
            "windowSeconds": WINDOW_SIZE,
            "seed": seed,
            "raiseAfterWindows": RAISE_AFTER,
            "libraries": {
                "scikit-learn": sklearn.__version__,
                "skl2onnx": skl2onnx.__version__,
                "onnx": onnx.__version__,
            },
        },
    )
    return onnx_bytes, card


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Train and export the press anomaly model.")
    parser.add_argument("--config", type=Path, required=True, help="model config JSON (ml/models/*.json)")
    parser.add_argument("--out", type=Path, required=True, help="directory for model.onnx and model.json")
    args = parser.parse_args(argv)
    onnx_bytes, card = train(TrainConfig.load(args.config))
    model_store.save(args.out, onnx_bytes, card)
    print(json.dumps({"version": card.version, "sha256": card.sha256, "threshold": card.threshold, **card.metrics}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
