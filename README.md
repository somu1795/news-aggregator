<div align="center">
  <h1>News Aggregator</h1>
  <p>
    A production-ready, containerized news aggregator application built with FastAPI and Redis.
  </p>
</div>

---

## Features

- **Fast & Asynchronous**: Built with FastAPI for high performance.
- **Robust Caching**: Uses **Redis** to cache headlines, providing fast responses and reducing load on external news sources.
- **High Concurrency Ready**: Implements a Redis-based distributed lock to prevent cache stampedes under high load.
- **Resilient Fetching**: Automatically retries failed requests to RSS feeds and supports fallback URLs for each source.
- **Containerized & Production-Ready**: Fully containerized with a multi-stage **Docker** build for a small, secure, and fast-starting image.
- **Orchestrated with Docker Compose**: Services are managed with a single `docker-compose.yml` file for easy setup and deployment.
- **Secure by Default**: Runs as a non-root user inside the container and uses an internal reverse proxy (**Caddy**) for load balancing.
- **Monitoring**: Exposes Prometheus metrics for observability via `/metrics`.
- **Well-Tested**: Includes a suite of unit tests using `pytest` and `fakeredis`.

## Architecture Overview

The system is designed as a set of microservices orchestrated by Docker Compose. An external reverse proxy (like Nginx or Traefik) is expected to handle TLS termination and route traffic to the internal **Caddy** service. Caddy then load-balances requests across multiple **FastAPI/Uvicorn** application workers. **Redis** serves as both a high-speed cache and a distributed lock manager.

For a detailed diagram and explanation, please see the ARCHITECTURE.md file.

## Tech Stack

- **Backend**: FastAPI, Python 3.10
- **Cache & Locking**: Redis
- **Internal Proxy**: Caddy
- **Containerization**: Docker, Docker Compose
- **Testing**: Pytest, fakeredis, respx

## Getting Started

### Prerequisites

- Docker
- Docker Compose (v2+)

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

3.  **Build and run the services:**
    This command will build the Docker images and start all services in the background.
    ```sh
    docker-compose up --build -d
    ```

The application will be available at `http://<your-host-ip>:7001` (or whichever port you set for `CADDY_HTTP_PORT`).

## Configuration

All configuration is managed via the `.env` file. See `.env.example` for a full list of available options and their descriptions.

| Variable           | Description                                                              | Default      |
| ------------------ | ------------------------------------------------------------------------ | ------------ |
| `ALLOWED_HOSTS`    | Comma-separated list of allowed hostnames. **Required.**                 | `localhost`  |
| `ENABLE_API_DOCS`  | Set to "true" to enable interactive API docs at `/docs`.                   | `false`      |
| `ADMIN_API_KEY`    | A secret key to protect administrative endpoints. **Required.**            | `changeme`   |
| `UVICORN_WORKERS`  | Number of Uvicorn worker processes per container.                        | `9`          |
| `CADDY_HTTP_PORT`  | The external port Caddy will listen on.                                  | `7001`       |
| `API_RATE_LIMIT`   | Rate limit for the /api/headlines endpoint per IP.                       | `100/minute` |
| `APP_CPUS`         | Max CPU cores for each app container.                                    | `4`          |
| `APP_MEM_LIMIT`    | Max memory for each app container (e.g., `512m`, `2G`).                  | `2G`         |
| `REDIS_MAX_MEMORY` | Max memory for the Redis container.                                      | `256mb`      |

## Running Tests

To run the unit tests, execute the following command. It will install the development dependencies and then run `pytest` inside the application container.

```sh
docker-compose exec -it app sh -c "pip install -r requirements-dev.txt && pytest"
```

## Scaling for High Traffic

To handle a large number of users (e.g., 10,000+), you will need to adjust the default configuration and potentially scale your infrastructure.

1.  **Vertical Scaling (More Power to One Instance)**:
    -   In your `.env` file, increase `UVICORN_WORKERS`. A good formula is `(2 * number of CPU cores) + 1`. For example, if you allocate 8 cores (`APP_CPUS=8`), set `UVICORN_WORKERS=17`.
    -   Increase `APP_MEM_LIMIT` and `REDIS_MAX_MEMORY` as needed.

2.  **Horizontal Scaling (More Instances)**:
    -   The most effective way to scale is to run multiple instances of the `app` container. Docker Compose makes this easy:
        ```sh
        docker-compose up --build -d --scale app=3
        ```
    -   This will start 3 `app` containers, and Caddy will automatically load-balance traffic between them. The shared Redis instance ensures caching and locking work correctly across all containers.
```

## API Endpoints

- `GET /`: Serves the HTML frontend.
- `GET /api/headlines`: Fetches the latest news headlines.
- `GET /health`: Health check endpoint for monitoring.
- `GET /metrics`: Prometheus metrics endpoint.