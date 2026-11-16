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
