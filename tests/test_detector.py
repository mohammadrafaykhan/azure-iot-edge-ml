from datetime import timedelta
from itertools import count

import numpy as np
import pytest
from fakes import START, VibrationScorer, card, reading

from detector import ALERT_SCHEMA, SUMMARY_SCHEMA, Detector, DetectorConfig, OnnxScorer
from edgeml.features import WINDOW_SIZE


def _feed(detector, asset, start, windows, vibration):
    alerts = []
    for sequence in range(start, start + windows * WINDOW_SIZE):
        alerts += detector.add(reading(asset, sequence, vibration))
    return alerts


def _detector(**config):
    ticks = count()
    return Detector(card(), VibrationScorer(), DetectorConfig(**config), perf_counter=lambda: next(ticks) * 0.002)


def test_scores_one_window_per_ten_readings_per_press():
    detector = _detector()
    _feed(detector, "press-01", 0, 3, 2.0)
    _feed(detector, "press-02", 0, 1, 2.0)
    assert detector.windows_total == 4
    assert detector.readings_in == 40
    assert detector.statistics()["latencyMs"] == {"p50": 2.0, "p95": 2.0}


def test_raises_after_consecutive_anomalous_windows_with_an_explanation():
    detector = _detector()
    assert _feed(detector, "press-01", 0, 2, 2.0) == []
    assert _feed(detector, "press-01", 20, 2, 5.0) == []
    alerts = _feed(detector, "press-01", 40, 1, 5.0)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["schema"] == ALERT_SCHEMA and alert["state"] == "raised"
    assert alert["assetId"] == "press-01" and alert["modelVersion"] == "9"
    assert alert["at"] == "2026-11-23T12:00:49.000Z"
    assert alert["explanation"][0]["feature"] == "vibration_rms_mean"
    assert detector.statistics()["activeAlerts"] == ["press-01"]


def test_clears_after_consecutive_normal_windows():
    detector = _detector(raise_after=1, clear_after=2)
    raised = _feed(detector, "press-01", 0, 1, 5.0)
    assert _feed(detector, "press-01", 10, 1, 2.0) == []
    cleared = _feed(detector, "press-01", 20, 1, 2.0)
    assert raised[0]["state"] == "raised"
    assert cleared[0]["state"] == "cleared" and cleared[0]["since"] == raised[0]["at"]
    assert "explanation" not in cleared[0]


def test_a_single_anomalous_window_does_not_raise():
    detector = _detector()
    alerts = _feed(detector, "press-01", 0, 1, 5.0) + _feed(detector, "press-01", 10, 5, 2.0)
    assert alerts == []


def test_duplicate_readings_are_ignored():
    detector = _detector()
    for sequence in (0, 1, 1, 2, 0):
        detector.add(reading("press-01", sequence, at=START + timedelta(seconds=min(sequence, 2))))
    assert detector.readings_in == 3 and detector.duplicates == 2


def test_a_sensor_restart_starts_a_new_window():
    detector = _detector()
    for sequence in range(5):
        detector.add(reading("press-01", sequence))
    later = START + timedelta(minutes=5)
    for sequence in range(WINDOW_SIZE):
        detector.add(reading("press-01", sequence, at=later + timedelta(seconds=sequence)))
    assert detector.windows_total == 1 and detector.duplicates == 0


def test_threshold_override_changes_the_decision():
    detector = _detector(raise_after=1)
    detector.reconfigure(DetectorConfig(threshold=0.6, raise_after=1))  # healthy score 0.5 < 0.6
    assert detector.threshold == 0.6
    assert _feed(detector, "press-01", 0, 1, 2.0)[0]["state"] == "raised"


def test_reconfigure_applies_debounce_to_existing_presses():
    detector = _detector()
    _feed(detector, "press-01", 0, 1, 2.0)
    detector.reconfigure(DetectorConfig(raise_after=1))
    assert _feed(detector, "press-01", 10, 1, 5.0)[0]["state"] == "raised"


def test_config_patch_validation():
    config, errors = DetectorConfig().apply(
        {"threshold": -0.3, "raiseAfterWindows": 5, "clearAfterWindows": 0, "summarySeconds": "60"}
    )
    assert config.threshold == -0.3 and config.raise_after == 5
    assert config.clear_after == 6 and config.summary_seconds == 60
    assert len(errors) == 2
    reset, errors = config.apply({"threshold": None})
    assert reset.threshold is None and not errors
    _, errors = config.apply({"threshold": True})
    assert errors == ["threshold must be a number or null"]


def test_reported_config_names_the_threshold_source():
    assert DetectorConfig().to_reported(card(threshold=-0.2))["thresholdSource"] == "model"
    reported = DetectorConfig(threshold=0.1).to_reported(card())
    assert reported["threshold"] == 0.1 and reported["thresholdSource"] == "desired"


def test_summary_covers_the_interval_and_resets():
    detector = _detector(raise_after=1)
    _feed(detector, "press-01", 0, 2, 2.0)
    _feed(detector, "press-02", 0, 1, 5.0)
    summary = detector.summary(START, START + timedelta(minutes=1))
    assert summary["schema"] == SUMMARY_SCHEMA
    assert summary["readings"] == 30
    assert summary["assets"]["press-01"] == {
        "windows": 2,
        "anomalous": 0,
        "meanScore": 0.5,
        "minScore": 0.5,
        "alert": False,
    }
    assert summary["assets"]["press-02"]["alert"] is True
    again = detector.summary(START, START)
    assert again["readings"] == 0 and again["assets"]["press-01"]["windows"] == 0
    assert again["assets"]["press-02"]["meanScore"] is None


def test_upstream_reduction_counts_messages_sent_to_the_cloud():
    detector = _detector()
    _feed(detector, "press-01", 0, 6, 2.0)
    assert detector.statistics()["upstreamReduction"] is None
    detector.summary(START, START)
    assert detector.statistics()["upstreamReduction"] == 60.0


def test_onnx_scorer_scores_a_single_window(tmp_path):
    from skl2onnx import to_onnx
    from sklearn.svm import OneClassSVM

    data = np.random.default_rng(0).normal(size=(200, 8)).astype(np.float32)
    model = OneClassSVM(nu=0.05).fit(data)
    onnx_bytes = to_onnx(model, data[:1], target_opset={"": 17, "ai.onnx.ml": 3}).SerializeToString()
    scorer = OnnxScorer(onnx_bytes)
    assert scorer.score(data[0]) == pytest.approx(float(model.decision_function(data[:1])[0]), abs=1e-4)
