#!/bin/sh
# Ensure mounted volumes are writable by appuser (UID 999)
# Needed when host directories are mounted with root ownership (e.g. Unraid, NAS)
chown -R appuser:appuser /app/db 2>/dev/null || true

exec gosu appuser "$@"
