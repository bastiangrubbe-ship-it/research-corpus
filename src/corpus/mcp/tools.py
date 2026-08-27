"""MCP tool implementations. Mirror the corpus's capabilities, not its retrieval
lanes (build plan, step 9): `corpus_search` is the hybrid retrieval pipeline end to
end, `corpus_analytics` is the whole step-5 analytics surface behind one selector
argument, `corpus_provenance` answers "where did this come from and how sure are we."

`corpus_synthesize` is the plan's fourth tool — a SQL/lexical-filtered *set* read in
full and reduced to prose with citations. It is the only tool here that spends real
quota per call (one LLM call per matched document), and the only one whose argument
choices carry a cost the caller should see first; `dry_run` exists for exactly that.

Tenant resolution never happens here. Every function takes `tenant_id` as a plain
argument supplied by `server.py`, which resolves it once from server config at
startup — never from a tool argument, never from anything a model says. That
boundary is drawn in `server.py`, not repeated per function, so there is exactly one
place it could be gotten wrong instead of one per tool.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from typing import Literal

from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import select
from sqlalchemy.orm import Session

from corpus.analytics import diffusion, drift, emergence, saturation, velocity
from corpus.db.enums import Domain
from corpus.db.models import Document, Entity, EntityMention, Source, TranscriptVersion
from corpus.db.query_log import record_query
from corpus.retrieval.search import hybrid_search


def _parse_domain(domain: str | None) -> Domain | None:
    if domain is None:
        return None
    try:
        return Domain(domain)
    except ValueError as exc:
        valid = ", ".join(d.value for d in Domain)
        raise ToolError(f"unknown domain {domain!r}; valid values: {valid}") from exc


def _resolve_entity(
    session: Session, *, tenant_id: uuid.UUID, name: str, kind: str | None = None
) -> Entity:
    """Case-insensitive canonical_name match, falling back to a case-insensitive
    alias match. Raises with the near-miss candidates rather than returning None —
    an MCP tool caller (an LLM) can act on "did you mean X" text, it can't act on a
    bare null.

    `canonical_name` is unique per `kind`, not per tenant alone (see `Entity`'s table
    constraint) — the same name legitimately exists twice under different kinds in
    this corpus ("Claude" as both `product` and `vendor`, confirmed directly against
    real data while building this). An exact-name match with no `kind` given and
    more than one hit picks the entity with the most mentions — the "what people
    mean by default" reading — rather than erroring on genuinely ambiguous input;
    pass `kind` to disambiguate explicitly instead.
    """
    from sqlalchemy import func

    exact_stmt = select(Entity).where(
        Entity.tenant_id == tenant_id, Entity.canonical_name.ilike(name)
    )
    if kind is not None:
        exact_stmt = exact_stmt.where(Entity.kind == kind)
    exact_matches = session.execute(exact_stmt).scalars().all()

    if len(exact_matches) == 1:
        return exact_matches[0]
    if len(exact_matches) > 1:
        counts = dict(
            session.execute(
                select(EntityMention.entity_id, func.count())
                .where(EntityMention.entity_id.in_([e.id for e in exact_matches]))
                .group_by(EntityMention.entity_id)
            ).all()
        )
        return max(exact_matches, key=lambda e: counts.get(e.id, 0))

    candidates = (
        session.execute(
            select(Entity)
            .where(
                Entity.tenant_id == tenant_id,
                (Entity.canonical_name.ilike(f"%{name}%"))
                | (func.array_to_string(Entity.aliases, ",").ilike(f"%{name}%")),
            )
            .limit(5)
        )
        .scalars()
        .all()
    )
    if len(candidates) == 1:
        return candidates[0]
    names = [c.canonical_name for c in candidates]
    raise ToolError(
        f"no exact entity named {name!r}"
        + (f" — did you mean one of: {names}?" if names else " and no close matches either")
    )


def corpus_search(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    query: str,
    domain: str | None = None,
    top_k: int = 10,
    candidate_pool: int = 50,
    rerank: bool = True,
    surface: str = "mcp",
) -> list[dict]:
    """Hybrid search (lexical + dense + RRF fusion + cross-encoder rerank) over the
    corpus. Returns ranked results with enough metadata to cite or follow up on —
    `corpus_provenance` gives the fuller picture for any document_id returned here.

    `candidate_pool` is the real latency knob, and it is steep: reranking is a
    cross-encoder pass over every candidate, so cost scales with the pool, not with
    `top_k`. Measured warm, re-measured 2026-08-27: pool=10 ~8s, pool=20 ~14s,
    pool=50 ~36s. The earlier figures here (~3s/~6s/~37s) were taken before the
    chunk-dense lane existed and before the corpus reached 3,349 documents — the
    small pools have roughly doubled since, so re-measure rather than trusting these
    after any change to lanes or corpus size. `rerank=False` skips the cross-encoder
    entirely and returns RRF-fused order.

    The default stays 50 — quality-first, which is right for an agent that can wait.
    Interactive callers (the dashboard) pass something smaller deliberately, trading
    recall for responsiveness, rather than having that tradeoff made for them here.
    """
    domain_enum = _parse_domain(domain)
    started = time.monotonic()
    hits = hybrid_search(
        session,
        tenant_id=tenant_id,
        query_text=query,
        domain=domain_enum,
        top_k=top_k,
        candidate_pool=candidate_pool,
        rerank=rerank,
    )
    record_query(
        session,
        tenant_id=tenant_id,
        tool="search",
        surface=surface,
        query_text=query,
        domain=domain,
        result_count=len(hits),
        top_document_ids=[doc_id for doc_id, _ in hits[:10]],
        latency_ms=int((time.monotonic() - started) * 1000),
    )
    if not hits:
        return []

    doc_ids = [doc_id for doc_id, _score in hits]
    docs = {
        row.id: row
        for row in session.execute(select(Document).where(Document.id.in_(doc_ids))).scalars().all()
    }
    return [
        {
            "document_id": str(doc_id),
            "title": docs[doc_id].title if doc_id in docs else None,
            "url": docs[doc_id].url if doc_id in docs else None,
            "published_at": (
                docs[doc_id].published_at.isoformat()
                if doc_id in docs and docs[doc_id].published_at
                else None
            ),
            "score": score,
        }
        for doc_id, score in hits
        if doc_id in docs
    ]


AnalysisType = Literal[
    "rising_entities",
    "emerging_entities",
    "saturated_entities",
    "co_occurrence_drift",
    "diffusion_timeline",
]


def corpus_analytics(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    analysis: AnalysisType,
    domain: str | None = None,
    entity_name: str | None = None,
    entity_kind: str | None = None,
    as_of: str | None = None,
) -> dict:
    """One entry point for all of step 5's analytics. `analysis` selects which
    question is being asked — the plan's own framing is that these are one
    capability with several angles, not five separate tools to remember.

    `domain` is required by every analysis except `diffusion_timeline` (diffusion is
    explicitly cross-domain by design, see corpus.analytics.diffusion) — pass it
    explicitly, `None` included, matching corpus.analytics's own "no silent default"
    convention. `entity_name` is required for `co_occurrence_drift` and
    `diffusion_timeline`, resolved case-insensitively against canonical names and
    aliases. The same name can exist under more than one `kind` (e.g. "Claude" as
    both a product and a vendor); an ambiguous name defaults to whichever reading has
    the most mentions — pass `entity_kind` (vendor/person/technique/regulation/
    product/organization) to pick the other one explicitly instead.
    """
    as_of_date = dt.date.fromisoformat(as_of) if as_of else dt.date.today()
    domain_enum = _parse_domain(domain)

    if analysis == "rising_entities":
        results = velocity.top_rising_entities(
            session, tenant_id=tenant_id, domain=domain_enum, as_of=as_of_date
        )
        return {
            "analysis": analysis,
            "results": [
                {
                    "canonical_name": r.canonical_name,
                    "kind": r.kind,
                    "recent_count": r.recent_count,
                    "prior_count": r.prior_count,
                    "pct_change": r.pct_change,
                }
                for r in results
            ],
        }

    if analysis == "emerging_entities":
        since = as_of_date - dt.timedelta(days=180)
        # Capped like its two ranked siblings (both default limit=20). Uncapped,
        # a six-month window returns ~3,200 rows on this corpus — a context-window
        # flood for an agent and 3,200 DOM nodes for the dashboard, for no gain
        # over the most-mentioned head of the list.
        results = emergence.newly_emerged_entities(
            session, tenant_id=tenant_id, domain=domain_enum, since=since, limit=20
        )
        return {
            "analysis": analysis,
            "since": since.isoformat(),
            "results": [
                {
                    "canonical_name": r.canonical_name,
                    "kind": r.kind,
                    "first_mention_date": r.first_mention_date.isoformat(),
                    "mention_count_since": r.mention_count_since,
                }
                for r in results
            ],
        }

    if analysis == "saturated_entities":
        results = saturation.most_saturated_entities(
            session, tenant_id=tenant_id, domain=domain_enum
        )
        return {
            "analysis": analysis,
            "results": [
                {
                    "canonical_name": r.canonical_name,
                    "kind": r.kind,
                    "distinct_sources": r.distinct_sources,
                    "total_mentions": r.total_mentions,
                    "mentions_per_source": r.mentions_per_source,
                }
                for r in results
            ],
        }

    if analysis == "co_occurrence_drift":
        if not entity_name:
            raise ToolError("co_occurrence_drift requires entity_name")
        entity = _resolve_entity(session, tenant_id=tenant_id, name=entity_name, kind=entity_kind)
        periods = drift.co_occurring_kinds_by_period(
            session, tenant_id=tenant_id, entity_id=entity.id, domain=domain_enum, as_of=as_of_date
        )
        return {
            "analysis": analysis,
            "entity": entity.canonical_name,
            "periods": [
                {"start": start.isoformat(), "end": end.isoformat(), "co_occurring_kinds": kinds}
                for (start, end), kinds in periods.items()
            ],
        }

    if analysis == "diffusion_timeline":
        if not entity_name:
            raise ToolError("diffusion_timeline requires entity_name")
        entity = _resolve_entity(session, tenant_id=tenant_id, name=entity_name, kind=entity_kind)
        timeline = diffusion.diffusion_timeline(session, tenant_id=tenant_id, entity_id=entity.id)
        return {
            "analysis": analysis,
            "entity": entity.canonical_name,
            "timeline": [
                {
                    "source_title": row.source_title,
                    "first_mention_date": row.first_mention_date.isoformat(),
                }
                for row in timeline
            ],
        }

    raise ToolError(f"unknown analysis type {analysis!r}")


def corpus_coverage(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    query: str,
    domain: str | None = None,
    surface: str = "mcp",
) -> dict:
    """How well the corpus covers a topic, and what would improve it.

    Worth calling before trusting a `corpus_search` result set as evidence of
    anything: search returns its best ten documents whether those are ten strong
    matches or the ten least-bad in a corpus that holds nothing on the subject.
    This distinguishes those two cases and says why.
    """
    from dataclasses import asdict

    from corpus.analytics.coverage import assess_coverage

    started = time.monotonic()
    report = assess_coverage(
        session, tenant_id=tenant_id, query=query, domain=_parse_domain(domain)
    )
    # The most valuable row this corpus writes: a graded verdict on a real question,
    # with the index completeness that produced it. `answered_well` is the grade's
    # own judgement, not a guess about whether the user was satisfied.
    record_query(
        session,
        tenant_id=tenant_id,
        tool="coverage",
        surface=surface,
        query_text=query,
        domain=domain,
        result_count=report.n_documents,
        coverage_grade=report.grade,
        indexed_documents=report.indexed_documents,
        total_documents=report.total_documents,
        latency_ms=int((time.monotonic() - started) * 1000),
        answered_well=report.grade in ("good", "partial"),
    )
    payload = asdict(report)
    payload["date_earliest"] = report.date_earliest.isoformat() if report.date_earliest else None
    payload["date_latest"] = report.date_latest.isoformat() if report.date_latest else None
    # TemporalShape carries dt.date in every bucket, which asdict leaves as date
    # objects — the whole payload is unserializable without this.
    temporal = payload.get("temporal") or {}
    if temporal.get("peak_period") is not None:
        temporal["peak_period"] = report.temporal.peak_period.isoformat()
    temporal["buckets"] = [
        {"period": b.period.isoformat(), "n_documents": b.n_documents, "n_sources": b.n_sources}
        for b in report.temporal.buckets
    ]
    payload["temporal"] = temporal
    return payload


#: A synthesis run costs one LLM call per matched document, so an unbounded filter on
#: this corpus is a four-figure number of calls made while someone waits. This is the
#: default ceiling for the MCP tool specifically — the Python API takes
#: `max_documents=None` and will genuinely read everything, which is right for a
#: deliberate batch job and wrong for a tool an agent can call on a whim.
#:
#: The cap is never silent: `dry_run` reports the true match count before spending
#: anything, and a capped run says so in its result.
DEFAULT_SYNTHESIS_MAX_DOCUMENTS = 60


def corpus_synthesize(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    question: str,
    query: str | None = None,
    domain: str | None = None,
    entity_name: str | None = None,
    since: str | None = None,
    until: str | None = None,
    max_documents: int = DEFAULT_SYNTHESIS_MAX_DOCUMENTS,
    dry_run: bool = False,
) -> dict:
    """Read every document matching a filter and synthesize an answer with citations.

    This is not search. Search ranks and returns the best few; this reads the whole
    matched set, which is what questions like "how has X been argued over time" need —
    the fortieth document counts as much as the first, and a ranker would drop it
    unread.

    `dry_run` returns the plan only: how many documents match, how many would be read,
    how many escalate to a larger model. Nothing is spent.
    """
    from corpus.synthesis.mapreduce import FilterSpec, plan_synthesis, synthesize

    if not question.strip():
        raise ToolError("question is required — this tool answers a question, it does not browse")
    if not any([query, domain, entity_name, since, until]):
        raise ToolError(
            "at least one filter (query, domain, entity_name, since, until) is required; "
            "an unfiltered synthesis would read the entire corpus"
        )

    def _date(value: str | None, label: str) -> dt.date | None:
        if value is None:
            return None
        try:
            return dt.date.fromisoformat(value)
        except ValueError as exc:
            raise ToolError(f"{label} must be an ISO date (YYYY-MM-DD), got {value!r}") from exc

    spec = FilterSpec(
        query=query,
        domain=_parse_domain(domain),
        entity_name=entity_name,
        since=_date(since, "since"),
        until=_date(until, "until"),
    )

    if dry_run:
        plan = plan_synthesis(
            session, tenant_id=tenant_id, spec=spec, max_documents=max_documents
        )
        return {
            "dry_run": True,
            "matched_documents": plan.matched_documents,
            "documents_to_read": plan.documents_to_read,
            "escalated_documents": plan.escalated_documents,
            "total_chars": plan.total_chars,
            "capped": plan.capped,
            "dropped_by_cap": plan.dropped_by_cap,
            "llm_calls": plan.documents_to_read,
        }

    report = synthesize(
        session,
        tenant_id=tenant_id,
        question=question,
        spec=spec,
        max_documents=max_documents,
    )
    return report.to_dict()


def corpus_provenance(session: Session, *, tenant_id: uuid.UUID, document_id: str) -> dict:
    """Full provenance for one document: source, authority tier, publication date
    and how precisely it's known, and transcript origin — whether it came from an
    auto-generated (ASR) transcript or a real caption track, and how confident that
    provenance signal itself is (see TranscriptVersion.provenance_confidence)."""
    try:
        doc_uuid = uuid.UUID(document_id)
    except ValueError as exc:
        raise ToolError(f"{document_id!r} is not a valid document_id (expected a UUID)") from exc
    row = session.execute(
        select(Document, Source)
        .join(Source, Source.id == Document.source_id)
        .where(Document.id == doc_uuid, Document.tenant_id == tenant_id)
    ).first()
    if row is None:
        raise ToolError(f"no document {document_id!r} in this corpus")
    document, source = row

    transcript = session.execute(
        select(TranscriptVersion)
        .where(TranscriptVersion.document_id == doc_uuid, TranscriptVersion.tenant_id == tenant_id)
        .order_by(TranscriptVersion.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    return {
        "document_id": str(doc_uuid),
        "title": document.title,
        "url": document.url,
        "source_title": source.title,
        "source_kind": source.kind.value,
        "authority_tier": source.authority_tier.value,
        "domain": source.domain.value,
        "published_at": document.published_at.isoformat() if document.published_at else None,
        "published_at_precision": document.published_at_precision.value,
        "transcript_provider": transcript.provider.value if transcript else None,
        "is_auto_generated": transcript.is_auto_generated if transcript else None,
        "provenance_confidence": transcript.provenance_confidence.value if transcript else None,
    }
