"""The presses on this line: one simulator each, with faults switched on and off from the module twin."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np

from edgeml.physics import FAULT_MODES, Fault, PressSimulator
from edgeml.readings import Reading, iso

MIN_INTERVAL_MS = 200
MAX_INTERVAL_MS = 60_000


@dataclass(frozen=True)
class ActiveFault:
    mode: str
    since: datetime


class Line:
    def __init__(self, assets: list[str], seed: int, interval_ms: int = 1000, ramp_seconds: float = 60.0) -> None:
        if not assets:
            raise ValueError("at least one asset is required")
        rng = np.random.default_rng(seed)
        self.presses = {
            a: PressSimulator(a, np.random.default_rng(rng.integers(2**32)), phase=float(rng.random())) for a in assets
        }
        self.interval_ms = interval_ms
        self.ramp_seconds = ramp_seconds
        self.faults: dict[str, ActiveFault] = {}
        self.sent = 0

    def apply(self, desired: dict[str, Any], t: float, now: datetime) -> list[str]:
        """Apply desired properties; returns errors for anything ignored.

        ``intervalMs``: sample period. ``faultRampSeconds``: how fast a new fault reaches full
        severity. ``faults``: ``{"press-02": "bearing"}`` starts a fault, ``null`` clears it.
        """
        errors: list[str] = []
        if "intervalMs" in desired:
            value = desired["intervalMs"]
            if isinstance(value, int) and not isinstance(value, bool) and MIN_INTERVAL_MS <= value <= MAX_INTERVAL_MS:
                self.interval_ms = value
            else:
                errors.append(f"intervalMs must be an integer from {MIN_INTERVAL_MS} to {MAX_INTERVAL_MS}")
        if "faultRampSeconds" in desired:
            value = desired["faultRampSeconds"]
            if isinstance(value, int | float) and not isinstance(value, bool) and 0 <= value <= 3600:
                self.ramp_seconds = float(value)
            else:
                errors.append("faultRampSeconds must be a number from 0 to 3600")
        faults = desired.get("faults")
        if faults is not None and not isinstance(faults, dict):
            errors.append("faults must be an object of asset id to fault mode")
            faults = None
        for asset, mode in (faults or {}).items():
            if asset.startswith("$"):
                continue
            press = self.presses.get(asset)
            if press is None:
                errors.append(f"unknown asset {asset!r}")
            elif mode is None or mode == "none":
                press.fault = None
                self.faults.pop(asset, None)
            elif mode not in FAULT_MODES:
                errors.append(f"unknown fault mode {mode!r} for {asset}")
            elif asset not in self.faults or self.faults[asset].mode != mode:
                press.fault = Fault(mode, started_at=t, ramp_seconds=self.ramp_seconds)
                self.faults[asset] = ActiveFault(mode, now)
        return errors

    def sample(self, t: float, now: datetime) -> list[Reading]:
        readings = [press.read(t, now) for press in self.presses.values()]
        self.sent += len(readings)
        return readings

    def reported(self) -> dict[str, Any]:
        """Reported properties. Twins merge reported patches, so a cleared fault is reported as
        null; leaving it out would keep the old value in the twin forever."""
        faults = {a: None for a in self.presses}
        faults.update({a: {"mode": f.mode, "since": iso(f.since)} for a, f in self.faults.items()})
        return {
            "assets": list(self.presses),
            "intervalMs": self.interval_ms,
            "faultRampSeconds": self.ramp_seconds,
            "faults": faults,
            "readingsSent": self.sent,
        }
