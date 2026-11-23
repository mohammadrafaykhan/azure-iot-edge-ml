#!/usr/bin/env bash
# Runs on the edge VM through Azure Run Command. The VM is also the image builder: it downloads the
# build context from Blob storage and pushes to the registry, both with its managed identity, then
# prints the pushed digests for the deployment manifest.
#
#   build-images.sh <storage-account> <context-blob> <registry-login-server> <model>
set -euo pipefail

storage=$1
blob=$2
registry=$3
model=$4
[[ $blob =~ ^context-([0-9a-f]{12})\.tar\.gz$ ]] || { echo "ERROR unexpected blob name $blob"; exit 1; }
source_hash=${BASH_REMATCH[1]}
[[ $model =~ ^v[0-9]+$ ]] || { echo "ERROR model must look like v1"; exit 1; }

token() {
  curl -fsS -H Metadata:true \
    "http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=$1" |
    jq -r .access_token
}

work=$(mktemp -d)
cleanup() {
  docker logout "$registry" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

curl -fsS -H "Authorization: Bearer $(token https://storage.azure.com/)" -H "x-ms-version: 2023-11-03" \
  "https://$storage.blob.core.windows.net/build/$blob" -o "$work/context.tar.gz"
mkdir "$work/src"
tar -xzf "$work/context.tar.gz" -C "$work/src"

# Exchange the identity's Entra token for a registry refresh token and log in with it.
entra=$(token https://containerregistry.azure.net/)
refresh=$(curl -fsS -X POST "https://$registry/oauth2/exchange" \
  --data-urlencode grant_type=access_token --data-urlencode "service=$registry" \
  --data-urlencode "access_token=$entra" | jq -r .refresh_token)
[[ -n $refresh && $refresh != null ]] || { echo "ERROR registry token exchange failed"; exit 1; }
printf '%s' "$refresh" | docker login "$registry" --username 00000000-0000-0000-0000-000000000000 --password-stdin >/dev/null

# Tags name the source (and model), so an image already in the registry is reused as-is: a rebuild
# would get a new digest and needlessly restart that module on the device.
build() {
  local module=$1 reference=$2
  shift 2
  if ! docker pull --quiet "$reference" >/dev/null 2>&1; then
    docker build --quiet --file "$work/src/modules/$module/Dockerfile" --tag "$reference" "$@" "$work/src" >/dev/null
    docker push --quiet "$reference" >/dev/null
  fi
  docker inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$reference" |
    grep -m1 "^$registry/edge-ml/$module@sha256:"
}

sensor=$(build press-sensor "$registry/edge-ml/press-sensor:$source_hash")
detector=$(build anomaly-detector "$registry/edge-ml/anomaly-detector:$source_hash-model-$model" --build-arg "MODEL=$model")
docker image prune --force >/dev/null

printf 'IMAGES {"press-sensor":"%s","anomaly-detector":"%s","model":"%s","source":"%s"}\n' \
  "$sensor" "$detector" "$model" "$source_hash"
