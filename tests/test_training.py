import json
from pathlib import Path

import numpy as np
import pytest

import ml.train as train_module
from edgeml import model as model_store
from edgeml.debounce import CLEARED, RAISED, Debouncer
from edgeml.features import FEATURE_NAMES
from ml.train import TrainConfig, false_raises_per_day, onnx_scores, seconds_to_detect

MODELS = Path(__file__).parent.parent / "ml" / "models"


@pytest.fixture(autouse=True)
def short_evaluation(monkeypatch):
    monkeypatch.setattr(train_module, "EVALUATION_HOURS", 0.5)


def _config(tmp_path: Path, **changes) -> TrainConfig:
    data = {**json.loads((MODELS / "v2.json").read_text()), "trainingHours": 0.5, **changes}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    return TrainConfig.load(path)


@pytest.fixture(scope="module")
def trained():
    """Both shipped model configs, trained once for the module (evaluation sets shortened)."""
    patch = pytest.MonkeyPatch()
    patch.setattr(train_module, "EVALUATION_HOURS", 0.5)
    try:
        return {v: train_module.train(TrainConfig.load(MODELS / f"{v}.json")) for v in ("v1", "v2")}
    finally:
        patch.undo()


def test_shipped_configs_load():
    v1, v2 = TrainConfig.load(MODELS / "v1.json"), TrainConfig.load(MODELS / "v2.json")
    assert (v1.version, v1.algorithm) == ("1", "isolationForest")
    assert (v2.version, v2.algorithm) == ("2", "oneClassSvm")


@pytest.mark.parametrize(
    "changes",
    [{"algorithm": "kmeans"}, {"falseAlarmRate": 0}, {"falseAlarmRate": 0.6}, {"trainingHours": 0}, {"assets": 0}],
)
def test_invalid_configs_are_rejected(tmp_path, changes):
    with pytest.raises(ValueError):
        _config(tmp_path, **changes)


def test_v2_catches_every_fault_without_false_alerts(trained):
    _, card = trained["v2"]
    recall = card.metrics["recallAtFullSeverity"]
    assert all(recall[mode] >= 0.95 for mode in ("bearing", "overheat", "jam")), recall
    assert all(card.metrics["secondsToDetectFiveMinuteRamp"].values())
    assert card.metrics["falseAlertsPerPressDay"] < 1


def test_v1_isolation_forest_misses_overheating(trained):
    """The blind spot v2 fixes: a single signal beyond the training range scores like its edge."""
    _, card = trained["v1"]
    assert card.metrics["recallAtFullSeverity"]["overheat"] < 0.5
    assert card.metrics["recallAtFullSeverity"]["bearing"] >= 0.95
    assert card.metrics["secondsToDetectFiveMinuteRamp"]["overheat"] is None


def test_onnx_model_scores_like_scikit_learn_and_matches_its_card(trained):
    for onnx_bytes, card in trained.values():
        assert card.metrics["onnxMaxAbsDifference"] <= train_module.PARITY_TOLERANCE
        assert card.sha256 == model_store.sha256(onnx_bytes)
        assert card.features == FEATURE_NAMES
        assert len(card.feature_mean) == len(FEATURE_NAMES)


def test_training_is_reproducible(tmp_path):
    config = _config(tmp_path)
    first, _ = train_module.train(config)
    second, _ = train_module.train(config)
    assert first == second


def test_model_round_trips_and_rejects_tampering(tmp_path, trained):
    onnx_bytes, card = trained["v2"]
    model_store.save(tmp_path, onnx_bytes, card)
    loaded_bytes, loaded = model_store.load(tmp_path)
    assert loaded == card and loaded_bytes == onnx_bytes

    (tmp_path / model_store.MODEL_FILE).write_bytes(onnx_bytes + b"x")
    with pytest.raises(model_store.ModelError, match="does not match"):
        model_store.load(tmp_path)


def test_card_rejects_a_different_feature_list(trained):
    _, card = trained["v2"]
    data = {**card.to_dict(), "features": ["vibration"]}
    with pytest.raises(model_store.ModelError, match="features"):
        model_store.ModelCard.from_dict(data)
    with pytest.raises(model_store.ModelError):
        model_store.ModelCard.from_dict({"version": "1"})


def test_explanation_ranks_the_most_unusual_features(trained):
    _, card = trained["v2"]
    features = np.array(card.feature_mean, dtype=np.float32)
    temperature = FEATURE_NAMES.index("temperature_mean")
    features[temperature] += 10 * card.feature_std[temperature]
    explanation = card.explain(features)
    assert explanation[0]["feature"] == "temperature_mean"
    assert explanation[0]["z"] == pytest.approx(10, abs=0.2)
    assert len(explanation) == 3


def test_onnx_scores_helper_returns_one_score_per_window(trained):
    onnx_bytes, card = trained["v2"]
    scores = onnx_scores(onnx_bytes, np.array([card.feature_mean, card.feature_mean], dtype=np.float32))
    assert scores.shape == (2,) and (scores > card.threshold).all()


def test_debouncer_raises_and_clears_on_streaks():
    debouncer = Debouncer(raise_after=2, clear_after=3)
    flags = [True, False, True, True, False, True, False, False, False]
    events = [debouncer.update(flag) for flag in flags]
    assert events == [None, None, None, RAISED, None, None, None, None, CLEARED]
    with pytest.raises(ValueError):
        Debouncer(raise_after=0)


def test_detection_metrics():
    scores = np.array([1, 1, -1, -1, -1, -1])
    assert seconds_to_detect(scores, 0.0) == 50.0
    assert seconds_to_detect(np.ones(5), 0.0) is None
    parts = [np.zeros((8640, 8))]  # one day of windows for one press
    assert false_raises_per_day(parts, [np.r_[-np.ones(3), np.ones(8637)]], 0.0) == 1.0


def test_command_line_writes_model_and_card(tmp_path, capsys):
    config = _config(tmp_path)
    path = tmp_path / "c.json"
    path.write_text(json.dumps({**json.loads((MODELS / "v2.json").read_text()), "trainingHours": 0.25}))
    assert train_module.main(["--config", str(path), "--out", str(tmp_path / "out")]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["version"] == config.version
    assert (tmp_path / "out" / model_store.MODEL_FILE).exists()


@pytest.mark.parametrize(("version", "expected"), [("v1", []), ("v2", ["raised"])])
def test_overheating_press_through_the_real_detector(trained, version, expected):
    """End to end on the device path: simulated readings, window features, ONNX Runtime, debounce."""
    from detector import Detector, OnnxScorer
    from edgeml.physics import Fault, simulate

    onnx_bytes, card = trained[version]
    detector = Detector(card, OnnxScorer(onnx_bytes))
    readings = simulate("press-02", 900, seed=99, fault=Fault("overheat", started_at=120, ramp_seconds=60))
    alerts = [alert for r in readings for alert in detector.add(r)]
    assert [a["state"] for a in alerts][:1] == expected
    if expected:
        assert alerts[0]["explanation"][0]["feature"].startswith("temperature")
        assert detector.statistics()["activeAlerts"] == ["press-02"]
