import base64
import json
from datetime import UTC, datetime

import pytest
from fakes import FakeBlobs

from service.events import hour_prefixes, parse_records, recent


def _record(kind, body, enqueued="2026-11-23T12:05:30.1234567Z", encode=False):
    return json.dumps(
        {
            "EnqueuedTimeUtc": enqueued,
            "Properties": {"kind": kind, "schema": body.get("schema", "")},
            "SystemProperties": {"connectionDeviceId": "edge-01", "connectionModuleId": "anomaly-detector"},
            "Body": base64.b64encode(json.dumps(body).encode()).decode() if encode else body,
        }
    )


ALERT = {"schema": "edge-ml/alert/1", "assetId": "press-02", "state": "raised", "at": "2026-11-23T12:00:30.000Z"}


def test_parses_json_and_base64_bodies_and_seven_digit_times():
    text = "\n".join([_record("alert", ALERT), _record("alert", ALERT, encode=True), "", "not json"])
    events = parse_records(text)
    assert len(events) == 2
    assert events[0].body == events[1].body == ALERT
    assert events[0].enqueued_at == datetime(2026, 11, 23, 12, 5, 30, 123456, tzinfo=UTC)
    assert (events[0].device_id, events[0].module_id, events[0].kind) == ("edge-01", "anomaly-detector", "alert")


def test_delay_shows_how_late_the_cloud_received_an_event():
    event = parse_records(_record("alert", ALERT))[0]
    assert event.delay_seconds() == pytest.approx(300.1)
    no_time = parse_records(_record("alert", {"schema": "x"}))[0]
    assert no_time.delay_seconds() is None


def test_records_without_an_enqueued_time_or_with_bad_bodies_are_handled():
    assert parse_records(json.dumps({"Body": {}})) == []
    event = parse_records(_record("alert", {}, encode=False).replace("{}", '"!!notbase64"'))[0]
    assert event.body == {}


def test_hour_prefixes_span_the_window():
    start = datetime(2026, 11, 23, 22, 50, tzinfo=UTC)
    end = datetime(2026, 11, 24, 0, 10, tzinfo=UTC)
    assert hour_prefixes("iot-edge", start, end) == [
        "iot-edge/2026/11/23/22/",
        "iot-edge/2026/11/23/23/",
        "iot-edge/2026/11/24/00/",
    ]


def test_recent_filters_by_kind_and_time():
    summary = {"schema": "edge-ml/summary/1", "to": "2026-11-23T12:05:00.000Z"}
    blobs = FakeBlobs(
        {
            "iot-edge/2026/11/23/12/05/0": "\n".join([_record("alert", ALERT), _record("summary", summary)]),
            "iot-edge/2026/11/23/11/00/1": _record("alert", ALERT, enqueued="2026-11-23T11:00:00Z"),
        }
    )
    now = datetime(2026, 11, 23, 12, 10, tzinfo=UTC)
    alerts = recent(blobs, "iot-edge", 30, "alert", now)
    assert [e.body["assetId"] for e in alerts] == ["press-02"]
    assert len(recent(blobs, "iot-edge", 120, "alert", now)) == 2
    assert len(recent(blobs, "iot-edge", 30, "summary", now)) == 1
    with pytest.raises(ValueError):
        recent(blobs, "iot-edge", 0, "alert", now)
