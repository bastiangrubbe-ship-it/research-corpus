"""Does this corpus actually cover a given topic, and if not, what would fix it.

The honest problem this solves: search always returns *something*. Ten ranked
results look identical whether they're ten strong matches or the ten least-bad
documents in a corpus that has nothing on the subject. This module answers the
question a ranked list can't — is there really anything here, and is it broad enough
to be worth trusting.

Two measurement decisions, both taken from this project's own eval work rather than
invented here (docs/EVAL_RELEVANCE_GATE.md, docs/DECISIONS.md 2026-08-25):

1. **Top score gates existence, not per-row score.** Measured across three real
   queries, the best reranker score was 0.82 (strong match), 0.068 (weaker but
   real), and 0.0006 (nothing in the corpus) — a ~100x separation, clean enough to
   threshold on. But *within* a query that has genuine matches, real hits scored as
   low as 0.003, so a per-row quality bar would silently discard true positives.
   Hence: top score decides "is anything here", and breadth is measured over the
   top-ranked set rather than over "documents above a quality bar", which is not a
   set this corpus can reliably identify.

2. **Breadth is structural, not scored.** How many independent sources, over how
   long a period, across which domains — the same reasoning
   `corpus.analytics.saturation` already applies to entities: one channel repeating
   itself is not coverage, fifty creators independently converging is.

Suggestions are derived only from facts computed here (absent domains, single-source
concentration, staleness, sources with nothing ingested, well-covered adjacent
entities). Nothing is inferred about sources that don't exist in this corpus — a
suggestion to "go find a podcast about X" would be a guess wearing a data costume.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corpus.db.enums import Domain, SourceStatus
from corpus.db.models import Document, Entity, EntityMention, Source
from corpus.retrieval.search import hybrid_search

#: Below this top reranker score, nothing in the corpus meaningfully addresses the
#: query. Measured: a query with no real match topped out at 0.0006; the weakest
#: query that *did* have a real match topped out at 0.068. 0.01 sits in that gap and
#: is the same default `corpus.enrich.relevance_gate` already gates on.
NOTHING_THERE = 0.01

#: A topic covered by fewer than this many distinct sources is one perspective, not
#: a corpus. Mirrors `saturation.most_saturated_entities`'s own `min_sources=3`.
MIN_SOURCES_FOR_BREADTH = 3

#: The corpus's value proposition is comparative over time (see the build plan), so
#: coverage confined to a narrow window can't support that even if it's voluminous.
MIN_SPAN_DAYS = 90

#: Newest match older than this and the topic has gone quiet — worth flagging, since
#: the corpus itself is still being fed.
STALE_AFTER_DAYS = 120


@dataclass(frozen=True, slots=True)
class CoverageReport:
    query: str
    grade: str  # none | thin | partial | good
    headline: str
    best_score: float
    #: How much of the corpus the semantic lane can actually see. Reported on every
    #: assessment because a partially-built index doesn't look broken from the
    #: outside — it looks decisive, returning confident rankings over whatever
    #: fraction happens to be embedded. A "no coverage" verdict computed against a
    #: 4%-complete index is meaningless, and this is the number that says so.
    #: See docs/DECISIONS.md, 2026-08-25 (the correction entry).
    indexed_documents: int
    total_documents: int
    n_documents: int
    n_sources: int
    date_earliest: dt.date | None
    date_latest: dt.date | None
    span_days: int | None
    top_sources: list[tuple[str, int]] = field(default_factory=list)
    domain_breakdown: dict[str, int] = field(default_factory=dict)
    absent_domains: list[str] = field(default_factory=list)
    authority_breakdown: dict[str, int] = field(default_factory=dict)
    related_entities: list[tuple[str, str, int]] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)


def _index_completeness(session: Session, *, tenant_id: uuid.UUID) -> tuple[int, int]:
    """(documents the dense lane can see, documents that exist).

    The first number counts `document_summary` rows with an embedding — nothing else
    is searchable semantically or rerankable, regardless of how many documents were
    ingested.
    """
    from corpus.db.enums import SummaryMethod
    from corpus.db.models import DocumentSummary

    indexed = session.execute(
        select(func.count())
        .select_from(DocumentSummary)
        .where(
            DocumentSummary.tenant_id == tenant_id,
            DocumentSummary.method == SummaryMethod.EXTRACTIVE_TEXTRANK,
            DocumentSummary.embedding.is_not(None),
        )
    ).scalar_one()
    total = session.execute(
        select(func.count()).select_from(Document).where(Document.tenant_id == tenant_id)
    ).scalar_one()
    return indexed, total


def _empty_sources(session: Session, *, tenant_id: uuid.UUID) -> list[str]:
    """Active sources with nothing ingested — real, addressable gaps rather than a
    guess about what else might exist."""
    rows = (
        session.execute(
            select(Source.title)
            .outerjoin(Document, Document.source_id == Source.id)
            .where(Source.tenant_id == tenant_id, Source.status == SourceStatus.ACTIVE)
            .group_by(Source.id)
            .having(func.count(Document.id) == 0)
        )
        .scalars()
        .all()
    )
    return [t for t in rows if t]


def _build_suggestions(
    *,
    grade: str,
    n_sources: int,
    top_sources: list[tuple[str, int]],
    n_documents: int,
    absent_domains: list[str],
    date_latest: dt.date | None,
    span_days: int | None,
    related_entities: list[tuple[str, str, int]],
    empty_sources: list[str],
    as_of: dt.date,
    indexed_documents: int = 0,
    total_documents: int = 0,
) -> list[str]:
    out: list[str] = []

    # Stated first and unconditionally when true: with a partial index, every other
    # line below is a claim about a fraction of the corpus rather than the corpus.
    if total_documents and indexed_documents < total_documents * 0.95:
        pct = 100 * indexed_documents / total_documents
        out.append(
            f"Only {indexed_documents:,} of {total_documents:,} documents ({pct:.0f}%) are "
            "semantically searchable — the rest have no summary embedding, so this verdict "
            "is about that subset, not the whole corpus. Run "
            "`uv run python flows/backfill_summaries.py` (local, free) before relying on it."
        )

    if grade == "none":
        out.append(
            "Nothing in the corpus addresses this. The only fix is new sources — no "
            "amount of re-querying surfaces documents that were never ingested."
        )
        if related_entities:
            names = _dedupe_names(related_entities, 5)
            out.append(
                f"The nearest material the corpus does hold is about {names}. If that is not "
                "what you meant, this is a sourcing gap rather than a phrasing problem."
            )
        if empty_sources:
            out.append(
                f"{len(empty_sources)} active source(s) have nothing ingested yet "
                f"({', '.join(empty_sources[:3])}) — running ingestion for them costs "
                "nothing extra and may already close part of this gap."
            )
        return out

    if n_sources < MIN_SOURCES_FOR_BREADTH:
        who = top_sources[0][0] if top_sources else "one source"
        out.append(
            f"Only {n_sources} source(s) cover this — {who} carries most of it. That is "
            "one perspective, not a consensus; add sources before treating any pattern "
            "here as a trend."
        )
    elif top_sources and top_sources[0][1] > n_documents / 2:
        out.append(
            f"{top_sources[0][0]} alone accounts for {top_sources[0][1]} of {n_documents} "
            "matches. Breadth looks adequate but is concentrated — check whether the "
            "others are really saying the same thing or just echoing it."
        )

    if span_days is not None and span_days < MIN_SPAN_DAYS:
        out.append(
            f"All matches fall inside {span_days} days. This corpus's value is comparative "
            "over time, and a window this narrow can't support a before/after claim."
        )

    if date_latest is not None:
        age = (as_of - date_latest).days
        if age > STALE_AFTER_DAYS:
            out.append(
                f"Newest match is {age} days old ({date_latest}), while the corpus is still "
                "being fed generally — either the topic has gone quiet or the sources "
                "covering it have."
            )

    if absent_domains:
        out.append(
            "No coverage at all from these domains: "
            f"{', '.join(absent_domains)}. If the topic should appear there, that is a "
            "sourcing gap rather than a retrieval one."
        )

    if related_entities:
        out.append(
            "Frequently discussed alongside these, which may be sharper queries: "
            f"{_dedupe_names(related_entities, 5)}."
        )

    return out


def _dedupe_names(entities: list[tuple[str, str, int]], limit: int) -> str:
    """Collapse by name for prose. The same name legitimately exists under several
    kinds ("Claude" is both a product and a vendor here), which is meaningful in the
    structured breakdown but reads as a bug in a sentence listing search terms."""
    seen: list[str] = []
    for name, _kind, _count in entities:
        if name not in seen:
            seen.append(name)
        if len(seen) == limit:
            break
    return ", ".join(seen)


def assess_coverage(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    query: str,
    domain: Domain | None = None,
    top_k: int = 20,
    candidate_pool: int = 40,
    as_of: dt.date | None = None,
) -> CoverageReport:
    """How well this corpus covers `query`, and what would improve it.

    `top_k` is the set breadth is measured over — deliberately *not* "every document
    above a quality threshold" (see the module docstring for why that set can't be
    identified reliably here).
    """
    as_of = as_of or dt.date.today()
    hits = hybrid_search(
        session,
        tenant_id=tenant_id,
        query_text=query,
        domain=domain,
        top_k=top_k,
        candidate_pool=candidate_pool,
        rerank=True,
    )

    empty_sources = _empty_sources(session, tenant_id=tenant_id)
    indexed_documents, total_documents = _index_completeness(session, tenant_id=tenant_id)

    if not hits or hits[0][1] < NOTHING_THERE:
        # Deliberately still derive adjacency from the (low-scoring) nearest
        # documents. They are not answers to the query — that's what the grade
        # says — but "here is the closest thing this corpus holds" is the most
        # useful thing available when the honest answer is "nothing". Reporting
        # n_documents=0 keeps the two claims separate: no coverage, some neighbours.
        nearest_ids = [doc_id for doc_id, _ in hits[:10]]
        return CoverageReport(
            query=query,
            grade="none",
            headline="No real coverage — the corpus has nothing on this.",
            best_score=hits[0][1] if hits else 0.0,
            indexed_documents=indexed_documents,
            total_documents=total_documents,
            n_documents=0,
            n_sources=0,
            date_earliest=None,
            date_latest=None,
            span_days=None,
            related_entities=_related_entities(
                session, tenant_id=tenant_id, document_ids=nearest_ids
            ),
            suggestions=_build_suggestions(
                grade="none",
                n_sources=0,
                top_sources=[],
                n_documents=0,
                absent_domains=[],
                date_latest=None,
                span_days=None,
                related_entities=_related_entities(
                    session, tenant_id=tenant_id, document_ids=nearest_ids
                ),
                empty_sources=empty_sources,
                as_of=as_of,
                indexed_documents=indexed_documents,
                total_documents=total_documents,
            ),
        )

    document_ids = [doc_id for doc_id, _ in hits]
    rows = session.execute(
        select(
            Document.id,
            Document.published_at,
            Source.title,
            Source.domain,
            Source.authority_tier,
        )
        .join(Source, Source.id == Document.source_id)
        .where(Document.id.in_(document_ids))
    ).all()

    source_counts: dict[str, int] = {}
    domain_counts: dict[str, int] = {}
    authority_counts: dict[str, int] = {}
    dates: list[dt.date] = []
    for _id, published_at, source_title, dom, tier in rows:
        title = source_title or "(unnamed source)"
        source_counts[title] = source_counts.get(title, 0) + 1
        domain_counts[dom.value] = domain_counts.get(dom.value, 0) + 1
        authority_counts[tier.value] = authority_counts.get(tier.value, 0) + 1
        if published_at is not None:
            dates.append(published_at.date())

    top_sources = sorted(source_counts.items(), key=lambda kv: kv[1], reverse=True)
    earliest = min(dates) if dates else None
    latest = max(dates) if dates else None
    span = (latest - earliest).days if earliest and latest else None

    # Domains that exist in this corpus but contribute nothing to these matches.
    present_domains = set(
        session.execute(
            select(Source.domain)
            .where(Source.tenant_id == tenant_id, Source.status == SourceStatus.ACTIVE)
            .distinct()
        )
        .scalars()
        .all()
    )
    absent = sorted(d.value for d in present_domains if d.value not in domain_counts)

    related = _related_entities(session, tenant_id=tenant_id, document_ids=document_ids)

    n_sources = len(source_counts)
    n_documents = len(rows)
    if n_documents < 3 or n_sources < 2:
        grade = "thin"
        headline = "Thin — a couple of documents, not enough to generalise from."
    elif (
        n_sources < MIN_SOURCES_FOR_BREADTH
        or (span is not None and span < MIN_SPAN_DAYS)
        or top_sources[0][1] > n_documents / 2
    ):
        grade = "partial"
        headline = "Partial — real material, but narrow in sources or in time."
    else:
        grade = "good"
        headline = f"Good — {n_documents} matches across {n_sources} independent sources."

    return CoverageReport(
        query=query,
        grade=grade,
        headline=headline,
        best_score=hits[0][1],
        indexed_documents=indexed_documents,
        total_documents=total_documents,
        n_documents=n_documents,
        n_sources=n_sources,
        date_earliest=earliest,
        date_latest=latest,
        span_days=span,
        top_sources=top_sources[:5],
        domain_breakdown=domain_counts,
        absent_domains=absent,
        authority_breakdown=authority_counts,
        related_entities=related,
        suggestions=_build_suggestions(
            grade=grade,
            n_sources=n_sources,
            top_sources=top_sources,
            n_documents=n_documents,
            absent_domains=absent,
            date_latest=latest,
            span_days=span,
            related_entities=related,
            empty_sources=empty_sources,
            as_of=as_of,
            indexed_documents=indexed_documents,
            total_documents=total_documents,
        ),
    )


def _related_entities(
    session: Session, *, tenant_id: uuid.UUID, document_ids: list[uuid.UUID], limit: int = 8
) -> list[tuple[str, str, int]]:
    """Entities most mentioned across the matching documents — grounded adjacency,
    used to suggest sharper queries. Empty when there are no matches to draw on:
    suggesting related topics for a query with no results would mean inventing them.
    """
    if not document_ids:
        return []
    rows = session.execute(
        select(Entity.canonical_name, Entity.kind, func.count().label("n"))
        .join(EntityMention, EntityMention.entity_id == Entity.id)
        .where(
            EntityMention.tenant_id == tenant_id,
            EntityMention.document_id.in_(document_ids),
        )
        .group_by(Entity.id)
        .order_by(func.count().desc())
        .limit(limit)
    ).all()
    return [(name, kind.value, n) for name, kind, n in rows]
