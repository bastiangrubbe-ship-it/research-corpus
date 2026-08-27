#!/usr/bin/env python
"""Re-fetch transcripts for documents whose fetch failed but which CAN be fetched.

    uv run python flows/retry_failed_transcripts.py [--limit N] [--dry-run]

Only touches documents that `flows/probe_missing_transcripts.py` marked
`fetch_failed` — reachable, captioned, and holding no transcript. Documents blocked by
a membership, missing captions, or removal are left alone: re-fetching those cannot
work and would spend credits proving it.

Attaches to the EXISTING document row rather than creating one. The stub already
carries the external_id, the source link, and whatever metadata was recovered, and
`ingest_source`'s persist path only knows how to insert new documents — using it here
would duplicate every one of these.

Provider order is ytapi-first (free) exactly as the normal ingest path uses, so this
usually costs nothing. It can fall through to Supadata, which does spend credits, for
at most `--limit` documents.
"""

from __future__ import annotations

import argparse
import datetime as dt

from sqlalchemy import select

from corpus.config import get_settings
from corpus.db.models import Document, Segment, TranscriptVersion
from corpus.db.session import tenant_session
from corpus.ingest.runner import build_adapter, resolve_tenant_id

RETRYABLE = "fetch_failed"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true", help="list what would be retried")
    args = parser.parse_args()

    settings = get_settings()
    tenant_id = resolve_tenant_id(settings)

    with tenant_session(tenant_id) as session:
        rows = session.execute(
            select(Document.id, Document.external_id, Document.source_id, Document.title)
            .where(
                Document.tenant_id == tenant_id,
                Document.transcript_unavailable_reason == RETRYABLE,
            )
            .order_by(Document.external_id)
        ).all()
        if args.limit:
            rows = rows[: args.limit]

        print(f"{len(rows)} document(s) marked {RETRYABLE}")
        if args.dry_run or not rows:
            for r in rows:
                print(f"  {r.external_id}  {(r.title or '(no title)')[:60]}")
            return 0

        adapter, _client = build_adapter(tenant_id)
        recovered = still_failing = 0

        for row in rows:
            try:
                result = adapter.fetch(row.external_id)
            except Exception as exc:
                print(f"  FAIL {row.external_id}: {type(exc).__name__}: {exc}", flush=True)
                still_failing += 1
                continue

            if result.transcript is None:
                print(f"  none {row.external_id}: still no transcript", flush=True)
                still_failing += 1
                continue

            tv = TranscriptVersion(
                tenant_id=tenant_id,
                document_id=row.id,
                provider=result.transcript.provider,
                is_auto_generated=result.transcript.is_auto_generated,
                provenance_confidence=result.transcript.provenance_confidence,
                lang=result.transcript.lang,
                available_langs=list(result.transcript.available_langs),
            )
            session.add(tv)
            session.flush()
            session.add_all(
                Segment(
                    tenant_id=tenant_id,
                    transcript_version_id=tv.id,
                    idx=seg.idx,
                    text=seg.text,
                    offset_ms=seg.offset_ms,
                    duration_ms=seg.duration_ms,
                )
                for seg in result.transcript.segments
            )

            # Fill in metadata only where the stub has none. Never overwrite a value
            # already recorded — the stub's title may have come from a better source
            # than this retry's metadata call.
            doc = session.get(Document, row.id)
            for field in ("title", "url", "description", "duration_s", "published_at"):
                if getattr(doc, field, None) in (None, "") and getattr(
                    result.document, field, None
                ):
                    setattr(doc, field, getattr(result.document, field))
            doc.transcript_unavailable_reason = None
            doc.transcript_probed_at = dt.datetime.now(dt.UTC)

            session.commit()
            recovered += 1
            n_seg = len(result.transcript.segments)
            print(f"  OK   {row.external_id}: {n_seg} segments", flush=True)

        print()
        print(f"recovered {recovered}, still failing {still_failing}")
        if recovered:
            print("Downstream stages do NOT run automatically. To make these searchable:")
            print("  uv run python flows/backfill_summaries.py")
            print("  uv run python flows/backfill_chunks.py")
            print("  uv run python flows/restore_transcripts.py")
        if still_failing:
            print(
                f"{still_failing} kept their {RETRYABLE} marker. Re-probe to reclassify:"
                "\n  uv run python flows/probe_missing_transcripts.py --reprobe --concurrency 1"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
