import asyncio
import json
from datetime import timedelta

import pytest
from fakes import START, FakeMessage, FakeModuleClient, VibrationScorer, card, reading

from anomaly_detector import ALERTS, SUMMARIES, DetectorModule
from detector import Detector, DetectorConfig
from edgeml.features import WINDOW_SIZE
from edgeml.module import desired_only, json_message, wait_or_stop
from press_sensor import OUTPUT, SensorModule
from sensor import Line


def _message(sequence: int, vibration: float = 2.0, asset: str = "press-01") -> FakeMessage:
    return FakeMessage(json.dumps(reading(asset, sequence, vibration).to_dict()).encode())


def _detector_module(client, **config):
    detector = Detector(card(), VibrationScorer(), DetectorConfig(**config))
    return DetectorModule(client, detector, clock=lambda: START + timedelta(minutes=1))


# ---------------------------------------------------------------- shared helpers


def test_desired_only_drops_twin_metadata():
    assert desired_only({"$version": 4, "$metadata": {}, "faults": {}}) == {"faults": {}}
    assert desired_only(None) == {}


def test_json_message_is_routable_as_json():
    message = json_message({"schema": "edge-ml/alert/1", "x": 1}, "alert")
    assert message.content_type == "application/json" and message.content_encoding == "utf-8"
    assert message.custom_properties == {"kind": "alert", "schema": "edge-ml/alert/1"}
    assert json.loads(message.data) == {"schema": "edge-ml/alert/1", "x": 1}


async def test_wait_or_stop():
    stop = asyncio.Event()
    assert await wait_or_stop(stop, 0.01) is False
    assert await wait_or_stop(stop, -1) is False
    stop.set()
    assert await wait_or_stop(stop, 5) is True


async def test_wait_or_stop_sees_a_stop_when_the_deadline_has_passed():
    # A module running behind schedule must still stop when IoT Edge asks it to.
    stop = asyncio.Event()
    stop.set()
    assert await wait_or_stop(stop, 0) is True
    assert await wait_or_stop(stop, -3) is True


# ---------------------------------------------------------------- anomaly-detector


async def test_detector_start_applies_desired_and_reports_the_model():
    client = FakeModuleClient(desired={"threshold": -0.4, "raiseAfterWindows": 99})
    module = _detector_module(client)
    await module.start()
    assert module.detector.config.threshold == -0.4
    reported = client.reported[-1]
    assert reported["model"]["version"] == "9" and reported["model"]["algorithm"] == "oneClassSvm"
    assert reported["config"]["thresholdSource"] == "desired"
    assert reported["configErrors"] == ["raiseAfterWindows must be an integer from 1 to 60"]


async def test_detector_sends_alerts_to_the_alerts_output():
    client = FakeModuleClient()
    module = _detector_module(client, raise_after=1)
    for sequence in range(WINDOW_SIZE):
        await module.handle_message(_message(sequence, vibration=5.0))
    alerts = client.payloads(ALERTS)
    assert len(alerts) == 1 and alerts[0]["state"] == "raised"
    assert client.sent[0][1].custom_properties["kind"] == "alert"


async def test_detector_counts_invalid_readings_and_ignores_other_inputs():
    client = FakeModuleClient()
    module = _detector_module(client)
    await module.handle_message(FakeMessage(b"not json"))
    await module.handle_message(FakeMessage(b'{"schema": "edge-ml/reading/1"}'))
    await module.handle_message(FakeMessage(b"{}", input_name="other"))
    assert module.invalid == 2
    assert module.reported()["stats"]["invalidReadings"] == 2


async def test_detector_desired_patch_reconfigures_and_reports():
    client = FakeModuleClient()
    module = _detector_module(client)
    await module.handle_desired({"$version": 7, "summarySeconds": 30})
    assert module.detector.config.summary_seconds == 30
    assert client.reported[-1]["config"]["summarySeconds"] == 30


async def test_detector_summary_goes_to_the_summaries_output():
    client = FakeModuleClient()
    module = _detector_module(client)
    for sequence in range(WINDOW_SIZE):
        await module.handle_message(_message(sequence))
    await module.send_summary()
    summary = client.payloads(SUMMARIES)[0]
    assert summary["readings"] == WINDOW_SIZE and summary["to"] == "2026-11-23T12:01:00.000Z"
    assert module.reported()["stats"]["summariesOut"] == 1


async def test_detector_run_returns_when_stopped():
    module = _detector_module(FakeModuleClient())
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(module.run(stop), timeout=1)


async def test_detector_run_sends_summaries_and_survives_send_failures():
    client = FakeModuleClient(fail_sends=1)
    clock = iter(float(t) for t in range(0, 100_000, 20))  # each call passes the next deadline
    module = DetectorModule(
        client,
        Detector(card(), VibrationScorer(), DetectorConfig(summary_seconds=10)),
        monotonic=lambda: next(clock),
        report_seconds=10_000,
    )
    stop = asyncio.Event()

    async def stop_soon():
        while len(client.payloads(SUMMARIES)) < 1:
            await asyncio.sleep(0)
        stop.set()

    await asyncio.wait_for(asyncio.gather(module.run(stop), stop_soon()), timeout=2)
    assert client.payloads(SUMMARIES)


# ---------------------------------------------------------------- press-sensor


def _line(**kwargs) -> Line:
    return Line(["press-01", "press-02"], seed=1, **kwargs)


def test_line_applies_and_clears_faults():
    line = _line()
    errors = line.apply({"faults": {"press-02": "overheat"}, "faultRampSeconds": 30}, t=10, now=START)
    assert errors == []
    fault = line.presses["press-02"].fault
    assert (fault.mode, fault.started_at, fault.ramp_seconds) == ("overheat", 10, 30)
    assert line.reported()["faults"] == {
        "press-01": None,
        "press-02": {"mode": "overheat", "since": "2026-11-23T12:00:00.000Z"},
    }

    line.apply({"faults": {"press-02": "overheat"}}, t=50, now=START)
    assert line.presses["press-02"].fault.started_at == 10  # same fault: keeps ramping, not restarted
    line.apply({"faults": {"press-02": None}}, t=60, now=START)
    assert line.presses["press-02"].fault is None and line.faults == {}
    # Reported patches merge into the twin: a cleared fault must be reported as null to disappear.
    assert line.reported()["faults"] == {"press-01": None, "press-02": None}


@pytest.mark.parametrize(
    ("desired", "error"),
    [
        ({"intervalMs": 50}, "intervalMs"),
        ({"intervalMs": True}, "intervalMs"),
        ({"faultRampSeconds": -1}, "faultRampSeconds"),
        ({"faults": ["press-01"]}, "faults must be an object"),
        ({"faults": {"press-09": "jam"}}, "unknown asset"),
        ({"faults": {"press-01": "melt"}}, "unknown fault mode"),
    ],
)
def test_line_rejects_invalid_settings(desired, error):
    errors = _line().apply(desired, t=0, now=START)
    assert len(errors) == 1 and error in errors[0]


def test_line_samples_every_press():
    line = _line(interval_ms=500)
    readings = line.sample(0.0, START)
    assert [r.asset_id for r in readings] == ["press-01", "press-02"]
    assert line.sent == 2 and line.reported()["intervalMs"] == 500
    with pytest.raises(ValueError):
        Line([], seed=1)


async def test_sensor_start_applies_faults_from_the_twin():
    client = FakeModuleClient(desired={"faults": {"press-01": "bearing"}, "intervalMs": 2000})
    module = SensorModule(client, _line(), clock=lambda: START)
    await module.start()
    assert module.line.interval_ms == 2000 and module.line.presses["press-01"].fault.mode == "bearing"
    assert client.reported[-1]["faults"]["press-01"]["mode"] == "bearing"


async def test_sensor_tick_sends_one_reading_per_press():
    client = FakeModuleClient()
    module = SensorModule(client, _line(), clock=lambda: START)
    await module.tick()
    payloads = client.payloads(OUTPUT)
    assert [p["assetId"] for p in payloads] == ["press-01", "press-02"]
    assert payloads[0]["schema"] == "edge-ml/reading/1"


async def test_sensor_counts_send_failures_and_keeps_going():
    client = FakeModuleClient(fail_sends=1)
    module = SensorModule(client, _line(), clock=lambda: START)
    await module.tick()
    assert module.send_failures == 1 and len(client.payloads(OUTPUT)) == 1


async def test_sensor_desired_patch_reports_errors():
    client = FakeModuleClient()
    module = SensorModule(client, _line(), clock=lambda: START)
    await module.handle_desired({"$version": 2, "faults": {"press-01": "melt"}})
    assert client.reported[-1]["configErrors"] == ["unknown fault mode 'melt' for press-01"]


async def test_sensor_run_samples_on_schedule_until_stopped():
    client = FakeModuleClient()
    module = SensorModule(client, _line(interval_ms=200), clock=lambda: START)
    stop = asyncio.Event()

    async def stop_after_two_ticks():
        while len(client.payloads(OUTPUT)) < 4:
            await asyncio.sleep(0.01)
        stop.set()

    await asyncio.wait_for(asyncio.gather(module.run(stop), stop_after_two_ticks()), timeout=3)
    assert len(client.payloads(OUTPUT)) >= 4
