# Smokescreen (github.com/stripe/smokescreen): an HTTP(S) proxy that only connects to
# public addresses. It is the headless browser's only way out (see docker-compose.yml).
FROM golang:1.26.8-alpine AS build
ARG VERSION=v0.1.0
ARG COMMIT=609eb8931420453daf5893509be0b25b21bd9edb
RUN apk add --no-cache git
WORKDIR /src
RUN git clone --depth 1 --branch "$VERSION" https://github.com/stripe/smokescreen . \
    && test "$(git rev-parse HEAD)" = "$COMMIT"
RUN CGO_ENABLED=0 go build -mod=vendor -trimpath -ldflags="-s -w" -o /smokescreen .

FROM alpine:3.24.2
COPY --from=build /smokescreen /usr/local/bin/smokescreen
USER 65534:65534
EXPOSE 4750
ENTRYPOINT ["smokescreen"]
