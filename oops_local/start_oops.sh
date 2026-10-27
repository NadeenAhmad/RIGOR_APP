#!/usr/bin/env bash
# Build (first time only) and start a local OOPS! server on http://localhost:8080.
# Then launch RIGOR with:  OOPS_ENDPOINT=http://localhost:8080/OOPS/rest python rigor_app.py
set -euo pipefail
cd "$(dirname "$0")"

IMAGE=rigor-oops-local
NAME=rigor-oops
PORT="${OOPS_PORT:-8080}"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "Building $IMAGE (first time only, downloads ~140 MB)..."
  docker build -t "$IMAGE" .
fi

docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" -p "$PORT:8080" "$IMAGE" >/dev/null

echo "Waiting for OOPS! to start..."
for _ in $(seq 1 60); do
  if curl -s -o /dev/null "http://localhost:$PORT/OOPS/"; then
    echo "OOPS! is running at http://localhost:$PORT/OOPS/rest"
    echo "Start RIGOR with:"
    echo "  OOPS_ENDPOINT=http://localhost:$PORT/OOPS/rest python rigor_app.py"
    exit 0
  fi
  sleep 2
done
echo "OOPS! did not start; see: docker logs $NAME" >&2
exit 1
