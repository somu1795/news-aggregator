#!/usr/bin/env bash
# Upgrades the two Cloudflare Tunnel connectors (services cloudflared and
# cloudflared-b in docker-compose.yml) one at a time, so the site stays up: the
# other connector serves while one restarts. Each is checked (its tunnel
# connections registered, the site answering) before the next is touched; one
# that fails is rolled back and the upgrade stops.
#
#   scripts/upgrade-cloudflared.sh 2026.11.0
#
# Then commit docker-compose.yml.
set -euo pipefail

VERSION="${1:?usage: $0 <cloudflared version, e.g. 2026.11.0>}"
[[ "$VERSION" =~ ^[0-9]{4}\.[0-9]+\.[0-9]+$ ]] || { echo "Not a cloudflared version: $VERSION"; exit 1; }
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
CONNECTIONS=4  # Each connector opens 4 connections to Cloudflare's edge
# The site's public hostnames: the non-local entries of ALLOWED_HOSTS in .env
SITES="$(grep -E '^ALLOWED_HOSTS=' .env | cut -d= -f2- | tr ',' '\n' | grep -vE '^(localhost|caddy|127\.0\.0\.1)$' || true)"

log() { echo "$(date -u +%FT%TZ) $*"; }

pinned() {  # service -> its pinned version
    awk -v svc="$1" '/^  [a-z0-9-]+:/ {in_svc = ($1 == svc":")} in_svc && /image: cloudflare\/cloudflared:/ {sub(/.*:/, ""); print; exit}' docker-compose.yml
}

pin() {  # service version
    awk -v svc="$1" -v version="$2" '
        /^  [a-z0-9-]+:/ {in_svc = ($1 == svc":")}
        in_svc && /image: cloudflare\/cloudflared:/ {sub(/cloudflared:.*/, "cloudflared:" version)}
        {print}' docker-compose.yml > docker-compose.yml.tmp
    mv docker-compose.yml.tmp docker-compose.yml
}

connected() {  # service -> 0 when its current container registered all its connections
    local id started
    id="$(docker compose ps -q "$1")"
    [ -n "$id" ] || return 1
    started="$(docker inspect -f '{{.State.StartedAt}}' "$id")"
    [ "$(docker logs --since "$started" "$id" 2>&1 | grep -c 'Registered tunnel connection')" -ge "$CONNECTIONS" ]
}

site_up() {  # Any of its hostnames answering through Cloudflare will do
    [ -z "$SITES" ] && return 0
    local site
    for site in $SITES; do
        [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "https://$site/health")" = "200" ] && return 0
    done
    return 1
}

wait_connected() {  # service
    for _ in $(seq 30); do
        connected "$1" && return 0
        sleep 2
    done
    return 1
}

log "Pulling cloudflare/cloudflared:$VERSION"
docker pull -q "cloudflare/cloudflared:$VERSION" >/dev/null

for service in cloudflared cloudflared-b; do
    other=$([ "$service" = cloudflared ] && echo cloudflared-b || echo cloudflared)
    previous="$(pinned "$service")"
    if [ "$previous" = "$VERSION" ]; then
        log "$service is already on $VERSION"
        continue
    fi
    connected "$other" || { log "$other isn't connected, so upgrading $service would take the site down; stopping"; exit 1; }

    log "Upgrading $service: $previous -> $VERSION ($other is serving)"
    pin "$service" "$VERSION"
    docker compose up -d "$service"
    if wait_connected "$service" && site_up; then
        log "$service is connected on $VERSION"
    else
        log "$service didn't connect on $VERSION; rolling it back to $previous"
        pin "$service" "$previous"
        docker compose up -d "$service"
        wait_connected "$service" || log "Warning: $service isn't connected on $previous either"
        exit 1
    fi
done
log "Both connectors are on $VERSION. Commit docker-compose.yml."
