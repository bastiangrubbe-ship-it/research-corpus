#!/usr/bin/env bash
# Nightly pipeline: refresh sources, then extract entities from what's new.
#
# Invoked by a scheduler (launchd on macOS today, systemd later — see
# launchd/README.md) as a single chained job rather than two independently-timed
# ones, so entity extraction never races ahead of a still-running ingest.
#
# `set -a` exports every variable `.env` defines into this script's environment
# before the child processes start. That matters specifically for
# CLAUDE_CODE_OAUTH_TOKEN: pydantic-settings reads .env into the Settings object
# without touching the process environment, but the `claude` CLI (subprocess.run
# in corpus.enrich.entities) reads its credential from the real environment, so it
# must actually be exported here, not just parsed by Python.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

set -a
[ -f .env ] && source .env
set +a

echo "[$(date -u +%FT%TZ)] nightly: ingest"
uv run python flows/ingest_youtube.py

echo "[$(date -u +%FT%TZ)] nightly: entity extraction"
uv run python flows/nightly_entities.py

echo "[$(date -u +%FT%TZ)] nightly: done"
