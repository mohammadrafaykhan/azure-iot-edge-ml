"""The IoT Edge deployment manifest for the line: runtime, modules pinned by digest, routes, twins."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Any

EDGE_AGENT_IMAGE = "mcr.microsoft.com/azureiotedge-agent:1.6"
EDGE_HUB_IMAGE = "mcr.microsoft.com/azureiotedge-hub:1.6"
MODULES = ("press-sensor", "anomaly-detector")
ASSETS = ("press-01", "press-02", "press-03")
DIGEST_REFERENCE = re.compile(r"^[a-z0-9.\-]+(:[0-9]+)?/[a-z0-9._/\-]+@sha256:[0-9a-f]{64}$")
REDACTED = "***"

# Raw readings stay on the device; only these reach the cloud. Alerts outrank summaries when a
# backlog drains after an outage, and they are kept for a day instead of two hours.
DETECTOR_INPUT = 'BrokeredEndpoint("/modules/anomaly-detector/inputs/readings")'
ROUTES = {
    "sensorToDetector": {
        "route": f"FROM /messages/modules/press-sensor/outputs/readings INTO {DETECTOR_INPUT}",
        "priority": 1,
        "timeToLiveSecs": 300,
    },
    "alertsToCloud": {
        "route": "FROM /messages/modules/anomaly-detector/outputs/alerts INTO $upstream",
        "priority": 0,
        "timeToLiveSecs": 86400,
    },
    "summariesToCloud": {
        "route": "FROM /messages/modules/anomaly-detector/outputs/summaries INTO $upstream",
        "priority": 5,
        "timeToLiveSecs": 7200,
    },
}

# No threshold by default: the model card's applies. IoT Hub rejects null anywhere in a
# deployment's desired properties (null only means "delete" in a twin patch).
DEFAULT_DESIRED: dict[str, dict[str, Any]] = {
    "press-sensor": {"intervalMs": 1000, "faultRampSeconds": 60, "faults": {}},
    "anomaly-detector": {"raiseAfterWindows": 3, "clearAfterWindows": 6, "summarySeconds": 60},
}

# Operator settings that survive a redeploy (a model rollout must not clear an injected fault).
PRESERVED = {"press-sensor": ("faults", "intervalMs", "faultRampSeconds"), "anomaly-detector": ("threshold",)}


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class RegistryCredential:
    address: str
    username: str
    password: str


def _create_options(options: dict[str, Any]) -> str:
    return json.dumps(options, separators=(",", ":"))


def _storage(path: str, host: str) -> tuple[dict[str, Any], str]:
    return {"StorageFolder": {"value": path}}, _create_options({"HostConfig": {"Binds": [f"{host}:{path}"]}})


def _module(image: str, order: int, memory_mb: int, env: dict[str, str]) -> dict[str, Any]:
    return {
        "version": "1.0",
        "type": "docker",
        "status": "running",
        "restartPolicy": "always",
        "startupOrder": order,
        "settings": {
            "image": image,
            "createOptions": _create_options({"HostConfig": {"Memory": memory_mb * 1024 * 1024}}),
        },
        "env": {k: {"value": v} for k, v in env.items()},
    }


def _without_nulls(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _without_nulls(v) for k, v in value.items() if v is not None}
    return value


def merge_desired(current: dict[str, dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """Defaults, overlaid with the preserved keys of the modules' current desired properties.

    Null values are dropped at every level, since a deployment may not contain them.
    """
    desired = copy.deepcopy(DEFAULT_DESIRED)
    for module, keys in PRESERVED.items():
        for key in keys:
            value = (current or {}).get(module, {}).get(key, ...)
            if value is not ... and value is not None:
                desired[module][key] = _without_nulls(value)
    return desired


def build(
    images: dict[str, str],
    registry: RegistryCredential,
    desired: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    missing = [m for m in MODULES if m not in images]
    if missing:
        raise ManifestError(f"no image for {', '.join(missing)}")
    for module in MODULES:
        if not DIGEST_REFERENCE.match(images[module]):
            raise ManifestError(f"{module} image must be pinned by digest (registry/repo@sha256:...)")
        if not images[module].startswith(registry.address + "/"):
            raise ManifestError(f"{module} image is not in {registry.address}")
    if not registry.password:
        raise ManifestError("registry password is empty")

    agent_env, agent_options = _storage("/tmp/edgeAgent", "/srv/edgeAgent")
    hub_env, hub_options = _storage("/tmp/edgeHub", "/srv/edgeHub")
    desired = desired or merge_desired(None)
    return {
        "$edgeAgent": {
            "properties.desired": {
                "schemaVersion": "1.1",
                "runtime": {
                    "type": "docker",
                    "settings": {
                        "registryCredentials": {
                            "acr": {
                                "address": registry.address,
                                "username": registry.username,
                                "password": registry.password,
                            }
                        }
                    },
                },
                "systemModules": {
                    "edgeAgent": {
                        "type": "docker",
                        "settings": {"image": EDGE_AGENT_IMAGE, "createOptions": agent_options},
                        "env": agent_env,
                    },
                    "edgeHub": {
                        "type": "docker",
                        "status": "running",
                        "restartPolicy": "always",
                        "startupOrder": 0,
                        "settings": {"image": EDGE_HUB_IMAGE, "createOptions": hub_options},
                        "env": hub_env,
                    },
                },
                "modules": {
                    "press-sensor": _module(images["press-sensor"], 1, 128, {"ASSETS": ",".join(ASSETS)}),
                    "anomaly-detector": _module(images["anomaly-detector"], 2, 384, {}),
                },
            }
        },
        "$edgeHub": {
            "properties.desired": {
                "schemaVersion": "1.1",
                "routes": copy.deepcopy(ROUTES),
                "storeAndForwardConfiguration": {"timeToLiveSecs": 7200},
            }
        },
        "press-sensor": {"properties.desired": desired["press-sensor"]},
        "anomaly-detector": {"properties.desired": desired["anomaly-detector"]},
    }


def redact(manifest: dict[str, Any]) -> dict[str, Any]:
    """A copy safe to print or save: registry passwords replaced."""
    safe = copy.deepcopy(manifest)
    credentials = safe.get("$edgeAgent", {}).get("properties.desired", {}).get("runtime", {})
    for entry in credentials.get("settings", {}).get("registryCredentials", {}).values():
        entry["password"] = REDACTED
    return safe
