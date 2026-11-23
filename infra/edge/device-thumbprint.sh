#!/usr/bin/env bash
# Runs on the edge VM through Azure Run Command: wait for first boot to finish, then print the
# device certificate's SHA-256 thumbprint (public; the private key stays on the device).
set -euo pipefail

cloud-init status --wait >/dev/null || true # exit code 2 means "done with warnings"
if [[ ! -f /var/lib/edge-ready ]]; then
  cloud-init status --long || true
  echo "ERROR first boot did not complete"
  exit 1
fi

echo "IOTEDGE $(iotedge version)"
fingerprint=$(openssl x509 -in /var/aziot/certs/device.cert.pem -noout -fingerprint -sha256)
echo "THUMBPRINT ${fingerprint#*=}" | tr -d ':'
