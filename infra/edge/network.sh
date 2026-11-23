#!/usr/bin/env bash
# Runs on the edge VM through Azure Run Command: cut or restore the device's link to IoT Hub,
# like a site losing its uplink. Rules apply to the host (OUTPUT) and to containers (DOCKER-USER),
# and reset existing connections immediately. Run Command itself is unaffected.
#
#   network.sh offline|online <iothub-hostname>
set -euo pipefail

mode=$1
hub=$2
mapfile -t addresses < <(getent ahostsv4 "$hub" | awk '{print $1}' | sort -u)
[[ ${#addresses[@]} -gt 0 ]] || { echo "ERROR cannot resolve $hub"; exit 1; }

for chain in OUTPUT DOCKER-USER; do
  for address in "${addresses[@]}"; do
    rule=(-d "$address" -p tcp -j REJECT --reject-with tcp-reset)
    case $mode in
      offline) iptables -C "$chain" "${rule[@]}" 2>/dev/null || iptables -I "$chain" "${rule[@]}" ;;
      online) while iptables -D "$chain" "${rule[@]}" 2>/dev/null; do :; done ;;
      *) echo "ERROR mode must be offline or online"; exit 1 ;;
    esac
  done
done
echo "NETWORK $mode ${addresses[*]}"
