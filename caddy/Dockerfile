# --- Stage 1: Builder ---
# This stage uses the 'builder' image which contains Go and xcaddy
# to build the latest Caddy binary from the master branch on GitHub.
FROM caddy:builder AS builder

RUN xcaddy build master \
    --with github.com/caddyserver/cache-handler

# --- Stage 2: Final Image ---
# This stage uses the lean 'alpine' image for the final production container.
# We use the official Caddy alpine image as a base to ensure all necessary
# dependencies like ca-certificates are present.
FROM caddy:alpine

# Copy the custom-built Caddy binary from the builder stage, replacing the
# default one.
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
