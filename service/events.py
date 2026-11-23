"""Read the alerts and summaries IoT Hub routed to Blob storage, with Entra ID.

The hub writes JSON records in batches, one blob per minute per partition, named
``{iothub}/{YYYY}/{MM}/{DD}/{HH}/{mm}/{partition}`` (set in infra/hub-routing.bicep), so a
time window maps to a handful of hourly prefixes.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

CONTAINER = "edge-insights"
MAX_MINUTES = 24 * 60


@dataclass(frozen=True)
class RoutedEvent:
    kind: str
    enqueued_at: datetime
    device_id: str
    module_id: str
    body: dict[str, Any]

    def event_time(self) -> datetime | None:
        value = self.body.get("at") or self.body.get("to")
        if not isinstance(value, str):
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            return None

    def delay_seconds(self) -> float | None:
        """How long after it happened the cloud received it; large after an outage (store and forward)."""
        happened = self.event_time()
        return round((self.enqueued_at - happened).total_seconds(), 1) if happened else None


def iso_seconds(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _body(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):  # bodies without a JSON content type are stored base64-encoded
        try:
            decoded = json.loads(base64.b64decode(raw, validate=True))
        except (binascii.Error, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.replace("Z", "+00:00")
    if "." in text:  # the hub writes 7 fractional digits; Python accepts up to 6
        head, _, rest = text.partition(".")
        digits = "".join(ch for ch in rest if ch.isdigit())
        text = f"{head}.{digits[:6]}{rest[len(digits) :]}"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).astimezone(UTC)


def parse_records(text: str) -> list[RoutedEvent]:
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        system = record.get("SystemProperties") or {}
        enqueued = _time(record.get("EnqueuedTimeUtc") or system.get("enqueuedTime"))
        if enqueued is None:
            continue
        properties = record.get("Properties") or {}
        body = _body(record.get("Body"))
        events.append(
            RoutedEvent(
                kind=str(properties.get("kind") or body.get("schema", "")),
                enqueued_at=enqueued,
                device_id=str(system.get("connectionDeviceId", "")),
                module_id=str(system.get("connectionModuleId", "")),
                body=body,
            )
        )
    return events


def hour_prefixes(hub_name: str, start: datetime, end: datetime) -> list[str]:
    prefixes, hour = [], start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    while hour <= end:
        prefixes.append(f"{hub_name}/{hour:%Y/%m/%d/%H}/")
        hour += timedelta(hours=1)
    return prefixes


class BlobLister(Protocol):
    def list_names(self, prefix: str) -> list[str]: ...
    def read_text(self, name: str) -> str: ...


class AzureBlobs:
    def __init__(self, account: str, credential: Any) -> None:
        from azure.storage.blob import BlobServiceClient

        service = BlobServiceClient(account_url=f"https://{account}.blob.core.windows.net", credential=credential)
        self._container = service.get_container_client(CONTAINER)

    def list_names(self, prefix: str) -> list[str]:
        return [b.name for b in self._container.list_blobs(name_starts_with=prefix)]

    def read_text(self, name: str) -> str:
        return self._container.download_blob(name).readall().decode("utf-8")


def recent(blobs: BlobLister, hub_name: str, minutes: int, kind: str, now: datetime) -> list[RoutedEvent]:
    if not 1 <= minutes <= MAX_MINUTES:
        raise ValueError(f"minutes must be between 1 and {MAX_MINUTES}")
    start = now - timedelta(minutes=minutes)
    events = []
    for prefix in hour_prefixes(hub_name, start, now):
        for name in sorted(blobs.list_names(prefix)):
            events.extend(e for e in parse_records(blobs.read_text(name)) if e.kind == kind and e.enqueued_at >= start)
    return sorted(events, key=lambda e: e.enqueued_at)
