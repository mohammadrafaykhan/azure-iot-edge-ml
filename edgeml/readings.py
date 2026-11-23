"""The reading a press sensor produces once per sample, and its wire format between modules."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

SCHEMA = "edge-ml/reading/1"


class InvalidReading(ValueError):
    pass


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise InvalidReading("timestamp must be an ISO 8601 string")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidReading(f"bad timestamp {value!r}") from exc
    if moment.tzinfo is None:
        raise InvalidReading("timestamp must include a time zone")
    return moment.astimezone(UTC)


@dataclass(frozen=True)
class Reading:
    asset_id: str
    timestamp: datetime
    sequence: int
    vibration_rms: float  # mm/s
    vibration_peak: float  # mm/s
    temperature: float  # °C
    current: float  # A
    rpm: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "assetId": self.asset_id,
            "timestamp": iso(self.timestamp),
            "sequence": self.sequence,
            "vibrationRms": round(self.vibration_rms, 4),
            "vibrationPeak": round(self.vibration_peak, 4),
            "temperature": round(self.temperature, 3),
            "current": round(self.current, 3),
            "rpm": round(self.rpm, 2),
        }

    @classmethod
    def from_dict(cls, data: Any) -> Reading:
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            raise InvalidReading(f"expected a {SCHEMA} object")
        asset = data.get("assetId")
        if not isinstance(asset, str) or not asset:
            raise InvalidReading("assetId is required")
        sequence = data.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            raise InvalidReading("sequence must be a non-negative integer")
        values = {}
        for key in ("vibrationRms", "vibrationPeak", "temperature", "current", "rpm"):
            value = data.get(key)
            if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
                raise InvalidReading(f"{key} must be a finite number")
            values[key] = float(value)
        return cls(
            asset_id=asset,
            timestamp=parse_time(data.get("timestamp")),
            sequence=sequence,
            vibration_rms=values["vibrationRms"],
            vibration_peak=values["vibrationPeak"],
            temperature=values["temperature"],
            current=values["current"],
            rpm=values["rpm"],
        )
