#!/usr/bin/env python
"""Extract entities for documents not yet processed at the current prompt version.

    uv run python flows/nightly_entities.py [--limit 50] [--concurrency 20] [--dry-run]

A plain script wrapped by the scheduler (launchd/systemd), same convention as
flows/ingest_youtube.py — no orchestration logic here, that lives in
corpus.enrich.entities. Meant to run once a day, after the YouTube ingest flow, so it
only ever sees documents whose transcripts have already landed.

Each document is its own `claude -p` subprocess call. `--concurrency` measured safe up
to 25 in practice (docs/DECISIONS.md) with zero errors and near-linear wall-clock
speedup — the extraction call itself (`extract_entities`) touches no database, so the
thread pool below runs it without ever holding a DB connection for the ~90s a call
typically takes. Only the fast span-finding + persist step opens a `tenant_session`,
one per document, held just long enough to write. One bad transcript (timeout,
malformed model output) is logged and skipped rather than losing the whole run.
"""

from __future__ import annotations

import argparse
import sys

import structlog

from corpus.config import get_settings
from corpus.db.session import tenant_session
from corpus.enrich.entities import (
    enrich_documents_concurrent,
    extractor_version,
    find_unenriched_documents,
)
from corpus.ingest.runner import resolve_tenant_id

log = structlog.get_logger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="cap documents processed this run")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="parallel `claude -p` calls (default 1, sequential)",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the queue, extract nothing")
    args = parser.parse_args()

    settings = get_settings()
    tenant_id = resolve_tenant_id(settings)
    version = extractor_version()
    # Deliberately not resolved here: passing None lets corpus.enrich.entities pick
    # per document by size, escalating oversized transcripts to a larger-context
    # model. Passing the configured default explicitly disables that.
    model = None

    with tenant_session(tenant_id) as session:
        pending = find_unenriched_documents(
            session, tenant_id=tenant_id, extractor_version_str=version, limit=args.limit
        )

    print(f"{len(pending)} document(s) queued for entity extraction (extractor_version={version})")
    if args.dry_run or not pending:
        return 0

    ok, failed = 0, 0
    for document_id, mention_count, error in enrich_documents_concurrent(
        pending, tenant_id=tenant_id, model=model, concurrency=args.concurrency
    ):
        if error is not None:
            log.warning("entity_extraction_failed", document_id=str(document_id), error=str(error))
            print(f"  ! {document_id} failed: {error}", file=sys.stderr)
            failed += 1
            continue
        print(f"  {document_id} -> {mention_count} mention(s)")
        ok += 1

    print(f"\ndone: {ok} succeeded, {failed} failed")
    return 1 if failed and not ok else 0


if __name__ == "__main__":
    raise SystemExit(main())
