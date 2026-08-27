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
) -> StageReport:
    params = {"t": tenant_id}
    done = session.execute(text(done_sql), params).scalar() or 0
    total = session.execute(text(total_sql), params).scalar() or 0
    if total == 0:
        status: Status = "unknown"
    elif done == 0:
        status = "empty"
    elif done / total >= COMPLETE_ENOUGH:
        status = "ok"
    else:
        status = "partial"
    return StageReport(
        stage=stage, done=done, total=total, status=status, impact=impact, remedy=remedy
    )


_DOCUMENTS = "select count(*) from document where tenant_id = :t"


def diagnose(session: Session, *, tenant_id: uuid.UUID) -> list[StageReport]:
    """Every pipeline stage, in the order the data flows through them."""
    return [
        _stage(
            session,
            tenant_id=tenant_id,
            stage="transcripts",
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
            stage="restoration",
            done_sql="""select count(*) from transcript_version p
                        where p.tenant_id = :t and p.provider <> 'restored'
                          and exists (select 1 from transcript_version c
                                      where c.derived_from_id = p.id)""",
            total_sql="""select count(*) from transcript_version
                         where tenant_id = :t and provider <> 'restored'""",
            impact="unpunctuated ASR gives NLTK no sentence boundaries, so summaries "
            "and synthesis quotes come out as unreadable blobs",
            remedy="flows/restore_transcripts.py",
        ),
        _stage(
            session,
            tenant_id=tenant_id,
            stage="summaries",
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


def format_report(reports: list[StageReport]) -> str:
    """Human-readable, impact shown only where it currently applies."""
    symbol = {"ok": "OK  ", "partial": "PART", "empty": "MISS", "unknown": "??  "}
    lines = []
    for r in reports:
        share = f"{r.share:6.1%}" if r.share is not None else "     -"
        lines.append(
            f"  [{symbol[r.status]}] {r.stage:<20} {r.done:>6} / {r.total:<6} {share}"
        )
        if r.status in ("partial", "empty"):
            lines.append(f"         {r.missing} missing — {r.impact}")
            lines.append(f"         fix: {r.remedy}")
    incomplete = [r for r in reports if r.status in ("partial", "empty")]
    lines.append("")
    if incomplete:
        lines.append(
            f"  {len(incomplete)} stage(s) incomplete. A partially-built stage does not "
            "look broken — it looks decisive."
        )
    else:
        lines.append("  All stages complete.")
    return "\n".join(lines)
