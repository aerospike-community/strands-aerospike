#!/usr/bin/env bash
# Start a local Aerospike Community Edition container for running the
# strands-aerospike test suite (see ../tests/), and wait until it is ready.
#
# Usage: ./scripts/start_aerospike_ce.sh [container-name]

set -euo pipefail

CONTAINER_NAME="${1:-strands-aerospike-ce}"
IMAGE="aerospike/aerospike-server:latest"

if docker ps -a --format '{{.Names}}' | grep -qx "${CONTAINER_NAME}"; then
  echo "Removing existing container '${CONTAINER_NAME}'..."
  docker rm -f "${CONTAINER_NAME}" >/dev/null
fi

echo "Starting Aerospike CE container '${CONTAINER_NAME}'..."
docker run -d \
  --name "${CONTAINER_NAME}" \
  --ulimit nofile=15000:15000 \
  -p 3000-3002:3000-3002 \
  "${IMAGE}"

echo "Waiting for Aerospike to report ready..."
for _ in $(seq 1 60); do
  if docker exec "${CONTAINER_NAME}" asinfo -v status 2>/dev/null | grep -q ok; then
    echo "Aerospike is ready on 127.0.0.1:3000 (namespace: test)."
    exit 0
  fi
  sleep 1
done

echo "Timed out waiting for Aerospike to become ready. Container logs:" >&2
docker logs "${CONTAINER_NAME}" >&2
exit 1
