# ADR 0005: Device identity and access

- **Status:** Accepted
- **Date:** 2026-11-23

## Context

The edge device, its modules, the operator, and the cloud services each need credentials. Each
should hold the least it needs, and no credential should be copied to a second place.

## Decision

- **The device key is born on the device.** At first boot, cloud-init generates:
  - an EC P-256 key, readable only by the IoT keys service (`aziotks`, mode 0600);
  - a self-signed client certificate (`CA:FALSE`, `clientAuth`).

  The deploy script reads only the certificate's SHA-256 thumbprint through Run Command and
  registers the IoT Edge device with that thumbprint pinned. The provisioning config
  (manual X.509) is applied only after registration.
- **Modules authenticate through the workload API.** The IoT Edge security daemon issues module
  tokens from keys it holds, so no module receives a connection string.
- **The hub accepts no shared access policies** (`disableLocalAuth`). The operator uses Entra ID
  with *IoT Hub Data Contributor* for registration, deployments, and module twins.
- **The storage account has shared keys disabled**, and every role is scoped to one container:

  | Identity | Role | Container |
  |---|---|---|
  | Hub | *Storage Blob Data Contributor* | `edge-insights` |
  | VM | *Storage Blob Data Reader* | `build` |
  | Operator | upload | `build` |
  | Operator | read | `edge-insights` |

- **Registry access is split by direction.** The VM identity can push (*AcrPush*); the device's
  token can only read two repositories (ADR 0001).

## Consequences

- No secret is shared between machines; the registry pull password is the only secret the device
  receives from outside, and it is narrow and expires.
- A rebuilt VM gets a new key, so re-registering the device updates the pinned thumbprint
  (`edgectl device register` handles create, unchanged, and update).
