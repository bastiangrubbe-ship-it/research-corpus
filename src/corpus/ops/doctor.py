"""Pipeline completeness check — is every stage actually built, over the whole corpus?

This project's recurring failure is not that a stage breaks. It is that a stage runs
over *part* of the corpus and everything downstream keeps working, confidently, over
whatever fraction happens to exist. Three separate bugs have come from it: dense
search running over 3.9% of the corpus while reporting "no coverage" for a topic with
26 documents; a summary backfill only ever run with test limits; and a restoration
pass that would have zeroed every chunk timestamp. None of them looked broken. All of
them looked decisive.

So each stage here reports two things beyond a count: whether it is complete, and
**what silently degrades while it is not**. The second is the point. A count tells you
3,272 of 3,349 documents have summaries; only the impact line tells you the other 77
are invisible to semantic search and to the reranker no matter how they are queried.

Read-only. Diagnoses, never repairs — the fix for each stage is a different flow with
different costs, and choosing between them is not a decision to make automatically.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

Status = Literal["ok", "partial", "empty", "unknown"]

#: Below this share, a stage is reported as `partial` rather than `ok`. Not 1.0: a
#: handful of documents legitimately have no transcript (empty captions, removed
#: videos), and a check that cries wolf on those gets ignored, which defeats it.
COMPLETE_ENOUGH = 0.99

#: A nightly job that has not succeeded in this long is presumed broken. Generous
#: relative to a daily cadence so a single missed night (laptop asleep at 03:00, which
#: is normal for this machine) is not reported as a fault.
NIGHTLY_STALE_AFTER_HOURS = 36


#: Restoration was dropped on 2026-08-27 and is deliberately not a stage here. It is
#: not "incomplete" — it is not part of the pipeline. Leaving it would have the check
#: permanently report a missing stage, which is precisely how a health check trains
#: people to ignore it. See docs/DECISIONS.md.
#:
#: Reasons a document can never gain a transcript, so can never gain anything derived
#: from one. `fetch_failed` is deliberately absent: it is retryable, so those documents
#: stay in the denominator and keep the stage honestly incomplete.
BLOCKED_REASONS = ("members_only", "no_captions", "removed")


@dataclass(frozen=True, slots=True)
class StageReport:
    stage: str
    done: int
    total: int
    status: Status
    #: What breaks, silently, while this stage is incomplete.
    impact: str
    #: Which flow fixes it.
    remedy: str
    #: Documents removed from `total` because they provably cannot reach this stage.
    #: Always reported, never merely subtracted — a denominator that quietly shrinks
    #: is how a check starts lying, and this file exists because of exactly that class
    #: of error.
    excluded: int = 0
    excluded_note: str = ""

    @property
    def share(self) -> float | None:
        if not self.total:
            return None
        return self.done / self.total

    @property
    def missing(self) -> int:
        return max(self.total - self.done, 0)


def _stage(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    stage: str,
    done_sql: str,
    total_sql: str,
    impact: str,
    remedy: str,
    excluded_sql: str | None = None,
    excluded_note: str = "",
) -> StageReport:
    params = {"t": tenant_id}
    done = session.execute(text(done_sql), params).scalar() or 0
    total = session.execute(text(total_sql), params).scalar() or 0
    excluded = 0
    if excluded_sql is not None:
        excluded = session.execute(text(excluded_sql), params).scalar() or 0
        total = max(total - excluded, 0)
    if total == 0:
        status: Status = "unknown"
    elif done == 0:
        status = "empty"
    elif done / total >= COMPLETE_ENOUGH:
        status = "ok"
    else:
        status = "partial"
    return StageReport(
        stage=stage,
        done=done,
        total=total,
        status=status,
        impact=impact,
        remedy=remedy,
        excluded=excluded,
        excluded_note=excluded_note,
    )


_DOCUMENTS = "select count(*) from document where tenant_id = :t"

#: Documents that provably cannot gain a transcript, and therefore cannot gain a
#: summary, chunks, or entity mentions either. Established by
#: flows/probe_missing_transcripts.py, never assumed.
_BLOCKED = (
    "select count(*) from document where tenant_id = :t "
    "and transcript_unavailable_reason in ('members_only', 'no_captions', 'removed')"
)
_BLOCKED_NOTE = (
    "documents that cannot have a transcript (members-only, no captions, or removed) "
    "and so cannot reach this stage — probed, not assumed"
)


def diagnose(session: Session, *, tenant_id: uuid.UUID) -> list[StageReport]:
    """Every pipeline stage, in the order the data flows through them."""
    return [
        _stage(
            session,
            tenant_id=tenant_id,
            stage="transcripts",
            excluded_sql=_BLOCKED,
            excluded_note=_BLOCKED_NOTE,
            done_sql="""select count(distinct d.id) from document d
                        join transcript_version tv on tv.document_id = d.id
                        where d.tenant_id = :t""",
            total_sql=_DOCUMENTS,
            impact="a document with no transcript is unreachable by every lane",
            remedy="flows/ingest_youtube.py",
        ),
        _stage(
            session,
            tenant_id=tenant_id,
            stage="summaries",
            excluded_sql=_BLOCKED,
            excluded_note=_BLOCKED_NOTE,
            done_sql="""select count(distinct document_id) from document_summary
                        where tenant_id = :t""",
            total_sql=_DOCUMENTS,
            impact="INVISIBLE to the dense lane and to the cross-encoder reranker — "
            "findable by title only, however it is queried",
            remedy="flows/backfill_summaries.py",
        ),
        _stage(
            session,
            tenant_id=tenant_id,
            stage="summary embeddings",
            done_sql="""select count(*) from document_summary
                        where tenant_id = :t and embedding is not null""",
            total_sql="select count(*) from document_summary where tenant_id = :t",
            impact="a summary without a vector is text the dense lane cannot search",
            remedy="flows/backfill_summaries.py",
        ),
        _stage(
            session,
            tenant_id=tenant_id,
            stage="chunks",
            excluded_sql=_BLOCKED,
            excluded_note=_BLOCKED_NOTE,
            done_sql="""select count(distinct document_id) from chunk where tenant_id = :t""",
            total_sql=_DOCUMENTS,
            impact="the chunk-dense lane cannot see inside the document, so a topic "
            "discussed forty minutes in is unreachable semantically",
            remedy="flows/backfill_chunks.py",
        ),
        _stage(
            session,
            tenant_id=tenant_id,
            stage="chunk embeddings",
            done_sql="""select count(*) from chunk_embedding ce
                        join chunk c on c.id = ce.chunk_id where c.tenant_id = :t""",
            total_sql="select count(*) from chunk where tenant_id = :t",
            impact="an unembedded chunk is dead weight — stored, never searched",
            remedy="flows/backfill_chunks.py",
        ),
        _stage(
            session,
            tenant_id=tenant_id,
            stage="entity extraction",
            excluded_sql=_BLOCKED,
            excluded_note=_BLOCKED_NOTE,
            done_sql="""select count(distinct document_id) from entity_extraction_run
                        where tenant_id = :t""",
            total_sql=_DOCUMENTS,
            impact="every analytics count (velocity, emergence, saturation, drift, "
            "diffusion) is computed over this fraction and reads as the whole picture",
            remedy="flows/nightly_entities.py",
        ),
        _stage(
            session,
            tenant_id=tenant_id,
            stage="speaker attribution",
            done_sql="select count(distinct document_id) from speaker where tenant_id = :t",
            total_sql=_DOCUMENTS,
            impact="no attribution at all; note a high 'unknown' rate within this is "
            "the designed tier-1 outcome, not an incompleteness",
            remedy="EnrichmentPanel, or corpus.enrich.speakers.attribute_speakers",
        ),
    ]


def check_heartbeats(
    session: Session, *, tenant_id: uuid.UUID, max_age_hours: float = NIGHTLY_STALE_AFTER_HOURS
) -> list[str]:
    """Warnings about scheduled work that has stopped succeeding.

    Separate from the stage list because it answers a different question. The stages
    ask "is the corpus fully built"; this asks "is anything still building it". A
    corpus can be 100% complete and completely dead, and those look identical from the
    counts alone — which is how the nightly ingest failed four nights running while
    every stage read green.
    """
    from corpus.ops.heartbeat import stale_flows

    warnings = []
    stale = stale_flows(session, tenant_id=tenant_id, max_age_hours=max_age_hours)
    for name, last_success, status in stale:
        if last_success is None:
            warnings.append(f"{name}: has never recorded a successful run (status={status})")
        else:
            age = (dt.datetime.now(dt.UTC) - last_success).total_seconds() / 3600
            warnings.append(
                f"{name}: last succeeded {age:.0f}h ago (status={status}) — "
                f"expected within {max_age_hours:.0f}h"
            )
    # No heartbeat row at all is its own failure: the flow has never run since
    # heartbeats were wired up, which is indistinguishable from never running.
    known = {n for n, _, _ in stale}
    from corpus.db.models import Heartbeat

    existing = {
        r.flow_name
        for r in session.execute(
            Heartbeat.__table__.select().where(Heartbeat.tenant_id == tenant_id)
        ).all()
    }
    if "nightly" not in existing and "nightly" not in known:
        warnings.append(
            "nightly: no heartbeat recorded at all — the scheduled job has not "
            "completed since heartbeats were added"
        )
    return warnings


def format_report(reports: list[StageReport], warnings: list[str] | None = None) -> str:
    """Human-readable, impact shown only where it currently applies."""
    symbol = {"ok": "OK  ", "partial": "PART", "empty": "MISS", "unknown": "??  "}
    lines = []
    for r in reports:
        share = f"{r.share:6.1%}" if r.share is not None else "     -"
        lines.append(
            f"  [{symbol[r.status]}] {r.stage:<20} {r.done:>6} / {r.total:<6} {share}"
        )
        if r.excluded:
            lines.append(f"         ({r.excluded} excluded — {r.excluded_note})")
        if r.status in ("partial", "empty"):
            lines.append(f"         {r.missing} missing — {r.impact}")
            lines.append(f"         fix: {r.remedy}")
    if warnings:
        lines.append("")
        lines.append("  SCHEDULED WORK:")
        for w in warnings:
            lines.append(f"    ! {w}")

    incomplete = [r for r in reports if r.status in ("partial", "empty")]
    lines.append("")
    if incomplete:
        lines.append(
            f"  {len(incomplete)} stage(s) incomplete. A partially-built stage does not "
            "look broken — it looks decisive."
        )
    elif warnings:
        # Never "all complete" while scheduled work is stalled: a corpus can be fully
        # built and entirely dead, and the counts alone cannot tell those apart.
        lines.append(
            "  All stages complete, but scheduled work is not running — the corpus is "
            "built and no longer growing."
        )
    else:
        lines.append("  All stages complete.")
    return "\n".join(lines)
