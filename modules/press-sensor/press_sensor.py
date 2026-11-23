"""press-sensor module: samples every press on the line and sends each reading to the detector."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from edgeml.module import configure_logging, desired_only, json_message, stop_event, wait_or_stop
from sensor import Line

logger = logging.getLogger("press-sensor")

OUTPUT = "readings"
REPORT_SECONDS = 300


class SensorModule:
    def __init__(
        self,
        client: Any,
        line: Line,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        report_seconds: float = REPORT_SECONDS,
    ) -> None:
        self.client = client
        self.line = line
        self.config_errors: list[str] = []
        self.send_failures = 0
        self._clock = clock
        self._monotonic = monotonic
        self._origin = monotonic()
        self._report_seconds = report_seconds

    def elapsed(self) -> float:
        return self._monotonic() - self._origin

    def _apply(self, desired: dict[str, Any]) -> None:
        self.config_errors = self.line.apply(desired, self.elapsed(), self._clock())
        for error in self.config_errors:
            logger.warning("ignored desired property: %s", error)
        if self.line.faults:
            logger.info("active faults: %s", {a: f.mode for a, f in self.line.faults.items()})

    async def start(self) -> None:
        twin = await self.client.get_twin()
        self._apply(desired_only(twin.get("desired", {})))
        await self.report()

    async def handle_desired(self, patch: dict[str, Any]) -> None:
        self._apply(desired_only(patch))
        await self.report()

    async def report(self) -> None:
        await self.client.patch_twin_reported_properties(
            {**self.line.reported(), "configErrors": self.config_errors, "sendFailures": self.send_failures}
        )

    async def tick(self) -> None:
        for reading in self.line.sample(self.elapsed(), self._clock()):
            try:
                await self.client.send_message_to_output(json_message(reading.to_dict(), "reading"), OUTPUT)
            except Exception as exc:  # edgeHub restarting: drop this sample, keep the schedule
                self.send_failures += 1
                logger.warning("send failed for %s: %s", reading.asset_id, exc)

    async def run(self, stop: asyncio.Event) -> None:
        next_tick = self._monotonic()
        next_report = self._monotonic() + self._report_seconds
        while True:
            await self.tick()
            next_tick += self.line.interval_ms / 1000
            if self._monotonic() >= next_report:
                next_report = self._monotonic() + self._report_seconds
                try:
                    await self.report()
                except Exception:
                    logger.exception("reporting properties failed")
            if self._monotonic() > next_tick + 5:  # fell far behind (host paused): resynchronise
                next_tick = self._monotonic()
            if await wait_or_stop(stop, next_tick - self._monotonic()):
                return


async def main() -> None:
    from azure.iot.device.aio import IoTHubModuleClient

    configure_logging()
    assets = [a.strip() for a in os.getenv("ASSETS", "press-01,press-02,press-03").split(",") if a.strip()]
    line = Line(assets, seed=int(os.getenv("SEED", "42")))
    client = IoTHubModuleClient.create_from_edge_environment()
    module = SensorModule(client, line)
    client.on_twin_desired_properties_patch_received = module.handle_desired
    await client.connect()
    logger.info("sampling %s every %d ms", ", ".join(assets), line.interval_ms)
    try:
        await module.start()
        await module.run(stop_event())
    finally:
        await client.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
