#!/usr/bin/env bash
# Start a local Aerospike Community Edition container for running the
# strands-aerospike test suite (see ../tests/), and wait until it is ready.
#
# Usage: ./scripts/start_aerospike_ce.sh [container-name]

set -euo pipefail

CONTAINER_NAME="${1:-strands-aerospike-ce}"
IMAGE="aerospike/aerospike-server:latest"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The image's default "test" namespace caps storage at 4GB (STORAGE_GB) with 1GB of
# primary-index memory (MEM_GB) -- too small for benchmark/benchmark.py's 10GB "bulk" scale
# tier. Additionally, the default write-flush-queue depth (max-write-cache, 64MB) is too small
# to absorb the backlog from a 10GB bulk-tier seed burst, causing DeviceOverload on subsequent
# writes even after seeding completes. Override all three here; raise further via the
# environment (e.g. STORAGE_GB=120 MEM_GB=4 MAX_WRITE_CACHE_MB=1024 ./scripts/start_aerospike_ce.sh)
# when --bulk-target-gb goes up.
STORAGE_GB="${STORAGE_GB:-20}"
MEM_GB="${MEM_GB:-2}"
MAX_WRITE_CACHE_MB="${MAX_WRITE_CACHE_MB:-512}"

# Optional: point the "test" namespace at a raw block device instead of the image's
# file-backed default (STORAGE_GB/filesize above). Device mode skips filesystem overhead
# and avoids double-buffering through the host's page cache -- closer to what a production
# deployment sees, which matters for benchmark/ numbers more than for correctness tests.
# Off by default; set AEROSPIKE_DEVICE=/dev/xvdb (e.g. AEROSPIKE_DEVICE=/dev/nvme1n1
# ./scripts/start_aerospike_ce.sh) to opt in. On AWS Nitro-based EC2 instances, a volume
# attached at /dev/sdX or /dev/xvdX is renamed to /dev/nvmeYn1 by the kernel -- the
# requested name never appears under /dev, so confirm the real path first with
# `lsblk` (match by size) or `ls -la /dev/disk/by-id/ | grep Elastic_Block_Store`
# (match by the attachment's volume ID) rather than assuming the requested name.
AEROSPIKE_DEVICE="${AEROSPIKE_DEVICE:-}"

if [ -n "${AEROSPIKE_DEVICE}" ]; then
  if [ ! -e "${AEROSPIKE_DEVICE}" ]; then
    echo "AEROSPIKE_DEVICE=${AEROSPIKE_DEVICE} does not exist." >&2
    exit 1
  fi
  # Aerospike's device storage-engine writes raw blocks with no filesystem underneath.
  # Pointing it at a partition that already holds a filesystem (including the host's own
  # root or boot partition) silently and irrecoverably destroys whatever was there -- there
  # is no "are you sure" prompt. Refuse rather than risk it; treat this like mkfs.
  existing_fstype="$(blkid -o value -s TYPE "${AEROSPIKE_DEVICE}" 2>/dev/null || true)"
  if [ -n "${existing_fstype}" ]; then
    echo "Refusing to use ${AEROSPIKE_DEVICE}: it already has a filesystem (${existing_fstype})." >&2
    exit 1
  fi
  if mount | grep -q "^${AEROSPIKE_DEVICE} "; then
    echo "Refusing to use ${AEROSPIKE_DEVICE}: it is currently mounted." >&2
    exit 1
  fi
  echo "Using raw block device ${AEROSPIKE_DEVICE} for the 'test' namespace's storage-engine."
fi

if docker ps -a --format '{{.Names}}' | grep -qx "${CONTAINER_NAME}"; then
  echo "Removing existing container '${CONTAINER_NAME}'..."
  docker rm -f "${CONTAINER_NAME}" >/dev/null
fi

DOCKER_DEVICE_ARGS=()
if [ -n "${AEROSPIKE_DEVICE}" ]; then
  DOCKER_DEVICE_ARGS=(--device="${AEROSPIKE_DEVICE}:${AEROSPIKE_DEVICE}")
fi

echo "Starting Aerospike CE container '${CONTAINER_NAME}'..."
docker run -d \
  --name "${CONTAINER_NAME}" \
  --ulimit nofile=15000:15000 \
  -e STORAGE_GB="${STORAGE_GB}" \
  -e MEM_GB="${MEM_GB}" \
  -e AEROSPIKE_DEVICE="${AEROSPIKE_DEVICE}" \
  -v "${SCRIPT_DIR}/aerospike.template.conf:/etc/aerospike/aerospike.template.conf" \
  -p 3000-3002:3000-3002 \
  "${DOCKER_DEVICE_ARGS[@]}" \
  "${IMAGE}"

echo "Waiting for Aerospike to report ready..."
for _ in $(seq 1 60); do
  if docker exec "${CONTAINER_NAME}" asinfo -v status 2>/dev/null | grep -q ok; then
    echo "Aerospike is ready on 127.0.0.1:3000 (namespace: test)."

    # Configure max-write-cache (write-flush-queue depth) to prevent DeviceOverload during
    # bulk-tier write bursts. This must be set via asinfo after the server is up, not via
    # environment variables.
    docker exec "${CONTAINER_NAME}" asinfo -v "set-config:context=namespace;id=test;max-write-cache=$((MAX_WRITE_CACHE_MB * 1024 * 1024))" | grep -q ok || {
      echo "Failed to set max-write-cache on the test namespace." >&2
      exit 1
    }

    exit 0
  fi
  sleep 1
done

echo "Timed out waiting for Aerospike to become ready. Container logs:" >&2
docker logs "${CONTAINER_NAME}" >&2
exit 1
