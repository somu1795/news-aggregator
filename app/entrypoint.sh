#!/bin/sh
set -e

# --- Dynamic Worker Configuration ---
# If UVICORN_WORKERS is set in the .env file, use that value.
# Otherwise, calculate an optimal number of workers based on available CPU cores.
# The formula (2 * cores) + 1 is a common recommendation for Gunicorn/Uvicorn.
if [ -n "$UVICORN_WORKERS" ]; then
    WORKERS="$UVICORN_WORKERS"
    echo "Using specified worker count from environment: $WORKERS"
else
    CPU_CORES=$(nproc)
    WORKERS=$((2 * CPU_CORES + 1))
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
    --log-config logging.conf
