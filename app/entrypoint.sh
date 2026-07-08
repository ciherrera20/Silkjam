#!/bin/bash
set -euo pipefail

PUID=${PUID:-1001}
PGID=${PGID:-1001}

CURRENT_UID=$(id -u silkjam)
CURRENT_GID=$(id -g silkjam)

if [ "$CURRENT_GID" != "$PGID" ]; then
    groupmod -o -g "$PGID" silkjam
fi

if [ "$CURRENT_UID" != "$PUID" ]; then
    usermod -o -u "$PUID" silkjam
fi

# Fix only top-level ownership.
chown silkjam:silkjam /app/data /etc/caddy/share

exec /usr/bin/supervisord