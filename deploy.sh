#!/usr/bin/env bash
# Retired security boundary.
#
# This file previously represented a mutable, host-specific deployment path.
# It is intentionally non-operational so a maintenance command cannot bypass
# the immutable release, TLS, migration and release-lock gates.
set -euo pipefail

cat >&2 <<'MESSAGE'
deploy.sh is retired and will not connect to any target.

Use the immutable release procedure in README.md instead:
1. verify a digest-pinned release manifest;
2. run the controlled owner migration job;
3. render the architecture-specific release.env;
4. start deploy/docker-compose.release*.yml with --no-build.
MESSAGE
exit 64
