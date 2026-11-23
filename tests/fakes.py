"""In-memory stand-ins for the IoT Edge module client, the model, and Azure services."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from edgeml.features import FEATURE_NAMES
from edgeml.model import ModelCard
from edgeml.readings import Reading
from service.rest import ApiError, Response

START = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
VIBRATION = FEATURE_NAMES.index("vibration_rms_mean")


def card(threshold: float = 0.0, version: str = "9") -> ModelCard:
    return ModelCard(
        version=version,
        sha256="ab" * 32,
        threshold=threshold,
        features=FEATURE_NAMES,
        feature_mean=(2.2, 0.12, 2.6, 61.5, 0.0, 12.25, 1500.0, 8.2),
        feature_std=(0.1, 0.03, 0.15, 1.0, 0.5, 0.4, 3.0, 0.3),
        metrics={"recallAtFullSeverity": {"bearing": 1.0}},
        training={"algorithm": "oneClassSvm"},
    )


class VibrationScorer:
    """Anomalous (score -1) when mean vibration exceeds 3 mm/s, otherwise healthy (score 0.5)."""

    def __init__(self) -> None:
        self.calls = 0

    def score(self, features: np.ndarray) -> float:
        self.calls += 1
        return -1.0 if features[VIBRATION] > 3.0 else 0.5


def reading(asset: str, sequence: int, vibration: float = 2.0, at: datetime | None = None) -> Reading:
    return Reading(
        asset_id=asset,
        timestamp=at or START + timedelta(seconds=sequence),
        sequence=sequence,
        vibration_rms=vibration,
        vibration_peak=vibration * 2.6,
        temperature=61.0,
        current=12.0,
        rpm=1500.0,
    )


@dataclass
class FakeMessage:
    data: bytes
    input_name: str = "readings"


@dataclass
class FakeModuleClient:
    desired: dict[str, Any] = field(default_factory=dict)
    fail_sends: int = 0
    sent: list[tuple[str, Any]] = field(default_factory=list)
    reported: list[dict[str, Any]] = field(default_factory=list)

    async def get_twin(self) -> dict[str, Any]:
        return {"desired": {**self.desired, "$version": 3}, "reported": {"$version": 1}}

    async def patch_twin_reported_properties(self, patch: dict[str, Any]) -> None:
        self.reported.append(json.loads(json.dumps(patch, default=str)))

    async def send_message_to_output(self, message: Any, output: str) -> None:
        if self.fail_sends:
            self.fail_sends -= 1
            raise ConnectionError("edgeHub unavailable")
        self.sent.append((output, message))

    def payloads(self, output: str) -> list[dict[str, Any]]:
        return [json.loads(m.data) for o, m in self.sent if o == output]


class FakeRequester:
    """Routes REST calls to handlers keyed by (method, path); records every call."""

    def __init__(self, routes: dict[tuple[str, str], Any]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, Any, dict]] = []

    def request(self, method: str, path: str, *, body: Any = None, headers: dict | None = None) -> Response:
        self.calls.append((method, path, body, headers or {}))
        handler = self.routes.get((method, path))
        if handler is None:
            raise ApiError(404, f"no route {method} {path}")
        result = handler(body) if callable(handler) else handler
        if isinstance(result, ApiError):
            raise result
        return Response(200, result)


class FakeBlobs:
    def __init__(self, blobs: dict[str, str]) -> None:
        self.blobs = blobs

    def list_names(self, prefix: str) -> list[str]:
        return [name for name in self.blobs if name.startswith(prefix)]

    def read_text(self, name: str) -> str:
        return self.blobs[name]
