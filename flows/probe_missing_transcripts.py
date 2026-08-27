#!/usr/bin/env python
"""Find out WHY documents have no transcript, and record it.

    uv run python flows/probe_missing_transcripts.py [--limit N] [--reprobe]

Free: yt-dlp metadata only, no Supadata credits and no transcript fetch. Safe to
re-run.

Why this is worth a flow rather than a one-off script: without a recorded reason, a
document with no transcript is indistinguishable from one not yet processed, so
`flows/doctor.py` reports stages as permanently incomplete and the check stops being
read. Measured on this corpus: of 74 such documents, 38 are members-only, 22 have no
captions at all, 2 are removed, and 12 could actually be fetched.

`--reprobe` re-checks documents that already carry a reason. Worth doing occasionally:
none of the reasons is permanent. A members-only video becomes fetchable to an account
with that membership, and a creator can add captions later.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import subprocess

from sqlalchemy import text

from corpus.config import get_settings
from corpus.db.enums import TranscriptUnavailableReason as Reason
from corpus.db.session import tenant_session
from corpus.ingest.runner import resolve_tenant_id

PROBE_TIMEOUT_S = 60


#: Errors that mean the PROBE was blocked, not that the video lacks a transcript.
#: YouTube rate-limits after a burst of metadata requests, and its message ("Sign in
#: to confirm you're not a bot") says nothing whatsoever about captions. Recording
#: these as a reason would coalesce "we could not check" into "we checked and it
#: failed" — the same class of error this schema forbids for `is_auto_generated`.
#: An earlier version of this flow did exactly that and overwrote 22 correct
#: `no_captions` findings with a rate-limit artefact.
_PROBE_BLOCKED_MARKERS = (
    "sign in to confirm",
    "not a bot",
    "too many requests",
    "http error 429",
    "rate limit",
    "temporarily blocked",
)


def classify(video_id: str) -> tuple[str, str] | None:
    """(reason, note), or None when the probe itself was blocked and nothing was
    learned. None must leave the column NULL rather than writing a guess."""
    try:
        proc = subprocess.run(
            [
                "yt-dlp",
                "-J",
                "--skip-download",
                "--no-warnings",
                f"https://www.youtube.com/watch?v={video_id}",
            ],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return None  # a timeout tells us nothing about the video

    if proc.returncode != 0 or not proc.stdout.strip():
        err = (proc.stderr or "").lower()
        if any(marker in err for marker in _PROBE_BLOCKED_MARKERS):
            return None  # blocked, not determined
        if "member" in err:
            return Reason.MEMBERS_ONLY, "needs a channel membership"
        if "private" in err or "removed" in err or "unavailable" in err or "deleted" in err:
            return Reason.REMOVED, "gone"
        return Reason.FETCH_FAILED, (proc.stderr or "").strip()[:100]

    try:
        # strict=False: yt-dlp leaves raw control characters in descriptions.
        payload = json.loads(proc.stdout, strict=False)
    except json.JSONDecodeError:
        return None  # we got something back but could not read it — not a finding

    has_captions = bool(payload.get("subtitles")) or bool(payload.get("automatic_captions"))
    if not has_captions:
        return Reason.NO_CAPTIONS, "reachable, no captions of any kind"
    # Reachable and captioned, yet we hold no transcript: the fetch itself failed.
    # This is the only value a retry can act on.
    return Reason.FETCH_FAILED, "reachable and captioned — retryable"


PENDING_SQL = """
select d.id, d.external_id
from document d
where d.tenant_id = :t
  and d.external_id is not null
  and not exists (select 1 from transcript_version tv where tv.document_id = d.id)
  {only_unprobed}
order by d.external_id
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument(
        "--reprobe",
        action="store_true",
        help="re-check documents that already carry a reason; none of the reasons is permanent",
    )
    args = parser.parse_args()

    settings = get_settings()
    tenant_id = resolve_tenant_id(settings)
    clause = "" if args.reprobe else "and d.transcript_unavailable_reason is null"

    with tenant_session(tenant_id) as session:
        rows = session.execute(
            text(PENDING_SQL.format(only_unprobed=clause)), {"t": tenant_id}
        ).all()
        if args.limit:
            rows = rows[: args.limit]
        if not rows:
            print("nothing to probe")
            return 0

        print(f"probing {len(rows)} documents with no transcript (free, no credits)", flush=True)
        counts: dict[str, int] = {}
        now = dt.datetime.now(dt.UTC)

        blocked = 0
        with cf.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            results = pool.map(lambda r: (r.id, classify(r.external_id)), rows)
            for done, (doc_id, verdict) in enumerate(results, start=1):
                if verdict is None:
                    # Leave the column untouched: NULL, or whatever a successful
                    # earlier probe found. Never overwrite a finding with a
                    # non-finding.
                    blocked += 1
                else:
                    reason, _note = verdict
                    session.execute(
                        text(
                            "update document set transcript_unavailable_reason = :r, "
                            "transcript_probed_at = :now where id = :id"
                        ),
                        {"r": str(reason), "now": now, "id": doc_id},
                    )
                    counts[str(reason)] = counts.get(str(reason), 0) + 1
                if done % 20 == 0:
                    session.commit()
                    print(f"  {done}/{len(rows)} probed, {blocked} blocked", flush=True)
        session.commit()

    print()
    for reason, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>4}  {reason}")
    retryable = counts.get(str(Reason.FETCH_FAILED), 0)
    print()
    if blocked:
        print(
            f"  {blocked} probe(s) BLOCKED (rate limit / bot check) — left unrecorded rather "
            "than guessed. Re-run later to determine those."
        )
    print(f"  {retryable} retryable; the rest cannot be fixed by re-fetching.")
    print("  Reasons are not permanent — re-run with --reprobe occasionally.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
