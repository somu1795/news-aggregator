# Project Architecture

This document provides a high-level overview of the News Aggregator application's architecture, its components, and the flow of data.

## 1. Overview

The application is designed as a containerized, multi-service system orchestrated by Docker Compose. It follows a classic reverse proxy pattern where a web server (Caddy) acts as the entry point, routing traffic to one or more backend application workers (FastAPI/Uvicorn). A dedicated Redis instance serves as a high-speed cache to minimize latency and reduce load on external news sources.

This architecture is scalable, secure, and easy to deploy, making it suitable for production environments.

## 2. Architecture Diagram

The following diagram illustrates the flow of a user request through the system.

```
       +--------------------------------+
       |          End User              |
       | (Browser, Mobile App, etc.)    |
       +--------------------------------+
                   |
                   | HTTPS Request (e.g., on port 7001)
                   v
    +--------------------------------------+
    |      Caddy (Reverse Proxy)           |
    |--------------------------------------|
    | - Listens on public port (e.g., 7001) |
    | - Handles TLS termination (HTTPS)    |
    | - Load balances requests to App      |
    |   workers                            |
    +--------------------------------------+
                   |
                   | HTTP Request (to port 8989 internally)
                   v
+------------------------------------------------------------------------------------+
| Docker Network                                                                     |
|                                                                                    |
|   +---------------------------------+      +-------------------------------------+ |
|   |   FastAPI App (Worker 1)        |      |   FastAPI App (Worker N)            | |
|   |---------------------------------|      |-------------------------------------| |
|   | - Serves frontend & API         |      | - Serves frontend & API             | |
|   | - Checks Redis for cache        | <--->| - Checks Redis for cache            | |
|   | - Fetches from external sources |      | - Fetches from external sources     | |
|   +---------------------------------+      +-------------------------------------+ |
|                   ^                                      ^                         |
|                   |                                      |                         |
|                   +------------------+-------------------+                         |
|                                      |                                             |
|          (Cache Check / Read / Write)|                                             |
|                                      v                                             |
|                         +--------------------------+                               |
|                         |     Redis (Cache)        |                               |
|                         |--------------------------|                               |
|                         | - Stores headlines (TTL) |                               |
|                         | - Provides fast lookups  |                               |
|                         +--------------------------+                               |
|                                                                                    |
+------------------------------------------------------------------------------------+
                                      ^
                                      |
                                      | HTTP/HTTPS Requests
                                      v
                  +------------------------------------------+
                  |          External RSS Feeds              |
                  | (BBC, Reuters, CNN, Al Jazeera, etc.)    |
                  +------------------------------------------+

```

## 3. Component Breakdown

### Caddy
- **Role**: Reverse Proxy and Web Server.
- **Function**: Caddy is the public-facing entry point of the application. It listens for incoming HTTP/HTTPS requests on the port defined by `CADDY_HTTP_PORT`. Its primary responsibilities are to terminate TLS (handle HTTPS), and then forward the requests to the backend `app` service in a load-balanced fashion. This isolates the application from the public internet and simplifies its logic.

### FastAPI Application (`app` service)
- **Role**: Backend API and Application Logic.
- **Framework**: Built with Python and the **FastAPI** framework, served by **Uvicorn** with `uvloop` and `httptools` for high performance.
- **Structure**: The application follows a modular design pattern:
  - **`main.py`**: Entry point handling configuration, middleware wiring, and the application lifespan.
  - **`config.py`**: Pydantic-based centralized settings management.
  - **`routes/`**: Distinct API route controllers (`headlines.py`, `admin.py`, `frontend.py`, `health.py`).
  - **`services/`**: Core business logic modules (`cache_service.py`, `feed_service.py`, `redirect.py`).
  - **`middleware/`**: Smart exception handling and rate limiting (`slowapi`).
- **Function**:
  - Validates security headers, handles CORS, and enforces rate limits via a Redis-backed `slowapi` instance.
  - Manages frontend serving and robust background news fetching routines.

### Redis
- **Role**: In-Memory Cache and Distributed Lock Manager.
- **Function**: Redis is used to store the aggregated list of headlines for a short period (defined by `CACHE_TTL_SECONDS`). When a request for headlines arrives, the FastAPI app first checks Redis. If valid, non-expired data is found, it is returned immediately. **Additionally, Redis is used to implement a distributed lock to prevent "cache stampede" scenarios, ensuring only one application worker fetches live data at a time when the cache expires.** This provides a very fast response and avoids repeatedly fetching data from the external RSS feeds.

### Docker & Docker Compose
- **Role**: Containerization and Orchestration.
- **Function**: The entire application is defined as a set of services in `docker-compose.yml`.
  - **Docker** packages each component (Caddy, FastAPI app, Redis) into a standardized, isolated container.
  - **Docker Compose** reads the YAML file to build, configure, and run the multi-container application with a single command, managing the services, networks, and volumes.

## 4. Request & Data Flow

This section describes the lifecycle of a request to the `/api/headlines` endpoint.

1.  **Request Initiation**: A user's browser loads `index.html`, and the JavaScript on that page sends an asynchronous `fetch` request to `/api/headlines`.

2.  **Caddy Proxy**: Caddy receives the request on its public port. It forwards the request to one of the available `app` service containers on the internal Docker network.

3.  **FastAPI App Logic**: The `get_headlines` function in `main.py` is executed.

4.  **Cache Check (Cache Hit)**:
    - The app first queries Redis using the `CACHE_KEY`.
    - If the key exists, the cached JSON data is retrieved, parsed, and returned to the user immediately with `source: "cache"`. The request cycle ends here.

5.  **Cache Check (Cache Miss)**:
    - If the main cache key does not exist, the application implements a locking mechanism to prevent multiple workers from fetching the same data simultaneously (a "cache stampede").
    - **Lock Acquisition**: The worker attempts to acquire a short-lived, exclusive lock in Redis.
    - **If Lock is Acquired (The "Leader")**:
        - This worker is now responsible for repopulating the cache.
        - It proceeds to step 6 (External Fetching).
        - After fetching and caching the data (step 7), it releases the lock.
    - **If Lock is Not Acquired (A "Follower")**:
        - Another worker is already fetching the data.
        - This worker will wait for a brief period, periodically checking if the cache has been populated by the leader.
        - If the cache is populated, it reads the new data and returns it.
        - If it times out waiting, it performs a fallback fetch to ensure the user gets a response.

6.  **External Fetching**:
    - The `feed_service` uses `httpx` and an `asyncio.Semaphore` to concurrently fetch RSS feed URLs from external sources without overwhelming the network stack.
    - The XML response is parsed into a list of headline objects using the `feedparser` library.
    - Source-specific logic allocates headline quotas based on configured weights.

7.  **URL Redirect Caching**:
    - For Google News RSS links, the application must resolve the final target URL to bypass Google's redirect pages.
    - The `redirect` service intercepts these URLs and caches the resolved destination in Redis for 24 hours (`RESOLVED_URL_TTL`), saving extensive HTTP overhead on subsequent cache misses.

8.  **Aggregation & Caching**:
    - The headlines from all successful fetches are combined into a single, randomized list.
    - This final list is stored in Redis with a specific Time-To-Live (TTL), so it will automatically expire.

9.  **Response**: The newly fetched list is returned to the user with `source: "live"`.

This architecture ensures the application is fast for most users (due to caching) while being robust enough to handle failures when fetching from external sources.