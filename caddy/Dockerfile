# --- Stage 1: Builder ---
# This stage uses the 'builder' image which contains Go and xcaddy
# to build a pinned Caddy binary with the cache-handler plugin.
FROM caddy:builder AS builder

RUN xcaddy build v2.9.1 \
    --with github.com/caddyserver/cache-handler@v0.14.0

# --- Stage 2: Final Image ---
# This stage uses the lean 'alpine' image for the final production container.
# We use the official Caddy alpine image as a base to ensure all necessary
# dependencies like ca-certificates are present.
FROM caddy:alpine

# Copy the custom-built Caddy binary from the builder stage, replacing the
# default one.
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
