"""Helpers shared by both edge modules' runtimes."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
from typing import Any

CONTENT_TYPE = "application/json"


def desired_only(patch: dict[str, Any]) -> dict[str, Any]:
    """Drop twin metadata ($version, $metadata) from a desired-properties document or patch."""
    return {k: v for k, v in (patch or {}).items() if not k.startswith("$")}


def json_message(payload: dict[str, Any], kind: str) -> Any:
    """A JSON message that IoT Hub routing and storage endpoints can read as JSON."""
    from azure.iot.device import Message

    message = Message(json.dumps(payload, separators=(",", ":")), content_encoding="utf-8", content_type=CONTENT_TYPE)
    message.custom_properties["kind"] = kind
    message.custom_properties["schema"] = str(payload.get("schema", ""))
    return message


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    logging.getLogger("azure").setLevel(logging.WARNING)


def stop_event() -> asyncio.Event:
    """An event set on SIGTERM or SIGINT, which is how IoT Edge stops a module."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):  # Windows development only
            loop.add_signal_handler(sig, stop.set)
    return stop


async def wait_or_stop(stop: asyncio.Event, seconds: float) -> bool:
    """Sleep up to ``seconds``; return True if asked to stop meanwhile.

    A deadline already passed still yields once and still honours a stop request (wait_for with
    a zero timeout would cancel the wait before it could see the event).
    """
    if seconds <= 0:
        await asyncio.sleep(0)
        return stop.is_set()
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
        return True
    except TimeoutError:
        return stop.is_set()
