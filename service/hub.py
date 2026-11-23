"""IoT Hub operations for the edge device over REST with Entra ID: identity, deployment, module twins."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from service.rest import ApiError, Requester

HUB_SCOPE = "https://iothubs.azure.net/.default"
HUB_API_VERSION = "2021-04-12"


def _id(value: str) -> str:
    return quote(value, safe="")


def _reported(twin: dict[str, Any] | None) -> dict[str, Any]:
    reported = ((twin or {}).get("properties") or {}).get("reported") or {}
    return {k: v for k, v in reported.items() if not k.startswith("$")}


def _desired(twin: dict[str, Any] | None) -> dict[str, Any]:
    desired = ((twin or {}).get("properties") or {}).get("desired") or {}
    return {k: v for k, v in desired.items() if not k.startswith("$")}


class EdgeHub:
    def __init__(self, requester: Requester) -> None:
        self._rest = requester

    def _get(self, path: str) -> dict[str, Any] | None:
        try:
            return self._rest.request("GET", path).body
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise

    def register_edge_device(self, device_id: str, thumbprint: str) -> dict[str, Any]:
        """Create the IoT Edge device with its certificate thumbprint pinned, or update the pin.

        The one certificate fills both slots: the IoT Edge hub cannot load a device whose secondary
        thumbprint is empty ("secondaryThumbprint is null or whitespace"), and then authorizes no module.
        """
        path = f"/devices/{_id(device_id)}"
        authentication = {
            "type": "selfSigned",
            "x509Thumbprint": {"primaryThumbprint": thumbprint, "secondaryThumbprint": thumbprint},
        }
        existing = self._get(path)
        if existing is None:
            body = {
                "deviceId": device_id,
                "status": "enabled",
                "capabilities": {"iotEdge": True},
                "authentication": authentication,
            }
            self._rest.request("PUT", path, body=body)
            action = "created"
        elif (existing.get("authentication") or {}).get("x509Thumbprint") == authentication["x509Thumbprint"] and (
            existing.get("capabilities") or {}
        ).get("iotEdge"):
            action = "unchanged"
        else:
            body = {**existing, "capabilities": {"iotEdge": True}, "authentication": authentication}
            self._rest.request("PUT", path, body=body, headers={"If-Match": f'"{existing["etag"]}"'})
            action = "updated"
        return {"deviceId": device_id, "identity": action, "thumbprint": thumbprint}

    def apply(self, device_id: str, modules_content: dict[str, Any]) -> None:
        self._rest.request(
            "POST",
            f"/devices/{_id(device_id)}/applyConfigurationContent",
            body={"modulesContent": modules_content},
        )

    def module_twin(self, device_id: str, module_id: str) -> dict[str, Any] | None:
        return self._get(f"/twins/{_id(device_id)}/modules/{_id(module_id)}")

    def module_desired(self, device_id: str, modules: tuple[str, ...]) -> dict[str, dict[str, Any]]:
        return {m: _desired(self.module_twin(device_id, m)) for m in modules}

    def patch_desired(self, device_id: str, module_id: str, desired: dict[str, Any]) -> dict[str, Any]:
        twin = self._rest.request(
            "PATCH",
            f"/twins/{_id(device_id)}/modules/{_id(module_id)}",
            body={"properties": {"desired": desired}},
            headers={"If-Match": "*"},
        ).body
        return {"module": module_id, "desired": _desired(twin)}

    def status(self, device_id: str) -> dict[str, Any]:
        device = self._get(f"/devices/{_id(device_id)}")
        if device is None:
            raise ApiError(404, f"device {device_id} is not registered")
        agent = _reported(self.module_twin(device_id, "$edgeAgent"))
        runtime = {}
        for group in ("systemModules", "modules"):
            for name, module in (agent.get(group) or {}).items():
                runtime[name] = {
                    "status": module.get("runtimeStatus"),
                    "image": (module.get("settings") or {}).get("image"),
                    "restarts": module.get("restartCount", 0),
                    "lastStart": module.get("lastStartTimeUtc"),
                }
        detector = _reported(self.module_twin(device_id, "anomaly-detector"))
        sensor = _reported(self.module_twin(device_id, "press-sensor"))
        return {
            "device": {
                "deviceId": device_id,
                "connectionState": device.get("connectionState"),
                "lastActivity": device.get("lastActivityTime"),
            },
            "runtime": runtime,
            "detector": {k: detector.get(k) for k in ("model", "config", "stats", "configErrors") if k in detector},
            "sensor": {
                k: sensor.get(k)
                for k in ("assets", "intervalMs", "faults", "readingsSent", "configErrors")
                if k in sensor
            },
        }
