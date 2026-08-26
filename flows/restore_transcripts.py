#!/usr/bin/env python
"""Restore punctuation on every raw transcript that has no restored derivation yet.

    uv run python flows/restore_transcripts.py [--limit N] [--min-words N]

Entirely local — a transformer on MPS, no API calls, no credits, no subscription
quota. Safe to leave running unattended, like flows/backfill_summaries.py and unlike
flows/nightly_entities.py.

Resumable by construction: the work list is "raw versions with no child version
deriving from them", so an interrupted run simply picks up where it stopped. Each
document commits on its own; nothing is batched across documents.

Why bother, given entity extraction does not need it: `summarize_extractive`
tokenizes into sentences with NLTK before running TextRank, and unpunctuated ASR
gives NLTK nothing to split on. That is what produced the 38k-character "sentences"
that made reranking take 306s (docs/DECISIONS.md, 2026-08-25).

Note what this does NOT do: existing document summaries and chunks are left alone.
They were derived from raw text and stay that way until their own backfills are
re-run against the restored versions, which is a separate, deliberate decision --
re-deriving 3,267 summaries and 70k chunks is a much bigger job than this one.
"""

from __future__ import annotations

import argparse
import time

from sqlalchemy import text

from corpus.config import get_settings
from corpus.db.session import tenant_session
from corpus.enrich.restore import restore_transcript_version
from corpus.ingest.runner import resolve_tenant_id

# Ordered shortest-first so an interrupted run has completed as many documents as
# possible, rather than having sunk its time into a few very long ones.
PENDING_SQL = """
select p.id,
       sum(array_length(regexp_split_to_array(sg.text, '\\s+'), 1)) as words
from transcript_version p
join segment sg on sg.transcript_version_id = p.id
where p.tenant_id = :tenant_id
  and p.provider <> 'restored'
  and not exists (
      select 1 from transcript_version c where c.derived_from_id = p.id
  )
group by p.id
having sum(array_length(regexp_split_to_array(sg.text, '\\s+'), 1)) >= :min_words
order by words asc
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--min-words",
        type=int,
        default=1,
        help="skip versions shorter than this; 1 keeps everything with any text at all",
    )
    args = parser.parse_args()

    settings = get_settings()
    tenant_id = resolve_tenant_id(settings)

    with tenant_session(tenant_id) as session:
        rows = session.execute(
            text(PENDING_SQL), {"tenant_id": tenant_id, "min_words": args.min_words}
        ).all()
        if args.limit:
            rows = rows[: args.limit]

        total_words = sum(r.words or 0 for r in rows)
        print(f"{len(rows)} versions pending, {total_words:,} words", flush=True)

        started = time.time()
        done = words_done = failed = fell_back = 0

        for row in rows:
            try:
                new_id = restore_transcript_version(
                    session, tenant_id=tenant_id, transcript_version_id=row.id
                )
            except Exception as exc:  # keep going; one bad transcript is not the run
                session.rollback()
                failed += 1
                print(f"  FAILED {row.id}: {type(exc).__name__}: {exc}", flush=True)
                continue

            if new_id is None:
                continue

            # A single-segment result means _realign_to_segments hit a word-count
            # mismatch and refused to distribute text over offsets it no longer
            # matched. Worth counting: it is the difference between a restored
            # transcript that can still be cited by timestamp and one that cannot.
            n_segments = session.execute(
                text("select count(*) from segment where transcript_version_id = :i"), {"i": new_id}
            ).scalar()
            parent_segments = session.execute(
                text("select count(*) from segment where transcript_version_id = :i"), {"i": row.id}
            ).scalar()
            if n_segments != parent_segments:
                fell_back += 1

            done += 1
            words_done += row.words or 0
            if done % 25 == 0:
                rate = words_done / max(time.time() - started, 1e-9)
                remaining = (total_words - words_done) / rate if rate else 0
                print(
                    f"  {done}/{len(rows)}  {words_done:,}/{total_words:,} words  "
                    f"{rate:,.0f} w/s  ~{remaining / 3600:.1f}h left  "
                    f"(fell back to 1 segment: {fell_back}, failed: {failed})",
                    flush=True,
                )

        elapsed = time.time() - started
        print(
            f"done: {done} restored, {failed} failed, {fell_back} lost segment structure, "
            f"{words_done:,} words in {elapsed / 3600:.2f}h",
            flush=True,
        )


if __name__ == "__main__":
    main()
