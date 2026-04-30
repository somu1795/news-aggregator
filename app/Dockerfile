# --- Backend ---
FROM python:3.14-slim-bookworm

# Set environment variables to prevent Python from writing .pyc files and to buffer output
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install curl (Docker healthcheck).
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# Create a non-root user for security
RUN addgroup --system --gid 1001 app && \
    adduser --system --uid 1001 --ingroup app app

# Create and set permissions for the log directory before switching user.
RUN mkdir -p /var/log/app && \
    chown -R app:app /var/log/app

WORKDIR /app

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code and static assets, then set ownership
COPY --chown=app:app . .

# Ensure the entrypoint script is executable
RUN chmod +x ./entrypoint.sh

# Switch to the non-root user
USER app

# Expose the port the app runs on
EXPOSE 8989

# The entrypoint script will execute the main command (Uvicorn)
ENTRYPOINT ["./entrypoint.sh"]
