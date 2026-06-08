#!/bin/bash

set -euo pipefail

IMAGE="${BARK_DOCKER_IMAGE:-finab/bark-server}"
CONTAINER_NAME="${BARK_DOCKER_CONTAINER_NAME:-jppost-bark}"
ADDR="0.0.0.0:8080"
DATA_DIR=""
FORWARD_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    -addr)
      ADDR="${2:?missing value for -addr}"
      shift 2
      ;;
    -data|-d)
      DATA_DIR="${2:?missing value for -data}"
      shift 2
      ;;
    *)
      FORWARD_ARGS+=("$1")
      shift
      ;;
  esac
done

HOST="${ADDR%:*}"
PORT="${ADDR##*:}"
if [[ "$HOST" == "$ADDR" ]]; then
  HOST="0.0.0.0"
fi

if [[ -z "$DATA_DIR" ]]; then
  DATA_DIR="$(pwd)/bark-data"
fi
mkdir -p "$DATA_DIR"

PUBLISH="${PORT}:8080"
if [[ "$HOST" != "0.0.0.0" ]]; then
  PUBLISH="${HOST}:${PORT}:8080"
fi

cleanup() {
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
}

trap cleanup EXIT INT TERM

cleanup

docker run \
  --rm \
  --name "$CONTAINER_NAME" \
  -p "$PUBLISH" \
  -v "$DATA_DIR:/data" \
  "$IMAGE" \
  "${FORWARD_ARGS[@]}" &

wait $!
