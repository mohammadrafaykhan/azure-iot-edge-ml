"""edgectl configuration from the environment (or a local .env file written by infra/deploy.ps1)."""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class ServiceConfig:
    hub_hostname: str
    device_id: str
    storage_account: str
    registry: str  # ACR login server, e.g. acredge123.azurecr.io
    pull_token: str  # ACR token name the device pulls with


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set (run infra/deploy.ps1 or see .env.example)")
    return value


def load_config() -> ServiceConfig:
    from dotenv import load_dotenv

    load_dotenv()
    return ServiceConfig(
        hub_hostname=_required("IOTHUB_HOSTNAME").removeprefix("https://").rstrip("/"),
        device_id=os.getenv("EDGE_DEVICE_ID", "edge-01").strip() or "edge-01",
        storage_account=_required("STORAGE_ACCOUNT_NAME"),
        registry=_required("ACR_LOGIN_SERVER"),
        pull_token=os.getenv("ACR_PULL_TOKEN", "edge-pull").strip() or "edge-pull",
    )
