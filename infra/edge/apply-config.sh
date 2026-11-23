#!/usr/bin/env bash
# Runs on the edge VM through Azure Run Command once the device is registered in IoT Hub:
# apply the staged provisioning config and wait for the IoT Edge runtime to come up.
set -euo pipefail

if [[ -f /etc/aziot/config.toml.pending ]]; then
  install -m 0600 /etc/aziot/config.toml.pending /etc/aziot/config.toml
  rm /etc/aziot/config.toml.pending
fi
iotedge config apply >/dev/null

for _ in $(seq 1 30); do
  if iotedge list >/dev/null 2>&1 && iotedge list | grep -q edgeAgent; then
    iotedge list
    echo "CONFIGURED $(iotedge version)"
    exit 0
  fi
  sleep 5
done
iotedge system status || true
echo "ERROR edgeAgent did not start"
exit 1
