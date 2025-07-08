# --- Stage 1: Builder ---
# This stage uses the 'builder' image which contains the 'xcaddy' tool
# to build a custom Caddy binary with the required plugins.
FROM caddy:2.8.4-builder AS builder

RUN xcaddy build \
    --with github.com/caddyserver/cache-handler

# --- Stage 2: Final Image ---
# This stage uses the lean 'alpine' image for the final production container.
FROM caddy:2.8.4-alpine

# Copy the custom-built Caddy binary from the builder stage.
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
