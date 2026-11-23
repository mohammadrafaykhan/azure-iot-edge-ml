import json
import os
import subprocess
import sys
from contextlib import contextmanager

import pytest
from fakes import FakeBlobs, FakeRequester

from service.cli import Services, main
from service.config import ConfigError, ServiceConfig
from service.hub import EdgeHub
from service.rest import ApiError

CONFIG = ServiceConfig(
    hub_hostname="iot-edge.azure-devices.net",
    device_id="edge-01",
    storage_account="stedge",
    registry="acredge.azurecr.io",
    pull_token="edge-pull",
)
THUMB = "AB" * 32
DIGEST = "sha256:" + "c" * 64


def _run(argv, requester, blobs=None, config=CONFIG):
    @contextmanager
    def factory(_config):
        yield Services(hub=EdgeHub(requester), blobs=blobs or FakeBlobs({}))

    return main(argv, config_loader=lambda: config, services_factory=factory)


def _output(capsys):
    return json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------- hub operations


def test_register_creates_an_edge_device_with_a_pinned_thumbprint():
    requester = FakeRequester({("GET", "/devices/edge-01"): ApiError(404, "nope"), ("PUT", "/devices/edge-01"): {}})
    result = EdgeHub(requester).register_edge_device("edge-01", THUMB)
    assert result["identity"] == "created"
    body = requester.calls[-1][2]
    assert body["capabilities"] == {"iotEdge": True}
    assert body["authentication"]["type"] == "selfSigned"
    # edgeHub rejects an X.509 device without a secondary thumbprint and then authorizes no module.
    assert body["authentication"]["x509Thumbprint"] == {"primaryThumbprint": THUMB, "secondaryThumbprint": THUMB}


def test_register_is_idempotent_and_updates_a_changed_or_incomplete_pin():
    existing = {
        "deviceId": "edge-01",
        "etag": "e1",
        "capabilities": {"iotEdge": True},
        "authentication": {
            "type": "selfSigned",
            "x509Thumbprint": {"primaryThumbprint": THUMB, "secondaryThumbprint": THUMB},
        },
    }
    requester = FakeRequester({("GET", "/devices/edge-01"): existing, ("PUT", "/devices/edge-01"): {}})
    hub = EdgeHub(requester)
    assert hub.register_edge_device("edge-01", THUMB)["identity"] == "unchanged"
    assert hub.register_edge_device("edge-01", "CD" * 32)["identity"] == "updated"
    assert requester.calls[-1][3] == {"If-Match": '"e1"'}

    existing["authentication"]["x509Thumbprint"]["secondaryThumbprint"] = None  # registered before the fix
    assert hub.register_edge_device("edge-01", THUMB)["identity"] == "updated"


def test_status_combines_identity_runtime_and_module_twins():
    agent = {
        "properties": {
            "reported": {
                "systemModules": {"edgeHub": {"runtimeStatus": "running", "settings": {"image": "hub:1.6"}}},
                "modules": {
                    "anomaly-detector": {"runtimeStatus": "running", "restartCount": 1, "settings": {"image": "d@x"}}
                },
            }
        }
    }
    detector = {"properties": {"reported": {"$version": 3, "model": {"version": "2"}, "stats": {"windows": 5}}}}
    sensor = {"properties": {"reported": {"faults": {"press-02": {"mode": "overheat"}}, "readingsSent": 30}}}
    requester = FakeRequester(
        {
            ("GET", "/devices/edge-01"): {"connectionState": "Connected", "lastActivityTime": "t"},
            ("GET", "/twins/edge-01/modules/%24edgeAgent"): agent,
            ("GET", "/twins/edge-01/modules/anomaly-detector"): detector,
            ("GET", "/twins/edge-01/modules/press-sensor"): sensor,
        }
    )
    status = EdgeHub(requester).status("edge-01")
    assert status["device"]["connectionState"] == "Connected"
    assert status["runtime"]["anomaly-detector"] == {
        "status": "running",
        "image": "d@x",
        "restarts": 1,
        "lastStart": None,
    }
    assert status["detector"] == {"model": {"version": "2"}, "stats": {"windows": 5}}
    assert status["sensor"]["faults"]["press-02"]["mode"] == "overheat"


def test_status_of_an_unregistered_device_fails():
    with pytest.raises(ApiError):
        EdgeHub(FakeRequester({})).status("edge-01")


# ---------------------------------------------------------------- CLI


def test_device_register_validates_the_thumbprint(capsys):
    with pytest.raises(SystemExit):
        _run(["device", "register", "xyz"], FakeRequester({}))
    requester = FakeRequester({("GET", "/devices/edge-01"): ApiError(404, "no"), ("PUT", "/devices/edge-01"): {}})
    assert _run(["device", "register", ":".join(["ab"] * 32)], requester) == 0
    assert _output(capsys)["thumbprint"] == THUMB


def _images(tmp_path):
    path = tmp_path / "images.json"
    path.write_text(
        json.dumps(
            {
                "press-sensor": f"acredge.azurecr.io/edge-ml/press-sensor@{DIGEST}",
                "anomaly-detector": f"acredge.azurecr.io/edge-ml/anomaly-detector@{DIGEST}",
                "model": "v2",
            }
        )
    )
    return path


def _twin_routes(applied):
    return {
        ("GET", "/twins/edge-01/modules/press-sensor"): {
            "properties": {"desired": {"faults": {"press-02": "overheat"}, "$version": 4}}
        },
        ("GET", "/twins/edge-01/modules/anomaly-detector"): ApiError(404, "new"),
        ("POST", "/devices/edge-01/applyConfigurationContent"): lambda body: applied.append(body) or {},
    }


def test_deploy_requires_the_password_from_the_environment(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ACR_PULL_PASSWORD", raising=False)
    applied = []
    assert _run(["deploy", "--images", str(_images(tmp_path))], FakeRequester(_twin_routes(applied))) == 2
    assert "ACR_PULL_PASSWORD" in capsys.readouterr().err and not applied


def test_deploy_applies_the_manifest_and_keeps_active_faults(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ACR_PULL_PASSWORD", "pw-from-env")
    applied = []
    assert _run(["deploy", "--images", str(_images(tmp_path))], FakeRequester(_twin_routes(applied))) == 0
    content = applied[0]["modulesContent"]
    agent = content["$edgeAgent"]["properties.desired"]
    assert agent["runtime"]["settings"]["registryCredentials"]["acr"]["password"] == "pw-from-env"
    assert content["press-sensor"]["properties.desired"]["faults"] == {"press-02": "overheat"}
    printed = capsys.readouterr().out
    assert "pw-from-env" not in printed and json.loads(printed)["applied"] is True


def test_deploy_dry_run_prints_a_redacted_manifest(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ACR_PULL_PASSWORD", raising=False)
    applied = []
    assert _run(["deploy", "--images", str(_images(tmp_path)), "--dry-run"], FakeRequester(_twin_routes(applied))) == 0
    out = _output(capsys)
    assert out["$edgeAgent"]["properties.desired"]["runtime"]["settings"]["registryCredentials"]["acr"]["password"] == (
        "***"
    )
    assert not applied


def test_deploy_rejects_a_bad_images_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ACR_PULL_PASSWORD", "pw")
    path = tmp_path / "images.json"
    path.write_text("[]")
    assert _run(["deploy", "--images", str(path)], FakeRequester({})) == 2
    assert _run(["deploy", "--images", str(tmp_path / "missing.json")], FakeRequester({})) == 2


def _patch_route(seen):
    def handler(body):
        seen.append(body)
        return {"properties": {"desired": {**body["properties"]["desired"], "$version": 9}}}

    return handler


def test_fault_patches_the_sensor_twin(capsys):
    seen = []
    requester = FakeRequester({("PATCH", "/twins/edge-01/modules/press-sensor"): _patch_route(seen)})
    assert _run(["fault", "press-02", "overheat", "--ramp", "30"], requester) == 0
    assert seen[0]["properties"]["desired"] == {"faults": {"press-02": "overheat"}, "faultRampSeconds": 30}
    assert requester.calls[0][3] == {"If-Match": "*"}
    assert _run(["fault", "press-02", "none"], requester) == 0
    assert seen[1]["properties"]["desired"] == {"faults": {"press-02": None}}
    with pytest.raises(SystemExit):
        _run(["fault", "press-09", "jam"], requester)


def test_tune_patches_the_detector_twin(capsys):
    seen = []
    requester = FakeRequester({("PATCH", "/twins/edge-01/modules/anomaly-detector"): _patch_route(seen)})
    assert _run(["tune", "--threshold", "-0.3", "--raise-after", "2"], requester) == 0
    assert seen[0]["properties"]["desired"] == {"threshold": -0.3, "raiseAfterWindows": 2}
    assert _run(["tune", "--model-threshold"], requester) == 0
    assert seen[1]["properties"]["desired"] == {"threshold": None}
    assert _run(["tune"], requester) == 2
    with pytest.raises(SystemExit):
        _run(["tune", "--threshold", "1", "--model-threshold"], requester)


def _record(kind, body, enqueued):
    return json.dumps({"EnqueuedTimeUtc": enqueued, "Properties": {"kind": kind}, "Body": body})


def test_alerts_and_summaries_read_routed_events(capsys, monkeypatch):
    from datetime import UTC, datetime

    import service.cli as cli

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 23, 12, 10, tzinfo=UTC)

    monkeypatch.setattr(cli, "datetime", FrozenDatetime)
    alert = {
        "assetId": "press-02",
        "state": "raised",
        "at": "2026-11-23T12:00:00Z",
        "score": -1.2,
        "explanation": [{"feature": "temperature_mean"}],
    }
    summaries = [
        _record("summary", {"to": f"2026-11-23T12:0{i}:00Z", "readings": 180, "assets": {}}, f"2026-11-23T12:0{i}:01Z")
        for i in range(4)
    ]
    blobs = FakeBlobs(
        {"iot-edge/2026/11/23/12/09/0": "\n".join([_record("alert", alert, "2026-11-23T12:08:00Z"), *summaries])}
    )
    assert _run(["alerts", "--minutes", "30"], FakeRequester({}), blobs) == 0
    rows = _output(capsys)
    assert rows == [
        {
            "enqueuedAt": "2026-11-23T12:08:00Z",
            "at": "2026-11-23T12:00:00Z",
            "delaySeconds": 480.0,
            "assetId": "press-02",
            "state": "raised",
            "score": -1.2,
            "threshold": None,
            "modelVersion": None,
            "explanation": [{"feature": "temperature_mean"}],
        }
    ]
    assert _run(["summaries", "--last", "2"], FakeRequester({}), blobs) == 0
    rows = _output(capsys)
    assert [r["to"] for r in rows] == ["2026-11-23T12:02:00Z", "2026-11-23T12:03:00Z"]


def test_errors_map_to_exit_codes(capsys):
    def missing():
        raise ConfigError("IOTHUB_HOSTNAME is not set")

    assert main(["status"], config_loader=missing) == 2
    assert _run(["status"], FakeRequester({})) == 1
    assert "not registered" in capsys.readouterr().err


def test_config_loads_from_the_environment(monkeypatch):
    from service.config import load_config

    for name, value in {
        "IOTHUB_HOSTNAME": "https://iot-edge.azure-devices.net/",
        "STORAGE_ACCOUNT_NAME": "stedge",
        "ACR_LOGIN_SERVER": "acredge.azurecr.io",
        "EDGE_DEVICE_ID": "",
    }.items():
        monkeypatch.setenv(name, value)
    config = load_config()
    assert config.hub_hostname == "iot-edge.azure-devices.net"
    assert config.device_id == "edge-01" and config.pull_token == "edge-pull"
    monkeypatch.setenv("ACR_LOGIN_SERVER", " ")
    with pytest.raises(ConfigError, match="ACR_LOGIN_SERVER"):
        load_config()


def test_piped_output_is_utf8_even_on_an_ansi_code_page():
    code = "from service.cli import _utf8_output; _utf8_output(); print('z ≥ 3')"
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env, check=True)
    assert result.stdout.decode("utf-8").strip() == "z ≥ 3"
