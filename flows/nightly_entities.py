#!/usr/bin/env python
"""Extract entities for documents not yet processed at the current prompt version.

    uv run python flows/nightly_entities.py [--limit 50] [--dry-run]

A plain script wrapped by the scheduler (launchd/systemd), same convention as
flows/ingest_youtube.py — no orchestration logic here, that lives in
corpus.enrich.entities. Meant to run once a day, after the YouTube ingest flow, so it
only ever sees documents whose transcripts have already landed.

Each document is its own `claude -p` subprocess call and its own transaction: one bad
transcript (timeout, malformed model output) is logged and skipped rather than losing
the whole run.
"""

from __future__ import annotations

import argparse
import sys

import structlog

from corpus.config import get_settings
from corpus.db.session import tenant_session
from corpus.enrich.entities import (
    ClaudeCodeCallError,
    ExtractionError,
    enrich_document,
    extractor_version,
    find_unenriched_documents,
)
from corpus.ingest.runner import resolve_tenant_id

log = structlog.get_logger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="cap documents processed this run")
    parser.add_argument("--dry-run", action="store_true", help="print the queue, extract nothing")
    args = parser.parse_args()

    settings = get_settings()
    tenant_id = resolve_tenant_id(settings)
    version = extractor_version()

    with tenant_session(tenant_id) as session:
        pending = find_unenriched_documents(
            session, tenant_id=tenant_id, extractor_version_str=version, limit=args.limit
        )

    print(f"{len(pending)} document(s) queued for entity extraction (extractor_version={version})")
    if args.dry_run or not pending:
        return 0

    ok, failed = 0, 0
    for document_id, transcript_version_id in pending:
        with tenant_session(tenant_id) as session:
            try:
                count = enrich_document(
                    session,
                    tenant_id=tenant_id,
                    document_id=document_id,
                    transcript_version_id=transcript_version_id,
                )
                print(f"  {document_id} -> {count} mention(s)")
                ok += 1
            except (ClaudeCodeCallError, ExtractionError) as exc:
                log.warning(
                    "entity_extraction_failed", document_id=str(document_id), error=str(exc)
                )
                print(f"  ! {document_id} failed: {exc}", file=sys.stderr)
                failed += 1

    print(f"\ndone: {ok} succeeded, {failed} failed")
    return 1 if failed and not ok else 0


if __name__ == "__main__":
    raise SystemExit(main())
