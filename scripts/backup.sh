#!/usr/bin/env bash
# Backs up everything needed to bring the site back on another machine, as a
# dated, compressed archive in each destination (see "Backup and restore" in
# the README). Run daily from cron; restore with scripts/restore.sh.
#
#   scripts/backup.sh [destination ...]
#
# Each destination gets:
#   archives/news-aggregator-<UTC time>.tar.zst   the project folder (code, .git with unpushed
#                                                 commits, .env, untracked notes), Redis's dump,
#                                                 the tagger's database and classifier, the archive
#   archives/news-aggregator-<UTC time>.tar.zst.sha256
#   archives/news-aggregator-<UTC time>.images    the images that archive runs on
#   images/<image>@<id>.tar.zst                   each image version, saved once
#   restore.sh, latest.tar.zst (link), last-success, backup.log
# Keeps the newest KEEP archives per destination and the images they use.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DESTS=("$@")
[ ${#DESTS[@]} -gt 0 ] || DESTS=(/mnt/media/app_bkp/news_aggregator /mnt/mediatoshiba/app_bkp/news_aggregator)
KEEP="${KEEP:-14}"
STAGING_ROOT="${STAGING_ROOT:-${DESTS[0]}/.staging}"  # On the backup disk: /tmp can be RAM
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
NAME="news-aggregator-$STAMP"

log() { echo "$(date -u +%FT%TZ) $*"; }

# The destinations must already exist: if a disk isn't mounted, its folder is missing,
# and creating it would quietly fill the system disk instead
for dest in "${DESTS[@]}"; do
    [ -d "$dest" ] || { log "Destination $dest doesn't exist (is its disk mounted?)"; exit 1; }
done

mkdir -p "$STAGING_ROOT"
exec 9>"$STAGING_ROOT/.lock"
flock -n 9 || { log "Another backup is running"; exit 1; }

cd "$REPO"
compose() { docker compose "$@"; }
STAGE="$STAGING_ROOT/$NAME"
rm -rf "$STAGING_ROOT"/news-aggregator-*  # Left by an interrupted run
mkdir -p "$STAGE/data"
trap 'rm -rf "$STAGE"' EXIT

log "Backing up to ${DESTS[*]}"

# 1. Redis: a fresh snapshot (BGSAVE, then wait for LASTSAVE to move), then its file
before="$(compose exec -T redis redis-cli LASTSAVE)"
compose exec -T redis redis-cli BGSAVE >/dev/null
for _ in $(seq 120); do
    [ "$(compose exec -T redis redis-cli LASTSAVE)" != "$before" ] && break
    sleep 1
done
[ "$(compose exec -T redis redis-cli LASTSAVE)" != "$before" ] || { log "Redis didn't finish its snapshot"; exit 1; }
docker cp "$(compose ps -q redis)":/data/dump.rdb "$STAGE/data/dump.rdb"

# 2. Tagger: SQLite's online backup (consistent while in use), and the classifier
tagger="$(compose ps -q tagger)"
compose exec -T tagger python -c "
import sqlite3
src, dst = sqlite3.connect('/data/tagger.db'), sqlite3.connect('/data/backup-tagger.db')
src.backup(dst)
dst.close()
"
docker cp "$tagger":/data/backup-tagger.db "$STAGE/data/tagger.db"
docker cp "$tagger":/data/classifier.pkl "$STAGE/data/classifier.pkl"
compose exec -T tagger rm -f /data/backup-tagger.db

# 2b. The headline archive, the same way (a new install has none until its first refresh)
refresher="$(compose ps -q refresher)"
if compose exec -T refresher test -e /archive/archive.db; then
    compose exec -T refresher python -c "
import sqlite3
src, dst = sqlite3.connect('/archive/archive.db'), sqlite3.connect('/archive/backup-archive.db')
src.backup(dst)
dst.close()
"
    docker cp "$refresher":/archive/backup-archive.db "$STAGE/data/archive.db"
    compose exec -T refresher rm -f /archive/backup-archive.db
fi

# 3. The project folder as it is now (code, .git, .env, untracked notes), without caches
tar -C "$(dirname "$REPO")" --exclude='__pycache__' --exclude='.pytest_cache' \
    -cf "$STAGE/project.tar" "$(basename "$REPO")"

# 4. Image list (name and id) for this archive; each image is saved once below
images=()
while read -r image; do
    id="$(docker image inspect "$image" -f '{{.Id}}')"
    images+=("$image ${id#sha256:}")
done < <(compose config --images | sort -u)
printf '%s\n' "${images[@]}" > "$STAGE/images.txt"
{
    echo "created: $STAMP"
    echo "host: $(hostname)"
    echo "commit: $(git rev-parse HEAD) ($(git log -1 --format=%s))"
    echo "unpushed commits: $(git rev-list --count '@{u}..HEAD' 2>/dev/null || echo unknown)"
    echo "project: $(basename "$REPO") (compose project $(compose config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])'))"
} > "$STAGE/MANIFEST"

# 5. Archive, written to the first destination, verified, then copied to the others
first="${DESTS[0]}"
mkdir -p "$first/archives" "$first/images"
tar -C "$STAGE" -cf - . | zstd -q -T0 -10 -o "$first/archives/$NAME.tar.zst.part"
mv "$first/archives/$NAME.tar.zst.part" "$first/archives/$NAME.tar.zst"
(cd "$first/archives" && sha256sum "$NAME.tar.zst" > "$NAME.tar.zst.sha256")
cp "$STAGE/images.txt" "$first/archives/$NAME.images"

image_file() { echo "$(echo "$1" | tr '/:' '__')@${2:0:12}.tar.zst"; }  # name id
save_image() {  # name id destination-dir
    local file="$3/$(image_file "$1" "$2")"
    [ -s "$file" ] && return 0
    log "Saving image $1 (${2:0:12})"
    docker save "$1" | zstd -q -T0 -3 -o "$file.part"
    mv "$file.part" "$file"
    (cd "$3" && sha256sum "$(basename "$file")" > "$(basename "$file").sha256")
}
for entry in "${images[@]}"; do save_image ${entry} "$first/images"; done

for dest in "${DESTS[@]}"; do
    if [ "$dest" != "$first" ]; then
        mkdir -p "$dest/archives" "$dest/images"
        cp "$first/archives/$NAME".* "$dest/archives/"
        for entry in "${images[@]}"; do
            read -r image id <<< "$entry"
            file="$(image_file "$image" "$id")"
            [ -s "$dest/images/$file" ] || cp "$first/images/$file" "$first/images/$file.sha256" "$dest/images/"
        done
    fi
    (cd "$dest/archives" && sha256sum -c --quiet "$NAME.tar.zst.sha256") || { log "Checksum failed in $dest"; exit 1; }
    cp "$REPO/scripts/restore.sh" "$dest/restore.sh"
    ln -sfn "archives/$NAME.tar.zst" "$dest/latest.tar.zst"

    # Retention: the newest KEEP archives, and the images they use
    ls -1 "$dest/archives"/news-aggregator-*.tar.zst | sort | head -n "-$KEEP" | while read -r old; do
        rm -f "$old" "$old.sha256" "${old%.tar.zst}.images"
    done
    used="$(cat "$dest/archives"/*.images | while read -r i d; do echo "$(image_file "$i" "$d")"; done)"
    for file in "$dest/images"/*.tar.zst; do
        [ -e "$file" ] || continue
        grep -qxF "$(basename "$file")" <<< "$used" \
            || { log "Removing unused image $(basename "$file") from $dest"; rm -f "$file" "$file.sha256"; }
    done
    echo "$STAMP" > "$dest/last-success"
    log "Done: $dest/archives/$NAME.tar.zst ($(du -h "$dest/archives/$NAME.tar.zst" | cut -f1))"
done
