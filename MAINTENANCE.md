# Maintenance

What in this project is written by hand and ages, how often to look at it, and a monthly checklist. Two things run on their own (the `pi` user's crontab):

- **Backups**, daily at 03:30 UTC: `scripts/backup.sh` (README, "Backup and Restore"). Log: `/mnt/media/app_bkp/news_aggregator/backup.log`; date of the last good run in `last-success` next to it.
- **Update check**, Mondays 04:00 UTC: `scripts/check-updates.py` compares every pinned version (images, tools, the NPU driver, the browser identity, Python packages) with the latest release and writes `logs/updates.txt`. It reads the pins from the repo's files, so it needs no upkeep itself. Errors go to `logs/check-updates.errors.log`. Run it any time: `scripts/check-updates.py`.

## Monthly checklist (about 15 minutes)

1. Read `logs/updates.txt`. For each `>>` line older than two weeks, bump the pin (the file is named in brackets), test (`docker compose -p na-test …`, see README "Running Tests"), deploy, commit. Python packages: update `app/requirements.in` / regenerate `app/requirements.txt` with `pip-compile`, or `tagger/requirements.txt` from the tagger image.
2. Check the backup ran: `cat /mnt/media/app_bkp/news_aggregator/last-success` (today's date), no `FAILED` or `Warning` in `backup.log`, and the latest one restores: `/mnt/media/app_bkp/news_aggregator/restore.sh --check` (changes nothing, ~15 s).
3. Look for failing feeds: `docker compose logs --since 168h refresher | grep 'exhausted all retries'`. Fix or remove them in `config/sources.yaml` (quarterly review below).
4. Glance at the site's tabs for anything odd (empty tabs, the same story many times, a region showing another region's news).

## Deploying a change

1. Test it on an isolated copy first: `docker compose -p na-test -f docker-compose.yml -f <override giving na-test-* image names>`, on its own port (`CADDY_HTTP_PORT=17001`), never the live project. Leave out `cloudflared`, which would connect to the live tunnel.
2. Tag the running image before building: `docker tag news-aggregator-app:latest news-aggregator-app:rollback-<name>` (likewise for the tagger or Caddy when they change). Keep the newest **4** rollback tags per image; when a deploy makes a fifth, delete the oldest (`docker rmi news-aggregator-app:rollback-<oldest>`).
3. `docker compose build app && docker compose up -d --no-build app refresher summarizer` (the three share the app image), then check `/health`, a search and the logs.
4. To roll back: `docker tag news-aggregator-app:rollback-<name> news-aggregator-app:latest && docker compose up -d --no-build app refresher summarizer`.

## What's hand-written, and how often to review it

### Monthly: security updates (`scripts/check-updates.py` reports them)
| What | Where |
| --- | --- |
| Python app packages | `app/requirements.in` → `app/requirements.txt` |
| Base images: Python, Caddy, Redis | `app/Dockerfile`, `caddy/Dockerfile`, `docker-compose.yml` |
| FlareSolverr, Byparr (sites keep changing their bot checks) | `docker-compose.yml` |

### Quarterly: things on the internet change
| What | Where | Sign it needs attention |
| --- | --- | --- |
| The news and topic feeds | `config/sources.yaml` | Failing fetches (checklist step 3), a feed whose content changed (e.g. an outlet turned into an events guide), new outlets worth adding (TODO.md) |
| Browser identity sent with requests | `app/config.py` `BROWSER_USER_AGENT` | The update check reports it when it's more than two Chrome versions behind |
| Paywalled sites | `config/site.yaml` `paywalled_hosts` | Summaries failing for a site that added a paywall |
| The browsers' ad and tracker block list | `smokescreen/filter/acl.yaml`: run `scripts/update-blocklist.py`, then `docker compose restart browser-egress-filtered` | Ad networks change domains; sites that break without a blocked domain are retried with it on their own (`docker compose logs summarizer \| grep 'whole page'`) |
| Bot-check signals | `app/services/summaries/reading.py` `BLOCKING_STATUSES`, `BOT_CHECK_HEADERS` | Many unreadable articles from one site with a new status or header |

### Once or twice a year
| What | Where |
| --- | --- |
| Place list (which places belong to which region) | `config/regions.yaml` `places`: add a place when a story about it isn't routed to its region; add an ambiguous name to the left-out list when one misroutes |
| Country → starting region | `config/regions.yaml` `countries` |
| Tagger evaluation (598 hand-labelled headlines) and seed labels | `tagger/data/eval.json`, `tagger/data/seed.json`: add recent hand-labelled headlines so the retrain gate judges current news |
| Story-grouping labelled pairs | `tagger/data/story_pairs.json`: only to re-check the 0.92 threshold if grouping looks off |
| Heatmap places and outlets' countries | `stats/data/places.json`, `homes.json`, by `stats/build_places.py` (its docstring lists its inputs, and it needs an export for `homes.json`): a country wrongly placed goes in `LEFT_OUT` or `ALIASES`; new outlets get a country only when this is rebuilt |
| Heatmap models and colour scale | From the repo's root: `docker compose exec -T tagger python3 - < stats/export_tagger.py > export.jsonl`, then `python stats/label.py export.jsonl <count> <Ollama URL>` for new LLM labels (it shares the summarizer's Ollama, one request at a time), `python stats/train.py export.jsonl`, and set `BLUE`, `ORANGE` and `DOT_STRESS` in `stats/scoring.py`. Add hand-labelled headlines to `stats/data/eval.json` |
| Heatmap after retraining | Stored stress comes from the old model, so stop `stats`, delete its database and run the backfill below, or colours and the red-dot baseline mix two scales |
| Heatmap map grid | `caddy/static/stats/map.json`, by `stats/build_map.py`: only if countries change |
| Stats Python packages | `stats/requirements.txt` |
| NPU driver, Ubuntu base, models (topic, story grouping, summarizer) | `tagger/Dockerfile`, `.env` `SUMMARIZER_MODEL` |
| Smokescreen, Go, Alpine | `smokescreen/Dockerfile` |
| Tagger Python packages | `tagger/requirements.txt` |

### Only when something breaks

- **Heatmap database lost:** `stats_data` isn't backed up. From the repo's root: `docker compose stop stats`, `docker compose exec -T tagger python3 - < stats/export_tagger.py > export.jsonl`, `docker compose run --rm --no-deps -T -v "$PWD/export.jsonl:/tmp/export.jsonl:ro" stats python backfill.py /tmp/export.jsonl`, then `docker compose up -d --no-build stats`. Without it the red dots need 7 days to start.

| What | Where | Sign it broke |
| --- | --- | --- |
| Google link resolution | `app/services/links/google_news.py` | `redirect_fallbacks_total` rises, or headlines link to `news.google.com` |
| archive.ph | `app/services/summaries/reading.py` `ARCHIVE`, the page's bypass option | Archive reads fail, the bypass links stop opening |
| Site icon service (DuckDuckGo) | `app/config.py` `ICON_SOURCE_URL` | Icons stop appearing |
| Summary and same-story prompts | `config/prompts.yaml` | Summaries get worse, or the model changes |
| Tagger thresholds (topic 0.9, uplifting 0.8, grouping 0.92) | `tagger/classifier.py`, `tagger/stories.py` | Topic tabs or grouping visibly worse |
| Security policy, trusted proxies | `app/routes/frontend.py`, `caddy/Caddyfile` | When the setup in front of Caddy changes |
| Hostnames, backup paths and schedule | `.env` `ALLOWED_HOSTS`, crontab | When domains or disks change |

Tuning values (refresh interval, items per tab, story age, retry and backoff times, summary length, per-source caps) live in `app/config.py` and `config/sources.yaml`. They don't age; change them only to change behaviour.
