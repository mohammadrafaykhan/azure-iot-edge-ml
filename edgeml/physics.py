"""A hydraulic press model: healthy behaviour that follows a load cycle, plus three faults.

Signals are correlated through load, as on a real machine: more load means more motor
current, slightly lower speed, more vibration, and (with a thermal lag) a hotter frame.
A fault breaks one of those relationships and ramps in over ``ramp_seconds``:

* ``bearing``   vibration rises and becomes impulsive (higher crest factor)
* ``overheat``  the frame heats up with no matching rise in load (cooling failure)
* ``jam``       current rises while speed drops (mechanical overload)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from edgeml.readings import Reading

FAULT_MODES = ("bearing", "overheat", "jam")


@dataclass(frozen=True)
class Fault:
    mode: str
    started_at: float  # simulation seconds
    ramp_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.mode not in FAULT_MODES:
            raise ValueError(f"unknown fault mode {self.mode!r}; expected one of {', '.join(FAULT_MODES)}")
        if self.ramp_seconds < 0:
            raise ValueError("ramp_seconds must not be negative")

    def severity(self, t: float) -> float:
        if t < self.started_at:
            return 0.0
        if self.ramp_seconds == 0:
            return 1.0
        return min(1.0, (t - self.started_at) / self.ramp_seconds)


class PressSimulator:
    """One press. ``read`` must be called with non-decreasing simulation times."""

    THERMAL_TAU = 120.0  # seconds

    def __init__(
        self,
        asset_id: str,
        rng: np.random.Generator,
        *,
        cycle_seconds: float = 300.0,
        phase: float = 0.0,
    ) -> None:
        self.asset_id = asset_id
        self.fault: Fault | None = None
        self._rng = rng
        self._cycle = cycle_seconds
        self._phase = phase
        self._temperature: float | None = None
        self._last_t: float | None = None
        self._sequence = 0

    def load(self, t: float) -> float:
        cyclic = 0.75 + 0.15 * math.sin(2 * math.pi * (t / self._cycle + self._phase))
        return float(np.clip(cyclic + self._rng.normal(0, 0.02), 0.4, 1.0))

    def read(self, t: float, timestamp: datetime) -> Reading:
        rng = self._rng
        load = self.load(t)
        severity = self.fault.severity(t) if self.fault else 0.0
        mode = self.fault.mode if self.fault else None

        overheating = 18.0 * severity if mode == "overheat" else 0.0
        target = 45.0 + 22.0 * load + overheating
        if self._temperature is None:
            # Start where a warmed-up frame sits: the thermal lag smooths the load cycle to its mean.
            self._temperature = 45.0 + 22.0 * 0.75 + overheating
        else:
            dt = max(0.0, t - (self._last_t or t))
            self._temperature += (target - self._temperature) * (1 - math.exp(-dt / self.THERMAL_TAU))
        self._last_t = t

        rpm = 1500.0 - 40.0 * (load - 0.75) + rng.normal(0, 4.0)
        current = 4.0 + 11.0 * load + rng.normal(0, 0.25)
        vibration = 1.6 + 0.8 * load + rng.normal(0, 0.12)
        crest = 2.6 + rng.normal(0, 0.15)
        if mode == "bearing":
            vibration += 2.0 * severity + rng.normal(0, 0.24 * severity)
            crest += 2.0 * severity
        elif mode == "jam":
            current += 4.0 * severity
            rpm -= 60.0 * severity
            vibration += 0.4 * severity

        reading = Reading(
            asset_id=self.asset_id,
            timestamp=timestamp,
            sequence=self._sequence,
            vibration_rms=max(0.05, vibration),
            vibration_peak=max(0.05, vibration) * max(1.0, crest),
            temperature=self._temperature + rng.normal(0, 0.15),
            current=max(0.0, current),
            rpm=max(0.0, rpm),
        )
        self._sequence += 1
        return reading


def simulate(
    asset_id: str,
    seconds: int,
    seed: int,
    *,
    fault: Fault | None = None,
    interval: float = 1.0,
    start: datetime | None = None,
    phase: float | None = None,
) -> list[Reading]:
    """Simulate ``seconds`` of one press sampled every ``interval`` seconds."""
    rng = np.random.default_rng(seed)
    press = PressSimulator(asset_id, rng, phase=float(rng.random()) if phase is None else phase)
    press.fault = fault
    origin = start or datetime(2026, 1, 1)
    steps = int(seconds / interval)
    return [press.read(i * interval, origin + timedelta(seconds=i * interval)) for i in range(steps)]
