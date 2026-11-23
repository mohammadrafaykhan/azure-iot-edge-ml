import json

import pytest

from service import manifest
from service.manifest import ManifestError, RegistryCredential, build, merge_desired, redact

REGISTRY = RegistryCredential("acredge.azurecr.io", "edge-pull", "s3cret-password")
DIGEST = "sha256:" + "a" * 64
IMAGES = {
    "press-sensor": f"acredge.azurecr.io/edge-ml/press-sensor@{DIGEST}",
    "anomaly-detector": f"acredge.azurecr.io/edge-ml/anomaly-detector@{DIGEST}",
}


def _agent(content):
    return content["$edgeAgent"]["properties.desired"]


def test_runtime_is_iot_edge_1_6_with_persistent_storage():
    agent = _agent(build(IMAGES, REGISTRY))
    assert agent["schemaVersion"] == "1.1"
    system = agent["systemModules"]
    assert system["edgeAgent"]["settings"]["image"] == "mcr.microsoft.com/azureiotedge-agent:1.6"
    assert system["edgeHub"]["settings"]["image"] == "mcr.microsoft.com/azureiotedge-hub:1.6"
    hub_options = json.loads(system["edgeHub"]["settings"]["createOptions"])
    assert hub_options == {"HostConfig": {"Binds": ["/srv/edgeHub:/tmp/edgeHub"]}}  # no published ports
    assert system["edgeHub"]["env"] == {"StorageFolder": {"value": "/tmp/edgeHub"}}


def test_modules_are_pinned_by_digest_with_memory_limits():
    modules = _agent(build(IMAGES, REGISTRY))["modules"]
    assert modules["press-sensor"]["settings"]["image"] == IMAGES["press-sensor"]
    assert modules["anomaly-detector"]["startupOrder"] > modules["press-sensor"]["startupOrder"]
    options = json.loads(modules["anomaly-detector"]["settings"]["createOptions"])
    assert options["HostConfig"]["Memory"] == 384 * 1024 * 1024
    assert modules["press-sensor"]["env"]["ASSETS"]["value"] == "press-01,press-02,press-03"


def test_registry_credential_uses_the_pull_token():
    credentials = _agent(build(IMAGES, REGISTRY))["runtime"]["settings"]["registryCredentials"]
    assert credentials == {
        "acr": {"address": "acredge.azurecr.io", "username": "edge-pull", "password": "s3cret-password"}
    }


def test_routes_keep_raw_readings_local_and_prioritise_alerts():
    hub = build(IMAGES, REGISTRY)["$edgeHub"]["properties.desired"]
    routes = hub["routes"]
    upstream = {name: r for name, r in routes.items() if "$upstream" in r["route"]}
    assert set(upstream) == {"alertsToCloud", "summariesToCloud"}
    assert "press-sensor" not in "".join(r["route"] for r in upstream.values())
    assert routes["alertsToCloud"]["priority"] < routes["summariesToCloud"]["priority"]
    assert routes["alertsToCloud"]["timeToLiveSecs"] > routes["summariesToCloud"]["timeToLiveSecs"]
    assert 'BrokeredEndpoint("/modules/anomaly-detector/inputs/readings")' in routes["sensorToDetector"]["route"]
    assert hub["storeAndForwardConfiguration"] == {"timeToLiveSecs": 7200}


def test_module_twins_get_desired_defaults():
    content = build(IMAGES, REGISTRY)
    assert content["press-sensor"]["properties.desired"] == {"intervalMs": 1000, "faultRampSeconds": 60, "faults": {}}
    assert "threshold" not in content["anomaly-detector"]["properties.desired"]  # the model's threshold applies


def _nulls(value, path=""):
    if value is None:
        return [path]
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in _nulls(v, f"{path}/{k}")]
    return []


def test_deployment_never_contains_null_values():
    # IoT Hub rejects null anywhere in a deployment ("Value of Property or Tag cannot be null").
    current = {
        "press-sensor": {"faults": {"press-01": None, "press-02": "jam"}},
        "anomaly-detector": {"threshold": None},
    }
    content = build(IMAGES, REGISTRY, merge_desired(current))
    assert _nulls(content) == []
    assert content["press-sensor"]["properties.desired"]["faults"] == {"press-02": "jam"}


def test_redeploy_keeps_operator_settings_only():
    current = {
        "press-sensor": {"faults": {"press-02": "overheat"}, "intervalMs": 500, "unknown": 1},
        "anomaly-detector": {"threshold": -0.2, "summarySeconds": 5},
    }
    desired = merge_desired(current)
    assert desired["press-sensor"] == {"intervalMs": 500, "faultRampSeconds": 60, "faults": {"press-02": "overheat"}}
    assert desired["anomaly-detector"]["threshold"] == -0.2
    assert desired["anomaly-detector"]["summarySeconds"] == 60  # not preserved: back to default
    assert merge_desired(None) == manifest.DEFAULT_DESIRED
    assert merge_desired({"press-sensor": {}})["press-sensor"]["faults"] == {}


@pytest.mark.parametrize(
    ("images", "error"),
    [
        ({"press-sensor": IMAGES["press-sensor"]}, "no image for anomaly-detector"),
        ({**IMAGES, "press-sensor": "acredge.azurecr.io/edge-ml/press-sensor:latest"}, "pinned by digest"),
        ({**IMAGES, "anomaly-detector": f"other.azurecr.io/edge-ml/anomaly-detector@{DIGEST}"}, "not in"),
    ],
)
def test_invalid_images_are_rejected(images, error):
    with pytest.raises(ManifestError, match=error):
        build(images, REGISTRY)


def test_empty_password_is_rejected():
    with pytest.raises(ManifestError, match="password"):
        build(IMAGES, RegistryCredential("acredge.azurecr.io", "edge-pull", ""))


def test_redact_hides_the_password_without_touching_the_original():
    content = build(IMAGES, REGISTRY)
    safe = redact(content)
    assert "s3cret-password" not in json.dumps(safe)
    assert _agent(safe)["runtime"]["settings"]["registryCredentials"]["acr"]["password"] == "***"
    assert _agent(content)["runtime"]["settings"]["registryCredentials"]["acr"]["password"] == "s3cret-password"
