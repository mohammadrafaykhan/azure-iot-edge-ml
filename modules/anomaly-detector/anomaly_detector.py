"""anomaly-detector module: readings in, alerts and summaries out, model and stats in the module twin."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from detector import Detector, OnnxScorer
from edgeml import model as model_store
from edgeml.module import configure_logging, desired_only, json_message, stop_event, wait_or_stop
from edgeml.readings import InvalidReading, Reading

logger = logging.getLogger("anomaly-detector")

INPUT = "readings"
ALERTS = "alerts"
SUMMARIES = "summaries"
REPORT_SECONDS = 300


class DetectorModule:
    def __init__(
        self,
        client: Any,
        detector: Detector,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        report_seconds: float = REPORT_SECONDS,
    ) -> None:
        self.client = client
        self.detector = detector
        self.config_errors: list[str] = []
        self.invalid = 0
        self._clock = clock
        self._monotonic = monotonic
        self._report_seconds = report_seconds
        self._interval_start = clock()

    async def start(self) -> None:
        twin = await self.client.get_twin()
        self._apply(desired_only(twin.get("desired", {})))
        await self.report()

    def _apply(self, desired: dict[str, Any]) -> None:
        config, errors = self.detector.config.apply(desired)
        self.detector.reconfigure(config)
        self.config_errors = errors
        for error in errors:
            logger.warning("ignored desired property: %s", error)

    async def handle_desired(self, patch: dict[str, Any]) -> None:
        self._apply(desired_only(patch))
        await self.report()

    async def handle_message(self, message: Any) -> None:
        if getattr(message, "input_name", INPUT) != INPUT:
            return
        try:
            reading = Reading.from_dict(json.loads(message.data))
        except (InvalidReading, ValueError, TypeError) as exc:
            self.invalid += 1
            logger.warning("dropped invalid reading: %s", exc)
            return
        for alert in self.detector.add(reading):
            logger.warning("ALERT %s %s score=%.3f", alert["assetId"], alert["state"], alert["score"])
            await self.client.send_message_to_output(json_message(alert, "alert"), ALERTS)

    async def send_summary(self) -> None:
        end = self._clock()
        summary = self.detector.summary(self._interval_start, end)
        self._interval_start = end
        await self.client.send_message_to_output(json_message(summary, "summary"), SUMMARIES)

    def reported(self) -> dict[str, Any]:
        card = self.detector.card
        return {
            "model": {
                "version": card.version,
                "sha256": card.sha256,
                "algorithm": card.training.get("algorithm"),
                "threshold": card.threshold,
                "recallAtFullSeverity": card.metrics.get("recallAtFullSeverity"),
            },
            "config": self.detector.config.to_reported(card),
            "configErrors": self.config_errors,
            "stats": {**self.detector.statistics(), "invalidReadings": self.invalid},
            "reportedAt": self._clock().isoformat(timespec="seconds"),
        }

    async def report(self) -> None:
        await self.client.patch_twin_reported_properties(self.reported())

    async def run(self, stop: asyncio.Event) -> None:
        next_summary = self._monotonic() + self.detector.config.summary_seconds
        next_report = self._monotonic() + self._report_seconds
        while True:
            wait = min(next_summary, next_report) - self._monotonic()
            if await wait_or_stop(stop, wait):
                return
            now = self._monotonic()
            try:
                if now >= next_summary:
                    next_summary = now + self.detector.config.summary_seconds
                    await self.send_summary()
                if now >= next_report:
                    next_report = now + self._report_seconds
                    await self.report()
            except Exception:  # the SDK retries; one failed send must not stop the module
                logger.exception("periodic send failed")


async def main() -> None:
    from azure.iot.device.aio import IoTHubModuleClient

    configure_logging()
    onnx_bytes, card = model_store.load(Path(os.getenv("MODEL_DIR", "/app/model")))
    logger.info("model %s (%s) sha256 %s", card.version, card.training.get("algorithm"), card.sha256[:12])
    client = IoTHubModuleClient.create_from_edge_environment()
    module = DetectorModule(client, Detector(card, OnnxScorer(onnx_bytes)))
    client.on_message_received = module.handle_message
    client.on_twin_desired_properties_patch_received = module.handle_desired
    await client.connect()
    try:
        await module.start()
        await module.run(stop_event())
    finally:
        await client.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
