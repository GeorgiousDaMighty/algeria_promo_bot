#!/bin/sh
set -eu

# Railway mounts persistent volumes at runtime, after image ownership is set.
# Fix only the application data directory, then drop privileges permanently.
chown jet:jet /app/data
exec gosu jet "$@"
