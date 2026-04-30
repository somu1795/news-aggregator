"""
Centralized structured logging configuration using structlog.

Provides JSON-formatted output in production for machine parsing by log
aggregators (Loki, ELK, Datadog), and pretty console output during
local development.  Routes stdlib loggers (including Uvicorn) through
the same structlog pipeline for a unified log format.
"""

import logging
import sys

import structlog


def configure_logging(log_level: str = "INFO") -> None:
    """
    Configure structlog for the entire application.

    Must be called once, early in the application startup (before any logger
    is used), so that all loggers — including those created at module level —
    pick up the configuration on first use.
    """
    level = getattr(logging, log_level.upper(), logging.INFO)

    # Shared processors used by both structlog and stdlib loggers
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    # Configure structlog's own pipeline
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Create a formatter that renders structlog events as JSON
    json_formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )

    # Apply to the root stdlib logger so Uvicorn and third-party logs
    # are also routed through the structlog pipeline.
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(json_formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Reduce noise from third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
