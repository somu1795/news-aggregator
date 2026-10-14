<div align="center">
  <h1>News Aggregator</h1>
  <p>
    A production-ready, containerized news aggregator application built with FastAPI and Redis.
  </p>
</div>

---

## Features
- **Fast & Asynchronous**: Built with FastAPI for high performance.
- **Background Refresh**: A dedicated `refresher` service fetches feeds on a schedule and publishes a snapshot to **Redis**. User requests never trigger upstream fetches, so traffic spikes don't reach the news sources.
- **High Concurrency Ready**: Each API worker serves an in-memory copy of the snapshot, re-synced from Redis every few seconds, and keeps serving it through Redis or upstream outages.
- **Regional News Without a Home-Country Bias**: Tabs for Asia, Europe, the Americas, Africa and Oceania, each built from that region's own local news (Google News local editions' national sections, BBC regional feeds and about 65 local outlets from Japan, Korea and Taiwan to the Baltics, the Caribbean, North Africa and the Pacific, such as Kyodo, Yonhap, ANSA, Yle, MercoPress, Daily Maverick, ABC and RNZ Pacific). Every tab keeps the same mix: 50% news (that region's, or all regions equally on the home page), 30% science and 20% Hacker News.
- **Topic Tabs**: Every region can be filtered to uplifting, tech, science, nature, culture or sport news. A small model (`bge-small-en-v1.5`, fine-tuned once on a GPU by `tagger/finetune.py`) runs on the host's Intel NPU in its own `tagger` container (or on the CPU without one). It's trained on labels the feeds themselves imply (a Sports section is sport) and retrains itself daily on every headline it has stored.
- **Article Summaries**: The top 20 stories of every tab get a short summary, shown in place under the headline. A `summarizer` service extracts each article's text and has an LLM (Ollama, e.g. on a GPU machine on your network) summarize it. Sites whose bot checks refuse plain requests are read with an isolated headless browser (`browser`, behind a public-only proxy); otherwise from the feed's own text or an archive.ph snapshot. Optional: set `SUMMARIZER_LLM_URL` in `.env`.
- **Search**: `[/]search` (or the `/` key) searches every tab's headlines and summaries as you type, entirely in the browser: opening it loads every tab's headlines in one request, then results update on every keystroke, each labelled with the tabs it's in.
- **One Item per Story**: When several outlets cover the same story under different headlines, a tab shows it once, with "+N sources" opening the others. A second small model on the NPU (`multilingual-e5-small`) recognises the same story, also across languages.
- **Reading Controls**: Sort each tab by the default mix, newest, oldest or random; an *advanced* row has **reset** (back to the defaults), **install** (installs the site as an app; it has a web app manifest and icons) **site icons** (each website's icon before its headlines, served from the site's own domain) **auto refresh** (off, 5m, 15m, 30m or 1h; refreshes the open tab while it's visible) and **back action** (whether Back leaves the site, the default, or returns to the last tab).
- **Configurable Sources and Weighting**: Regions, topics, their feeds and each feed's share are defined in `app/sources.json`; the page's tabs follow the file.
- **Balanced, Stable Mix**: Sources are interleaved by weight so every part of the page has the configured mix; stale items, duplicates and over-represented publishers are dropped, and the order only changes when the news does.
- **Resilient Fetching**: Automatically retries failed requests to RSS feeds; a failed refresh keeps the previous headlines.
- **Publisher Links with a Fallback**: Google News links are resolved to the publisher's URL by the custom resolver; an independent fallback decoder takes over only for links the custom resolver fails on.
- **Rate Limited**: 100 requests/minute per real client IP by default, shared across all workers and safe against spoofed headers.
- **Containerized & Production-Ready**: Fully containerized with a production-optimized **Docker** build for a small, secure, and fast-starting image.
- **Orchestrated with Docker Compose**: Services are managed with a single `docker-compose.yml` file for easy setup and deployment.
- **Secure by Default**: Runs as a non-root user inside the container and uses an internal reverse proxy (**Caddy**) for load balancing.
- **Monitoring**: Exposes Prometheus metrics, including refresh health and Google News link resolution outcomes (see ARCHITECTURE.md).
- **Well-Tested**: Includes a suite of unit tests using `pytest` and `fakeredis`.

## Architecture Overview

The system is a set of services orchestrated by Docker Compose. An external reverse proxy (like Nginx or Traefik) is expected to handle TLS termination and route traffic to the internal **Caddy** service. Caddy load-balances requests across the **FastAPI/Uvicorn** API workers, which only read the latest headlines snapshot. A separate **refresher** service builds those snapshots every 5 minutes and stores them in **Redis**, which also holds the resolved-link cache and rate-limit counters. A **tagger** service tags headlines by topic for the topic views.

For the full details — diagram, how and when data is refreshed, link resolution and its fallback, rate limits, failure behaviour, metrics and every tunable — see [ARCHITECTURE.md](ARCHITECTURE.md).

## Tech Stack

- **Backend**: FastAPI, Python 3.14.8 (Debian 13 "trixie" slim image)
- **Snapshot store, cache & rate limits**: Redis 8.10
- **Internal Proxy**: Caddy 2.11
- **Topic tagging**: bge-small-en-v1.5 on OpenVINO (Intel NPU), scikit-learn, SQLite
- **Containerization**: Docker, Docker Compose
- **Testing**: Pytest, fakeredis, respx

## Getting Started

### Prerequisites

- Docker
- Docker Compose (v2+)
- For the tagger: an Intel NPU (`/dev/accel`, e.g. Core Ultra / Meteor Lake) is used if present. Set `RENDER_GID` in `.env` to `getent group render`'s id. Without one, remove the `devices` and `group_add` lines of the `tagger` service in `docker-compose.yml`; it then runs on the CPU.

### Installation

1.  **Clone the repository:**
    ```sh
    git clone https://github.com/your-username/news-aggregator.git
    cd news-aggregator
    ```

2.  **Configure your environment:**
    Copy the example environment file. This file contains all the necessary configuration variables.
    ```sh
    cp .env.example .env
    ```
    **Important:** You must edit `.env` and set `ALLOWED_HOSTS` to the domain name or IP address you will use to access the application. You should also set a secure `ADMIN_API_KEY`.

3.  **Build and start the services:**
    Instead of using raw docker-compose commands, use the provided robust management scripts which handle pre-flight checks, cleanup, and health verification:
    ```sh
    ./start.sh
    ```
    To stop the services gracefully:
    ```sh
    ./stop.sh
    ```

On this machine and the LAN the application is at `http://<this host>:7001` (or whichever port you set for `CADDY_HTTP_PORT`), including Prometheus metrics at `/metrics`, which are served only to direct requests from private (LAN) addresses. The public site goes through the Cloudflare Tunnel (below), where `/metrics` is blocked.

## Cloudflare Tunnel (recommended)

The simplest public setup: no open ports, Cloudflare's DDoS protection, and the real client IP handled for you.

1. In Cloudflare Zero Trust, create a tunnel (Cloudflared, Docker) and copy its token.
2. Give it a public hostname (your site) with the service `http://caddy:7001`.
3. In `.env`, set `COMPOSE_PROFILES=cloudflare` and `CLOUDFLARE_TUNNEL_TOKEN=<token>`, then `docker compose up -d`. The `cloudflared` container connects out to Cloudflare.

Cloudflare sets `CF-Connecting-IP` to the visitor's IP and rejects requests that try to send their own, so the rate limit sees real IPs. Caddy adds HSTS and the other security headers itself.

To let Cloudflare answer most requests without reaching this server, add a cache rule (Caching → Cache Rules) for `/api/headlines`, `/api/search-index`, `/site-icon/*` and the icon files (`/manifest.webmanifest`, `/favicon.ico`, `/apple-touch-icon.png`, `/icon-192.png`, `/icon-512.png`): eligible for cache, edge TTL from the origin's Cache-Control header. Every response already says how long it stays fresh. The page's stylesheet and script (`/static/`) are cached without a rule. What is cached where, the settings it depends on (keep the caching level at Standard) and what happens after a deploy: ARCHITECTURE.md, §5, Caching.

## Configuration

Most configuration is managed via the `.env` file. See `.env.example` for a full list of available options and their descriptions.

Regions, news sources and their weights are defined in `app/sources.json` (format and rules in [ARCHITECTURE.md §10](ARCHITECTURE.md#10-configuration-reference)). The file is mounted into the refresher, so after changing feeds or weights run `docker compose restart refresher`; after adding or renaming a region or topic also restart `app`, since the page's tabs come from the same file. Other tunables (refresh interval — 5 minutes by default — timeouts, headline count) are in `app/config.py` and can be overridden with environment variables of the same name.

| Variable           | Description                                                              | Default      |
| ------------------ | ------------------------------------------------------------------------ | ------------ |
| `ALLOWED_HOSTS`    | Comma-separated list of allowed hostnames. **Required.**                 | `localhost`  |
| `ENABLE_API_DOCS`  | Set to "true" to enable interactive API docs at `/docs`.                 | `false`      |
| `ADMIN_API_KEY`    | A secret key to protect administrative endpoints. **Required.**          | `changeme`   |
| `UVICORN_WORKERS`  | Uvicorn worker processes per container; keep equal to `APP_CPUS`.       | `4`          |
| `CADDY_HTTP_PORT`  | The port Caddy listens on (this machine and the LAN).                   | `7001`       |
| `API_RATE_LIMIT`   | Rate limit per client IP for `/`, `/api/headlines` and `/api/search-index`. | `100/minute` |
| `APP_CPUS`         | Max CPU cores for each app container.                                    | `4`          |
| `APP_MEM_LIMIT`    | Max memory for each app container (e.g., `512m`, `2G`).                  | `2G`         |
| `RENDER_GID`       | Host group id owning the NPU (`getent group render`), for the tagger.    | `991`        |
| `REDIS_PASSWORD`   | Password for Redis. **Required.** Generate with `openssl rand -hex 24`.  | none         |
| `SUMMARIZER_LLM_URL` | Ollama server for article summaries, e.g. `http://<host>:11434`; empty disables them. | empty |

## Backup and Restore

For what needs reviewing and when (pinned versions, feeds, lists), see [MAINTENANCE.md](MAINTENANCE.md).

`scripts/backup.sh` runs daily at 03:30 UTC from the `pi` user's crontab and writes to both media disks (`/mnt/media/app_bkp/news_aggregator` and `/mnt/mediatoshiba/app_bkp/news_aggregator`; its log is `backup.log` in the first). Each run saves a dated, compressed archive with a checksum:

- the **project folder** as it is: code, `.git` (including commits not pushed yet), `.env` (secrets: keep the backup disks private), and untracked notes;
- **Redis's** snapshot (`dump.rdb`: published views, summaries, resolved links, site icons, which sites need the browser);
- the **tagger's** database (every stored headline, its labels and embeddings; copied with SQLite's online backup) and classifier.

It also saves each **Docker image** the stack runs on, once per version (`images/`), so a restore needs no downloads. It keeps the newest 14 archives and the images they use, and refuses to run if a destination folder is missing (an unmounted disk). Not backed up, as they rebuild themselves: the NPU compile cache, Byparr's GeoIP database, Caddy's volumes. A run takes a few seconds (about a minute when images changed); an archive is about 30 MB, the images about 2.2 GB.

**Restore** on this machine or a new one (Docker installed; for the tagger, the NPU driver and the `render` group, or set `TAGGER_DEVICE=CPU`; Tailscale for the summarizer's LLM):

```bash
/mnt/media/app_bkp/news_aggregator/restore.sh   # The latest archive into ~/news-aggregator
```

It verifies the checksums, unpacks the project, loads the images, puts the data into fresh volumes and starts the stack. The Cloudflare Tunnel reconnects with the token from `.env`, so the site is back at the same address. Give `restore.sh` an archive and a folder to restore an older one elsewhere. The project folder and its volumes must not exist yet (`docker compose down -v` removes the volumes of a broken install). Tested: a restore from the backup took 21 seconds, served the backed-up views and summaries at once, and the tagger kept its history and classifier.

## Running Tests

The tests don't need Docker or a running Redis (they use `fakeredis` and `respx`). They also run in CI before images are built.

```sh
cd app
pip install -r requirements-dev.txt
python -m pytest
```

Dependencies are pinned in `app/requirements.txt`, generated from `app/requirements.in`. To change a dependency, edit `requirements.in` and run `pip-compile requirements.in -o requirements.txt`.

The tagger has its own tests (a fake embedder, so no NPU or OpenVINO model needed):

```sh
cd tagger
pip install -r requirements.txt pytest
python -m pytest
```

## Scaling for High Traffic

Because requests are served from memory and never fetch upstream, a single app container handles thousands of requests per second. Upstream load is constant (one refresh every 5 minutes) regardless of traffic.

1.  **Vertical Scaling (More Power to One Instance)**:
    -   In your `.env` file, raise `APP_CPUS` and set `UVICORN_WORKERS` to the same number. More workers than CPUs only adds memory use (measured: 4 workers on 4 CPUs ≈ 3,250 req/s, same as 9 workers, at half the memory).
    -   Increase `APP_MEM_LIMIT` as needed.

2.  **Horizontal Scaling (More Instances)**:
    -   The most effective way to scale is to run multiple instances of the `app` container. Docker Compose makes this easy:
        ```sh
        docker-compose up --build -d --scale app=3
        ```
    -   This will start 3 `app` containers, and Caddy will automatically load-balance traffic between them. All of them serve the same snapshot from Redis. Keep the `refresher` at a single replica.
```

## API Endpoints

- `GET /`: Serves the main page (minimalist retro design with region and topic tabs).
- `/archive` and `/retro` (old URLs) redirect to `/`.
- `GET /api/headlines?region=<view>&topic=<topic>`: Returns the latest snapshot for `all` (default) or a region (`asia`, `europe`, `americas`, `africa`, `oceania`), optionally for one topic (`uplifting`, `tech`, `science`, `nature`, `culture`, `sport`): `region`, `topic`, `data`, `fetched_at`, `expires_at`. 404 for an unknown region or topic; 503 with `Retry-After` only before that view's first refresh.
- `GET /api/search-index`: Every view's headlines in one response, for the page's search: `items` (each link once, with `views`, the views it's in), `fetched_at`, `expires_at`. 503 with `Retry-After` only before the first refresh.
- `GET /site-icon/<domain>`: A website's icon, when the refresher has cached it (404 otherwise).
- `GET /health`: Health check. Reports `healthy`, `degraded` (Redis down but still serving), or `unhealthy`, plus the headlines' age.
- `POST /admin/cache/clear`: Triggers an immediate refresh (requires `X-API-Key`). Current headlines stay available meanwhile.
- `GET /metrics`: Prometheus metrics endpoint (blocked at Caddy).
