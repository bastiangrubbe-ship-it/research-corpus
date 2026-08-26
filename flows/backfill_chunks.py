#!/usr/bin/env python
"""Chunk every transcript and embed the chunks — builds the chunk-level dense index.

    uv run python flows/backfill_chunks.py [--limit N] [--batch 25]

Local only (windowing plus a local embedding model): no API calls, no credits, no
subscription quota, safe to run unattended over the whole corpus.

What it buys: the dense lane previously searched `document_summary`, roughly **7% of
a median transcript**. With chunks it searches the whole document, and because chunks
carry `start_ms`/`end_ms`, a hit can cite a position in the video rather than just
naming it. See docs/DECISIONS.md (2026-08-25) for the measurement that motivated it.

Commits per batch, so an interrupted run keeps its progress and resumes.
"""

from __future__ import annotations

import argparse
import time

from corpus.chunking.backfill import backfill_chunks
from corpus.config import get_settings
from corpus.db.session import tenant_session
from corpus.ingest.runner import resolve_tenant_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="stop after this many documents")
    parser.add_argument("--batch", type=int, default=25, help="documents per commit (default 25)")
    args = parser.parse_args()

    settings = get_settings()
    tenant_id = resolve_tenant_id(settings)

    total_docs = 0
    total_chunks = 0
    started = time.monotonic()
    while args.limit is None or total_docs < args.limit:
        size = args.batch if args.limit is None else min(args.batch, args.limit - total_docs)
        with tenant_session(tenant_id) as session:
            docs, chunks = backfill_chunks(session, tenant_id=tenant_id, limit=size)
        if docs == 0:
            break
        total_docs += docs
        total_chunks += chunks
        elapsed = time.monotonic() - started
        print(
            f"  {total_docs} docs / {total_chunks:,} chunks "
            f"({total_chunks / elapsed:.0f} chunks/s, {elapsed / 60:.1f}m)",
            flush=True,
        )

    elapsed = time.monotonic() - started
    print(f"\ndone: {total_docs} documents -> {total_chunks:,} chunks in {elapsed / 60:.1f}m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
