# Maintenance

What in this project is written by hand and ages, how often to look at it, and a monthly checklist. Two things run on their own (the `pi` user's crontab):

- **Backups**, daily at 03:30 UTC: `scripts/backup.sh` (README, "Backup and Restore"). Log: `/mnt/media/app_bkp/news_aggregator/backup.log`; date of the last good run in `last-success` next to it.
- **Update check**, Mondays 04:00 UTC: `scripts/check-updates.py` compares every pinned version (images, tools, the NPU driver, the browser identity, Python packages) with the latest release and writes `logs/updates.txt`. It reads the pins from the repo's files, so it needs no upkeep itself. Errors go to `logs/check-updates.errors.log`. Run it any time: `scripts/check-updates.py`.

## Monthly checklist (about 15 minutes)

1. Read `logs/updates.txt`. For each `>>` line older than two weeks, bump the pin (the file is named in brackets), test (`docker compose -p na-test …`, see README "Running Tests"), deploy, commit. Python packages: update `app/requirements.in` / regenerate `app/requirements.txt` with `pip-compile`, or `tagger/requirements.txt` from the tagger image.
2. Check the backup ran: `cat /mnt/media/app_bkp/news_aggregator/last-success` (today's date), and no `FAILED` in `backup.log`.
3. Look for failing feeds: `docker compose logs --since 168h refresher | grep 'exhausted all retries'`. Fix or remove them in `app/sources.json` (quarterly review below).
4. Glance at the site's tabs for anything odd (empty tabs, the same story many times, a region showing another region's news).

## What's hand-written, and how often to review it

### Monthly: security updates (`scripts/check-updates.py` reports them)
| What | Where |
| --- | --- |
| cloudflared | `docker-compose.yml` |
| Python app packages | `app/requirements.in` → `app/requirements.txt` |
| Base images: Python, Caddy, Redis | `app/Dockerfile`, `caddy/Dockerfile`, `docker-compose.yml` |
| FlareSolverr, Byparr (sites keep changing their bot checks) | `docker-compose.yml` |

### Quarterly: things on the internet change
| What | Where | Sign it needs attention |
| --- | --- | --- |
| The news and topic feeds | `app/sources.json` | Failing fetches (checklist step 3), a feed whose content changed (e.g. an outlet turned into an events guide), new outlets worth adding (TODO.md) |
| Browser identity sent with requests | `app/config.py` `BROWSER_USER_AGENT` | The update check reports it when it's more than two Chrome versions behind |
| Paywalled sites | `app/config.py` `PAYWALLED_HOSTS` | Summaries failing for a site that added a paywall |
| Bot-check signals | `app/summarizer.py` `BLOCKING_STATUSES`, `BOT_CHECK_HEADERS` | Many unreadable articles from one site with a new status or header |

### Once or twice a year
| What | Where |
| --- | --- |
| Place list (which places belong to which region) | `app/services/places.py`: add a place when a story about it isn't routed to its region; add an ambiguous name to the left-out list when one misroutes |
| Country → starting region | `app/services/geo.py` |
| Tagger evaluation (598 hand-labelled headlines) and seed labels | `tagger/data/eval.json`, `tagger/data/seed.json`: add recent hand-labelled headlines so the retrain gate judges current news |
| Story-grouping labelled pairs | `tagger/data/story_pairs.json`: only to re-check the 0.92 threshold if grouping looks off |
| NPU driver, Ubuntu base, models (topic, story grouping, summarizer) | `tagger/Dockerfile`, `.env` `SUMMARIZER_MODEL` |
| Smokescreen, Go, Alpine | `smokescreen/Dockerfile` |
| Tagger Python packages | `tagger/requirements.txt` |

### Only when something breaks
| What | Where | Sign it broke |
| --- | --- | --- |
| Google link resolution | `app/services/redirect.py`, `app/services/link_resolver.py` | `redirect_fallbacks_total` rises, or headlines link to `news.google.com` |
| archive.ph | `app/summarizer.py` `ARCHIVE`, the page's bypass option | Archive reads fail, the bypass links stop opening |
| Site icon service (DuckDuckGo) | `app/config.py` `ICON_SOURCE_URL` | Icons stop appearing |
| Google Fonts on error pages | `app/templates/404.html`, `500.html`, `error.html` | Error pages lose their font |
| Summary prompt | `app/summarizer.py` `PROMPT` | Summaries get worse, or the model changes |
| Tagger thresholds (topic 0.9, uplifting 0.8, grouping 0.92) | `tagger/tagger.py` | Topic tabs or grouping visibly worse |
| Security policy, trusted proxies | `app/routes/frontend.py`, `caddy/Caddyfile` | When the setup in front of Caddy changes |
| Hostnames, backup paths and schedule | `.env` `ALLOWED_HOSTS`, crontab | When domains or disks change |

Tuning values (refresh interval, items per tab, story age, retry and backoff times, summary length, per-source caps) live in `app/config.py` and `app/sources.json`. They don't age; change them only to change behaviour.
