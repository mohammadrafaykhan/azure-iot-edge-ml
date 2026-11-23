"""edgectl: operate the edge line with your Microsoft Entra ID identity.

    edgectl device register <thumbprint>          create or update the IoT Edge device identity
    edgectl deploy --images <file> [--dry-run]    apply the deployment manifest (modules pinned by digest)
    edgectl status                                runtime, modules, model, and detector statistics
    edgectl fault <asset> <mode> [--ramp S]       start (bearing | overheat | jam) or clear (none) a fault
    edgectl tune [--threshold X | --model-threshold] [--raise-after N] [--clear-after N] [--summary-seconds N]
    edgectl alerts [--minutes N]                  alerts the hub routed to storage, with delivery delay
    edgectl summaries [--minutes N] [--last N]    periodic summaries, with delivery delay

``deploy`` reads the registry password from the ACR_PULL_PASSWORD environment variable, never
from the command line. Results are JSON on stdout; errors go to stderr with exit code 1
(operation failed) or 2 (invalid input or configuration).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from azure.core.exceptions import AzureError, ClientAuthenticationError

from service import events, manifest
from service.config import ConfigError, ServiceConfig, load_config
from service.hub import HUB_API_VERSION, HUB_SCOPE, EdgeHub
from service.rest import ApiError, RestClient

logger = logging.getLogger("edgectl")

FAULT_MODES = ("bearing", "overheat", "jam", "none")
PASSWORD_VARIABLE = "ACR_PULL_PASSWORD"
THUMBPRINT = re.compile(r"^(?:[0-9A-Fa-f]{40}|[0-9A-Fa-f]{64})$")


@dataclass
class Services:
    hub: EdgeHub
    blobs: events.BlobLister


ServicesFactory = Callable[[ServiceConfig], AbstractContextManager[Services]]


def _thumbprint(text: str) -> str:
    value = text.replace(":", "").strip()
    if not THUMBPRINT.match(value):
        raise argparse.ArgumentTypeError("expected a SHA-1 (40) or SHA-256 (64) hex thumbprint")
    return value.upper()


def _bounded(low: int, high: int) -> Callable[[str], int]:
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("expected a whole number") from exc
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return value

    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="edgectl", description="Operate the IoT Edge anomaly detection line.")
    parser.add_argument("-v", "--verbose", action="store_true", help="log HTTP requests")
    commands = parser.add_subparsers(dest="command", required=True)

    device = commands.add_parser("device", help="the edge device identity").add_subparsers(dest="action", required=True)
    register = device.add_parser("register", help="create or update the IoT Edge device")
    register.add_argument("thumbprint", type=_thumbprint, help="device certificate thumbprint")

    deploy = commands.add_parser("deploy", help="apply the deployment manifest")
    deploy.add_argument("--images", type=Path, required=True, help="JSON of module name to image@digest")
    deploy.add_argument("--dry-run", action="store_true", help="print the manifest (password redacted) only")

    commands.add_parser("status", help="runtime, modules, model, and statistics")

    fault = commands.add_parser("fault", help="start or clear a simulated fault")
    fault.add_argument("asset", choices=manifest.ASSETS)
    fault.add_argument("mode", choices=FAULT_MODES)
    fault.add_argument("--ramp", type=_bounded(0, 3600), help="seconds to reach full severity")

    tune = commands.add_parser("tune", help="change detector settings through its module twin")
    threshold = tune.add_mutually_exclusive_group()
    threshold.add_argument("--threshold", type=float, help="override the model's decision threshold")
    threshold.add_argument("--model-threshold", action="store_true", help="go back to the model's threshold")
    tune.add_argument("--raise-after", type=_bounded(1, 60))
    tune.add_argument("--clear-after", type=_bounded(1, 360))
    tune.add_argument("--summary-seconds", type=_bounded(10, 3600))

    alerts = commands.add_parser("alerts", help="routed alerts, oldest first")
    alerts.add_argument("--minutes", type=_bounded(1, events.MAX_MINUTES), default=60)

    summaries = commands.add_parser("summaries", help="routed summaries, oldest first")
    summaries.add_argument("--minutes", type=_bounded(1, events.MAX_MINUTES), default=15)
    summaries.add_argument("--last", type=_bounded(1, 500), default=5)
    return parser


@contextmanager
def _default_services(config: ServiceConfig) -> Iterator[Services]:
    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential()
    try:
        with RestClient(f"https://{config.hub_hostname}", credential, HUB_SCOPE, HUB_API_VERSION) as rest:
            yield Services(hub=EdgeHub(rest), blobs=events.AzureBlobs(config.storage_account, credential))
    finally:
        credential.close()


def _load_images(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read images file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("images file must be a JSON object")
    return {k: v for k, v in data.items() if k in manifest.MODULES and isinstance(v, str)}


def _deploy(args: argparse.Namespace, services: Services, config: ServiceConfig) -> Any:
    images = _load_images(args.images)
    password = os.environ.get(PASSWORD_VARIABLE, "")
    if not password and not args.dry_run:
        raise ConfigError(f"{PASSWORD_VARIABLE} is not set")
    current = services.hub.module_desired(config.device_id, manifest.MODULES)
    content = manifest.build(
        images,
        manifest.RegistryCredential(config.registry, config.pull_token, password or "dry-run"),
        manifest.merge_desired(current),
    )
    if args.dry_run:
        return manifest.redact(content)
    services.hub.apply(config.device_id, content)
    return {"deviceId": config.device_id, "applied": True, "images": images}


def _tune_patch(args: argparse.Namespace) -> dict[str, Any]:
    patch: dict[str, Any] = {}
    if args.threshold is not None:
        patch["threshold"] = args.threshold
    if args.model_threshold:
        patch["threshold"] = None
    for name, key in (
        ("raise_after", "raiseAfterWindows"),
        ("clear_after", "clearAfterWindows"),
        ("summary_seconds", "summarySeconds"),
    ):
        if getattr(args, name) is not None:
            patch[key] = getattr(args, name)
    if not patch:
        raise ValueError("nothing to change; see edgectl tune --help")
    return patch


def _alert_row(event: events.RoutedEvent) -> dict[str, Any]:
    body = event.body
    row = {
        "enqueuedAt": events.iso_seconds(event.enqueued_at),
        "at": body.get("at"),
        "delaySeconds": event.delay_seconds(),
        "assetId": body.get("assetId"),
        "state": body.get("state"),
        "score": body.get("score"),
        "threshold": body.get("threshold"),
        "modelVersion": body.get("modelVersion"),
    }
    if body.get("explanation"):
        row["explanation"] = body["explanation"]
    return row


def _summary_row(event: events.RoutedEvent) -> dict[str, Any]:
    body = event.body
    return {
        "enqueuedAt": events.iso_seconds(event.enqueued_at),
        "to": body.get("to"),
        "delaySeconds": event.delay_seconds(),
        "modelVersion": body.get("modelVersion"),
        "readings": body.get("readings"),
        "latencyMs": body.get("latencyMs"),
        "assets": body.get("assets"),
    }


def _execute(args: argparse.Namespace, services: Services, config: ServiceConfig) -> Any:
    hub_name = config.hub_hostname.split(".")[0]
    now = datetime.now(UTC)
    if args.command == "device":
        return services.hub.register_edge_device(config.device_id, args.thumbprint)
    if args.command == "deploy":
        return _deploy(args, services, config)
    if args.command == "status":
        return services.hub.status(config.device_id)
    if args.command == "fault":
        patch: dict[str, Any] = {"faults": {args.asset: None if args.mode == "none" else args.mode}}
        if args.ramp is not None:
            patch["faultRampSeconds"] = args.ramp
        return services.hub.patch_desired(config.device_id, "press-sensor", patch)
    if args.command == "tune":
        return services.hub.patch_desired(config.device_id, "anomaly-detector", _tune_patch(args))
    if args.command == "alerts":
        return [_alert_row(e) for e in events.recent(services.blobs, hub_name, args.minutes, "alert", now)]
    rows = events.recent(services.blobs, hub_name, args.minutes, "summary", now)
    return [_summary_row(e) for e in rows[-args.last :]]


def main(
    argv: Sequence[str] | None = None,
    *,
    config_loader: Callable[[], ServiceConfig] = load_config,
    services_factory: ServicesFactory = _default_services,
) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
    )
    try:
        config = config_loader()
        with services_factory(config) as services:
            result = _execute(args, services, config)
    except (ConfigError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ClientAuthenticationError as exc:
        print(f"error: not signed in to Azure ({exc.message}); run 'az login'", file=sys.stderr)
        return 1
    except ApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except AzureError as exc:
        print(f"error: {exc.message or exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    return 0


def _utf8_output() -> None:
    """Emit UTF-8 when piped: Windows otherwise uses the ANSI code page."""
    for stream in (sys.stdout, sys.stderr):
        if not stream.isatty() and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def console() -> None:
    _utf8_output()
    raise SystemExit(main())
