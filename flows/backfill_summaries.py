#!/usr/bin/env python
"""Summarize + embed every document that doesn't have a document_summary yet.

    uv run python flows/backfill_summaries.py [--limit N] [--batch 100]

Entirely local — TextRank plus a local embedding model, no API calls, no credits, no
subscription quota. That is what makes it safe to run over the whole corpus
unattended, unlike flows/nightly_entities.py.

Why this matters more than it looks: `document_summary.embedding` is the *only* thing
the dense retrieval lane searches, and `document_summary.text` is the only thing the
cross-encoder reranks. A document without a summary row is invisible to both — it can
still be found lexically by title, but semantic search and reranking cannot see it at
all. Running this partially (as happened during development, leaving 130 of 3,344
documents embedded) silently caps recall at that fraction while every query still
returns confident-looking results (see docs/DECISIONS.md, 2026-08-25).

Committed per batch rather than at the end, so an interrupted run keeps its progress
and simply resumes on the next invocation.
"""

from __future__ import annotations

import argparse
import time

from corpus.config import get_settings
from corpus.db.session import tenant_session
from corpus.enrich.relevance_gate import backfill_document_summaries
from corpus.ingest.runner import resolve_tenant_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="stop after this many documents")
    parser.add_argument("--batch", type=int, default=100, help="documents per commit (default 100)")
    parser.add_argument(
        "--redo",
        action="store_true",
        help=(
            "re-derive summaries that already exist, overwriting them — for when the "
            "summariser or embedding model changes. It will NOT pick up restored "
            "text: summaries pin to the newest non-restored version, because restored "
            "text measured worse as retrieval input. Without this flag a re-run is a "
            "silent no-op."
        ),
    )
    args = parser.parse_args()

    settings = get_settings()
    tenant_id = resolve_tenant_id(settings)

    total = 0
    started = time.monotonic()
    while args.limit is None or total < args.limit:
        size = args.batch if args.limit is None else min(args.batch, args.limit - total)
        with tenant_session(tenant_id) as session:
            # A redo's work list never shrinks -- every document matches on every
            # call -- so it walks forward on an explicit offset instead.
            done = backfill_document_summaries(
                session,
                tenant_id=tenant_id,
                limit=size,
                redo=args.redo,
                offset=total if args.redo else 0,
            )
        if done == 0:
            break
        total += done
        elapsed = time.monotonic() - started
        rate = total / elapsed if elapsed else 0
        print(
            f"  {total} summarized+embedded  ({rate:.1f}/s, {elapsed / 60:.1f}m elapsed)",
            flush=True,
        )

    mins = (time.monotonic() - started) / 60
    verb = "re-derived" if args.redo else "summarized and embedded"
    print(f"\ndone: {total} documents {verb} in {mins:.1f}m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
