#!/usr/bin/env bash
# ============================================================================
# start.sh — Build and start the News Aggregator stack
#
# Handles:
#   - Pre-flight checks (.env, Docker, disk space)
#   - Stopping already-running containers (idempotent restarts)
#   - Building images with error handling
#   - Waiting for health checks to pass
#   - Rollback logging on failure
#
# Usage:
#   ./start.sh              # Normal start (build + up)
#   ./start.sh --no-build   # Start without rebuilding images
#   ./start.sh --force      # Force full rebuild (no cache)
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"
ENV_FILE="${SCRIPT_DIR}/.env"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-120}"   # Seconds to wait for healthy status
STOP_TIMEOUT="${STOP_TIMEOUT:-30}"

# --- Parse Arguments --------------------------------------------------------

BUILD_FLAG="--build"
NO_BUILD=false
FORCE_BUILD=false

for arg in "$@"; do
    case "${arg}" in
        --no-build)  NO_BUILD=true;  BUILD_FLAG="" ;;
        --force)     FORCE_BUILD=true ;;
        -h|--help)
            echo "Usage: $0 [--no-build] [--force]"
            echo "  --no-build   Skip image build, start existing images"
            echo "  --force      Force full rebuild with no Docker cache"
            exit 0
            ;;
        *)
            echo "Unknown argument: ${arg}. Use --help for usage."
            exit 1
            ;;
    esac
done

# --- Helpers ----------------------------------------------------------------

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${GREEN}[start]${NC} $*"; }
warn() { echo -e "${YELLOW}[start]${NC} $*"; }
err()  { echo -e "${RED}[start]${NC} $*" >&2; }
info() { echo -e "${CYAN}[start]${NC} $*"; }

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

# --- Pre-flight Checks ------------------------------------------------------

preflight() {
    log "Running pre-flight checks..."

    # 1. Docker daemon
    if ! docker info &>/dev/null; then
        err "Docker daemon is not running. Start it first."
        exit 1
    fi

    # 2. .env file
    if [ ! -f "${ENV_FILE}" ]; then
        err ".env file not found at ${ENV_FILE}"
        err "Copy .env.example to .env and configure it: cp .env.example .env"
        exit 1
    fi

    # 3. Required variables in .env
    local missing=()
    grep -q "^ALLOWED_HOSTS=" "${ENV_FILE}" || missing+=("ALLOWED_HOSTS")
    grep -q "^ADMIN_API_KEY=" "${ENV_FILE}" || missing+=("ADMIN_API_KEY")
    if [ ${#missing[@]} -gt 0 ]; then
        err "Missing required variables in .env: ${missing[*]}"
        exit 1
    fi

    # 4. Check for default/insecure API key
    if grep -q "^ADMIN_API_KEY=changeme$" "${ENV_FILE}"; then
        warn "⚠ ADMIN_API_KEY is set to 'changeme'. Generate a secure key:"
        warn "  openssl rand -hex 32"
    fi

    # 5. Compose file exists
    if [ ! -f "${COMPOSE_FILE}" ]; then
        err "docker-compose.yml not found at ${COMPOSE_FILE}"
        exit 1
    fi

    # 6. Disk space (warn if < 1 GB free)
    local free_kb
    free_kb=$(df "${SCRIPT_DIR}" --output=avail | tail -1 | tr -d ' ')
    if [ "${free_kb}" -lt 1048576 ]; then
        warn "⚠ Less than 1 GB of disk space free. Builds may fail."
    fi

    log "Pre-flight checks passed."
}

# --- Main -------------------------------------------------------------------

detect_compose
cd "${SCRIPT_DIR}"
preflight

# Stop any already-running containers (idempotent restart)
RUNNING=$(${COMPOSE_CMD} -f "${COMPOSE_FILE}" ps -q 2>/dev/null || true)
if [ -n "${RUNNING}" ]; then
    log "Existing containers detected. Stopping them first..."
    if [ -x "${SCRIPT_DIR}/stop.sh" ]; then
        "${SCRIPT_DIR}/stop.sh"
    else
        ${COMPOSE_CMD} -f "${COMPOSE_FILE}" down --timeout "${STOP_TIMEOUT}" --remove-orphans 2>/dev/null || true
    fi
fi

# Build
if [ "${NO_BUILD}" = false ]; then
    if [ "${FORCE_BUILD}" = true ]; then
        log "Building images (forced, no cache)..."
        ${COMPOSE_CMD} -f "${COMPOSE_FILE}" build --no-cache --pull
    else
        log "Building images..."
        ${COMPOSE_CMD} -f "${COMPOSE_FILE}" build
    fi
fi

# Start
log "Starting services..."
${COMPOSE_CMD} -f "${COMPOSE_FILE}" up -d ${BUILD_FLAG:+}

# Wait for health checks
log "Waiting for services to become healthy (timeout: ${HEALTH_TIMEOUT}s)..."

ELAPSED=0
INTERVAL=5
ALL_HEALTHY=false

while [ "${ELAPSED}" -lt "${HEALTH_TIMEOUT}" ]; do
    # Parse health from plain-text output (works across all compose versions)
    PS_OUTPUT=$(${COMPOSE_CMD} -f "${COMPOSE_FILE}" ps 2>/dev/null || true)
    HEALTHY_COUNT=$(echo "${PS_OUTPUT}" | grep -c "(healthy)" || true)

    # Check for any exited/restarting containers (early failure detection)
    FAILED=$(echo "${PS_OUTPUT}" | grep -cE "Exited|Restarting" || true)
    if [ "${FAILED}" -gt 0 ]; then
        err "One or more containers have exited or are restarting:"
        ${COMPOSE_CMD} -f "${COMPOSE_FILE}" ps
        echo ""
        err "Logs from failed containers:"
        ${COMPOSE_CMD} -f "${COMPOSE_FILE}" logs --tail 30
        exit 1
    fi

    # app + redis = 2 services with healthchecks
    if [ "${HEALTHY_COUNT}" -ge 2 ]; then
        ALL_HEALTHY=true
        break
    fi

    info "  ${HEALTHY_COUNT} service(s) healthy... (${ELAPSED}s elapsed)"
    sleep "${INTERVAL}"
    ELAPSED=$((ELAPSED + INTERVAL))
done

echo ""
if [ "${ALL_HEALTHY}" = true ]; then
    log "✓ All services are up and healthy!"
else
    warn "Health check timeout reached. Some services may still be starting."
fi

# Show final status
${COMPOSE_CMD} -f "${COMPOSE_FILE}" ps
echo ""

# Show access info
APP_PORT=$(grep "^CADDY_HTTP_PORT=" "${ENV_FILE}" 2>/dev/null | cut -d= -f2 || echo "7001")
APP_PORT="${APP_PORT:-7001}"
log "Application available at: http://localhost:${APP_PORT}"
