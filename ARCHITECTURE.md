# Project Architecture

This document describes how the News Aggregator is built: its components, how regional headlines are collected, mixed, tagged by topic, stored and served, the rules each step follows, rate limiting, failure behaviour, and observability.

## 1. Overview

The application is a containerized system orchestrated by Docker Compose. Its core design rule is **user requests never trigger upstream fetches.**

- A single background **refresher** fetches all feeds on a schedule, builds one headline list per **view** (each region, plus "all"), resolves the links and publishes one **snapshot** per view to Redis.
- The **tagger** tags every headline by topic with a small model on the host's NPU; the refresher then publishes one more snapshot per region and topic (`asia:sport`, …).
- The **API** workers only read those snapshots, from an in-process copy.

So request volume costs almost nothing upstream or in Redis, and users keep getting headlines through slow feeds, Google failures and even Redis outages.

**Views**: `all` (the home page) and five regions: `asia`, `europe`, `americas`, `africa`, `oceania`. Each of these six also has a view per **topic**: `uplifting`, `tech`, `science`, `nature`, `culture`, `sport` (36 topic views). Regions and topics are defined in `app/sources.json`. The UI and the API take their lists from that file, so adding or renaming a region needs no code change.

## 2. Architecture Diagram

```
  End users ──► Cloudflare ──► VPS proxy ──(Tailscale)──┐
                                                         │  (or LAN / Nginx Proxy Manager)
                                                         ▼
                                     +-----------------------------------+
                                     |  Caddy 2.11  (:7001)              |
                                     |  - resolves real client IP from   |
                                     |    trusted proxies only, sets     |
                                     |    X-Real-IP                      |
                                     |  - compression, load balancing    |
                                     |  - blocks /metrics                |
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
|                                     |  - fetches 37 feeds (+46 topic     |          |
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
+-------------------------------------------│----------------------------------------+
                                                  ▼
             Google News local editions (Nation section) · BBC regional feeds · local outlets
             (ABC, RNZ, Politico Europe, AllAfrica, …) · Google News Science · Hacker News ·
             topic feeds: Google News Technology/Science/Sports/Entertainment sections,
             Guardian environment, good-news outlets
```

## 3. Components

### Caddy (`caddy` service)
- **Role**: Internal reverse proxy and load balancer. TLS is terminated upstream (Cloudflare / VPS proxy).
- **Client IP trust**: `trusted_proxies` lists the peers allowed to tell Caddy the real client IP: private ranges and Tailscale `100.64.0.0/10`. For those peers Caddy reads `CF-Connecting-IP`, `X-Real-IP` or `X-Forwarded-For`; for anyone else it uses the connection's own IP. It then **overwrites** `X-Real-IP` before proxying, so clients can't spoof the IP used for rate limiting.
- Compresses responses (zstd/gzip), health-checks app containers via `/health`, and blocks `/metrics`.

### API (`app` service)
- **Framework**: FastAPI on Uvicorn (`uvloop`, `httptools`), one worker per CPU (`UVICORN_WORKERS` = `APP_CPUS` = 4).
- **Code layout**:
  - **`main.py`**: app wiring — middleware, exception handling, lifespan, routes.
  - **`config.py`**: Pydantic settings (environment / `.env`); loads and validates the source catalog from **`sources.json`**; `snapshot_key(view)`.
  - **`deps.py`**: admin API key check, rate limiter and client-IP key function.
  - **`models.py`**: the `/api/headlines` response schema, shared with the refresher.
  - **`routes/`**: `headlines.py`, `frontend.py`, `admin.py`, `health.py`.
  - **`services/`**:
    - `headlines_cache.py`: per-process copy of every view.
    - `redis_client.py`: Redis client factory.
    - `feed_service.py`: fetching, per-view mixing, link resolution orchestration.
    - `redirect.py`: the custom Google News resolver.
    - `link_resolver.py`: custom resolver first, then the fallback.
  - **`templates/`**: `retro.html` (the page) and the error pages.
  - **`middleware/`**: content-negotiated error pages (HTML for browsers, JSON for API clients).

### Refresher (`refresher` service)
- Runs `refresher.py` from the same image. **Exactly one replica.**
- Owns all upstream traffic: feeds, Google link resolution, writing the snapshots (see §4).

### Tagger (`tagger` service)
- `tagger/tagger.py`, its own image (Ubuntu 24.04, OpenVINO and Intel's NPU user-space driver). Internal only: `POST /tag`, `GET /health`. See §6.
- Runs on the Intel NPU (`/dev/accel`); falls back to the CPU if the NPU can't be used.

### Summarizer (`summarizer` service)
- Runs `summarizer.py` from the app image. **One replica.** Optional: without `SUMMARIZER_LLM_URL` it idles.
- Every minute it reads the published snapshots and takes the first 20 items of every view without a summary: the main views first, then the topic views, top positions first.
- For each, it reads the article's text (trafilatura; at most 6,000 characters; a precise extraction first, then a looser one if that finds too little) and asks an **Ollama** server for 2–3 sentences, using only the article's facts. The model is `qwen3:8b` (thinking off), and the server is a desktop GPU (RTX 3060 Ti) on the tailnet. Ollama loads the model on demand and frees the GPU two minutes after the last request.
- The summary is stored in Redis at `summary:<hash of link>` for 7 days, with how its text was read (`via`). The refresher adds it to the item (`summary`) when it next publishes, so summaries appear within one refresh of being made.
- **Reading the article**, each step only if the previous ones failed:
  1. **direct**: a plain request (public addresses only, see §7b);
  2. **browser**: headless Chromium (`browser` service, FlareSolverr), which gets through bot checks that refuse plain requests (Cloudflare challenges, AWS WAF's HTTP 202). A site that blocked a plain request (401/402/403/429/503, or a `cf-mitigated`, `x-datadome` or `x-amzn-waf-action` header) is remembered for 7 days (`browser-host:<host>`) and its articles go straight to the browser; after that the next article tries a plain request again, and the site is remembered again if it's refused. If the browser fails, a second one with a different engine tries (`browser2`, Byparr: a modified Firefox, same API);
  3. **feed**: the text the feed itself carried for the article. The refresher keeps it for every published item with at least 600 characters (`feed-text:<hash of link>`, 2 days);
  4. **archive**: the latest archive.ph snapshot.

  Paywalled sites (`PAYWALLED_HOSTS` in `config.py`, the same list the page's bypass uses) go direct, archive, then feed: a browser doesn't get past a paywall. Articles none of these can read (or with less than 600 characters of text) are marked and retried after 3 hours; links to non-public addresses are marked for a day and never reach the browser.
- Measured on the 153 top stories that the direct fetch alone couldn't read: 121 became readable (browser 100, direct after the looser extraction 8, archive 7, feed 2…), and the second browser read 4 of the remaining 32 (phys.org, ESPN). On pages the first browser reads, the second reads 28 of 30, at ~23 s a page instead of ~8 s, so it only runs after the first fails. Still unreadable: sites whose bot checks also stop a real browser (phys.org's own check, Forbes' DataDome, Akamai on Sky and News Corp sites) and paywalled articles without a snapshot. A browser page takes ~10 s (up to ~50 s).
- Counts per method are logged every pass and exported as `summarizer_article_reads_total{method}` on the summarizer's metrics port (9100, inside the Docker network).
- When the LLM server is unreachable (the desktop is off), nothing is fetched, summarized or marked: each pass first checks the server. It retries every minute, logging only when availability changes.

### Headless browsers (`browser`, `browser2` and `browser-egress` services)
- `browser` is FlareSolverr (pinned image): a headless Chromium behind a small HTTP API, used only by the summarizer. Pages run their own JavaScript in it, so it is cut off from everything else:
  - it sits only on the `browser` network, which is `internal` (no internet). Raw connections from it to the internet, the LAN or the tailnet fail, and the stack's other containers (Redis, app) don't even resolve;
  - the summarizer has it load every page through `browser-egress`, a Smokescreen proxy (`smokescreen/Dockerfile`, built from a pinned, verified commit). The proxy connects to public internet addresses only: private, loopback, link-local, Docker and Tailscale/CGNAT (100.64/10) addresses are refused, after DNS and at every redirect. Tested: the LAN, the tailnet, a public URL redirecting into the LAN, and container names were all refused;
  - `browser-egress` is the only container on both `browser` and `egress` (its own network to the internet).
- `browser2` is Byparr (pinned image), with the same isolation: only on the `browser` network, every page through `browser-egress` (`PROXY_SERVER`), and its own downloads too (`HTTPS_PROXY`: a GeoIP database at start, kept in the `browser2_cache` volume). Its locale and timezone are fixed so it doesn't look up its public IP.
- Limits: `browser` 1.5 CPUs, 1 GB, 512 processes, 256 MB shared memory (~300 MB while loading a page); `browser2` 1.5 CPUs, 1.5 GB (its temporary files are in memory), 512 processes.

### Site icons
- After each refresh, the refresher caches the icon of every website in the published views that it doesn't have yet. It fetches them from DuckDuckGo's icon service (`ICON_SOURCE_URL`) and keeps them in Redis for 7 days; a site without one is retried after a day.
- The app serves them at `/site-icon/<domain>` (cached by browsers for 7 days). It only reads Redis and never fetches, so visitors' browsers contact neither the websites nor the icon service.
- Measured on the first cycle: 112 of 126 websites had an icon.

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
The catalog (`app/sources.json`) has 37 news feeds (plus 46 topic feeds, Step 5). **Every view has the same overall mix: 50% news, 30% science, 20% Hacker News** (25 / 15 / 10 headlines). "News" is that region's news, or all regions equally on "all". Each region's weights sum to 1.0 and decide the make-up of its news share:

| Region | News sources (share of the region's news) |
| --- | --- |
| **Asia** | Google News India 20%, Singapore 12%, Philippines 12%, Pakistan 10%, Malaysia 8%, Israel 8% · BBC Asia 10% · BBC Middle East 10% · Al Jazeera 10% |
| **Europe** | Google News UK 25%, Ireland 15% · Politico Europe 20% · BBC Europe 15% · Kyiv Independent 10% · The Local France 7.5%, Germany 7.5% |
| **Americas** | Google News US 25%, Canada 20% · BBC Latin America 15% · BBC US & Canada 10% · Latin Times 10% · Mexico News Daily 10% · MercoPress 10% |
| **Africa** | Google News South Africa 20%, Nigeria 20%, Kenya 15%, Ghana 10%, Uganda 10% · AllAfrica 15% · BBC Africa 10% |
| **Oceania** | Google News Australia 30%, New Zealand 25% · ABC Australia 20% · RNZ 15% · BBC Australia 10% |
| **All** | The five regions' news lists, equally (5 headlines each) |
| **Every view** (`extras`) | Google News Science 30% · Hacker News 20% — the view's news gets the remaining 50% |

How the sources were chosen (all measured live):
- **Google News local editions, "Nation" section.** The backbone: 50–70 fresh items each from many local publishers. The Nation section is used instead of an edition's top stories because top stories mix in the biggest international story of the day. In every region view that same story led the list; Nation sections are domestic news (e.g. 0 international-keyword headlines for India, Ireland, Nigeria and New Zealand). The URLs in `sources.json` are the canonical `rss/topics/…` URLs that `…/section/topic/NATION?…` redirects to.
- **Only real local editions.** The English editions for UAE, Saudi Arabia, Lebanon, Hong Kong, Bangladesh, Germany and Egypt return the *US* edition's stories, so they are not used.
- **Local and regional outlets** (BBC regional, Al Jazeera, Politico Europe, Kyiv Independent, The Local, AllAfrica, ABC, RNZ, Latin American outlets) add depth and direct links that need no Google resolution. BBC regional feeds update slowly, so they have small weights.
- Rejected: DW (items carry no dates), Euronews and Africanews general feeds (global, not regional), Rio Times (no longer regional).

Fetching:
- All feeds are fetched **concurrently**, at most 15 requests in flight. Redirects are followed, so a moved feed keeps working.
- Each request has a `REQUEST_TIMEOUT` of **10s** and up to `FETCH_RETRIES` = **3** attempts, with exponential backoff (1s, then 2s).
- A feed counts as **failed** after all attempts fail, or if its XML is so malformed that no entries can be parsed. A failed feed contributes nothing and increments `feed_fetch_failures_total{source}`; everything else is unaffected.
- Each entry becomes a headline:
  - `title` and `link`;
  - `source`: the publisher named in the entry, or else the feed's name;
  - `published`: the entry's date, or the fetch time if it has none;
  - `last_updated`;
  - `comments`: the discussion page, when the feed has one (Hacker News).

### Step 2 — Mix each view (weighted interleave)
`build_views()` in `services/feed_service.py` builds every view in two stages, using `mix_headlines()` for each list:

- **Stage 1, news lists.** Each region's news from its sources. "World" news is built from the five regional lists, equally. That keeps every region represented and drops a story two regions both carry, and the per-publisher cap applies across regions, so e.g. BBC appears at most 3 times. Science and Hacker News are each mixed on their own.
- **Stage 2, views.** Each view interleaves its news list (50%) with science (30%) and Hacker News (20%). The lists were already capped, so this stage only interleaves and de-duplicates. News has the largest single weight, so it leads every view (pattern: *news, news, HN, news, science, …*).
- If a view's news list is empty (all of that region's feeds failed), the view stays empty and keeps its previous snapshot, rather than showing only science and HN.

`mix_headlines()` builds a list from weighted sources like this:

1. **Keep each source's own order.** Google's ranking and Hacker News's front-page rank are preserved; nothing is re-sorted by date.
2. **Drop stale items** published more than `MAX_HEADLINE_AGE_HOURS` (**48h**) ago. Items without a date count as new.
3. **Interleave by weight.** Sources take turns by *smooth weighted round-robin*: each turn, every source earns credit equal to its weight and the source with the most credit supplies the next headline. Every stretch of the list therefore has roughly the configured mix, rather than blocks per source.
4. **Skip duplicates and over-represented publishers:**
   - an item is a duplicate if its link, or its title (case and punctuation ignored, Google's " - Publisher" suffix removed), was already taken; the first occurrence wins;
   - at most `MAX_PER_PUBLISHER` (**3**) items per publisher. The publisher is the publisher name for Google items and the link's domain for direct links. **A feed whose items all come from one publisher** (e.g. ABC Australia's own feed) is exempt, because its weight already bounds its share.
5. **Redistribute unused slots.** A source that runs out drops out of the rotation, and the others fill up to `MAX_HEADLINES` (**80**) in proportion to their weights.

The result is **deterministic**: the same feeds give the same order, so lists only change when the news changes. Each view logs a `Mixed headlines` line with counts per source and items dropped as stale, duplicate or over the cap.

Not handled: the same event reported by two outlets under different headlines isn't detected, because de-duplication is by link and normalized title only.

### Step 3 — Resolve links
Google News RSS links point at Google, not at the publisher. Direct links (outlets, Hacker News) are used as-is with no network calls. Each distinct Google link across all views is resolved once, with at most 10 resolutions in flight, through `services/link_resolver.py`:

1. **Resolved-URL cache (Redis, 24h).** A link resolved within the last 24 hours is reused with no network call. In steady state almost all links come from here; a cold start resolves about 150 links in under 10 seconds.
2. **Custom resolver (`services/redirect.py`) — always the primary.**
   - Follows HTTP redirects; if they lead off Google, that URL is the answer.
   - Otherwise it reads the `c-wiz[data-p]` payload from the Google article page and calls Google's `batchexecute` endpoint to decode it.
   - Retries up to 3 times with backoff. Results that differ from the input are cached for 24h.
3. **Fallback decoder — only if the custom resolver really failed.** "Failed" means its result is **still a `news.google.com` URL**: all retries failed, the `c-wiz` payload was missing, or the response couldn't be parsed. The fallback is built independently:
   - it reads different page attributes (the article's `data-n-a-sg` signature and `data-n-a-ts` timestamp);
   - it builds its own `batchexecute` request and parses the response itself.

   It makes one attempt, with at most 5 fallback lookups in flight, and caches its result for 24h. If it also fails, the Google link is kept and the next refresh tries again.

Outcomes are counted in `redirect_resolutions_total{outcome}` (custom resolver) and `redirect_fallbacks_total{outcome}` (fallback).

### Step 4 — Publish
- Each view's snapshot (`region`, `data`, `fetched_at`, `expires_at = fetched_at + 5 min`) is validated against the API schema and written to `headlines:<view>` with **no TTL**. All views are written in one pipelined round-trip.
- **Publishing criteria — a view's previous snapshot is kept when:**
  - that view's news came out empty (e.g. all of a region's feeds failed). The other views are still published, and a warning names the empty views;
  - every view came out empty (`headlines_refreshes_total{result="empty"}`);
  - the cycle raised an error (`result="error"`) or exceeded `REFRESH_TIMEOUT_SECONDS` (**270s**, both phases) (`result="timeout"`).
- A partial view (some feeds down) **is** published, because fresh headlines from part of a region beat none.

### Step 5 — Topic views
After the main views are published, the refresher builds the topic views (`get_topic_views()` in `services/feed_service.py`). This phase can fail on its own (tagger down, or over `TOPIC_REFRESH_TIMEOUT_SECONDS`, **150s**): the main views are already out, the topic views keep their previous snapshots, and `headlines_topic_refreshes_total{result="error"}` counts it.

1. **Topic feeds.** 46 feeds about one topic each: the Technology, Science, Sports and Entertainment sections of 10 Google News local editions (India and Singapore for Asia; UK; US and Canada; Nigeria, South Africa and Kenya; Australia and New Zealand), plus three Guardian environment feeds (nature) and three good-news outlets (uplifting), which belong to no region. They change slowly and are many, so they're fetched every `TOPIC_FEEDS_INTERVAL_SECONDS` (**30 min**) and reused in between. They appear only in topic views.
2. **Tagging.** Every fetched headline (news, extras and topic feeds, about 3,500) goes to the tagger with its feed's **label**: the topic of a topic feed (or of Google News Science: `science`), `other` for the Google News national sections, or none. The tagger returns its tags (§6). A headline's tags are the tagger's plus its feed's topic, so a Sports section's items are always `sport`.
3. **Views.** For each topic, one view per region (and "all") from the items tagged with it: the region's news and topic feeds, plus the extras and topic extras. They're mixed with `mix_headlines()` (same rules as above), except the weights: the news/extras mix doesn't apply to one topic (Hacker News has little sport). **Feeds about the topic come first**, mixed equally: in a region's view, the region's own topic feeds, then the shared ones (extras, topic extras), which are the same in every region. Items the model tagged from other sources only fill the view (up to 80) when those run short. **Global stories stay out of region views:** a story found in two or more regions' own feeds (e.g. the same TechCrunch story in several Google News Technology editions) is shown only in "all", so region tabs don't repeat each other; what still repeats across regions is the shared feeds' items, after the region's own. **Uplifting is feed-only**: model tags never add to it, since even its most confident ones were about half wrong on live news. "All" draws from every region's sources.
4. **Links** are resolved as in Step 3, and all 36 views are published, **including empty ones** (a region can have no uplifting news right now; the page says so).

### Step 6 — Wait
- The refresher sleeps `REFRESH_INTERVAL_SECONDS` (**5 minutes**) after each cycle; cycles take a few seconds.
- **Why 5 minutes:** each cycle requests 37 feeds, 18 of them from Google News (plus, every 30 minutes, 46 topic feeds, 40 of them Google News sections). Doing that every minute from one IP risks Google rate-limiting or blocking it, which would also break link resolution. Most feeds update only a few times an hour anyway.
- `POST /admin/cache/clear` (with `X-API-Key`) wakes the refresher immediately. Repeated requests collapse into one refresh, and the current snapshots stay available meanwhile.
- If Redis is unreachable while waiting, the refresher sleeps the interval and tries again.

## 5. How Requests Are Served

1. A browser loads `/`; the page reads the region and topic lists embedded in it and calls `/api/headlines?region=<view>[&topic=<topic>]`.
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
- Minimalist retro design: monospace; light "paper" and dark amber themes, following the OS setting until toggled.
- **Region tabs**: `all · asia · europe · americas · africa · oceania`, and below them **topic tabs**: `everything · uplifting · tech · science · nature · culture · sport`. Any region combines with any topic; "everything" is the region's main view.
  - Tabs are real links (`/?region=asia&topic=sport`): shareable, open in a new tab, back/forward work.
  - Clicking switches in place without reloading.
  - Number keys `1`–`6` switch regions (keeping the topic); `t` / `Shift+t` cycle topics.
  - The last region and topic are remembered when the URL doesn't name them.
  - A tab that was already loaded switches instantly and refreshes in the background if older than a minute.
  - A slow response for one view can never overwrite another view's list.
- Each item shows the title (without Google's " - Publisher" suffix), the domain, the source and age, plus "discuss" for Hacker News posts (a feed's comments link is dropped when it's just the article itself) and "summary" for items with one, which opens it under the item, labelled as a machine summary (it stays open across refreshes).
- Other controls:
  - `r` refreshes;
  - `a` (or [a]dvanced) shows a second row: **reset** (forgets every setting on this device: region, topic, sort, theme, bypass, site icons, auto refresh, and closes open summaries), **install** (the browser's own install prompt, when it offers one; the site has a web app manifest, so Chrome can install it), **site icons** (off by default: each website's icon before its headlines, at text size), **auto refresh** (cycles off, 5m, 15m, 30m, 1h: the open view is fetched again once it's older than that, checked every 30 s and on returning to the tab, never while the tab is hidden), and **bypass** (off by default; "archive" opens known paywalled sites via `archive.ph/newest/<url>`, which goes straight to the latest snapshot);
  - `s` (or [s]ort) cycles the order: **mix** (the server's: each feed's own ranking, interleaved by weight), newest first, oldest first, or random (reshuffled each time it's chosen, kept across refreshes); remembered;
  - `d` toggles dark mode.

| Endpoint | Response |
| --- | --- |
| `GET /` | Main page for browsers; small JSON status for API clients. |
| `GET /archive`, `GET /retro` | Old URLs; redirect to `/`. |
| `GET /retro` | Old evaluation URL; `301` redirect to `/`. |
| `GET /api/headlines?region=<view>&topic=<topic>` | Snapshot JSON for `all` (default) or a region id, and optionally one topic: `region`, `topic` (null for a main view), `data` (each item: `title`, `link`, `source`, `published`, `last_updated`, `comments` or null), `fetched_at`, `expires_at`. **404** for an unknown region or topic (the message lists valid ones). **503 with `Retry-After: 10`** only before that view's first snapshot exists. |
| `GET /health` | `healthy` / `degraded` / `unhealthy`, plus the "all" view's `headlines.age_seconds` and `headlines.stale` (see §8). |
| `POST /admin/cache/clear` | Requests an immediate refresh; requires `X-API-Key`. |
| `GET /metrics` | Prometheus metrics (blocked at Caddy). |

## 6. Topic Tagging

The tagger (`tagger/tagger.py`) gives each headline zero or more of `tech`, `science`, `nature`, `culture`, `sport`, `uplifting`. Most hard news (politics, crime, economy) gets none.

**How it tags:**
- **Embedding:** `bge-small-en-v1.5` (33M parameters, MIT licence), fine-tuned on the seed labels (below), run by OpenVINO on the Intel NPU. The NPU needs static shapes, so titles are padded to batches of 32 × 48 tokens. The first start compiles the model for the NPU (~1 min); the result is cached in the volume, so later starts take seconds.
- **Classifier:** two logistic regressions on the embedding: one over the topics plus `other` (a tag when its probability is ≥ 0.9), one for uplifting or not (≥ 0.8; not used for views, see Step 5). Uplifting is a tone, not a subject, so it can combine with a topic.
- **Labels, no hand-labelling needed:** feeds imply labels. A Google News Sports section's items are `sport`, a good-news outlet's are `uplifting`, a national news section's are `other`. The bundled `tagger/data/seed.json` (3,250 such headlines) trains the first classifier; every refresh adds more.

**Storage:** SQLite (`/data/tagger.db` in the `tagger_data` volume, WAL mode). One row per headline, by cleaned title: source, link, feed label, tags, the classifier version that tagged it, and its embedding (float16, 768 bytes). A headline is embedded only the first time it's seen; after a retrain, stored headlines are re-tagged from their stored embeddings without re-embedding. Each headline takes roughly 1 KB.

**Retraining:** every `RETRAIN_INTERVAL_HOURS` (**24**, counted from the last training, so restarts don't postpone it), on every labelled headline in the database. The candidate replaces the current classifier only if its macro F1 on the hand-labelled `tagger/data/eval.json` (325 headlines, never trained on) is at least as good. Both scores are logged. Training runs on its own thread; tagging continues meanwhile. Measured: the first retrain, after one cycle's new labels, went from F1 0.623 to 0.633 and from 8.9% to 8.1% hard-news leak (with the base model, before fine-tuning).

**Why this design (benchmarked on 325 hand-labelled headlines):**

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

**Precision to expect:** the benchmark's best F1 (0.66) is at a 0.5 threshold, but there 1 in 4 topic tags on the evaluation set is wrong, and live news is mostly hard news, so wrong tags flooded the views. At 0.9, 4 in 5 are right (it finds 42% of topic stories) and 4% of hard news gets a tag (10% at 0.5). Topic views take feed items first anyway, so model tags mainly fill nature views and thin regions. Uplifting views come only from the three good-news feeds, which post a few items a day.

| Tagger resource | Value |
| --- | --- |
| Memory | ~450 MB in use; ~1.2 GB peak on the very first start (NPU compile + training). Limit 1.5 GB. |
| CPU | Limit 2. Idle between refreshes; a refresh tags its few hundred new headlines in about a second. |
| Device | `/dev/accel` plus the host's render group (`RENDER_GID`). Without an NPU, remove `devices`/`group_add` in `docker-compose.yml` and it uses the CPU. `GET /health` reports the device. |

## 7. Rate Limiting

| Aspect | Behaviour |
| --- | --- |
| Limit | `API_RATE_LIMIT`, default **100 requests per minute per client IP**, fixed window. |
| Routes limited | `GET /` and `GET /api/headlines` (all regions share one counter), each route with its own counter. `/health`, `/metrics`, `/admin/*` and the `/retro` redirect are not limited. |
| Client identity | The `X-Real-IP` header, which Caddy always overwrites with the IP it resolved. Clients connecting from untrusted addresses can't influence it; trusted proxies (LAN, Docker, Tailscale) forward the real visitor IP. |
| When exceeded | `429` with `{"error": "Rate limit exceeded: 100 per 1 minute"}`. |
| Storage | Counters live in Redis, so limits are shared across all workers and containers. Checks use 0.5s timeouts and no retries. |
| Redis down | Limits fall back to per-process memory automatically and switch back when Redis recovers. Requests are never failed because of the limiter. |
| Implementation note | Limits are applied per route with `@limiter.limit`. slowapi's middleware is not used because it can't see routes inside FastAPI's included routers and silently skips them. |

A visitor makes 2 requests per page load plus 1 per region viewed, so 100/minute is far above human use. Users behind one shared IP (e.g. an office NAT) share a budget; raise `API_RATE_LIMIT` if that matters.

## 7b. Security Hardening

- **Fetching links from outside** (the summarizer reads any top story's link; anyone can post one to Hacker News): only public internet addresses are fetched, checked again at every redirect (`services/safe_fetch.py`). Private, loopback, link-local and Tailscale (100.64/10) addresses are refused, so a crafted link can't make the server read the LAN or the tailnet. Summaries containing a link are discarded, since an article could try to steer the model. The headless browser that reads bot-protected sites can't make that check itself, so it has no network access except a proxy that enforces it (see the headless browser in §3).
- **Page:** a Content-Security-Policy with a fresh nonce per response allows only the page's own inline script and style, images and requests from its own origin, and no framing. Feed text, summaries and titles are inserted as text, never HTML; links must be http(s) and open with `noopener`.
- **Client IP:** the rate limit keys on the IP Caddy resolves from trusted proxies only. Through the Cloudflare Tunnel (`cloudflared` service), Cloudflare sets `CF-Connecting-IP` and rejects client-supplied ones (error 1000), and a forged `X-Forwarded-For` is ignored because `CF-Connecting-IP` comes first. Any other proxy in front (e.g. a VPS) must overwrite client-supplied IP headers; see the README.
- **Headers:** Caddy sets HSTS, `X-Content-Type-Options`, `X-Frame-Options` and `Referrer-Policy` on every response.
- **Containers:** every service has CPU, memory and process limits, rotated logs (10 MB × 3), `no-new-privileges`, and no Linux capabilities (Redis keeps the three its entrypoint needs to switch user). Redis requires a password (`REDIS_PASSWORD`) and is reachable only inside the Docker network.
- **Admin endpoint:** disabled unless `ADMIN_API_KEY` is set; the key is compared in constant time.
- **Tagger growth:** retraining uses the newest `TRAIN_MAX_ROWS` (100,000) labelled headlines, so its memory stays bounded as headlines accumulate (~14,000 a day).

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
| Fresh install | `/api/headlines` returns 503 (`Retry-After: 10`) for the few seconds until the first refresh completes. Topic views follow one cycle later (5 min): the tagger's first start takes ~1.5 min. |

`/health` returns 503 only when the worker cannot serve headlines at all. Staleness never fails the probe, because older headlines are better than taking the API offline.

## 9. Observability

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
| `CADDY_HTTP_PORT` | `7001` | Published port. |
| `ENABLE_API_DOCS` | `false` | Serve `/docs`. |
| `RENDER_GID` | `991` | Numeric id of the host group owning `/dev/accel/accel0` (`getent group render`), for the tagger. |

**Tunables in `app/config.py`** (override with an environment variable of the same name):

| Setting | Default | Meaning |
| --- | --- | --- |
| `REFRESH_INTERVAL_SECONDS` | 300 | Pause between refresh cycles (see §4, Step 6). |
| `REFRESH_TIMEOUT_SECONDS` | 270 | A cycle (both phases) taking longer is abandoned; previous snapshots stay. |
| `TOPIC_REFRESH_TIMEOUT_SECONDS` | 150 | Cap on the topic phase; on timeout the topic views keep their previous snapshots. |
| `TOPIC_FEEDS_INTERVAL_SECONDS` | 1800 | How often the topic feeds are re-fetched. |
| `TAGGER_URL` / `TAGGER_TIMEOUT` | `http://tagger:8000` / 90 | The tagger, and the timeout of one tagging request. |
| `SUMMARIZER_LLM_URL` / `SUMMARIZER_MODEL` | empty / `qwen3:8b` | The Ollama server (empty disables summaries) and its model. Set in `.env`. |
| `SUMMARY_TOP_N` / `SUMMARY_TTL_SECONDS` / `SUMMARY_FAILED_TTL_SECONDS` | 20 / 7 days / 1 day | Items per view that get a summary; how long a summary, and a failed article, are kept. |
| `SUMMARY_RETRY_SECONDS` | 3 hours | When an article no step could read is tried again. |
| `SUMMARIZER_BROWSER_URL` / `SUMMARIZER_BROWSER_PROXY` / `SUMMARIZER_BROWSER2_URL` | set in `docker-compose.yml` | The headless browser, the public-only proxy it must use, and the second browser; empty disables a browser step. |
| `BROWSER_HOST_TTL_SECONDS` | 7 days | How long a site that blocked a plain request goes straight to the browser. |
| `FEED_TEXT_TTL_SECONDS` | 2 days | How long the feed's own text of a published item is kept. |
| `MAX_HEADLINES` | 80 | Headlines per view (each region, and "all"). |
| `MAX_HEADLINE_AGE_HOURS` | 48 | Items older than this are dropped before mixing. |
| `MAX_PER_PUBLISHER` | 3 | Maximum items from one publisher in a view (single-publisher feeds are bounded by their weight instead). |
| `REQUEST_TIMEOUT` | 10.0 | Per upstream HTTP request (seconds). |
| `FETCH_RETRIES` / `BACKOFF_BASE` | 3 / 2 | Attempts per feed / link and the exponential backoff base. |
| `LOCAL_CACHE_SECONDS` | 5.0 | How long a worker reuses its in-memory copy of a view before re-reading Redis. |
| `STALE_AFTER_SECONDS` | 900 | Age at which `/health` reports the "all" snapshot as stale. |
| `HEADLINES_KEY` | `headlines` | Redis key prefix; snapshots live at `headlines:<view>`. |
| `SOURCES_FILE` | `sources.json` | The source catalog (below). Mounted into the refresher; apply edits with `docker compose restart refresher`. The page's tabs come from the same file, so after adding or renaming a region or topic also restart `app`. |

**Tagger environment** (`tagger` service): `TAGGER_DEVICE` (`NPU`; `CPU` to skip the NPU), `RETRAIN_INTERVAL_HOURS` (`24`), `DATA_DIR` (`/data`).

**`sources.json` format:**

```json
{
  "topics": [{"id": "uplifting", "label": "Uplifting"}, …],
  "regions": [
    {"id": "asia", "label": "Asia",
     "sources": [{"name": "…", "url": "…", "weight": 0.2, "label": "other"}, …],
     "topic_sources": [{"name": "Google News India Sports", "url": "…", "label": "sport"}, …]},
    …
  ],
  "extras": [{"name": "Hacker News", "url": "…", "weight": 0.2}, …],
  "topic_extras": [{"name": "Good News Network", "url": "…", "label": "uplifting"}, …]
}
```

`label` (optional on `sources` and `extras`): what the feed's items are, for the tagger: a topic (its items are always tagged with it) or `other` (general news, a training label only). Topic ids must be ones the tagger knows (`TOPICS` in `tagger/tagger.py`).

The file is validated at startup:
- region ids are lowercase letters, unique, and not `all`;
- source names are unique;
- each region's weights sum to 1.0;
- `extras` weights sum to less than 1.0. They are mixed into every view, and the view's news gets the rest;
- every `label` is a topic id or `other`.

## 11. Component Versions

Pinned in the Dockerfiles, `docker-compose.yml`, `.woodpecker.yml`, `app/requirements.txt` (generated from `app/requirements.in` with `pip-compile`) and `tagger/requirements.txt` (`pip freeze` of `tagger/requirements.in` in the image).

| Component | Version |
| --- | --- |
| Python base image | `python:3.14.8-slim-trixie` (Debian 13) |
| Caddy | `caddy:2.11.7-alpine` |
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
