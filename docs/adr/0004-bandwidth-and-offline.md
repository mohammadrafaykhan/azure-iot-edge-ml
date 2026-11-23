# ADR 0004: Raw data stays local; store and forward for the rest

- **Status:** Accepted
- **Date:** 2026-11-23

## Context

Three presses sampled at 1 Hz produce 259,200 readings a day, more than thirty times the free
hub's 8,000-message daily quota. A site's uplink also fails from time to time, and alerts raised
during an outage must not be lost.

## Decision

- **Only conclusions go upstream.** The sensor's readings take a local route to the detector
  (`BrokeredEndpoint`); they never leave the device. The detector sends:
  - an **alert** on each raised or cleared transition;
  - one **summary** a minute, with per-press windows, anomalous windows, scores, alert state, and
    inference latency.
- **Routes carry priorities and lifetimes** (`$edgeHub` schema 1.1). After an outage, edgeHub sends
  alerts before summaries:

  | Route | Priority | TTL |
  |---|---|---|
  | `alertsToCloud` | 0 | 24 h |
  | `summariesToCloud` | 5 | 2 h |
  | `sensorToDetector` (local) | 1 | 5 min (stale raw data is useless) |

- **The queue survives restarts.** edgeHub and edgeAgent keep their state on host storage
  (`/srv/edgeHub`, `/srv/edgeAgent`) through `StorageFolder` and a bind mount.
- **Cloud side:** IoT Hub routes messages from the `anomaly-detector` module to a Blob container as
  JSON, using its managed identity, in one-minute batches named `{iothub}/{YYYY}/{MM}/{DD}/{HH}/{mm}/{partition}`.
  `edgectl alerts` and `edgectl summaries` read them and show each message's delay: enqueue time
  minus event time.
- **Outage simulation** (`infra/network.ps1`) rejects TCP to the hub's address with iptables in
  `OUTPUT` (the host) and `DOCKER-USER` (containers), resetting open connections. An NSG rule
  would not do: NSG changes don't cut connections that are already established.

## Consequences

- Upstream traffic is one summary a minute plus alerts and twin reports: a few thousand messages a
  day, about half the free tier, against 259,200 raw readings.
- The cloud cannot reprocess raw history. A deeper investigation would need a temporary raw route
  or an on-device buffer.
- During an outage the device keeps detecting, and the delays shown after reconnection measure
  how long each message waited.
