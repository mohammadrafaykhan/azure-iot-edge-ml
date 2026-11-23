import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from edgeml.features import FEATURE_NAMES, WINDOW_SIZE, window_features, windows
from edgeml.physics import Fault, PressSimulator, simulate
from edgeml.readings import InvalidReading, Reading

START = datetime(2026, 1, 1, tzinfo=UTC)


def _reading(i: int, **overrides) -> Reading:
    values = dict(
        asset_id="press-01",
        timestamp=START + timedelta(seconds=i),
        sequence=i,
        vibration_rms=2.0,
        vibration_peak=5.0,
        temperature=60.0,
        current=12.0,
        rpm=1500.0,
    )
    values.update(overrides)
    return Reading(**values)


def _mean(readings, attribute):
    return float(np.mean([getattr(r, attribute) for r in readings]))


def test_simulation_is_deterministic_for_a_seed():
    assert simulate("press-01", 120, seed=5) == simulate("press-01", 120, seed=5)
    assert simulate("press-01", 120, seed=5) != simulate("press-01", 120, seed=6)


def test_healthy_press_stays_in_its_operating_envelope():
    readings = simulate("press-01", 1800, seed=1)
    assert 2.0 < _mean(readings, "vibration_rms") < 2.4
    assert 59 < _mean(readings, "temperature") < 64
    assert 11.5 < _mean(readings, "current") < 13
    assert 1495 < _mean(readings, "rpm") < 1505
    assert [r.sequence for r in readings[:3]] == [0, 1, 2]


def test_signals_are_correlated_through_load():
    readings = simulate("press-01", 3600, seed=2)
    current = [r.current for r in readings]
    rpm = [r.rpm for r in readings]
    assert np.corrcoef(current, rpm)[0, 1] < -0.5  # more load: more current, less speed


@pytest.mark.parametrize(
    ("mode", "attribute", "change"),
    [("bearing", "vibration_rms", 1.5), ("overheat", "temperature", 10.0), ("jam", "current", 3.0)],
)
def test_each_fault_moves_its_signal(mode, attribute, change):
    healthy = simulate("press-01", 900, seed=3)
    faulty = simulate("press-01", 900, seed=3, fault=Fault(mode, started_at=0, ramp_seconds=60))
    assert _mean(faulty[-300:], attribute) - _mean(healthy[-300:], attribute) > change


def test_overheating_does_not_change_current():
    healthy = simulate("press-01", 900, seed=4)
    faulty = simulate("press-01", 900, seed=4, fault=Fault("overheat", started_at=0))
    assert abs(_mean(faulty, "current") - _mean(healthy, "current")) < 0.05


def test_bearing_fault_makes_vibration_impulsive():
    healthy = windows(simulate("press-01", 600, seed=5))
    faulty = windows(simulate("press-01", 600, seed=5, fault=Fault("bearing", 0, 0)))
    crest = FEATURE_NAMES.index("crest_factor")
    assert faulty[:, crest].mean() > healthy[:, crest].mean() + 1


def test_fault_severity_ramps_and_validates():
    fault = Fault("jam", started_at=100, ramp_seconds=50)
    assert [fault.severity(t) for t in (50, 100, 125, 150, 500)] == [0, 0, 0.5, 1, 1]
    assert Fault("jam", 0, ramp_seconds=0).severity(0) == 1
    with pytest.raises(ValueError, match="unknown fault"):
        Fault("melt", 0)
    with pytest.raises(ValueError):
        Fault("jam", 0, ramp_seconds=-1)


def test_simulator_starts_warm_with_no_temperature_transient():
    press = PressSimulator("press-01", np.random.default_rng(0), phase=0.25)  # starts at peak load
    first = press.read(0, START)
    assert abs(first.temperature - 61.5) < 1


def test_reading_round_trips_through_its_wire_format():
    reading = simulate("press-02", 3, seed=1, start=START)[2]
    restored = Reading.from_dict(reading.to_dict())
    assert restored.asset_id == "press-02" and restored.sequence == 2
    assert restored.timestamp == reading.timestamp.replace(microsecond=0)
    assert math.isclose(restored.temperature, reading.temperature, abs_tol=1e-3)


@pytest.mark.parametrize(
    "change",
    [
        {"schema": "other"},
        {"assetId": ""},
        {"sequence": -1},
        {"sequence": True},
        {"temperature": float("nan")},
        {"rpm": "fast"},
        {"timestamp": "2026-01-01T00:00:00"},
        {"timestamp": "yesterday"},
    ],
)
def test_invalid_readings_are_rejected(change):
    data = {**_reading(0).to_dict(), **change}
    with pytest.raises(InvalidReading):
        Reading.from_dict(data)


def test_window_features_of_a_steady_window():
    features = window_features([_reading(i) for i in range(WINDOW_SIZE)])
    values = dict(zip(FEATURE_NAMES, features.tolist(), strict=True))
    assert values["vibration_rms_mean"] == pytest.approx(2.0)
    assert values["vibration_rms_std"] == pytest.approx(0.0)
    assert values["crest_factor"] == pytest.approx(2.5)
    assert values["temperature_slope"] == pytest.approx(0.0, abs=1e-4)
    assert values["current_per_krpm"] == pytest.approx(8.0)
    assert features.dtype == np.float32


def test_temperature_slope_is_per_minute():
    window = [_reading(i, temperature=60 + 0.1 * i) for i in range(WINDOW_SIZE)]
    slope = window_features(window)[FEATURE_NAMES.index("temperature_slope")]
    assert slope == pytest.approx(6.0, rel=1e-4)


def test_windows_drop_a_trailing_partial_window():
    readings = [_reading(i) for i in range(25)]
    assert windows(readings).shape == (2, len(FEATURE_NAMES))
    assert windows(readings[:5]).shape == (0, len(FEATURE_NAMES))
    with pytest.raises(ValueError):
        window_features(readings[:1])
