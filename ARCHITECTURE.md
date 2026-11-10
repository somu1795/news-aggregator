# Project Architecture

This document describes how the News Aggregator is built: its components, how regional headlines are collected, mixed, tagged by topic, stored and served, the rules each step follows, rate limiting, failure behaviour, and observability.

## 1. Overview

The application is a containerized system orchestrated by Docker Compose. Its core design rule is **user requests never trigger upstream fetches.**

- A single background **refresher** fetches all feeds on a schedule, builds one headline list per **view** (each region, plus "all"), resolves the links and publishes one **snapshot** per view to Redis.
- The **tagger** tags every headline by topic with a small model on the host's NPU; the refresher then publishes one more snapshot per region and topic (`asia:sport`, …).
- The **API** workers only read those snapshots, from an in-process copy.
- The **stats** service builds the heatmap on its own from a stream of every fetched headline; Caddy serves its file, so it can fail without touching the site.

So request volume costs almost nothing upstream or in Redis, and users keep getting headlines through slow feeds, Google failures and even Redis outages.

**Views**: `all` (the home page) and five regions: `asia`, `europe`, `americas`, `africa`, `oceania`. Each of these six also has a view per **topic**: `uplifting`, `tech`, `science`, `nature`, `culture`, `sport` (36 topic views). Regions and topics are defined in `app/sources.json`. The UI and the API take their lists from that file, so adding or renaming a region needs no code change.

## 2. Architecture Diagram

```
  End users ──► Cloudflare ──► Cloudflare Tunnel ──(cloudflared, outbound)──┐
                                                         ┌──────────────────┘
                                                         ▼
                                     +-----------------------------------+
                                     |  Caddy 2.11  (:7001)              |
                                     |  - resolves real client IP from   |
                                     |    trusted proxies only, sets     |
                                     |    X-Real-IP                      |
                                     |  - compression, load balancing    |
                                     |  - /metrics for the LAN only       |
                                     +-----------------------------------+
                                                         │
+--------------------------------------------------------│---------------------------+
| Docker network                                         ▼                           |
|                                     +-----------------------------------+          |
|                                     |  app  (FastAPI/Uvicorn, 4 workers)|          |
|                                     |  - serves / and                   |          |
|                                     |    /api/headlines?region=…        |          |
|                                     |  - in-memory copy of every view,  |          |
|                                     |    re-synced from Redis every 5s  |          |
|                                     |  - rate limiting per client IP    |          |
|                                     |  - /metrics on :8989 (internal)   |          |
|                                     +-----------------------------------+          |
|                                                 │ read snapshots                   |
|                                                 ▼                                  |
|                                     +-----------------------------------+          |
|                                     |  Redis 8                          |          |
|                                     |  - headlines:<view> × 42 (no TTL) |          |
|                                     |  - resolved URL cache (24h TTL)   |          |
|                                     |  - rate-limit counters            |          |
|                                     +-----------------------------------+          |
|                                                 ▲ write snapshots every 5 min      |
|                                                 │                                  |
|                                     +-----------------------------------+          |
|                                     |  refresher  (single replica)      |          |
|                                     |  - fetches 84 feeds (+55 topic     |          |
|                                     |    feeds every 30 min)            |          |
|                                     |  - mixes each region + "all"      |          |
|                                     |  - resolves Google News links     |          |
|                                     |    (custom resolver + fallback)   |          |
|                                     |  - /metrics on :9100 (internal)   |          |
|                                     +-----------------------------------+          |
|                                          │                 │ POST /tag              |
|                                          │                 ▼                        |
|                                          │   +-----------------------------------+  |
|                                          │   |  tagger  (bge-small on the NPU)   |  |
|                                          │   |  - topic tags per headline        |  |
|                                          │   |  - SQLite: every headline, its    |  |
|                                          │   |    embedding, labels and tags     |  |
|                                          │   |  - retrains daily                 |  |
|                                          │   +-----------------------------------+  |
|                                          │                                          |
|       every fetched headline: Redis stream stats:items, read by                     |
|       +-----------------------------------+                                         |
|       |  stats  (internal network, Redis  |  writes heatmap.json and stories.json   |
|       |  only; embeds and scores on CPU)  |  to a volume, which Caddy serves        |
|       +-----------------------------------+                                         |
+-------------------------------------------│----------------------------------------+
                                                  ▼
             Google News local editions (Nation section) · BBC regional feeds · local outlets
             (ABC, RNZ, Politico Europe, AllAfrica, …) · Google News Science · Hacker News ·
             topic feeds: Google News Technology/Science/Sports/Entertainment sections,
             Guardian environment, good-news outlets
```

## 3. Components

### Caddy (`caddy` service)
- **Role**: Internal reverse proxy and load balancer. TLS is terminated at Cloudflare; the tunnel connector (`cloudflared`) reaches Caddy over the Docker network. Its port is also open to the LAN (e.g. for `/metrics`).
- **Client IP trust**: `trusted_proxies` lists the peers allowed to tell Caddy the real client IP: private ranges (the Docker network the connector uses, and the LAN). For those peers Caddy reads `CF-Connecting-IP`, `X-Real-IP` or `X-Forwarded-For`; for anyone else it uses the connection's own IP. It then **overwrites** `X-Real-IP` before proxying, so clients can't spoof the IP used for rate limiting.
- Serves `/stats/` itself: the page's static files from its image, and `heatmap.json` from the volume the stats service writes, so the page needs neither the app nor the worker to load.
- Compresses responses (zstd/gzip) and health-checks app containers via `/health`. It serves `/metrics` and `/admin/*` only to direct requests from private (LAN) addresses: requests through the tunnel (they carry Cloudflare's `CF-Connecting-IP` header) and from public addresses get 403.

### API (`app` service)
- **Framework**: FastAPI on Uvicorn (`uvloop`, `httptools`), one worker per CPU (`UVICORN_WORKERS` = `APP_CPUS` = 4).
- **Code layout**:
  - **`main.py`**: app wiring — middleware, exception handling, lifespan, routes.
  - **`config.py`**: Pydantic settings (environment / `.env`), the only place their defaults are written.
  - **`catalog.py`**: the site's regions, topics and sources, loaded and validated from the config folder (§10).
  - **`site_config.py`**: the site's name, description, paywalled sites and LLM prompts (config folder).
  - **`keys.py`**: the Redis keys the services share (`snapshot_key(view)`, `summary_key(link)`, …).
  - **`deps.py`**: admin API key check, rate limiter and client-IP key function.
  - **`models.py`**: the API's response schemas (`/api/headlines`, shared with the refresher, `/api/search-index` and `/api/search`).
  - **`routes/`**: `headlines.py`, `frontend.py`, `admin.py`, `health.py`.
  - **`services/`**:
    - `headlines_cache.py`: per-process copy of every view, and of all of them combined for the page's search (rebuilt only when a view changes).
    - `redis_client.py`: Redis client factory.
    - `archive_search.py`: searching the refresher's headline archive (`archive.py`), read-only, for the page's older headlines (`/api/search`).
    - `archive_index.py`: the archive's search index (word → headlines, as roaring bitmaps): written by the refresher into a file next to the archive, mapped into memory by every app worker, where `archive_search.py` finds its matches.
    - `feeds/`: `poller.py` (`FeedPoller`: fetching feeds politely, remembering each feed and host between cycles; the refresher keeps one for its life), `parse.py` (a feed into headlines).
    - `views/`: `mixing.py` (`mix_headlines()`: weights, de-duplication, caps), `main_views.py` ("all" and the regions), `topic_views.py` (tagging and the topic views).
    - `links/`: `resolve_links()` for a list of headlines, and `google_news.py` (the custom Google News resolver first, then the fallback).
  - **`templates/`**: `retro.html` (the page) and the error pages.
  - **`static/`**: `retro.css` and `retro.js`, the page's stylesheet and script. The page links to them by a hash of their content (`?v=`), so browsers and Cloudflare keep them for a year and a changed file gets a new URL; any other version is served with `no-cache`.
  - **`middleware/`**: content-negotiated error pages (HTML for browsers, JSON for API clients).

### Refresher (`refresher` service)
- Runs `refresher.py` from the same image. **Exactly one replica.**
- Owns all upstream traffic: feeds, Google link resolution, writing the snapshots (see §4).
- Keeps the **headline archive** (`services/archive.py`): every headline it publishes, kept forever in a SQLite file of its own (`/archive/archive.db` in the `archive_data` volume, WAL mode), for the search of older headlines (below and §5) and a future timeline (choosing a date, scrolling back). The app reads it, read-only. Four tables, and a search index:
  - `items`: one row per link: title (the latest), source, published time, discussion link, summary, its **story** (the tagger's id, §6), and when it was first and last published. A headline only ever folded under another (`related`) is stored too, without a published time. A summary is kept once made, though the summarizer's copy in Redis expires after 7 days.
  - `titles`: every title a headline has had (feeds correct and update them), each with when it was first seen.
  - `snapshots`: each view's order once an hour (`ARCHIVE_SNAPSHOT_SECONDS`): a JSON list with one list per story, its headline's id then the ids folded under it. Enough to show a past front page again.
  - `progress`: how far it has followed the tagger's story changes (the last `story_moves` id applied).
  - `items_search`: a full-text index (SQLite FTS5) of each item's title, source and summary, kept up to date by triggers on `items`, which touch it only when one of those changes (every refresh rewrites each row, mostly unchanged). Words of any case and accents, indexed by their first 1–4 letters, and in which column each is, but not where (`detail=column`: no phrase searches, under half the size of a full index). `items_search_vocab` lists its words and how many items have each, for correcting typos. An archive from before the index is indexed when the refresher next opens it (95k headlines: under a second). Measured on a year-sized copy (1.9M headlines, 20 copies of 16 days): +529 MB a year; a search took 0.7 ms (median) to 6.7 ms (slowest keystroke).
  - **Stories** are kept up to date after the topic phase (`sync_stories` in `refresher.py`, at most `STORY_SYNC_TIMEOUT_SECONDS`, **60 s**): the tagger's story changes since the last applied (`GET /story-moves`: a story merged into another, or one headline moved out, by its judge) are applied, then headlines archived without a story (from before stories, or while the tagger was down) get one, 2,000 per request (`POST /stories`), first archived first. The first run gave the 99,528 headlines of Oct 12–31 their stories in one cycle. A failure is logged (`Archive stories not synced`) and left for the next cycle.
  - **The search index for the app** is written after that, every `ARCHIVE_INDEX_SECONDS` (**15 minutes**; `write_search_index`): `/archive/search-index.bin`, built from the whole archive (4 s at 150,000 headlines, growing with it) and renamed over the previous file; the app's workers map it into memory (§5, `/api/search`). A failure is logged (`Archive search index not written`) and the previous file stays in use.
  - Written after every publish (both phases), in a thread; a failure is logged (`Headlines not archived`) and never stops a refresh. Measured on the 43 views: 1,840 headlines, 0.01–0.03 s a refresh; an hour's snapshots are ~38 KB. At most ~6 MB a day, so a few GB a year; it is backed up daily with the rest (README).

### Tagger (`tagger` service)
- `tagger/` (`tagger.py`: the API, topic tagging and retraining; `embedder.py`, `classifier.py`, `store.py`, `stories.py`), its own image (Ubuntu 24.04, OpenVINO and Intel's NPU user-space driver). Internal only: `POST /tag`, `POST /group` (story grouping), `GET /health`. See §6.
- Runs on the Intel NPU (`/dev/accel`); falls back to the CPU if the NPU can't be used.

### Stats (`stats` service)
- `stats/`, its own image: the heatmap of the `/stats/` page. It reads the stream `stats:items`, keeps its own SQLite file (`/data/stats.db` in `stats_data`, 30 days) and writes `heatmap.json` to `stats_public`, which Caddy serves. No request reaches it and nothing depends on it, so it has no API; it needs Redis only, and neither the GPU nor the internet.
- **The stream:** after each fetch the refresher adds every link it hasn't sent since it started to `stats:items` (`services/stats_stream.py`): title, link, publisher, date and the feed's label, whether or not any tab shows the headline. The worker connects as its own Redis user, which can read this stream and nothing else, from an internal network with no way out but Redis. It is capped (`STATS_STREAM_MAXLEN`, **30,000** entries, ~9 MB) with titles cut to 300 characters and oversized links left out, as Redis can't evict it, has a 5 s timeout and catches every error, so it can't affect a refresh. A refresher restart sends every link again; the worker drops repeats.
- **Each minute** the worker takes up to 2,000 new entries and, as one transaction:
  1. drops Google's " - Publisher" from titles, feeds labelled `sport`, `culture`, `tech`, `science` or `nature`, and links it has seen; a headline dated over 7 days ago is noted as seen and not counted;
  2. embeds the titles with `multilingual-e5-small` on the CPU (the tagger's story model, ~68 titles/s on one core; its CPU vectors match the tagger's at cosine ≥ 0.99999);
  3. keeps those the `relevant` model passes, a logistic regression on the embedding learned from the tagger's tags (92.3% agreement; keeping everything would agree 90.5%, as labels do most of the filtering). Its threshold, log-odds 1.0, drops 60% of the topic stories that reach it and 4% of the news;
  4. scores **stress**, 0–3 (0 good news, 1 routine, 2 worrying, 3 severe), with a ridge regression on the embedding, trained on 3,802 headlines an LLM rated, severe ones weighing 3 times as much. On 206 hand-labelled headlines it finds "worrying or worse" with F1 0.76. It underrates severe events (they average about 1.9), so scores are only used in averages and are never shown;
  5. finds the **countries**: the first two the title names (`stats/data/places.json`: countries, their people, leaders, cities over 300,000 and states), or else the outlet's country (`homes.json`: 374 outlets where one country is at least half of 8 or more located headlines; worldwide outlets like the BBC have none). On 300 hand-labelled headlines: 88% precision, 82% recall. Two thirds of headlines are placed. Names that are often something else are left out, and "Niger" from a Nigerian outlet means the state;
  6. joins each to the closest story of the last 72 hours at cosine ≥ 0.92, or starts one. Each story counts once per country. Country scores match those from the tagger's stories (correlation 0.996 over 74 countries).
- **Colour:** a country's score is the mean stress of its stories, each weighing half as much per 48 hours, with 10 stories' worth of the world's mean added, so few stories stay near the mean. Colour runs from blue at 0.93 to orange at 1.5, fixed ends (the 10th and 90th percentile of country scores, Oct 2026), so a calmer or worse world changes the map. **Brightness** is evidence / (evidence + 10): how far the colour can be trusted, not how much news there is. Under 3 stories' worth of evidence a country shows no colour. Coverage is uneven: 86 of 206 countries get under one headline a day, and outlets cover quiet countries mostly when something goes wrong, which dimming only partly corrects.
- **Red dots** compare a country with its own past. Stories whose title names it are counted over the last 6 hours and compared with the 6-hour blocks of the previous 14 days. A dot appears at 2 or more above the mean, in units of the square root of (mean, at least 1, + variance), if one of those stories has stress ≥ 1.95 and 2 or more outlets, and at least 7 days of data exist. It stays for 24 hours after the condition last held. It sits on the city the title names in that country, or else the country's capital. Replayed on Oct 20–Nov 1, 2026: 18 dots in 12 days, most within 1–5 hours of the first headline. A new dot is published at once; the map otherwise every 5 minutes. The story lists go in `stories.json`, which the page loads when a country is tapped (`heatmap.json` stays small: it's polled every minute by each open, visible page).
- **Resources:** ~750 MB in use with 20 days of data, limit 1 GB, one CPU; a publish takes ~0.16 s. `stats_data` isn't in the daily backup, as `backfill.py` rebuilds it.

### Summarizer (`summarizer` service)
- Runs `summarizer.py` from the app image (the loop; reading articles is `services/summaries/reading.py`, the LLM calls `services/summaries/llm.py`, the prompts `config/prompts.yaml`). **One replica.** Optional: without `SUMMARIZER_LLM_URL` it idles.
- Two loops. **Reading:** every minute it reads the published snapshots and takes the first `SUMMARY_TOP_N` items (default 20) of every view with neither a summary nor a text yet: the main views first, then the topic views, top positions first. It reads each article's text (trafilatura; at most 6,000 characters; a precise extraction first, then a looser one if that finds too little), three articles at once but each site's one at a time (the first teaches the next that the site needs the browser), and each browser one page at a time (a page can take 1.3 GB of its 2 GB). The text waits in Redis (`article-text:<hash of link>`, 2 days) until it's summarized.
- **Summaries:** the read items in the same order, 10 at a time (the next 10 in the tabs' order of then), each asked of an **Ollama** server for 2–3 sentences that state the news itself (not "The article reports…"), using only the article's facts. The prompt first has the model check the text: when it is a block, error or cookie page rather than the article, the model answers `UNREADABLE` and the article is marked unreadable for a day instead of summarized. On 149 articles, 3 summaries still opened with "The article…" (8 with the earlier wording), and 21 of 21 tries on 7 block pages came back `UNREADABLE` (15). Qwen3.5 9B avoided the opening better but described block pages instead of rejecting them, and wrote longer summaries. The model is `qwen3:8b` (thinking off), and the server is a desktop GPU (RTX 3060 Ti) on the tailnet. Ollama loads the model on demand and frees the GPU two minutes after the last request. An article the server fails on three passes in a row (an error or an empty answer, while it answers otherwise) is set aside for 3 hours, so it doesn't hold up the articles after it.
- The summary is stored in Redis at `summary:<hash of link>` for 7 days, with how its text was read (`via`). The refresher adds it to the item (`summary`) when it next publishes, so summaries appear within one refresh of being made.
- **Reading the article**, each step only if the previous ones failed:
  1. **direct**: a plain request (public addresses only, see §7b);
  2. **browser**: headless Chromium (`browser` service, FlareSolverr), which gets through bot checks that refuse plain requests (Cloudflare challenges, AWS WAF's HTTP 202). A site that blocked a plain request (401/402/403/429/503, or a `cf-mitigated`, `x-datadome` or `x-amzn-waf-action` header) is remembered for 7 days (`browser-host:<host>`) and its articles go straight to the browser; after that the next article tries a plain request again, and the site is remembered again if it's refused. The browser first loads the page without ads, trackers and video players (through `browser-egress-filtered`, below): pages that took Chromium to 1.3 GB and 40 s took 0.6–0.7 GB and 3–5 s, with the same article text. If that fails (an error, a timeout or too little text), it loads the whole page through `browser-egress` (read "via browser-full"), and a site that only works that way is remembered for 7 days (`full-page-host:<host>`, logged as "Site needs the whole page in the browser"), so the block list needs no hand-made exceptions. If the browser fails, a second one with a different engine tries (`browser2`, Byparr: a modified Firefox, same API);
  3. **feed**: the text the feed itself carried for the article. The refresher keeps it for every published item with at least 600 characters (`feed-text:<hash of link>`, 2 days);
  4. **archive**: the latest archive.ph snapshot.

  Paywalled sites (`paywalled_hosts` in `config/site.yaml`, the same list the page's bypass uses) go direct, archive, then feed: a browser doesn't get past a paywall. Articles none of these can read (or with less than 600 characters of text) are marked and retried after 3 hours; links to non-public addresses are marked for a day and never reach the browser.
- Measured on the 153 top stories that the direct fetch alone couldn't read: 121 became readable (browser 100, direct after the looser extraction 8, archive 7, feed 2…), and the second browser read 4 of the remaining 32 (phys.org, ESPN). On pages the first browser reads, the second reads 28 of 30, at ~23 s a page instead of ~8 s, so it only runs after the first fails. Still unreadable: sites whose bot checks also stop a real browser (phys.org's own check, Forbes' DataDome, Akamai on Sky and News Corp sites) and paywalled articles without a snapshot. A browser page takes ~10 s (up to ~50 s).
- Counts per method are logged every pass and exported as `summarizer_article_reads_total{method}` on the summarizer's metrics port (9100, inside the Docker network).
- When the LLM server is unreachable (the desktop is off), reading goes on and nothing is summarized: a summary pass first checks the server when something is ready. It retries every minute, logging only when availability changes. When it's back, the articles are already read, so it summarizes them one after another without waiting on pages.

### Headless browsers (`browser`, `browser2` and `browser-egress` services)
- `browser` is FlareSolverr (pinned image): a headless Chromium behind a small HTTP API, used only by the summarizer. Pages run their own JavaScript in it, so it is cut off from everything else:
  - it sits only on the `browser` network, which is `internal` (no internet). Raw connections from it to the internet, the LAN or the tailnet fail, and the stack's other containers (Redis, app) don't even resolve;
  - the summarizer has it load every page through `browser-egress`, a Smokescreen proxy (`smokescreen/Dockerfile`, built from a pinned, verified commit). The proxy connects to public internet addresses only: private, loopback, link-local, Docker and Tailscale/CGNAT (100.64/10) addresses are refused, after DNS and at every redirect. Tested: the LAN, the tailnet, a public URL redirecting into the LAN, and container names were all refused;
  - `browser-egress` and `browser-egress-filtered` are the only containers on both `browser` and `egress` (their own network to the internet).
- `browser-egress-filtered` is the same proxy with a block list: ~3,600 ad and tracker domains (Peter Lowe's list) and video-player hosts (YouTube, JW Player, Brightcove, …), each with its subdomains, in `smokescreen/filter/acl.yaml`. `scripts/update-blocklist.py` regenerates it; then `docker compose restart browser-egress-filtered`. Its config file keeps Smokescreen's defaults, so private addresses are refused as by `browser-egress` (tested: the LAN, Docker, the tailnet, loopback, link-local). Byparr keeps the unfiltered proxy: it is the last browser tried.
- `browser2` is Byparr (pinned image), with the same isolation: only on the `browser` network, every page through `browser-egress` (`PROXY_SERVER`), and its own downloads too (`HTTPS_PROXY`: a GeoIP database at start, kept in the `browser2_cache` volume). Its locale and timezone are fixed so it doesn't look up its public IP.
- Limits: `browser` 1.5 CPUs, 2 GB (~150 MB idle; heavy news pages take Chromium to ~1.4 GB, which at 1 GB stalled, timed out or crashed tabs), 1024 processes and threads (a heavy page takes ~400; at the limit Chromium can't even quit, and the browser is stuck until restarted), 256 MB shared memory; `browser2` 1.5 CPUs, 1.5 GB (its temporary files are in memory), 512 processes.

### Site icons
- After each refresh, the refresher caches the icon of every website in the published views that it doesn't have yet. It fetches them from DuckDuckGo's icon service (`ICON_SOURCE_URL`) and keeps them in Redis for 7 days; a site without one is retried after a day.
- The app serves them at `/site-icon/<domain>` (cached by browsers for 7 days). It only reads Redis and never fetches, so visitors' browsers contact neither the websites nor the icon service.
- Each icon is sent with `Content-Security-Policy: default-src 'none'; sandbox`. Opened directly, an icon is a page on this domain, and an SVG icon (the publisher's own, passed on by the icon service) could carry a script: the policy stops anything in it from running or loading. As an image on the page it can't run anyway, and it looks the same.
- Measured on the first cycle: 112 of 126 websites had an icon.
- **Pixel art for the rest** (`services/pixel_icons.py`). When the icon service has none (or only one over 64 KB), the refresher finds the website's own icon and stores it redrawn as 16×16 pixel art, in the retro look of the page:
  - **Source**, in order: the largest raster icon its homepage declares (`<link rel="icon">`, `apple-touch-icon`; only the first 512 KB of the page is read, at most 6 icons, each up to 1 MB), or its `/favicon.ico`; if that's missing, under 32 px or unusable, Google's copy (`ICON_FALLBACK_URL`, 64 px; only the domain is sent). Public addresses only, every redirect checked (§7b). SVG icons aren't used. Only raster formats are decoded (ICO, PNG, JPEG, GIF, WebP, BMP), and a picture over 1024×1024 is refused, however small its file. Only the homepage's `<head>` is parsed.
  - **Kept apart from the site**: the pictures come from other people's servers, so they are decoded and redrawn only in a separate short-lived process (`python -m services.pixel_icons`), one at a time, with its memory capped at 256 MB (a 1024×1024 picture peaks at ~57 MB) and killed after 20 s; neither the refresher nor the API loads the image library (a test checks). A picture that crashes, hangs or exhausts it costs only that icon. The icon step runs last in a cycle, after every view is published, and is capped at 60 s (`ICON_REFRESH_TIMEOUT_SECONDS`); its errors are logged and never fail a refresh.
  - **Drawing**: the icon is trimmed to what's visible and squared, then each of the 16×16 pixels is decided from a 6×6 block of a 96×96 copy: the block's average colour matched to the icon's 8 main colours, so no blended in-between shades. A block that is mostly background but crossed by a line takes the line's colour, so thin strokes survive (as a pixel artist would draw them). The result is an SVG of square pixels (`shape-rendering: crispEdges`), sharp at any size, 1–4 KB, kept 7 days like any icon (`via`: `own` or `google`).
  - Measured on the 88 websites without an icon on 2026-10-14: 49 from their own icon, 23 from Google's copy, 12 that the icon service answered after all; 4 have no icon anywhere and show none. The whole step took 33 s (the cap is 60 s; what isn't done is picked up next cycle).

### Redis (`redis` service, Redis 8)
- Holds one snapshot per view (`headlines:all`, `headlines:asia`, `headlines:asia:sport`, … — up to about 20 KB each), the resolved-URL cache and rate-limit counters.
- `maxmemory 512mb` (container limit 640 MB) with `maxmemory-policy volatile-lru`: only keys with a TTL (resolved URLs, rate-limit counters, summaries, feed texts, icons) can be evicted, so it is kept well above use (~15 MB). Snapshots have no TTL and are never evicted.
- RDB persistence (`save 300 1`, `save 60 1000`, plus a save on graceful shutdown) keeps snapshots across restarts, so the API serves immediately after a reboot.

## 4. How Data Is Collected

The refresher runs this cycle forever:

```
┌─► 1. fetch all feeds ─► 2. mix each region, then "all" ─► 3. resolve links ─► 4. publish 6 snapshots ─┐
│                                                                                                        │
│   5. topic views: tag every headline, build region × topic, resolve links, publish 36 snapshots ◄──────┘
│                                                                                │
└──────────────── 6. wait REFRESH_INTERVAL_SECONDS (5 min), or less if an admin requests a refresh ◄─────┘
```

### Step 1 — Fetch feeds
The catalog (`config/sources.yaml`) has 84 news feeds (plus 55 topic feeds, Step 5). **Every view has the same overall mix: 75% news, 15% science, 10% Hacker News** (75 / 15 / 10 of its 100 headlines). "News" is that region's news, or all regions equally on "all". Each region's weights sum to 1.0 and decide the make-up of its news share:

| Region | News sources (share of the region's news) |
| --- | --- |
| **Asia** | Google News India 12%, Singapore 6%, Philippines 6%, Pakistan 5%, Malaysia 4%, Israel 4% · BBC Asia 6%, Middle East 5% · Al Jazeera 6% · East Asia: Kyodo 5%, Japan Times 4%, Yonhap 5%, Korea Times 3%, SCMP Asia 5%, Hong Kong Free Press 2%, Focus Taiwan 3% · Southeast Asia: Antara 3%, VnExpress 3%, Bangkok Post 2%, Rappler 2% · Middle East: The National 3%, Arab News 2%, Middle East Eye 2% · Times of Central Asia 2% |
| **Europe** | Google News UK 19%, Ireland 7% · Politico Europe 12% · BBC Europe 10% · France 24 Europe 6% · Kyiv Independent 6% · The Local France, Germany 3% each, Sweden, Spain, Italy, Switzerland 2% each · ANSA 5% · Portugal Resident 2% · Notes from Poland 4% · Balkan Insight 4% · Romania Insider 2% · Yle 3%, ERR, LSM, LRT 2% each |
| **Americas** | Google News US 22%, Canada 14% · BBC Latin America 12%, US & Canada 6% · Latin Times 6% · Mexico News Daily 7% · MercoPress 6% · Buenos Aires Times 4%, Herald 3% · Colombia One 6% · Agência Brasil 3% · Jamaica Gleaner 6%, Observer 5% |
| **Africa** | Google News South Africa 12%, Nigeria 12%, Kenya 10%, Ghana 6%, Uganda 5% · AllAfrica 13% · BBC Africa 9% · Egypt Independent 6% · North Africa Post 6% · Daily Maverick 7% · Premium Times 4% · The Africa Report 4% · Business Daily Africa 3% · Radio Dabanga (Sudan) 3% |
| **Oceania** | Google News Australia 24%, New Zealand 18% · ABC Australia 15% · RNZ 12% · BBC Australia 6% · Guardian Australia 8% · Pacific: RNZ Pacific 7%, Islands Business 4%, PNG Post-Courier 4%, Asia Pacific Report 2% |
| **All** | The five regions' news lists, equally (5 headlines each) |
| **Every view** (`extras`) | Google News Science 15% · Hacker News 10% — the view's news gets the remaining 75% |

How the sources were chosen (all measured live):
- **Google News local editions, "Nation" section.** The backbone: 50–70 fresh items each from many local publishers. The Nation section is used instead of an edition's top stories because top stories mix in the biggest international story of the day. In every region view that same story led the list; Nation sections are domestic news (e.g. 0 international-keyword headlines for India, Ireland, Nigeria and New Zealand). The URLs in `sources.json` are the canonical `rss/topics/…` URLs that `…/section/topic/NATION?…` redirects to.
- **Only real local editions.** The English editions for UAE, Saudi Arabia, Lebanon, Hong Kong, Bangladesh, Germany and Egypt return the *US* edition's stories, so they are not used.
- **Local and regional outlets** add depth, the parts of a region its Google News editions don't cover (Google has no English edition for Japan, Korea, Taiwan, Indonesia, Vietnam, Thailand, Gulf states, continental Europe, Latin America, North Africa or the Pacific), and direct links that need no Google resolution. Each was checked live from the server before being added: it parses, it's in English, its items carry dates, and it had at least 3 stories in the last 48 hours. They have small weights (2–7%) so each adds a story or two to a tab, and Google News editions keep 37–42% of each region. BBC regional feeds update slowly, so they have small weights.
- Rejected: DW, Taipei Times, Kathmandu Post, Daily Star Bangladesh (items carry no usable dates, so old stories would look new); Euronews and Africanews general feeds (global, not regional); Rio Times (an events guide now); Tehran Times and Anadolu (state-run, and Anadolu is global); feeds that refuse the server (Euractiv, Kathimerini, Nation Kenya, The East African, Morocco World News: HTTP 403) or are broken (Brussels Times, Tico Times, Fiji Times).

Fetching:
- All feeds are fetched **concurrently**, at most 15 requests in flight. Redirects are followed, so a moved feed keeps working.
- Each request has a `REQUEST_TIMEOUT` of **10s** and up to `FETCH_RETRIES` = **3** attempts on network errors, with exponential backoff (1s, then 2s).
- **Polite polling** (`FeedPoller.fetch()`; the refresher is one long-lived process, so its `FeedPoller` remembers each feed between cycles):
  - **Per-feed interval:** a fetch that brings new headlines keeps the feed on every cycle; one that brings nothing new skips a cycle, then waits twice as long each time (10, 20, then at most `FEED_MAX_INTERVAL_SECONDS`, **30 min**). In between, the feed's last headlines are reused without a request. Busy feeds (Google News sections) stay at 5 minutes; slow ones (BBC regional, The Local, Pacific outlets) drift to 20–30 minutes.
  - **Conditional requests:** the feed's `ETag` and `Last-Modified` are sent back (`If-None-Match`, `If-Modified-Since`), so an unchanged feed answers `304 Not Modified` with no body.
  - **Host backoff:** a `429` or `503` pauses **every feed on that host** (all Google News feeds together) for its `Retry-After`, or 10 minutes doubling up to 2 hours (`FEED_BACKOFF_SECONDS`, `FEED_BACKOFF_MAX_SECONDS`), with no retries. Meanwhile the feeds' last headlines are reused; with no new Google items there are also no new Google links to resolve.
  - **Jitter:** each request starts after a random delay of up to `FEED_JITTER_SECONDS` (**10s**), so they don't all go out at once.
  - A feed that can't be fetched keeps its last headlines (they still age out after 48 hours) instead of dropping out of its views.
  - Counted in `feed_fetches_total{result}`: `fetched`, `not_modified`, `not_due`, `backed_off`, `failed`.
- A feed counts as **failed** after all attempts fail, or if its XML is so malformed that no entries can be parsed. A failed feed contributes nothing and increments `feed_fetch_failures_total{source}`; everything else is unaffected.
- Each entry with a title and a link becomes a headline (one without is skipped, not the whole feed):
  - `title` and `link`;
  - `source`: the publisher named in the entry, or else the feed's name;
  - `published`: the entry's date, or the fetch time if it has none;
  - `last_updated`;
  - `comments`: the discussion page, when the feed has one (Hacker News).

### Step 2 — Mix each view (weighted interleave)
`build_views()` in `services/views/main_views.py` builds every view in two stages, using `mix_headlines()` for each list:

- **Stage 1, news lists.** Each region's news from its sources. "World" news is built from the five regional lists, equally. That keeps every region represented and drops a story two regions both carry, and the per-publisher cap applies across regions, so e.g. BBC appears at most 3 times. Science and Hacker News are each mixed on their own.
- **Stage 2, views.** Each view interleaves its news list (75%) with science (15%) and Hacker News (10%). The lists were already capped, so this stage only interleaves and de-duplicates. News has the largest single weight, so it leads every view (pattern: *news, news, science, news, news, HN, …*; about 19 news, 4 science and 2 HN in the top 25).
- If a view's news list is empty (all of that region's feeds failed), the view stays empty and keeps its previous snapshot, rather than showing only science and HN.

`mix_headlines()` builds a list from weighted sources like this:

1. **Keep each source's own order.** Google's ranking and Hacker News's front-page rank are preserved; nothing is re-sorted by date.
2. **Drop stale items** published more than `MAX_HEADLINE_AGE_HOURS` (**48h**) ago. Items without a date count as new.
3. **Interleave by weight.** Sources take turns by *smooth weighted round-robin*: each turn, every source earns credit equal to its weight and the source with the most credit supplies the next headline. Every stretch of the list therefore has roughly the configured mix, rather than blocks per source.
4. **Skip duplicates and over-represented publishers:**
   - an item is a duplicate if its link, or its title (case and punctuation ignored, Google's " - Publisher" suffix removed), was already taken; the first occurrence wins;
   - at most `MAX_PER_PUBLISHER` (**3**) items per publisher. The publisher is the publisher name for Google items and the link's domain for direct links. **A feed whose items all come from one publisher** (e.g. ABC Australia's own feed) is exempt, because its weight already bounds its share.
5. **Redistribute unused slots.** A source that runs out, or reaches its `max_items` (an optional per-source cap in `sources.yaml`, for a feed whose quality varies), drops out of the rotation, and the others fill the list in proportion to their weights. Views are built `STORY_GROUP_HEADROOM` (**40**) longer than `MAX_HEADLINES` (**100**); Step 4 cuts them to 100 once each story is shown once.

The result is **deterministic**: the same feeds give the same order, so lists only change when the news changes. Each view logs a `Mixed headlines` line with counts per source and items dropped as stale, duplicate or over the cap.

The same story reported by several outlets under different headlines is folded into one item in Step 4.

### Step 3 — Resolve links
Google News RSS links point at Google, not at the publisher. Direct links (outlets, Hacker News) are used as-is with no network calls. Each distinct Google link across all views is resolved once, with at most 10 resolutions in flight, through `services/links/google_news.py`:

1. **Resolved-URL cache (Redis, 24h).** A link resolved within the last 24 hours is reused with no network call. In steady state almost all links come from here; a cold start resolves about 150 links in under 10 seconds.
2. **Custom resolver (`resolve_redirect()` in `services/links/google_news.py`) — always the primary.**
   - Follows HTTP redirects; if they lead off Google, that URL is the answer.
   - Otherwise it reads the `c-wiz[data-p]` payload from the Google article page and calls Google's `batchexecute` endpoint to decode it.
   - Retries up to 3 times with backoff. Results that differ from the input are cached for 24h.
3. **Fallback decoder — only if the custom resolver really failed.** "Failed" means its result is **still a `news.google.com` URL**: all retries failed, the `c-wiz` payload was missing, or the response couldn't be parsed. The fallback is built independently:
   - it reads different page attributes (the article's `data-n-a-sg` signature and `data-n-a-ts` timestamp);
   - it builds its own `batchexecute` request and parses the response itself.

   It makes one attempt, with at most 5 fallback lookups in flight, and caches its result for 24h. If it also fails, the Google link is kept and the next refresh tries again.

Outcomes are counted in `redirect_resolutions_total{outcome}` (custom resolver) and `redirect_fallbacks_total{outcome}` (fallback).

### Step 4 — Publish
- **One item per story** (`services/story_groups.py`). Every headline in the views goes to the tagger's `POST /group`, which gives each its story (§6): an id that lasts across views, refreshes and days. In each view, the first-ranked headline of a story stays, with the story's id (`story`), and its other headlines in that view are folded under it (`related`: title, link, source; at most `MAX_RELATED`, **8**), so later stories move up; the view is then cut to 80. If the tagger fails, views are published ungrouped. Measured on one cycle: 8 headlines folded in the main views, 151 in the topic views (tech, science and culture repeat most, e.g. one Apple launch from five outlets).
- Each view's snapshot (`region`, `data`, `fetched_at`, `expires_at = fetched_at + 5 min`) is validated against the API schema and written to `headlines:<view>` with **no TTL**. All views are written in one pipelined round-trip.
- **Publishing criteria — a view's previous snapshot is kept when:**
  - that view's news came out empty (e.g. all of a region's feeds failed). The other views are still published, and a warning names the empty views;
  - every view came out empty (`headlines_refreshes_total{result="empty"}`);
  - the cycle raised an error (`result="error"`) or exceeded `REFRESH_TIMEOUT_SECONDS` (**270s**, both phases) (`result="timeout"`).
- A partial view (some feeds down) **is** published, because fresh headlines from part of a region beat none.

### Step 5 — Topic views
After the main views are published, the refresher builds the topic views (`get_topic_views()` in `services/views/topic_views.py`). This phase can fail on its own (tagger down, or over `TOPIC_REFRESH_TIMEOUT_SECONDS`, **150s**): the main views are already out, the topic views keep their previous snapshots, and `headlines_topic_refreshes_total{result="error"}` counts it.

1. **Topic feeds.** 55 feeds about one topic each: the Technology, Science, Sports and Entertainment sections of 10 Google News local editions (India and Singapore for Asia; UK; US and Canada; Nigeria, South Africa and Kenya; Australia and New Zealand), plus three Guardian environment feeds and Mongabay (nature) and ten good-news outlets (uplifting: Good News Network, Positive News, Reasons to be Cheerful, Optimist Daily, Upworthy, Good Good Good, DailyGood, Sunny Skyz, Not All News Is Bad, Squirrel News), which belong to no region, and The Better India (uplifting, Asia's). They change slowly and are many, so they're fetched every `TOPIC_FEEDS_INTERVAL_SECONDS` (**30 min**) and reused in between. They appear only in topic views.
2. **Tagging.** Every fetched headline (news, extras and topic feeds, about 3,500) goes to the tagger with its feed's **label**: the topic of a topic feed (or of Google News Science: `science`), `other` for the Google News national sections, or none. The tagger returns its tags (§6). A headline's tags are the tagger's plus its feed's topic, so a Sports section's items are always `sport`.
3. **Views.** For each topic, one view per region (and "all") from the items tagged with it: the region's news and topic feeds, plus the extras and topic extras. They're mixed with `mix_headlines()` (same rules as above), except the weights: the news/extras mix doesn't apply to one topic (Hacker News has little sport). **Feeds about the topic come first**, mixed equally: in a region's view, the region's own topic feeds, then the shared ones (extras, topic extras), which are the same in every region. Items the model tagged from other sources only fill the view (up to `MAX_HEADLINES`, 100) when those run short. **Shared feeds are routed by place:** the shared topic feeds (Mongabay, the Guardian's environment feeds, the good-news outlets) cover the whole world, so in a region's view they show only their stories that name one of that region's places (`config/regions.yaml` `places`, matched by `services/places.py`: a list of ~580 countries, their adjectives, US states, Canadian provinces, major cities and regions; checked in the headline, then in the first 300 characters of the feed's description), then their stories that name no place. A shared story about another region is left out. Names that are often something else are not in the list (Georgia, Jordan, Chad, Turkey, Victoria, Amazon). Measured on live feeds: nature views shared 83% of their stories between regions before, 42% after (uplifting 92% → 85%: good-news headlines rarely name a place); views got shorter (nature 20–36 stories per region). Regional nature feeds are few (most outlets' environment feeds are missing or blocked): Mongabay India (Asia) and Inside Climate News (Americas). **Global stories stay out of region views:** a story found in two or more regions' own feeds (e.g. the same TechCrunch story in several Google News Technology editions) is shown only in "all", so region tabs don't repeat each other; what still repeats across regions is the shared feeds' items, after the region's own. **Uplifting and nature are feed-only** (`feed_only` in `config/topics.yaml`): model tags never add to them. Uplifting's most confident tags were about half wrong on live news. Nature's filled half its "all" view on Nov 10 (43 of 93 stories) and 19 of those were wrong (a Black Sea food corridor, grain checks, a sports complex), at any confidence: wrong ones scored 0.90–0.99, like right ones; without them the views are shorter (50 stories in "all", 12–28 per region). Good-news outlets publish a few stories a day each, so uplifting views keep stories for 7 days (`max_age_hours` of the topic in `sources.json`) instead of 48 hours: 80 stories from 10–11 outlets per region instead of about 12 from 2–3. "All" draws from every region's sources.
4. **Links** are resolved as in Step 3, and all 36 views are published, **including empty ones** (a region can have no uplifting news right now; the page says so).

### Step 6 — Wait
- A cycle starts every `REFRESH_INTERVAL_SECONDS` (**5 minutes**), counted from the start of the previous one, so snapshots are replaced when their `expires_at` says; a cycle takes 20–45 s, and one that overruns is followed by the next at once.
- **Why 5 minutes:** the catalog has 84 feeds, 18 of them from Google News (plus 55 topic feeds, 40 of them Google News sections, fetched every 30 minutes). Every 5 minutes keeps busy feeds fresh; slow feeds are fetched less often by their own interval (Step 1), so a cycle requests only the feeds that are due. Doing it every minute from one IP would risk Google rate-limiting or blocking it, which would also break link resolution.
- `POST /admin/cache/clear` (with `X-API-Key`) wakes the refresher immediately. Repeated requests collapse into one refresh, and the current snapshots stay available meanwhile.
- If Redis is unreachable while waiting, the refresher sleeps the interval and tries again.

## 5. How Requests Are Served

1. A browser loads `/` and its stylesheet and script from `/static/` (cached after the first visit); the page reads the region and topic lists embedded in it and calls `/api/headlines?region=<view>[&topic=<topic>]`.
2. Caddy resolves the client IP, sets `X-Real-IP`, compresses and forwards to an app container.
3. The rate limiter checks that client's counter (§7).
4. The worker returns its **in-memory snapshot** of that view, pre-serialized, with `Cache-Control: public, max-age=<seconds until expires_at>`, and `CDN-Cache-Control` for Cloudflare: at least 60 s, `stale-while-revalidate=60` and `stale-if-error=86400` (an edge copy keeps being served for a day if the origin is unreachable).
5. **Snapshot sync rules (per worker, per view):**
   - Every worker loads all views at startup.
   - A worker re-reads a view from Redis when its copy is older than `LOCAL_CACHE_SECONDS` (**5s**); only one request per view per worker does the read.
   - Other requests arriving during that read are served the current copy immediately instead of waiting.
   - A view with no snapshot yet is re-checked on every request, so the first snapshot is picked up at once.
   - If Redis can't be read, the worker keeps its copies and retries 5s later.

Consequence: a newly published snapshot reaches users within about 5s, and the API makes at most one Redis read per view per worker every 5s, whatever the traffic.

### The main page (`/`)
- Minimalist retro design: monospace; light "paper" and dark amber themes; `DEFAULT_THEME` (light) until toggled.
- **Region tabs**: `all · asia · europe · americas · africa · oceania`, and below them **topic tabs**: `everything · uplifting · tech · science · nature · culture · sport`. Any region combines with any topic; "everything" is the region's main view.
  - Tabs are real links (`/?region=asia&topic=sport`): shareable ("Copy link") and open in a new tab; such an address opens its tab. Switching tabs doesn't change the address: it stays as typed (`/`), and a shared link's tab is dropped from it on the first switch, so it never names the wrong tab. A refresh reopens the last tab, which is remembered. By default a switch adds no history entry: Back leaves the site (in the installed app too) instead of stepping back through every tab visited. The advanced row's **back action** (`exit` by default, or `last tab`) switches to one history entry per tab change, holding its tab, so Back and Forward return to the previous and next tab.
  - Clicking switches in place without reloading.
  - Number keys `1`–`6` switch regions (keeping the topic); `t` / `Shift+t` cycle topics.
  - The last region and topic are remembered when the URL doesn't name them.
  - A tab that was already loaded switches instantly and refreshes in the background if older than a minute.
  - A refresh that brings the same snapshot (one isn't out yet) leaves the list as it is, so reading position, keyboard focus and selected text stay. Screen readers announce the status line's view, item count and "refreshing...", but not its age ("updated 3m ago"), which changes every minute.
  - A slow response for one view can never overwrite another view's list.
- The header's top right shows the moon's phase now, worked out in the browser (mean elongation plus Meeus' main periodic terms: the exact phases within about an hour), drawn as 16×16 square pixels like the site icons between four single-pixel stars that twinkle, a pixel cloud stepping across the top of the disc every 20 s (only where it covers the moon; both still with reduced motion), with its name and how much is lit ("waxing gibbous 87%"; the title adds when the next full or new moon is). It's lit on the right while waxing, mirrored for visitors from southern-hemisphere countries (`CF-IPCountry`, `services/geo.py`). It's redrawn every 30 s if it changed.
- Each item shows the title (without Google's " - Publisher" suffix), the domain, the source and age, plus "discuss" for Hacker News posts (a feed's comments link is dropped when it's just the article itself) and "summary" for items with one, which opens it under the item, labelled as a machine summary (it stays open across refreshes). A story that other outlets in the view also carry shows "+N sources", which opens their headlines (linked, with their source) under it.
- Other controls:
  - `r` refreshes;
  - `/` (or [/]search) opens a search box that searches **every tab's** headlines, sources and summaries, in the browser. Opening it loads every tab's headlines in one response, `/api/search-index` (each link once, with the views it's in: about 300 KB compressed, where loading the 42 views one by one took 41 requests and 540 KB), reused for 5 minutes; if it can't be loaded, the status line says so and reopening search tries again (an older copy, if any, stays in use). Each keystroke filters in memory: every word typed must start a word in the text (case and accents ignored; "elect" finds "election", "ai" doesn't find "said"); headline matches first, then summary matches, newest first, whatever the sort, the second group starting with an "· in summaries ·" line (it also holds the rare match found only in a source's name); the same story in several tabs (the same `story`, though each tab may lead it with another headline, or the same link or headline) is one result, labelled with its tabs ("every region / science"), shown by a headline of it that matches, with its other headlines (from every tab) under it as sources. At most 100 are shown. **Older headlines** from the archive (`/api/search`), as stories, join the same list, in the same two groups and order, so a headline match from last week comes before a summary match from today; they have no tabs label. They're asked for once typing pauses for 60 ms, a newer keystroke cancelling the request in flight; the last 30 answers are kept (with every page loaded), so going back to a query shows them at once. They come 50 at a time: while the archive has more, a "more headlines" link adds the next 50 (`next`, below), placed where they'll be listed: after the headline matches while there are older headline matches to come, then at the end of the list; it reads "loading more headlines..." meanwhile and keeps the keyboard focus. Search results aren't numbered (a tab's items are, by rank): each page adds results among those listed, by date and match, so a position would keep changing. An archived story the tabs' search found, or one listed already (a story can come back on a later page), adds its headlines to that result's sources instead; other headlines the tabs' search already found (same link or headline) are left out, as are repeats among them; a tab's headline that the tabs' search missed (a misspelled word the archive corrected) comes from the archive. The status line counts both, says which typed words were searched as another ("showing “ukraine” for “ukrane”"), "searching older headlines..." while waiting, and "couldn't search older headlines (…)" if the request fails. When nothing matches anywhere, a notice says "nothing matches “…”" (not before the archive has answered). Matched words (two letters or more) are marked in headlines, sources, summaries and sources lists: bold with a dotted accent underline, built from text nodes and `<mark>` elements, never HTML; a result found only in its summary shows the words around its first match. `Esc` or picking a tab closes it.
  - `a` (or [a]dvanced) shows a second row: **reset** (forgets every setting on this device: region, topic, sort, theme, bypass, site icons, auto refresh, back action, and closes open summaries), **install** (the browser's own install prompt, when it offers one; the site has a web app manifest, so Chrome can install it), **site icons** (on by default: each website's icon before its headlines, at text size), **back action** (`exit` by default: Back leaves the site; `last tab`: Back returns to the previous tab), **auto refresh** (cycles off, 5m, 15m, 30m, 1h: the open view is fetched again once it's older than that, checked every 30 s and on returning to the tab, never while the tab is hidden), and **bypass** (off by default; "archive" adds a "via archive" link beside headlines from known paywalled sites, to `archive.ph/newest/<url>`, which goes straight to the latest snapshot; the headline keeps the site's own link);
  - `s` (or [s]ort) cycles the order: **random** (the default: shuffled each time the page is opened or it's chosen, kept across refreshes while the page stays open, so a visit starts with other headlines than the last; the mix's top changes only as fast as the feeds' own), mix (the server's: each feed's own ranking, interleaved by weight), newest first or oldest first; remembered;
  - `d` toggles dark mode.

| Endpoint | Response |
| --- | --- |
| `GET /` | Main page (with `Vary: Accept`); small JSON status only when the client accepts `application/json` and not `text/html`. Crawlers and tools sending `*/*` or nothing get the page. |
| `GET /robots.txt`, `GET /sitemap.xml` | Built from the request's host. Search engines and AI search readers may crawl; AI training crawlers are disallowed, with `Content-Signal: search=yes, ai-train=no`. (Cloudflare's managed robots.txt, used while the origin had none, is no longer served.) |
| `GET /archive`, `GET /retro` | Old URLs; redirect to `/`. |
| `GET /static/retro.css`, `GET /static/retro.js` | The page's stylesheet and script. With the current `?v=` hash (the page's links): cached for a year (`immutable`); otherwise `no-cache`. |
| `GET /retro` | Old evaluation URL; `301` redirect to `/`. |
| `GET /api/headlines?region=<view>&topic=<topic>` | Snapshot JSON for `all` (default) or a region id, and optionally one topic: `region`, `topic` (null for a main view), `data` (each item: `title`, `link`, `source`, `published`, `last_updated`, `comments` or null), `fetched_at`, `expires_at`. **404** for an unknown region or topic (the message lists valid ones). **503 with `Retry-After: 10`** only before that view's first snapshot exists. |
| `GET /api/search?q=<words>[&after=<next>]` | Older stories from the archive, for the page's search (`services/archive_search.py`, read-only): `items` (up to 50 stories, each once per answer, as its newest matching headline with the story's other archived headlines under it, newest first, at most 20 (`related`: title, link, source, comments), and its `story`: `title`, `link`, `source`, `published` (when first seen, for a headline only ever folded under another), `summary`, `comments`), `words` (the words searched, corrected ones replaced, for highlighting) and `corrected` (`[{"from": "ukrane", "to": "ukraine"}]`) and `next` (null when there are no more; else passed as `after` for the next 50, each match once: `t<rowid>`, headline matches older than it, `r<rowid>` or `r`, the rest older than it or from the newest; under 4 ms a page on the 24-day archive). The query is split into words as the index splits text (anything but letters and digits is a break, so search syntax is never passed through; at most 10 words, 200 characters); every word must start a word in the title, source or summary, case and accents ignored. Headline matches first, then the rest, each newest by the date published (the date the page shows). The 50 are picked by when the archive first had them, which the index follows without sorting every match, then sorted; a headline a feed picked up late can come a page later than its date, never misplaced on its page (the page sorts every page loaded). With `SEARCH_TYPO_FALLBACK`, a word of 4 letters or more that starts no word at all is searched as the known word one edit away (a letter deleted, added, changed or two swapped) that the most items have: 5–8 ms on a year's words, only for such words. **In memory:** the refresher writes the archive's search index after every cycle (`write_search_index`, `services/archive_index.py`) into `/archive/search-index.bin`, renamed over the previous one, and every app worker maps it into memory (`mmap`, checked every 10 s), so the four share one copy. It holds, for every word of the titles, sources and summaries and every 1–3 letter prefix of them, the headlines that have it, anywhere and in the title, as roaring bitmaps (pyroaring: compressed bitmaps whose AND and OR work on whole blocks with AVX2), read from SQLite's own index (`fts5vocab`, so words are split and folded exactly as SQLite does), plus each headline's story and each story's headlines. A search reads the bitmaps it needs from the file (~1 µs each), and only its results' rows come from SQLite, over a connection the worker keeps; it runs in the request's own thread (no thread hand-off) and its JSON is written by orjson. On 1,324 queries and 2,877 pages of the real archive the answers were identical to SQLite's. Measured on 100,269 headlines: writing the file takes 1.2 s and 153 MB at the refresher's peak (65 MB after `malloc_trim`), the file is 23 MB; a worker maps a new one in 10 ms and keeps ~11 MB of its own (the word list); finding the matches takes 2–32 µs, a whole search 0.2–0.26 ms; with the file rewritten 3 times under 10 users' searches, the slowest search took 31 ms. Until the first file exists, SQLite answers (in a thread); a headline is found from the next file on, at most a cycle later. The file ends with a marker: one cut short (a full or failing disk) is refused, and so is any file that isn't an index; a worker then keeps the index it had, or SQLite, logs it once and tries again only with a new file. A write that fails midway leaves no temporary file behind. Cached 5 minutes (`Cache-Control` and `CDN-Cache-Control`). **503** without an archive, or before it has its index. **422** for an empty or too long `q`, or a malformed `after`. |
| `GET /api/search-index` | Every view's headlines in one response, for the page's search: `items` (each link once: the item as in `/api/headlines`, plus `views`, the views it's in, e.g. `["all", "asia", "asia:tech"]`, in tab order), `fetched_at` (the newest view's), `expires_at` (the soonest any view is due to be refreshed). Cached like `/api/headlines`. **503 with `Retry-After: 10`** only before the first snapshot exists. |
| `GET /health` | `healthy` / `degraded` / `unhealthy`, plus the "all" view's `headlines.age_seconds` and `headlines.stale` (see §8). |
| `POST /admin/cache/clear` | Requests an immediate refresh; requires `X-API-Key`. |
| `GET /metrics` | Prometheus metrics (blocked at Caddy). |

### Caching: browsers and Cloudflare

Each cacheable response says how long it stays fresh (`Cache-Control`), and browsers and Cloudflare read the same header. Only two things exist for Cloudflare alone:

1. **`CDN-Cache-Control`** on `/api/headlines` and `/api/search-index` (`_cached()` and `EDGE_MIN_SECONDS` in `routes/headlines.py`). Browsers ignore it. It keeps an edge copy for at least 60 s even when the next refresh is due sooner, serves the old copy for up to 60 s while fetching a new one (`stale-while-revalidate`), and for a day if this server can't be reached (`stale-if-error`).
2. **The cache rule** in the Cloudflare dashboard (not in this repo; README, Cloudflare Tunnel). Cloudflare doesn't cache JSON or paths without a file extension unless a rule says so.

Without Cloudflare nothing needs changing: everything else is browser caching, which Cloudflare reuses.

| Path | `Cache-Control` | At Cloudflare |
| --- | --- | --- |
| `/`, error pages | none | Never cached, and must stay so: each response has its own CSP nonce and the visitor's starting tab and moon (from `CF-IPCountry`). |
| `/api/headlines`, `/api/search-index` | seconds until the next refresh is due | By the cache rule, for `CDN-Cache-Control` (above). |
| `/api/search` | 5 minutes | Only if the cache rule lists it (it must keep the query string in the cache key); otherwise each search reaches the server, which is fine at a few ms each. |
| `/static/retro.css`, `/static/retro.js` | current `?v=` hash: a year, `immutable`; otherwise `no-cache` | Cached without a rule (`.css`/`.js`). The query string must stay in the cache key. |
| `/site-icon/<domain>` | 7 days; 404: 5 minutes | By the cache rule. |
| `/favicon.svg`, `/favicon.ico`, `/apple-touch-icon.png`, `/icon-192.png`, `/icon-512.png` (Caddy) | 7 days | Cached by file extension; the rule lists all of them except `favicon.svg`, which makes no difference. |
| `/manifest.webmanifest` (app, named from `config/site.yaml`) | 1 day | By the cache rule. |

Cloudflare settings this depends on:
- **Caching level: Standard**, so the query string is part of the cache key. With "Ignore query string", a new `?v=` would get the old file for a year.
- **No "Cache everything" for HTML**: the page and error pages stay uncached (above).
- **Browser Cache TTL** is Cloudflare's default, 4 hours. For paths outside the rule, Cloudflare raises a shorter `Cache-Control` to 4 hours. That only shows on `/static/` with an outdated `?v=` (`no-cache` reaches the browser as `max-age=14400`), which the page never requests. Adding `/static/*` to the rule, or setting Browser Cache TTL to "Respect existing headers", would let `no-cache` through.

After a deploy: the page isn't cached, so visitors get it at once, and its new hashes give the stylesheet and script new URLs. An edge copy of the API can be up to about two minutes older than the newest snapshot (the 60 s minimum, then 60 s stale while it's re-fetched). Icons already at the edge keep their headers for up to 7 days; purge `/site-icon/*` if those headers change.

## 6. Topic Tagging

The tagger (`tagger/`) gives each headline zero or more of `tech`, `science`, `nature`, `culture`, `sport`, `uplifting`. Most hard news (politics, crime, economy) gets none.

**How it tags:**
- **Embedding:** `bge-small-en-v1.5` (33M parameters, MIT licence), fine-tuned on the seed labels (below), run by OpenVINO on the Intel NPU. The NPU needs static shapes, so titles are padded to batches of 32 × 48 tokens. The first start compiles the model for the NPU (~1 min); the result is cached in the volume, so later starts take seconds.
- **Classifier:** two logistic regressions on the embedding: one over the topics plus `other` (a tag when its probability is ≥ 0.9), one for uplifting or not (≥ 0.8; not used for views, see Step 5). Uplifting is a tone, not a subject, so it can combine with a topic.
- **Labels, no hand-labelling needed:** feeds imply labels. A Google News Sports section's items are `sport`, a good-news outlet's are `uplifting`, a national news section's are `other`. The bundled `tagger/data/seed.json` (3,250 such headlines) trains the first classifier; every refresh adds more.

**Storage:** SQLite (`/data/tagger.db` in the `tagger_data` volume, WAL mode). One row per headline, by cleaned title: source, link, feed label, tags, the classifier version that tagged it, and its embedding (float16, 768 bytes). A headline is embedded only the first time it's seen; after a retrain, stored headlines are re-tagged from their stored embeddings without re-embedding. Each headline takes roughly 2.8 KB with its story embedding. Headlines first seen more than `RETENTION_DAYS` (**35**) ago are dropped daily, before retraining, except the seed's (grouping looks back 3 days, verdicts last 30, training takes ~18 days of labelled headlines); one seen again later is stored afresh. Without it the database grew ~25 MB a day. Its write-ahead log is capped at 64 MB once checkpointed (`journal_size_limit`), as is the archive's.

**Retraining:** every `RETRAIN_INTERVAL_HOURS` (**24**, counted from the last attempt, kept or not: `/data/last_retrain` records it, so a restart neither postpones a retraining nor starts one at once because rejected candidates left the saved classifier old), on the newest 100,000 labelled headlines in the database, plus always the 3,250 seed headlines (they hold most of the few `nature` and `uplifting` examples: on Oct 27's data a window without them scored macro F1 0.598 instead of 0.618). The candidate replaces the current classifier only if its macro F1 on the hand-labelled `tagger/data/eval.json` (902 headlines, never trained on) is at least as good. Both scores are logged. Training runs on its own thread; tagging continues meanwhile, but slower: while a retraining ran at the old 2-CPU limit, tagging took 24 s instead of 0.6 s and grouping over 60 s instead of 5 s, so the refresh's topic grouping timed out. Measured: the first retrain, after one cycle's new labels, went from F1 0.623 to 0.633 and from 8.9% to 8.1% hard-news leak (with the base model, before fine-tuning).

**Hardware:** the embeddings run on the NPU; the classifier's predictions, the story-grouping maths and retraining run on the CPU (scikit-learn). The container may use **8 CPUs**: retraining's fit takes ~44 s at 8 and 5.6 minutes at 2 (Oct 27, 97,001 headlines; reading and converting them takes ~1.5 s, the fit is the rest). The integrated GPU is not used. Compared on the same data: the same objective fitted with PyTorch took 18–21 s on 8 CPUs and 10–13 s on the integrated GPU (Intel Arc, through PyTorch's XPU build and Intel's newer GPU runtime), with the same tags on 99.4% of 5,000 recent headlines and macro F1 0.624 against scikit-learn's 0.622 (within the evaluation set's noise). Not worth a ~10 GB GPU image, a second GPU runtime and fitting code of our own for ~30 s a day; PyTorch stays where heavy training belongs, `finetune.py` on a GPU machine.

**Evaluation set:** `tagger/data/eval.json` has 902 hand-labelled headlines: 325 from the first version (48 of their labels were corrected, e.g. hurling and rugby headlines labelled `tech`, obituaries labelled `science`), 273 added from three days of live headlines in October (120 general news, the rest from topic feeds, labelled without seeing the feed), and 304 from Nov 5–10's (130 general news, 29 from each topic's feeds, labelled the same way), so the gate also judges on recent news. Labelling rules: tech covers technology, AI and video games; science covers research, space and medicine; nature covers environment, climate, weather and wildlife; culture covers entertainment, celebrities, arts and fashion; uplifting is a tone and can combine with a topic; politics, crime, economy and conflict get no tag. On it, the classifier in use scores F1 0.648 with 5.3% hard-news leak; a retrain on the 15,900 labelled headlines stored after three days scored F1 0.636 with 3.2% leak, so the gate (F1 only) kept the current one. Measured on Nov 10: the classifier in use (trained Oct 19) scores F1 0.651 on the first 598, 0.634 on the 304 recent ones and 0.646 on all 902 (7.2% leak); a retrain on that day's data scores 0.617, 0.647 and 0.629 (9.1% leak), so the gate still keeps the one from Oct 19: newer data doesn't make a better classifier yet, on old or recent headlines taken together.

**Why this design (benchmarked on the first 325 hand-labelled headlines):**

| Approach | Macro F1 | Hard news wrongly tagged | Speed |
| --- | --- | --- | --- |
| Keyword rules | 0.46 | 6% | — |
| Embeddings, zero-shot (label descriptions) | 0.55–0.57 | 17–36% | fast |
| bge-small + classifier trained on feed labels | 0.62 | 9% | 259/s on the NPU (101/s on the CPU) |
| **bge-small fine-tuned on feed labels + classifier (used)** | **0.66** | **10%** | same |
| EmbeddingGemma-300M + classifier | 0.64–0.65 | 3–6% | 102/s on the NPU, ~2.8 GB |
| Qwen3-4B LLM, tuned prompt (iGPU) | 0.62 | 14% | 0.8/s, ~3 GB |
| Qwen3-4B LLM on the NPU | 5 of 20 right | — | 7.9 s each |
| Qwen3-8B LLM (AWQ), same prompt, desktop RTX 3060 Ti | 0.63 | 10% | 17/s |

bge-small was chosen over EmbeddingGemma for memory (the tagger uses ~450 MB) and because its NPU and CPU embeddings are identical, so the CPU fallback works with the same classifier. EmbeddingGemma's NPU embeddings drift from its CPU ones (fp16). The iGPU is shared with other services and gave a modest gain only.

**Fine-tuning:** `tagger/finetune.py` trains the whole of bge-small (not just the classifier on top) on `seed.json`, then exports it to ONNX with fp16 weights. It needs a CUDA GPU (a desktop RTX 3060 Ti took ~20 s) and is deterministic: the same seed gives the same file, whose checksum the Dockerfile pins. On the NPU it scores F1 0.665 (base model 0.621) and 9.8% leak (8.9%: one more of the 123 hard-news headlines). Its NPU and CPU embeddings still match, so the CPU fallback is unaffected. The daily retrain keeps training only the classifier, on the fine-tuned embeddings. Replacing the model later needs care: stored embeddings come from the model that made them, so a new `model.onnx` would need every stored headline re-embedded first (not implemented).

**Story grouping (`POST /group`).** The topic model can't tell stories apart: fine-tuning on topic labels puts every football headline at a cosine similarity of 0.95–0.98 with every other. Grouping uses a second model, `multilingual-e5-small` (118M parameters, MIT licence, fp16 ONNX, mean pooling with the `query: ` prefix), which also matches the same story across languages. Its embedding is stored with the headline (`story_embedding`) the first time it's grouped. **Stories last**: a headline gets its story once, when first grouped, and keeps it, so a story is the same in every view, refresh and the archive (its id is its first headline's rowid). Headlines without one are taken in the caller's order (top positions first); each joins the story of the most similar headline first seen up to 3 days before it (`STORY_WINDOW_SECONDS`) at cosine ≥ `STORY_THRESHOLD` (**0.92**), if it is also ≥ `STORY_LEADER_THRESHOLD` (**0.88**) to that story's **leader** (its first headline) and the leader is at most 4 days old (`STORY_MAX_AGE_SECONDS`), or starts a new story. The window and age were chosen on all 115,566 grouped headlines of Oct 12–31, 2026, given stories in order of arrival: one study reported from Oct 28 to 31 by 16 outlets ("Comets probably didn't seed life on Earth…") is one story of 9, the judge's verdicts joining the rest; the largest stories are real ones (a train crash, 72 headlines over 2 days) or templated series, which a 7-day age let grow to a week (horoscopes, puzzle answers) and 4 days caps. Merging two stories whenever a new headline fits both chained different news (a train crash and a train strike), so it doesn't. A database from before stories gets them when the tagger starts (`assign_all`: 115,566 headlines in 84 s, once). `POST /stories` gives the stories of any headlines (the archive's) without storing unsure pairs. Matching any member catches a story worded several ways (one dog-ageing study was in three groups when only leaders counted); the leader's floor stops chains (A like B, B like C, but C unlike A). On the live headlines of Oct 15, 2026 it added 18 same-story pairs and no wrong ones, where lowering the threshold to 0.90–0.91 added 87–121 pairs with a few wrong (different matches' live blogs, different HomePod stories). Chosen on 323 hand-labelled pairs of live headlines (`tagger/data/story_pairs.json`: the 250 most similar pairs by each of three candidate models): at 0.92 it finds 80% of same-story pairs. Its wrong merges were templated series, which reading-wise are welcome as one item (horoscopes for different signs, daily puzzle answers, "Good News in History, October 12/13"), and one pair of different comparison articles. The other candidates: base bge-small-en found 72% at its best clean threshold, paraphrase-multilingual-MiniLM 38%. fp16 on the NPU gives the same groups as fp32 at 310 headlines/s (265/s on the CPU); the first compile takes ~50 s and is cached.

**Unsure pairs, judged later by the LLM.** Pairs of a grouping's headlines at cosine ≥ `STORY_CANDIDATE_THRESHOLD` (**0.84**) that are in different stories are stored in the tagger's `story_candidates` (up to 2,000 per grouping, the top positions first; ~2,100 on the live headlines of Oct 15, 2026, stored in ~0.1 s), each headline with the closest headline of each such story. So are its pairs at ≥ `STORY_THRESHOLD` (0.92) with headlines of other stories first seen in the last 3 days, no longer in the views: a story split when its headlines came in a different order ("Did comets really deliver…" from CNN, 0.935 to the Times of India's "Did comets really start life…", started a story of its own) can still be joined. That adds ~180 pairs a grouping (0.3–0.5 s); at 0.84 it would add ~35 per headline. When nothing waits to be summarized, the summarizer asks its LLM about them (`GET /story-candidates`, ~0.1 s a pair with qwen3:8b, thinking off) — "same news (event, announcement, study, match, release, incident), even from a different angle?", one word back — and returns the verdicts (`POST /story-verdicts`, table `story_verdicts`). A "yes" merges the two stories into the older one; a "no" on two headlines of one story moves the later one (never the leader) out to a story of its own, so it can also split a wrong e5 match. Each change is logged (`story_moves`) for the archive to follow. Every later grouping treats a "yes" as the most similar and a "no" as the least. Judging runs for at most `STORY_JUDGE_SECONDS` (240 s) per pass; with X-PC off nothing is judged and grouping is e5 alone. Candidates expire after a day unseen, verdicts after 30 days. On 152 hand-labelled unsure pairs (e5 cosine can't separate them: it finds 1 of 56 same-story pairs without wrong merges) this prompt found 34/56 with 3 wrong merges, all close calls (two previews of one match, two NFL pick lists); a stricter prompt found 18/56, a looser one 47/56 with 10 wrong; thinking mode was 60x slower and no better. A cross-encoder on the NPU (bge-reranker-v2-m3) found 26/56 but needs ~1.5 GB of memory; parked in TODO.

**Precision to expect:** the benchmark's best F1 (0.66) is at a 0.5 threshold, but there 1 in 4 topic tags on the evaluation set is wrong, and live news is mostly hard news, so wrong tags flooded the views. At 0.9, 4 in 5 are right (it finds 42% of topic stories) and 4% of hard news gets a tag (10% at 0.5). Topic views take feed items first anyway, so model tags mainly fill nature views and thin regions. Uplifting views come only from the three good-news feeds, which post a few items a day.

| Tagger resource | Value |
| --- | --- |
| Memory | ~860 MB in use with both models (470 MB process, 390 MB NPU buffers); ~1.5 GB peak on the very first start (two NPU compiles, training, embedding every headline). Limit 2 GB. |
| CPU | Limit 2. Idle between refreshes; a refresh tags its few hundred new headlines in about a second. |
| Device | `/dev/accel` plus the host's render group (`RENDER_GID`). Without an NPU, remove `devices`/`group_add` in `docker-compose.yml` and it uses the CPU. `GET /health` reports the device. |

## 7. Rate Limiting

| Aspect | Behaviour |
| --- | --- |
| Limit | `API_RATE_LIMIT`, default **100 requests per minute per client IP**, fixed window. |
| Routes limited | `GET /`, `GET /api/headlines` (all regions share one counter), `GET /api/search-index` and `GET /api/search`, each route with its own counter at `API_RATE_LIMIT`; `GET /site-icon/<domain>` at `ICON_RATE_LIMIT` (**1000/minute**: a page shows up to ~100 icons); `POST /admin/cache/clear` at `ADMIN_RATE_LIMIT` (**10/minute**). `/health`, `/metrics` and the `/retro` redirect are not limited. |
| Client identity | The `X-Real-IP` header, which Caddy always overwrites with the IP it resolved. Clients connecting from untrusted addresses can't influence it; trusted proxies (the Docker network, where the Cloudflare Tunnel connector connects from) forward the real visitor IP. |
| When exceeded | `429` with `{"error": "Rate limit exceeded: 100 per 1 minute"}`. |
| Storage | Counters live in Redis, so limits are shared across all workers and containers. Checks use 0.5s timeouts and no retries. |
| Redis down | Limits fall back to per-process memory automatically and switch back when Redis recovers. Requests are never failed because of the limiter. |
| Implementation note | Limits are applied per route with `@limiter.limit`. slowapi's middleware is not used because it can't see routes inside FastAPI's included routers and silently skips them. |

A visitor makes 2 requests per page load plus 1 per region viewed, so 100/minute is far above human use. Users behind one shared IP (e.g. an office NAT) share a budget; raise `API_RATE_LIMIT` if that matters.

## 7b. Security Hardening

- **Fetching links from outside** (the summarizer reads any top story's link; anyone can post one to Hacker News; the refresher fetches feeds, Google links and icons): only public internet addresses are fetched (`services/safe_fetch.py`). Private, loopback, link-local and Tailscale (100.64/10) addresses are refused, so a crafted link can't make the server read the LAN or the tailnet. The refresher's and summarizer's HTTP client enforces it when it connects (`public_client`): it resolves the host then and connects to that address, so a DNS answer that changes after a check (DNS rebinding) is refused too. Only our own services (the tagger, the LLM, the browsers) are reached otherwise. Each URL is also checked before its request and at every redirect, so an unusable link is told apart from a failed one. Summaries containing a link are discarded, since an article could try to steer the model. The headless browser that reads bot-protected sites can't make that check itself, so it has no network access except a proxy that enforces it (see the headless browser in §3).
- **Page:** a Content-Security-Policy with a fresh nonce per response allows only the page's own script and stylesheet (the `<script>` and `<link>` tags carry the nonce), images and requests from its own origin, and no framing. Feed text, summaries and titles are inserted as text, never HTML; links must be http(s) and open with `noopener`.
- **Error pages** (404, 500, and the fallback served if templating fails) use the page's stylesheet and load nothing from other sites, under a Content-Security-Policy that allows only stylesheets and images from this site.
- **Client IP:** the rate limit keys on the IP Caddy resolves from trusted proxies only. Through the Cloudflare Tunnel (`cloudflared` service), Cloudflare sets `CF-Connecting-IP` and rejects client-supplied ones (error 1000), and a forged `X-Forwarded-For` is ignored because `CF-Connecting-IP` comes first. Any other proxy put in front would have to overwrite client-supplied IP headers before they reach Caddy.
- **Searches aren't logged:** the page's own search runs in the browser, but older headlines are searched on the server, so what visitors type reaches it. Neither access log records it: Caddy's log filter deletes the `q` of every request (`caddy/Caddyfile`), and the app's Uvicorn access log shows `/api/search` without its query (`RedactSearches` in `logging_config.py`). Cloudflare's own logs are outside this repo.
- **Headers:** Caddy sets HSTS, `X-Content-Type-Options`, `X-Frame-Options` and `Referrer-Policy` on every response.
- **Stats worker:** on an internal network that reaches Redis and nothing else, as a Redis user that can only read its stream. Its page takes a strict Content-Security-Policy from Caddy and shows feed text as text.
- **Containers:** every service has CPU, memory and process limits, rotated logs (10 MB × 3), `no-new-privileges`, and no Linux capabilities (Redis keeps the three its entrypoint needs to switch user). Redis requires a password (`REDIS_PASSWORD`). Images are pinned by version (`scripts/check-updates.py` reports newer ones), `cloudflared` included; the app's Python packages are pinned with their hashes.
- **Networks:** the app, the tagger and Redis are on an internal network (`backend`) with no internet access; the refresher, the summarizer and Caddy join it, and only they (and the browsers' proxies) reach the internet. The tagger's API has no login and can change story groupings, so only the refresher and the summarizer reach it. The app's port is never published: it trusts the client IP Caddy sends.
- **Admin endpoint:** disabled unless `ADMIN_API_KEY` is set; the key is compared in constant time; only reachable from this machine and the LAN (Caddy), and rate limited.
- **API errors** carry only a message: no request URL, no version.
- **Tagger growth:** retraining uses the newest `TRAIN_MAX_ROWS` (100,000, about 2–3 weeks at ~5,500 labelled headlines a day) plus the seed, so its memory stays bounded as headlines accumulate: their embeddings go straight into one float32 array (measured +153 MB at 99,000; ~0.8 GB at 500,000), and the fit takes ~45 s on 8 CPUs. A larger window wasn't shown to help: on the hand-labelled set (all from Oct 12–13) a window scores higher the more of that week it keeps, which says nothing about current news; revisit with a current evaluation set.

## 8. Failure Behaviour

| Failure | What users see |
| --- | --- |
| One feed slow or down | Fresh headlines from the remaining feeds in that region (partial views are published). |
| All of a region's feeds down | That region keeps its previous snapshot; other regions update normally. |
| All feeds or Google down | The previous headlines in every view. |
| Custom link resolver broken | Links resolved by the fallback decoder (`redirect_fallbacks_total{outcome="success"}` rises). |
| Both link resolvers broken | Headlines published with Google News links, which still open the article via Google. Retried every refresh. |
| Tagger down or slow | Main views update normally; topic views keep their previous snapshots until it's back. |
| NPU unavailable | The tagger runs on the CPU (same results, ~2.5× slower); `GET /health` shows `"device": "CPU"`. |
| Refresher crashes or hangs | Last snapshots keep being served. Docker restarts crashes; hangs are cut off by the 270s cycle timeout. `/health` reports `headlines.stale: true` once the "all" snapshot is older than `STALE_AFTER_SECONDS` (15 min). |
| Redis down | Every worker keeps serving its in-memory copies; rate limits fall back to memory. `/health` reports `degraded` (HTTP 200), so Caddy keeps routing. Measured: 46,000+ requests at ~2,300 req/s during a 10s outage, 100% OK, p99 ≈ 420ms (same as with Redis up). |
| Redis down and a worker has no snapshot | That worker returns 503 for that view, and its `/health` fails if it has no "all" snapshot. Only possible if Redis was empty or down when the worker started. |
| Redis restarted | Snapshots, resolved URLs and counters are reloaded from disk. |
| Stats worker stopped or failing | The site is unaffected. `/stats/` keeps its last file and shows its age. The worker catches up from the stream when it restarts; entries older than the stream's cap are skipped and logged. |
| Stats stream can't be written | Logged (`Stats stream not written`); the refresh carries on and sends those links next time. |
| `heatmap.json` missing or unreadable | `/stats/` says the stats are offline and shows a plain map. |
| App down | `/stats/` still loads, unstyled, as Caddy serves it. |
| Fresh install | `/api/headlines` returns 503 (`Retry-After: 10`) for the few seconds until the first refresh completes. Topic views follow one cycle later (5 min): the tagger's first start takes ~1.5 min. |

`/health` returns 503 only when the worker cannot serve headlines at all. Staleness never fails the probe, because older headlines are better than taking the API offline.

## 9. Observability

**Stats** has no metrics. Its log says when Redis is unavailable, entries are skipped, or a whole batch is unusable, and `/stats/heatmap.json` carries `generated_at`, which the page shows as its age.

**API metrics** — `app:8989/metrics`, scraped from inside the Docker network (Caddy blocks it publicly). These are aggregated across all Uvicorn workers via Prometheus multi-process mode: `PROMETHEUS_MULTIPROC_DIR`, cleared on container start by `entrypoint.sh`.
- `http_requests_total`, `http_request_duration_seconds`, `http_requests_inprogress`, etc.
- `redis_errors_total{operation}`

**Refresher metrics** — `refresher:9100/metrics`:
- `headlines_refreshes_total{result}`: `success`, `empty`, `timeout`, `error`
- `headlines_refresh_duration_seconds` (histogram)
- `headlines_last_refresh_success_timestamp_seconds`
- `headlines_fetched_count{region}`: items in each published view (topic views as `asia:sport`, …)
- `headlines_topic_refreshes_total{result}`: topic phase — `success`, `error`
- `feed_fetch_failures_total{source}`
- `redirect_resolutions_total{outcome}`: custom resolver — `cache_hit`, `redirect`, `batchexecute`, `no_cwiz`, `parse_failed`, `failed`
- `redirect_fallbacks_total{outcome}`: fallback — `success`, `failed`

**Suggested alerts:**
- No successful refresh in 15 minutes: `time() - headlines_last_refresh_success_timestamp_seconds > 900`.
- A region running thin: `headlines_fetched_count < 20`.
- Custom resolver degrading: `rate(redirect_fallbacks_total[30m]) > 0`. Any fallback activity means the primary is failing on some links.
- Both resolvers failing: `rate(redirect_fallbacks_total{outcome="failed"}[30m]) > 0`.
- A feed failing repeatedly: `increase(feed_fetch_failures_total[1h]) > 3`.
- Topic views not updating: `increase(headlines_topic_refreshes_total{result="error"}[30m]) > 3`.

**Tagger** — `docker compose logs tagger` shows each retrain's scores; `GET /health` (inside the network) the device, classifier version and stored headline count.

## 10. Configuration Reference

**Environment / `.env`** (see `.env.example`):

| Variable | Default | Purpose |
| --- | --- | --- |
| `ALLOWED_HOSTS` | `localhost` | Hostnames the app accepts (required). |
| `ADMIN_API_KEY` | — | Key for `/admin/*` (required). |
| `API_RATE_LIMIT` | `100/minute` | Per-IP limit (§7). |
| `UVICORN_WORKERS` | `4` | Workers per app container; keep equal to `APP_CPUS`. |
| `APP_CPUS` / `APP_MEM_LIMIT` | `4` / `2G` | App container limits. |
| `CADDY_HTTP_PORT` | `7001` | Caddy's port (this machine and the LAN). |
| `ENABLE_API_DOCS` | `false` | Serve `/docs`. |
| `RENDER_GID` | `991` | Numeric id of the host group owning `/dev/accel/accel0` (`getent group render`), for the tagger. |

**Tunables in `app/config.py`** (their only defaults; override with an environment variable of the same name, which reaches a container only if `docker-compose.yml` passes it):

| Setting | Default | Meaning |
| --- | --- | --- |
| `REFRESH_INTERVAL_SECONDS` | 300 | Pause between refresh cycles (see §4, Step 6). |
| `REFRESH_TIMEOUT_SECONDS` | 270 | A cycle (both phases) taking longer is abandoned; previous snapshots stay. |
| `TOPIC_REFRESH_TIMEOUT_SECONDS` | 150 | Cap on the topic phase; on timeout the topic views keep their previous snapshots. |
| `TOPIC_FEEDS_INTERVAL_SECONDS` | 1800 | How often the topic feeds are re-fetched. |
| `TAGGER_URL` / `TAGGER_TIMEOUT` | `http://tagger:8000` / 90 | The tagger, and the timeout of one tagging request. |
| `STATS_STREAM_ENABLED` / `STATS_STREAM_MAXLEN` / `STATS_STREAM_TIMEOUT_SECONDS` | true / 30,000 / 5 s | The stream of fetched headlines for the stats service. |
| `SUMMARIZER_LLM_URL` / `SUMMARIZER_MODEL` | empty / `qwen3:8b` | The Ollama server (empty disables summaries) and its model. Set in `.env`. |
| `SUMMARY_TOP_N` / `SUMMARY_TTL_SECONDS` / `SUMMARY_FAILED_TTL_SECONDS` | 20 / 7 days / 1 day | Items per view that get a summary; how long a summary, and a failed article, are kept. |
| `SUMMARY_RETRY_SECONDS` | 3 hours | When an article no step could read is tried again. |
| `SUMMARIZER_BROWSER_URL` / `SUMMARIZER_BROWSER_PROXY` / `SUMMARIZER_BROWSER2_URL` | set in `docker-compose.yml` | The headless browser, the public-only proxy it must use, and the second browser; empty disables a browser step. |
| `BROWSER_HOST_TTL_SECONDS` | 7 days | How long a site that blocked a plain request goes straight to the browser. |
| `FEED_TEXT_TTL_SECONDS` | 2 days | How long the feed's own text of a published item is kept. |
| `ICON_SOURCE_URL` / `ICON_FALLBACK_URL` | DuckDuckGo / Google (`sz=64`) | The icon service, and where a website's icon is looked for when neither the service nor the website has one (redrawn as pixel art; empty disables it). |
| `ARCHIVE_PATH` / `ARCHIVE_SNAPSHOT_SECONDS` | empty (`/archive/archive.db` in `docker-compose.yml`, for the refresher and the app) / 3600 | The headline archive's SQLite file (empty disables it, and the search of older headlines), and how often each view's order is recorded in it. |
| `SEARCH_TYPO_FALLBACK` | true | Searching older headlines: a word nothing starts with is searched as the closest known word ("ukrane" as "ukraine"). False: prefixes only. |
| `MAX_HEADLINES` | 100 | Headlines per view (each region, and "all"). |
| `STORY_GROUP_HEADROOM` / `MAX_RELATED` / `STORY_GROUP_TIMEOUT` | 40 / 8 / 60 s | Views are built this much longer before each story is shown once; other outlets' headlines kept under a story; the tagger's grouping call timeout. |
| `STORY_SYNC_TIMEOUT_SECONDS` | 60 | Cap on the refresher's step that keeps the archive's stories up to date. |
| `MAX_HEADLINE_AGE_HOURS` | 48 | Items older than this are dropped before mixing. |
| `MAX_PER_PUBLISHER` | 3 | Maximum items from one publisher in a view (single-publisher feeds are bounded by their weight instead). |
| `REQUEST_TIMEOUT` | 10.0 | Per upstream HTTP request (seconds). |
| `FETCH_RETRIES` / `BACKOFF_BASE` | 3 / 2 | Attempts per feed / link on network errors and the exponential backoff base. |
| `FEED_MAX_INTERVAL_SECONDS` / `FEED_BACKOFF_SECONDS` / `FEED_BACKOFF_MAX_SECONDS` / `FEED_JITTER_SECONDS` | 1800 / 600 / 7200 / 10 | Polite polling (§4 Step 1): the longest interval of a quiet feed; a host's pause after 429/503 without Retry-After (doubling), and its cap; the random delay before each request. |
| `LOCAL_CACHE_SECONDS` | 5.0 | How long a worker reuses its in-memory copy of a view before re-reading Redis. |
| `STALE_AFTER_SECONDS` | 900 | Age at which `/health` reports the "all" snapshot as stale. |
| `HEADLINES_KEY` | `headlines` | Redis key prefix; snapshots live at `headlines:<view>`. |
| `CONFIG_DIR` | `config/` next to `app/` (`/config` in the containers) | The site's config folder (below). |

**Tagger environment** (`tagger` service): `TAGGER_DEVICE` (`NPU`; `CPU` to skip the NPU), `RETRAIN_INTERVAL_HOURS` (`24`), `STORY_THRESHOLD` (`0.92`), `DATA_DIR` (`/data`).

**The config folder** (`config/`): what the site is about, separate from the code, so another site can run the same code with its own folder. `docker-compose.yml` mounts it read-only into `app`, `refresher` and `summarizer` at `/config`; set `CONFIG_DIR` in `.env` to use a folder elsewhere. Edit it, then `docker compose restart app refresher summarizer` (no rebuild). Each file's header describes its fields.

| File | What it holds |
| --- | --- |
| `site.yaml` | The site's name (header, browser tab, installed app, structured data), description, and paywalled sites (the page's bypass, and summaries read from an archive) |
| `regions.yaml` | The region tabs, in order: id, label, the `countries` whose visitors start on it (Cloudflare's `CF-IPCountry`), and the `places` and `abbreviations` that put a worldwide feed's story in its topic tabs |
| `topics.yaml` | The topic tabs, in order: id (one of the tagger's `TOPICS`), label, optional `max_age_hours`, and `feed_only` (only stories from feeds about the topic, never model tags: `uplifting`) |
| `sources.yaml` | The feeds: `regions.<id>.sources` (that region's news, weights summing to 1.0) and `.topic_sources`, `extras` (mixed into every tab at their weight), `topic_extras` (topic feeds of no region) |
| `prompts.yaml` | The summarizer's prompts: an article's summary, and "same story?" for two headlines |

`max_items` (optional, any source): at most this many of the feed's items per view, so a feed whose quality varies can't fill a view (e.g. The Better India is Asia's only regional good-news feed; uncapped it took 50 of Asia's 80 uplifting slots). `max_age_hours` (optional, a topic): its views keep stories this long instead of `MAX_HEADLINE_AGE_HOURS`.

`label` (optional on `sources` and `extras`): what the feed's items are, for the tagger: a topic (its items are always tagged with it) or `other` (general news, a training label only). Topic ids must be ones the tagger knows (`TOPICS` in `tagger/classifier.py`).

The folder is validated when each service starts (a broken file stops it with its error):
- `sources.yaml` lists sources for exactly the regions of `regions.yaml`;
- region ids are lowercase letters, unique, and not `all`;
- source names are unique;
- each region's weights sum to 1.0;
- `extras` weights sum to less than 1.0. They are mixed into every view, and the view's news gets the rest;
- every `label` is a topic id or `other`;
- a country, and a place name, belong to one region only.

## 11. Component Versions

Pinned in the Dockerfiles, `docker-compose.yml`, `.woodpecker.yml`, `app/requirements.txt` (generated from `app/requirements.in` with `pip-compile`) and `tagger/requirements.txt` (`pip freeze` of `tagger/requirements.in` in the image).

| Component | Version |
| --- | --- |
| Python base image | `python:3.14.8-slim-trixie` (Debian 13) |
| Caddy | `caddy:2.11.7-alpine` |
| cloudflared | `latest`, pulled at every `docker compose up` (`pull_policy: always`) |
| Redis | `redis:8.10.2-alpine` |
| CI Docker CLI | `docker:29.9.0-cli` |
| FastAPI / Starlette / Uvicorn | 0.143.0 / 1.7.0 / 0.54.0 |
| redis-py | 8.1.0 (explicit retries: API 0, refresher 3, rate limiter 0) |
| structlog | 26.1.0 |
| prometheus-fastapi-instrumentator | 8.1.0 |
| Tagger base image | `ubuntu:24.04` (Python 3.12) |
| Intel NPU driver (user space) | linux-npu-driver 1.38.0 |
| OpenVINO / scikit-learn | 2026.4.1 / 1.9.1 |
| Embedding model | `bge-small-en-v1.5` fine-tuned by `tagger/finetune.py`, release `tagger-model-v1` (checksummed); tokenizer from `Xenova/bge-small-en-v1.5` revision `ea104dac` |
| Stats base image | `python:3.14.8-slim-trixie` |
| onnxruntime / tokenizers (stats) | 1.31.1 / 0.23.3 |
| Story grouping model | `multilingual-e5-small` fp16 ONNX and tokenizer from `Xenova/multilingual-e5-small` revision `761b726d` (checksummed) |
