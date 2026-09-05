#!/usr/bin/env python
"""Fill in metadata for documents ingested with `--metadata-source skip`.

    uv run python flows/backfill_metadata.py [--limit N] [--batch 50] [--dry-run]

`YouTubeAdapter.fetch_batch`'s `"skip"` path deliberately writes a document with no
`published_at` and no `description`, keeping only the title/duration/view_count the
channel listing already carried for free. Its docstring promises "a separate backfill
pass fills them in later (trivially findable: `published_at IS NULL`)". This is that
pass, which did not exist until now — `skip` was usable but left the corpus in a state
nothing could repair (docs/DECISIONS.md, 2026-08-29).

**Why this is not optional.** A document with no `published_at` is not merely
incomplete, it is invisible to every temporal analytic the corpus exists for:
`rising_entities`, `emerging_entities`, `diffusion_timeline` and `co_occurrence_drift`
all bucket by date, and `corpus_coverage`'s `temporal` block — the pattern/peak/quiet
months a caller is told to read before trusting a grade — is computed from it. An
undated document is searchable and analytically absent, and nothing reports that gap
except `flows/doctor.py` and this flow's own count.

Free: yt-dlp only, no Supadata credits. Serial by construction — bulk concurrent calls
to this endpoint tripped YouTube's bot detection once already (docs/DECISIONS.md), and
that is the same reason `--concurrency` refuses `metadata_source="ytdlp"`.

Committed per batch, so an interrupt keeps its progress and a re-run resumes: the work
list is `published_at IS NULL`, which shrinks as rows are written.
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import re
import time

import sqlalchemy as sa
import structlog
from sqlalchemy import select

from corpus.bronze.store import BronzeStore
from corpus.config import get_settings
from corpus.db.enums import TranscriptUnavailableReason
from corpus.db.models import Document, Source
from corpus.db.session import tenant_session
from corpus.ingest.runner import resolve_tenant_id
from corpus.sources.base import SourceError
from corpus.sources.youtube.ytdlp_meta import YtDlpMetadataClient

log = structlog.get_logger(__name__)


#: Provider phrasing -> the reason we record. Anything not matched here is treated as
#: transient and retried: the failure that actually dominates is YouTube throttling
#: after a few hundred sequential calls, and a throttled video recorded as settled is a
#: document silently excluded from every trend line.
#:
#: None of these is permanent, which is why the value is recorded next to a probe
#: timestamp rather than used to delete the row — MEMBERS_ONLY in particular is a
#: credentials gap, not a fact about the video (see enums.TranscriptUnavailableReason).
_REASON_PATTERNS: list[tuple[re.Pattern[str], TranscriptUnavailableReason]] = [
    (re.compile(r"members-only|join this channel", re.I), TranscriptUnavailableReason.MEMBERS_ONLY),
    (
        re.compile(r"private video|video unavailable|removed by the uploader|terminated", re.I),
        TranscriptUnavailableReason.REMOVED,
    ),
]


def _classify(message: str) -> TranscriptUnavailableReason | None:
    for pattern, reason in _REASON_PATTERNS:
        if pattern.search(message):
            return reason
    return None


def _fetch_with_retry(client, external_id: str, attempts: int = 4):
    """Return `(fetched, reason)`.

    `fetched` is the (normalized, raw) pair, or None. `reason` is the enum value to
    record when the fetch failed for a knowable cause, or None when it failed only
    because we were throttled — which is worth another run and must not be written
    down as if the video were the problem.
    """
    delay = 5.0
    for attempt in range(attempts):
        try:
            return client.fetch_metadata(external_id), None
        except SourceError as exc:
            reason = _classify(str(exc))
            if reason is not None:
                log.info("metadata_unavailable", video_id=external_id, reason=reason.value)
                return None, reason
            if attempt == attempts - 1:
                log.warning("metadata_retries_exhausted", video_id=external_id)
                return None, None
            # Jittered backoff: the throttle is per-client and lifts with time.
            time.sleep(delay + random.uniform(0, delay / 2))
            delay *= 2
    return None, None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="stop after this many documents")
    parser.add_argument("--batch", type=int, default=50, help="documents per commit (default 50)")
    parser.add_argument(
        "--dry-run", action="store_true", help="report what is missing, fetch nothing"
    )
    parser.add_argument(
        "--reprobe",
        action="store_true",
        help=(
            "re-check documents already marked unavailable. None of the reasons is "
            "permanent — a members-only video becomes fetchable with the membership — so "
            "this is the deliberate way to revisit them, not the default."
        ),
    )
    args = parser.parse_args()

    settings = get_settings()
    tenant_id = resolve_tenant_id(settings)
    bronze = BronzeStore(settings.bronze_dir)
    client = YtDlpMetadataClient()

    with tenant_session(tenant_id) as session:
        pending = session.execute(
            select(Document.id, Document.external_id, Source.title)
            .join(Source, Source.id == Document.source_id)
            .where(
                Document.tenant_id == tenant_id,
                Document.published_at.is_(None),
                # Already probed and explained. Re-asking costs a request per row and
                # is what produced the throttling that mislabelled 595 documents.
                sa.true() if args.reprobe else Document.metadata_unavailable_reason.is_(None),
            )
            .order_by(Document.ingested_at)
        ).all()

    print(f"{len(pending)} document(s) with no published_at")
    if args.dry_run or not pending:
        by_source: dict[str, int] = {}
        for _id, _ext, src in pending:
            by_source[src or "?"] = by_source.get(src or "?", 0) + 1
        for src, n in sorted(by_source.items(), key=lambda kv: -kv[1]):
            print(f"  {src[:44]:<45} {n}")
        return 0

    if args.limit:
        pending = pending[: args.limit]

    started = time.monotonic()
    done = failed = gone = 0

    for start in range(0, len(pending), args.batch):
        window = pending[start : start + args.batch]
        with tenant_session(tenant_id) as session:
            for document_id, external_id, _src in window:
                fetched, reason = _fetch_with_retry(client, external_id)
                if fetched is None:
                    # Left as published_at IS NULL rather than guessed at: an inferred
                    # date would silently enter every trend line. `permanent` separates
                    # "this video is gone" from "we were throttled", because only the
                    # second is worth another run.
                    if reason is not None:
                        document = session.get(Document, document_id)
                        if document is not None:
                            document.metadata_unavailable_reason = reason.value
                            document.metadata_probed_at = dt.datetime.now(dt.UTC)
                        gone += 1
                    else:
                        failed += 1
                    continue

                normalized, raw = fetched
                bronze.write(raw)

                document = session.get(Document, document_id)
                if document is None:  # deleted underneath us; nothing to repair
                    continue
                document.published_at = normalized.published_at
                document.published_at_precision = normalized.published_at_precision
                document.published_at_source = normalized.published_at_source
                if normalized.description is not None:
                    document.description = normalized.description
                if normalized.duration_s is not None and document.duration_s is None:
                    document.duration_s = normalized.duration_s
                done += 1
            session.commit()

        elapsed = (time.monotonic() - started) / 60
        rate = done / elapsed if elapsed else 0
        print(f"  {done} filled, {gone} explained, {failed} throttled  ({rate:.1f}/min)")

    tail = f"{failed} throttled — re-run to retry those" if failed else "none throttled"
    print(f"\ndone: {done} dated, {gone} marked unavailable, {tail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
