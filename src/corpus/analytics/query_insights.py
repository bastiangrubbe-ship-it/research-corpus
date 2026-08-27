"""Reading `query_log` — what this corpus is repeatedly asked and repeatedly fails.

The point of logging queries is not analytics for its own sake. It is that the corpus
cannot otherwise learn what it is missing: `corpus_coverage` grades a topic on demand
and the verdict evaporates with the response, so nobody could previously say "this was
asked fourteen times and graded thin every time." That sentence is a sourcing
decision; a single `thin` is not.

Two readings, deliberately separate:

* `sourcing_backlog` — where to point ingestion. This is where the real headroom is.
  Measured retrieval headroom on this corpus is +/-0.05 (docs/DECISIONS.md,
  2026-08-26); the gaps are whole topics, and no amount of reranking finds a document
  that was never ingested.
* `earning_its_keep` — the step-0 proxy. The build plan's kill criterion needs a
  web-search baseline that does not exist, so this substitutes the weaker but
  automatic question: on real queries, how often can this corpus answer at all?

**Both readings are biased and it matters.** They only see questions someone thought
to ask, which is shaped by what the corpus was already good at. A topic nobody asks
about because they learned it is useless here looks identical to a topic nobody cares
about. Keep the build plan's independent spec queries for that reason
(docs/BUILD_PLAN.md).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corpus.db.models import QueryLog

#: Grades that mean "this corpus could not answer that".
WEAK_GRADES = ("none", "thin")

#: A topic needs to fail this many times before it is a backlog item rather than a
#: one-off. One `thin` is noise -- a passing curiosity, or a badly worded query.
MIN_OCCURRENCES = 2


@dataclass(frozen=True, slots=True)
class BacklogItem:
    query_text: str
    times_asked: int
    times_weak: int
    worst_grade: str
    last_asked: dt.datetime
    #: Index completeness the last time this was asked. If this is well below 100%,
    #: the weak grade may be an artefact of an unfinished backfill rather than a real
    #: sourcing gap -- check before adding sources.
    indexed_documents: int | None
    total_documents: int | None

    @property
    def likely_index_artefact(self) -> bool:
        if not self.indexed_documents or not self.total_documents:
            return False
        return self.indexed_documents < self.total_documents * 0.95


@dataclass(frozen=True, slots=True)
class KeepReport:
    total_queries: int
    graded_queries: int
    answered_well: int
    weak: int
    since: dt.date | None

    @property
    def hit_rate(self) -> float | None:
        """Share of graded queries the corpus could actually answer. None when nothing
        has been graded -- rather than 0.0, which would read as "it always fails"."""
        if not self.graded_queries:
            return None
        return self.answered_well / self.graded_queries


def sourcing_backlog(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    since: dt.date | None = None,
    min_occurrences: int = MIN_OCCURRENCES,
    limit: int = 20,
) -> list[BacklogItem]:
    """Queries that repeatedly graded `none`/`thin`, most-asked first.

    Grouped on exact query text. That is deliberately literal: clustering
    near-duplicate phrasings would need embeddings and a threshold, and a threshold
    picked by eye would quietly merge distinct questions. Literal grouping
    under-counts rather than over-merges, which is the safer error for something that
    drives spending.
    """
    weak = func.count().filter(QueryLog.coverage_grade.in_(WEAK_GRADES))
    stmt = (
        select(
            QueryLog.query_text,
            func.count().label("times_asked"),
            weak.label("times_weak"),
            func.min(QueryLog.coverage_grade).label("worst_grade"),
            func.max(QueryLog.created_at).label("last_asked"),
            func.max(QueryLog.indexed_documents).label("indexed_documents"),
            func.max(QueryLog.total_documents).label("total_documents"),
        )
        .where(
            QueryLog.tenant_id == tenant_id,
            QueryLog.coverage_grade.isnot(None),
        )
        .group_by(QueryLog.query_text)
        .having(weak >= min_occurrences)
        .order_by(weak.desc(), func.max(QueryLog.created_at).desc())
        .limit(limit)
    )
    if since is not None:
        stmt = stmt.where(QueryLog.created_at >= since)

    return [
        BacklogItem(
            query_text=r.query_text,
            times_asked=r.times_asked,
            times_weak=r.times_weak,
            # min() over none/thin/partial/good is alphabetical, which happens to put
            # 'good' first -- so take the weakest present explicitly instead.
            worst_grade="none" if r.worst_grade == "none" else r.worst_grade,
            last_asked=r.last_asked,
            indexed_documents=r.indexed_documents,
            total_documents=r.total_documents,
        )
        for r in session.execute(stmt).all()
    ]


def earning_its_keep(
    session: Session, *, tenant_id: uuid.UUID, since: dt.date | None = None
) -> KeepReport:
    """How often the corpus could answer, over graded queries.

    Not a substitute for the build plan's kill criterion, which compares against a web
    baseline this does not have. It answers the narrower question the log can actually
    support: of the things you asked, how many did this corpus hold anything on.
    """
    stmt = select(
        func.count().label("total"),
        func.count().filter(QueryLog.coverage_grade.isnot(None)).label("graded"),
        func.count().filter(QueryLog.answered_well.is_(True)).label("well"),
        func.count().filter(QueryLog.coverage_grade.in_(WEAK_GRADES)).label("weak"),
        func.min(QueryLog.created_at).label("since"),
    ).where(QueryLog.tenant_id == tenant_id)
    if since is not None:
        stmt = stmt.where(QueryLog.created_at >= since)
    row = session.execute(stmt).one()
    return KeepReport(
        total_queries=row.total,
        graded_queries=row.graded,
        answered_well=row.well,
        weak=row.weak,
        since=row.since.date() if row.since else None,
    )
