#!/bin/sh
set -e

# --- Dynamic Worker Configuration ---
# If UVICORN_WORKERS is set in the .env file, use that value.
# Otherwise, use one worker per CPU core. Requests are served from an in-memory
# snapshot, so the app is not I/O-bound and extra workers only add memory.
if [ -n "$UVICORN_WORKERS" ]; then
    WORKERS="$UVICORN_WORKERS"
    echo "Using specified worker count from environment: $WORKERS"
else
    CPU_CORES=$(nproc)
    WORKERS="$CPU_CORES"
    echo "Dynamically calculated worker count: $WORKERS ($CPU_CORES cores)"
fi

# --- Prometheus multi-process metrics ---
# Each worker writes metric files here; /metrics aggregates them. Clear files
# left by a previous run so counters from dead processes don't linger.
if [ -n "$PROMETHEUS_MULTIPROC_DIR" ]; then
    rm -rf "$PROMETHEUS_MULTIPROC_DIR"
    mkdir -p "$PROMETHEUS_MULTIPROC_DIR"
fi

# Start the application
echo "Starting uvicorn..."
exec uvicorn main:app \
    --host 0.0.0.0 \
    --port 8989 \
    --workers "$WORKERS" \
    --loop uvloop \
    --http httptools \
    --proxy-headers \
    --forwarded-allow-ips='*'
