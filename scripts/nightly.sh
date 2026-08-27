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

# PROJECT_DATA_DIR lives in .envrc, not .env, and direnv only loads .envrc for an
# interactive shell. launchd never runs direnv, so this script sourced .env, got no
# PROJECT_DATA_DIR, and every nightly run died in Settings() validation -- four
# consecutive nights, silently, because nothing reads the log unless something
# already looks wrong (docs/DECISIONS.md, 2026-08-27).
#
# Re-exec under `direnv exec` rather than sourcing .envrc directly: .envrc calls
# direnv builtins (dotenv_if_exists) that plain bash has no definition for. Failing
# loudly here is deliberate -- a scheduled job that cannot resolve its data directory
# must not fall back to a default and write somewhere unexpected.
if [ -z "${PROJECT_DATA_DIR:-}" ]; then
  if command -v direnv >/dev/null 2>&1; then
    exec direnv exec . "$0" "$@"
  fi
  echo "[$(date -u +%FT%TZ)] nightly: FATAL — PROJECT_DATA_DIR unset and direnv not on PATH" >&2
  exit 1
fi

set -a
[ -f .env ] && source .env
set +a

# Any non-zero exit below records a failure heartbeat before the script dies, so the
# doctor can tell "ran and broke" from "never started".
on_failure() {
  uv run python -c "
from corpus.config import get_settings
from corpus.db.session import tenant_session
from corpus.ingest.runner import resolve_tenant_id
from corpus.ops.heartbeat import record_failure
s = get_settings(); tid = resolve_tenant_id(s)
with tenant_session(tid) as session:
    record_failure(session, tenant_id=tid, name='nightly', detail='nightly.sh exited non-zero')
" 2>/dev/null || true
}
trap on_failure ERR

echo "[$(date -u +%FT%TZ)] nightly: ingest"
uv run python flows/ingest_youtube.py

echo "[$(date -u +%FT%TZ)] nightly: entity extraction"
uv run python flows/nightly_entities.py

# Records a successful completion so `flows/doctor.py` can report when ingestion last
# actually finished. Without it the only evidence of a broken nightly is a traceback in
# a log nobody opens.
uv run python -c "
from corpus.config import get_settings
from corpus.db.session import tenant_session
from corpus.ingest.runner import resolve_tenant_id
from corpus.ops.heartbeat import record_heartbeat
s = get_settings(); tid = resolve_tenant_id(s)
with tenant_session(tid) as session:
    record_heartbeat(session, tenant_id=tid, name='nightly')
"

echo "[$(date -u +%FT%TZ)] nightly: done"
