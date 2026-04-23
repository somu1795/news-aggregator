#!/usr/bin/env bash
# ============================================================================
# stop.sh — Gracefully stop the News Aggregator stack
#
# Handles:
#   - Containers already stopped (no-op, clean exit)
#   - Partial shutdowns (stops whatever is still running)
#   - Orphaned containers from previous configs
#   - Stuck containers (forceful kill after timeout)
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"
PROJECT_NAME="news-aggregator"
STOP_TIMEOUT="${STOP_TIMEOUT:-30}"   # Seconds to wait before force-killing

# --- Helpers ----------------------------------------------------------------

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log()  { echo -e "${GREEN}[stop]${NC} $*"; }
warn() { echo -e "${YELLOW}[stop]${NC} $*"; }
err()  { echo -e "${RED}[stop]${NC} $*" >&2; }

# Detect docker compose command (v2 plugin vs v1 standalone)
detect_compose() {
    if docker compose version &>/dev/null; then
        COMPOSE_CMD="docker compose"
    elif docker-compose version &>/dev/null; then
        COMPOSE_CMD="docker-compose"
    else
        err "Neither 'docker compose' nor 'docker-compose' found. Is Docker installed?"
        exit 1
    fi
}

# --- Main -------------------------------------------------------------------

detect_compose

cd "${SCRIPT_DIR}"

# Check if anything is running for this project
RUNNING=$(${COMPOSE_CMD} -f "${COMPOSE_FILE}" ps -q 2>/dev/null || true)

if [ -z "${RUNNING}" ]; then
    log "No containers are running for project '${PROJECT_NAME}'. Nothing to do."
    exit 0
fi

RUNNING_COUNT=$(echo "${RUNNING}" | wc -l | tr -d ' ')
log "Stopping ${RUNNING_COUNT} container(s) (timeout: ${STOP_TIMEOUT}s)..."

# Attempt graceful shutdown
if ${COMPOSE_CMD} -f "${COMPOSE_FILE}" down --timeout "${STOP_TIMEOUT}" --remove-orphans 2>&1; then
    log "All containers stopped and removed successfully."
else
    warn "Graceful shutdown returned an error. Checking for stuck containers..."

    # Force-kill anything still running
    STUCK=$(${COMPOSE_CMD} -f "${COMPOSE_FILE}" ps -q 2>/dev/null || true)
    if [ -n "${STUCK}" ]; then
        warn "Force-killing stuck containers..."
        echo "${STUCK}" | xargs -r docker rm -f 2>/dev/null || true
        log "Stuck containers removed."
    fi
fi

# Final verification
REMAINING=$(${COMPOSE_CMD} -f "${COMPOSE_FILE}" ps -q 2>/dev/null || true)
if [ -z "${REMAINING}" ]; then
    log "✓ All containers are stopped."
else
    err "✗ Some containers are still running:"
    ${COMPOSE_CMD} -f "${COMPOSE_FILE}" ps
    exit 1
fi
