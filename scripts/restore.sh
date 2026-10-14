#!/usr/bin/env bash
# Brings the site back from a backup made by scripts/backup.sh: unpacks the
# project folder, loads its images (no downloads needed), puts Redis's dump, the
# tagger's database and the headline archive into fresh volumes, and starts the
# stack. With the Cloudflare Tunnel token in the restored .env, the site is
# online again at the same address. A copy of this script sits next to the backups.
#
#   restore.sh [archive.tar.zst] [target folder]
#
# Defaults: the latest archive next to this script, and ~/news-aggregator.
# PROJECT sets the compose project name (default news-aggregator). The target
# folder and the project's volumes must not exist yet: stop and remove the old
# stack first (docker compose down -v), or restore elsewhere.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ARCHIVE="$(readlink -f "${1:-$HERE/latest.tar.zst}")"
TARGET="${2:-$HOME/news-aggregator}"
PROJECT="${PROJECT:-news-aggregator}"
ARCHIVES="$(dirname "$ARCHIVE")"
IMAGES="$(dirname "$ARCHIVES")/images"

log() { echo "$(date -u +%FT%TZ) $*"; }
image_file() { echo "$(echo "$1" | tr '/:' '__')@${2:0:12}.tar.zst"; }  # Same naming as backup.sh

[ -s "$ARCHIVE" ] || { log "No archive at $ARCHIVE"; exit 1; }
[ ! -e "$TARGET" ] || [ -z "$(ls -A "$TARGET")" ] || { log "$TARGET already exists and isn't empty"; exit 1; }
for volume in redis_data tagger_data archive_data; do
    ! docker volume inspect "${PROJECT}_$volume" >/dev/null 2>&1 \
        || { log "Volume ${PROJECT}_$volume already exists (docker compose -p $PROJECT down -v removes it)"; exit 1; }
done

log "Checking $ARCHIVE"
(cd "$ARCHIVES" && sha256sum -c --quiet "$(basename "$ARCHIVE").sha256")
WORK="$(mktemp -d /var/tmp/news-aggregator-restore.XXXXXX)"  # Not /tmp: it can be RAM
trap 'rm -rf "$WORK"' EXIT
zstd -dc "$ARCHIVE" | tar -C "$WORK" -xf -
cat "$WORK/MANIFEST"

log "Unpacking the project into $TARGET"
mkdir -p "$TARGET"
tar -C "$TARGET" --strip-components=1 -xf "$WORK/project.tar"

log "Loading images"
while read -r image id; do
    if [ "$(docker image inspect "$image" -f '{{.Id}}' 2>/dev/null)" = "sha256:$id" ]; then
        continue  # Already here
    fi
    file="$IMAGES/$(image_file "$image" "$id")"
    if [ -s "$file" ]; then
        (cd "$IMAGES" && sha256sum -c --quiet "$(basename "$file").sha256")
        zstd -dc "$file" | docker load
    else
        log "Warning: $image isn't in the backup; compose will build or pull it"
    fi
done < "$WORK/images.txt"

log "Restoring Redis, the tagger's data and the archive"
helper="$(awk '$1 ~ /^redis:/ {print $1}' "$WORK/images.txt")"  # Any image with a shell will do; this one is restored above
volumes=(redis_data tagger_data)
[ ! -e "$WORK/data/archive.db" ] || volumes+=(archive_data)  # Backups older than the archive have none
for volume in "${volumes[@]}"; do
    docker volume create --label "com.docker.compose.project=$PROJECT" --label "com.docker.compose.volume=$volume" \
        "${PROJECT}_$volume" >/dev/null
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

log "Starting the stack"
cd "$TARGET"
docker compose -p "$PROJECT" up -d
for _ in $(seq 60); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose -p "$PROJECT" ps -q app)" 2>/dev/null)" = "healthy" ] && break
    sleep 5
done
docker compose -p "$PROJECT" ps --format '{{.Service}}: {{.Status}}'
log "Restored. The tagger takes a minute or two on first start (it compiles its models for the NPU)."
