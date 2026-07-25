#!/bin/sh
set -eu

export PORT="${PORT:-10000}"
envsubst '${PORT}' < /app/docker/nginx.conf.template > /tmp/nginx.conf

exec supervisord -c /app/docker/supervisord.conf
