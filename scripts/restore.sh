#!/usr/bin/env bash
# Brings the site back from a backup made by scripts/backup.sh: unpacks the
# project folder, loads its images (no downloads needed), puts Redis's dump, the
# tagger's database and the headline archive into fresh volumes, and starts the
# stack. A copy of this script sits next to the backups.
#
#   restore.sh --check [archive.tar.zst]          verify a backup, change nothing
#   restore.sh [archive.tar.zst] [target folder]  restore it
#
# Defaults: the latest archive next to this script, and ~/news-aggregator.
# PROJECT sets the compose project name (default news-aggregator). The target
# folder and the project's volumes must not exist yet: check the backup first,
# then stop and remove the old stack (docker compose down -v), or restore elsewhere.
#
# The stack starts without the Cloudflare Tunnel, so a restore (a test, or a new
# machine while the old one still runs) never takes visitors before it's checked;
# the last lines say how to put it online. ONLINE=1 starts the tunnel at once.
# Without an Intel NPU (/dev/accel), the tagger is started on the CPU
# (docker-compose.override.yml in the target folder). If the restore fails, what it
# created (folder, volumes, containers) is removed again, so it can simply be rerun.
set -euo pipefail

CHECK=0
if [ "${1:-}" = "--check" ]; then CHECK=1; shift; fi
HERE="$(cd "$(dirname "$0")" && pwd)"
ARCHIVE="$(readlink -f "${1:-$HERE/latest.tar.zst}")"
TARGET="${2:-$HOME/news-aggregator}"
PROJECT="${PROJECT:-news-aggregator}"
ARCHIVES="$(dirname "$ARCHIVE")"
IMAGES="$(dirname "$ARCHIVES")/images"

log() { echo "$(date -u +%FT%TZ) $*"; }
image_file() { echo "$(echo "$1" | tr '/:' '__')@${2:0:12}.tar.zst"; }  # Same naming as backup.sh
intact() {  # Same check as backup.sh
    python3 -c "import sqlite3, sys; sys.exit(sqlite3.connect('file:' + sys.argv[1] + '?mode=ro', uri=True).execute('PRAGMA quick_check').fetchone()[0] != 'ok')" "$1"
}

[ -s "$ARCHIVE" ] || { log "No archive at $ARCHIVE"; exit 1; }
if [ "$CHECK" = 0 ]; then
    [ ! -e "$TARGET" ] || [ -z "$(ls -A "$TARGET")" ] || { log "$TARGET already exists and isn't empty"; exit 1; }
    for volume in redis_data tagger_data archive_data; do
        ! docker volume inspect "${PROJECT}_$volume" >/dev/null 2>&1 \
            || { log "Volume ${PROJECT}_$volume already exists (docker compose -p $PROJECT down -v removes it)"; exit 1; }
    done
fi

# What this run has created, removed again if it fails
target_was=""  # "missing" or "empty", once this run has put files in it
created_volumes=()
started=0
finished=0
WORK=""
cleanup() {
    [ -z "$WORK" ] || rm -rf "$WORK"
    [ "$finished" = 0 ] && [ "$CHECK" = 0 ] || return 0
    [ -n "$target_was" ] || [ ${#created_volumes[@]} -gt 0 ] || [ "$started" = 1 ] || return 0
    log "Restore failed: removing what it created"
    if [ "$started" = 1 ]; then
        (cd "$TARGET" && docker compose -p "$PROJECT" down -v --remove-orphans) || true
    fi
    for volume in "${created_volumes[@]}"; do docker volume rm -f "$volume" >/dev/null 2>&1 || true; done
    case "$target_was" in
        missing) rm -rf "$TARGET" ;;
        empty) find "$TARGET" -mindepth 1 -delete ;;
    esac
}
trap cleanup EXIT

log "Checking $ARCHIVE"
(cd "$ARCHIVES" && sha256sum -c --quiet "$(basename "$ARCHIVE").sha256")
WORK="$(mktemp -d /var/tmp/news-aggregator-restore.XXXXXX)"  # Not /tmp: it can be RAM
zstd -dc "$ARCHIVE" | tar -C "$WORK" -xf -
cat "$WORK/MANIFEST"
for db in tagger.db archive.db; do
    [ ! -e "$WORK/data/$db" ] || intact "$WORK/data/$db" || { log "$db in the backup is damaged"; exit 1; }
done

missing=0
while read -r image id; do
    if [ "$(docker image inspect "$image" -f '{{.Id}}' 2>/dev/null)" = "sha256:$id" ]; then
        continue  # Already here
    fi
    file="$IMAGES/$(image_file "$image" "$id")"
    if [ -s "$file" ]; then
        (cd "$IMAGES" && sha256sum -c --quiet "$(basename "$file").sha256")
        [ "$CHECK" = 1 ] || zstd -dc "$file" | docker load
    else
        log "Warning: $image isn't in the backup; compose will build or pull it"
        missing=$((missing + 1))
    fi
done < "$WORK/images.txt"

if [ "$CHECK" = 1 ]; then
    log "Backup OK: checksums match, the databases are intact, $missing image(s) would be built or pulled"
    exit 0
fi

log "Unpacking the project into $TARGET"
if [ -e "$TARGET" ]; then target_was=empty; else target_was=missing; fi
mkdir -p "$TARGET"
tar -C "$TARGET" --strip-components=1 -xf "$WORK/project.tar"

log "Restoring Redis, the tagger's data and the archive"
helper="$(awk '$1 ~ /^redis:/ {print $1}' "$WORK/images.txt")"  # Any image with a shell will do; this one is restored above
volumes=(redis_data tagger_data)
[ ! -e "$WORK/data/archive.db" ] || volumes+=(archive_data)  # Backups older than the archive have none
for volume in "${volumes[@]}"; do
    docker volume create --label "com.docker.compose.project=$PROJECT" --label "com.docker.compose.volume=$volume" \
        "${PROJECT}_$volume" >/dev/null
    created_volumes+=("${PROJECT}_$volume")
done
docker run --rm --entrypoint sh -v "${PROJECT}_redis_data:/data" -v "$WORK/data:/backup:ro" "$helper" \
    -c 'cp /backup/dump.rdb /data/ && chown redis:redis /data/dump.rdb'
docker run --rm --entrypoint sh -v "${PROJECT}_tagger_data:/data" -v "$WORK/data:/backup:ro" "$helper" \
    -c 'cp /backup/tagger.db /backup/classifier.pkl /data/ && chown -R 1001:1001 /data'
if [ -e "$WORK/data/archive.db" ]; then  # Optional: the site starts without it
    docker run --rm --entrypoint sh -v "${PROJECT}_archive_data:/archive" -v "$WORK/data:/backup:ro" "$helper" \
        -c 'cp /backup/archive.db /archive/ && chown -R 1001:1001 /archive' \
        || log "Warning: the headline archive wasn't restored; the site starts without it"
fi

cd "$TARGET"
if [ ! -e /dev/accel ] && [ ! -e docker-compose.override.yml ]; then
    log "No Intel NPU here (/dev/accel): the tagger runs on the CPU (docker-compose.override.yml)"
    cat > docker-compose.override.yml <<'EOF'
# Written by restore.sh on a machine without an Intel NPU: the tagger on the CPU
services:
  tagger:
    devices: !reset []
    group_add: !reset []
    environment: [TAGGER_DEVICE=CPU]
EOF
fi

log "Starting the stack${ONLINE:+, with the Cloudflare Tunnel}"
started=1
if [ -n "${ONLINE:-}" ]; then
    docker compose -p "$PROJECT" up -d
else
    COMPOSE_PROFILES='' docker compose -p "$PROJECT" up -d  # Not the tunnel (.env's COMPOSE_PROFILES)
fi
finished=1  # From here on, a slow start isn't a failed restore
for _ in $(seq 60); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose -p "$PROJECT" ps -q app)" 2>/dev/null)" = "healthy" ] && break
    sleep 5
done
docker compose -p "$PROJECT" ps --format '{{.Service}}: {{.Status}}'
[ "$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose -p "$PROJECT" ps -q app)" 2>/dev/null)" = "healthy" ] \
    || log "Warning: the app isn't healthy after 5 minutes: docker compose -p $PROJECT logs app"
log "Restored. The tagger takes a minute or two on first start (it compiles its models for the NPU)."
if [ -z "${ONLINE:-}" ]; then
    log "Not online yet: check the site on this machine (port ${CADDY_HTTP_PORT:-from .env}), then put it online with:"
    log "  cd $TARGET && docker compose -p $PROJECT up -d"
fi
