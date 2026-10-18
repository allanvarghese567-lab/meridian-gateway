#!/bin/sh
set -eu

if [ ! -s /etc/nginx/certs/tls.crt ] || [ ! -s /etc/nginx/certs/tls.key ]; then
    mkdir -p /etc/nginx/certs
    if ! command -v openssl >/dev/null 2>&1; then
        apk add --no-cache openssl >/dev/null
    fi
    openssl req -x509 -nodes -newkey rsa:2048 -days 30 \
        -keyout /etc/nginx/certs/tls.key \
        -out /etc/nginx/certs/tls.crt \
        -subj "/CN=localhost"
fi

exec /docker-entrypoint.sh nginx -g "daemon off;"
