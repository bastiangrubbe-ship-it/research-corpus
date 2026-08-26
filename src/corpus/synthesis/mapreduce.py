"""Filter-then-synthesize: the third capability, and the one retrieval cannot serve.

`corpus_search` answers "find me the passage". This answers "read everything this
corpus holds on X and tell me what it collectively says" -- which is a different
question, and the reason top-k retrieval is the wrong primitive for it. Ranking is
designed to *discard*: it returns the best ten and drops the rest silently. For a
question like "how has the argument for on-device inference changed", the fortieth
document matters as much as the first, and a summary built from ten is not a weaker
answer to the question asked, it is a confident answer to a different one.

So the filter here is a SQL/lexical *set*, not a ranking. Every document that matches
is read.

Three stages:

1. **Filter** -- SQL over metadata (domain, date range, entity, source) optionally
   narrowed by Postgres full-text. Returns a set, in a stable order, with a count the
   caller can see before spending anything.
2. **Map** -- one `claude -p` call per document, over the *full* transcript. Each
   returns whether the document actually addresses the question and, if so, its
   findings with a supporting quote. Documents that turn out not to address it drop
   out here; that is what keeps the reduce tractable without a ranker throwing away
   things it never read.
3. **Reduce** -- findings combined into prose with citations. Hierarchically when
   there are more findings than fit one call: batches are reduced to partial
   syntheses, and those are reduced again, until one remains.

Nothing is truncated at any stage. Documents larger than the default model's context
escalate to Opus (`corpus.llm.headless`), exactly as entity extraction and the eval
judge do.

**Cost is the thing to be honest about.** This is one LLM call per matched document,
so a filter matching 800 documents is 800 calls, and unlike the nightly entity job it
runs while someone waits. `plan_synthesis` exists so a caller can see the document
count and the escalation count *before* committing, and `max_documents` bounds a run
-- but a bounded run says so in its report and names what it left out. A synthesis
that silently read 200 of 800 matches while sounding like it read everything would be
worse than no synthesis at all, because the whole value of this capability is the
claim that nothing was dropped.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field

import structlog
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from corpus.config import get_settings
from corpus.db.enums import Domain
from corpus.db.models import Document, Entity, EntityMention, Segment, Source, TranscriptVersion
from corpus.llm.headless import (
    ClaudeCliError,
    choose_model,
    parse_envelope,
    run_claude_headless,
    scale_timeout,
)

log = structlog.get_logger(__name__)

#: Bumped when a prompt or output schema below changes, so a stored synthesis can be
#: told apart from one produced by different instructions.
PROMPT_VERSION = "v1"

#: How many findings go into one reduce call. Conservative on purpose: findings are
#: short, but the reduce prompt also carries every citation marker, and a reduce that
#: overflows produces a truncated answer rather than an error.
REDUCE_BATCH_SIZE = 40

_MAP_PROMPT = """\
You are reading ONE document from a research corpus to answer a specific question.

The question:
{question}

Decide first whether this document actually says anything that bears on the question. \
Most documents in a filtered set will not, and saying so is a useful, correct answer \
-- do not stretch to find relevance that isn't there.

If it does bear on the question, record what THIS document specifically says. Each \
finding must be something a reader of this document would recognise, not a general \
statement about the topic. Include a short verbatim quote for each finding, copied \
exactly from the transcript.

The transcript may be auto-generated (ASR) and can contain mistranscriptions; quote \
what is actually written rather than correcting it.

Respond with ONLY a JSON object of this exact shape, no markdown fence, no prose \
before or after it:
{{"addresses_question": bool, "findings": [{{"claim": str, "quote": str}}], \
"summary": str}}

If addresses_question is false, use an empty findings list and a one-sentence summary \
saying what the document is about instead.

Transcript:
"""

_REDUCE_PROMPT = """\
You are writing the answer to a research question by combining findings that were \
each extracted from a different document in a corpus.

The question:
{question}

Below are numbered findings. Each carries a citation marker like [3]. Write a \
synthesis that answers the question from these findings.

Rules:
- Cite every claim with the marker(s) of the finding(s) it came from, like [3] or \
[3][7]. Use ONLY markers that appear below. Never invent a marker.
- Where documents disagree, say so and cite both sides. Disagreement is a finding, \
not a problem to smooth over.
- Where the findings show something changing over time, say so -- the dates are given.
- Do not add knowledge from outside these findings. If the findings do not answer \
some part of the question, say that plainly.
- Write prose, not a list of summaries. No preamble about what you were asked.

Findings:
{findings}
"""


class _MapFinding(BaseModel):
    claim: str
    quote: str = ""


class _MapOutput(BaseModel):
    addresses_question: bool
    findings: list[_MapFinding] = Field(default_factory=list)
    summary: str = ""


class SynthesisError(Exception):
    """The synthesis could not be produced at all."""


@dataclass(frozen=True, slots=True)
class FilterSpec:
    """What to read. Every field narrows; `None` means "do not narrow on this".

    `query` is Postgres full-text, not semantic search, and that is deliberate: this
    stage decides *membership*, and a membership test needs to be explainable and
    reproducible. A vector similarity threshold is neither -- "documents above 0.7
    cosine" is not a set anyone can check, and the caller would have no way to know
    what fell just below it.
    """

    query: str | None = None
    domain: Domain | None = None
    entity_name: str | None = None
    since: dt.date | None = None
    until: dt.date | None = None


@dataclass(frozen=True, slots=True)
class DocumentRef:
    document_id: uuid.UUID
    title: str | None
    url: str | None
    published_at: dt.date | None
    source_title: str | None
    chars: int


@dataclass(frozen=True, slots=True)
class SynthesisPlan:
    """What a run would cost, before it is run."""

    matched_documents: int
    documents_to_read: int
    escalated_documents: int
    total_chars: int
    capped: bool

    @property
    def dropped_by_cap(self) -> int:
        return self.matched_documents - self.documents_to_read


@dataclass
class Citation:
    marker: int
    document_id: uuid.UUID
    title: str | None
    url: str | None
    published_at: dt.date | None
    source_title: str | None
    claim: str
    quote: str


@dataclass
class SynthesisReport:
    question: str
    answer: str
    citations: list[Citation] = field(default_factory=list)
    matched_documents: int = 0
    documents_read: int = 0
    documents_addressing: int = 0
    documents_failed: int = 0
    capped: bool = False
    dropped_by_cap: int = 0
    invalid_markers: list[int] = field(default_factory=list)
    prompt_version: str = PROMPT_VERSION

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["citations"] = [
            {
                **{k: v for k, v in asdict(c).items() if k not in {"document_id", "published_at"}},
                "document_id": str(c.document_id),
                "published_at": c.published_at.isoformat() if c.published_at else None,
            }
            for c in self.citations
        ]
        return payload


def _base_query(tenant_id: uuid.UUID, spec: FilterSpec) -> Select:
    stmt = (
        select(
            Document.id,
            Document.title,
            Document.url,
            Document.published_at,
            Source.title.label("source_title"),
        )
        .join(Source, Source.id == Document.source_id)
        .where(Document.tenant_id == tenant_id)
    )

    if spec.domain is not None:
        stmt = stmt.where(Source.domain == spec.domain)
    if spec.since is not None:
        stmt = stmt.where(Document.published_at >= spec.since)
    if spec.until is not None:
        stmt = stmt.where(Document.published_at <= spec.until)

    if spec.query:
        haystack = func.coalesce(Document.title, "") + " " + func.coalesce(Document.description, "")
        stmt = stmt.where(
            func.to_tsvector("english", haystack).op("@@")(
                func.plainto_tsquery("english", spec.query)
            )
        )

    if spec.entity_name:
        entity_ids = select(Entity.id).where(
            Entity.tenant_id == tenant_id, Entity.canonical_name.ilike(spec.entity_name)
        )
        mentioned = select(EntityMention.document_id).where(
            EntityMention.tenant_id == tenant_id, EntityMention.entity_id.in_(entity_ids)
        )
        stmt = stmt.where(Document.id.in_(mentioned))

    # Oldest first, then id. Chronological order is the useful one for a capability
    # whose whole point is showing how a view developed, and it is stable across runs
    # so a capped run reads the same documents each time.
    return stmt.order_by(Document.published_at.asc().nulls_last(), Document.id.asc())


def select_documents(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    spec: FilterSpec,
    max_documents: int | None = None,
) -> tuple[list[DocumentRef], int]:
    """`(documents_to_read, total_matched)`. The second value is what makes a cap
    visible: callers must report it rather than presenting a capped read as complete.
    """
    rows = session.execute(_base_query(tenant_id, spec)).all()
    total = len(rows)
    if max_documents is not None:
        rows = rows[:max_documents]
    if not rows:
        return [], total

    sizes = _document_sizes(session, tenant_id=tenant_id, document_ids=[r.id for r in rows])
    return [
        DocumentRef(
            document_id=r.id,
            title=r.title,
            url=r.url,
            published_at=r.published_at,
            source_title=r.source_title,
            chars=sizes.get(r.id, 0),
        )
        for r in rows
    ], total


def _document_sizes(
    session: Session, *, tenant_id: uuid.UUID, document_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Transcript length per document, from its latest version -- the same version
    `document_text` will read, so the size a plan reports is the size that is sent."""
    latest = (
        select(
            TranscriptVersion.document_id,
            func.max(TranscriptVersion.created_at).label("created_at"),
        )
        .where(
            TranscriptVersion.tenant_id == tenant_id,
            TranscriptVersion.document_id.in_(document_ids),
        )
        .group_by(TranscriptVersion.document_id)
        .subquery()
    )
    rows = session.execute(
        select(TranscriptVersion.document_id, func.sum(func.length(Segment.text)))
        .join(latest, latest.c.document_id == TranscriptVersion.document_id)
        .join(Segment, Segment.transcript_version_id == TranscriptVersion.id)
        .where(TranscriptVersion.created_at == latest.c.created_at)
        .group_by(TranscriptVersion.document_id)
    ).all()
    return {doc_id: int(size or 0) for doc_id, size in rows}


def document_text(session: Session, *, tenant_id: uuid.UUID, document_id: uuid.UUID) -> str:
    """Full transcript text of a document's latest version, segments in order.

    Latest wins, matching chunking, the relevance gate and entity extraction -- so
    once punctuation restoration has run, synthesis reads the restored text, which is
    what it wants: the map step's quotes come out readable rather than as a wall of
    unpunctuated ASR.
    """
    version_id = session.execute(
        select(TranscriptVersion.id)
        .where(
            TranscriptVersion.tenant_id == tenant_id,
            TranscriptVersion.document_id == document_id,
        )
        .order_by(TranscriptVersion.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if version_id is None:
        return ""
    rows = session.execute(
        select(Segment.text)
        .where(Segment.transcript_version_id == version_id)
        .order_by(Segment.idx)
    ).all()
    return " ".join(r.text for r in rows if r.text)


def plan_synthesis(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    spec: FilterSpec,
    max_documents: int | None = None,
) -> SynthesisPlan:
    """What this filter would cost, without spending anything. One LLM call per
    document in `documents_to_read`."""
    from corpus.llm.headless import LARGE_INPUT_CHARS

    docs, total = select_documents(
        session, tenant_id=tenant_id, spec=spec, max_documents=max_documents
    )
    return SynthesisPlan(
        matched_documents=total,
        documents_to_read=len(docs),
        escalated_documents=sum(1 for d in docs if d.chars > LARGE_INPUT_CHARS),
        total_chars=sum(d.chars for d in docs),
        capped=len(docs) < total,
    )


def map_document(text: str, *, question: str, timeout_s: float | None = None) -> _MapOutput:
    """One document's findings for one question. Full text, never truncated -- the
    model escalates by size instead."""
    settings = get_settings()
    model = choose_model(text, default=settings.entity_extraction_model)
    if timeout_s is None:
        timeout_s = scale_timeout(text, base_timeout_s=settings.entity_extraction_timeout_s)

    stdout = run_claude_headless(
        _MAP_PROMPT.format(question=question),
        model=model,
        timeout_s=timeout_s,
        stdin_text=text,
    )
    result_text = parse_envelope(stdout)
    try:
        return _MapOutput.model_validate(json.loads(result_text))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise SynthesisError(f"map output did not match schema: {result_text[:300]!r}") from exc


@dataclass
class _MappedDocument:
    ref: DocumentRef
    output: _MapOutput


def map_documents_concurrent(
    session_factory,
    *,
    tenant_id: uuid.UUID,
    documents: list[DocumentRef],
    question: str,
    concurrency: int = 8,
) -> Iterator[tuple[DocumentRef, _MapOutput | None, str | None]]:
    """Yields `(ref, output, error)` as each document finishes, in completion order.

    `session_factory` is a zero-argument callable returning a new session: each worker
    thread needs its own, since a SQLAlchemy Session is not thread-safe.
    """

    def work(ref: DocumentRef) -> tuple[DocumentRef, _MapOutput | None, str | None]:
        try:
            with session_factory() as session:
                text = document_text(session, tenant_id=tenant_id, document_id=ref.document_id)
            if not text.strip():
                return ref, None, "no transcript text"
            return ref, map_document(text, question=question), None
        except (ClaudeCliError, SynthesisError) as exc:
            return ref, None, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(work, ref) for ref in documents]
        for future in as_completed(futures):
            yield future.result()


_MARKER = re.compile(r"\[(\d+)\]")


def _format_findings(citations: list[Citation]) -> str:
    lines = []
    for c in citations:
        date = c.published_at.isoformat() if c.published_at else "date unknown"
        source = c.source_title or "unknown source"
        lines.append(f"[{c.marker}] ({date}, {source}) {c.claim}")
        if c.quote:
            lines.append(f'      quote: "{c.quote}"')
    return "\n".join(lines)


def reduce_findings(
    citations: list[Citation],
    *,
    question: str,
    timeout_s: float | None = None,
    batch_size: int = REDUCE_BATCH_SIZE,
) -> str:
    """Findings to prose with citation markers, hierarchically when there are many.

    Markers are preserved through every level rather than renumbered per batch, so a
    marker in the final text still points at the document it came from. Renumbering
    per batch would make the last reduce's citations meaningless -- the same [3]
    meaning a different document in each partial.
    """
    if not citations:
        return ""

    settings = get_settings()
    model = settings.entity_extraction_model
    timeout = timeout_s or settings.entity_extraction_timeout_s

    if len(citations) <= batch_size:
        prompt = _REDUCE_PROMPT.format(question=question, findings=_format_findings(citations))
        return parse_envelope(run_claude_headless(prompt, model=model, timeout_s=timeout))

    partials: list[str] = []
    for i in range(0, len(citations), batch_size):
        batch = citations[i : i + batch_size]
        prompt = _REDUCE_PROMPT.format(question=question, findings=_format_findings(batch))
        partials.append(parse_envelope(run_claude_headless(prompt, model=model, timeout_s=timeout)))
        log.info("synthesis_reduce_batch", batch=i // batch_size + 1, findings=len(batch))

    joined = "\n\n".join(f"Partial synthesis {i + 1}:\n{p}" for i, p in enumerate(partials))
    final_prompt = _REDUCE_PROMPT.format(
        question=question,
        findings=(
            "These are partial syntheses of different subsets of the findings. Combine "
            "them into one answer, preserving every citation marker exactly as written "
            "and merging claims that repeat across partials.\n\n" + joined
        ),
    )
    return parse_envelope(run_claude_headless(final_prompt, model=model, timeout_s=timeout * 2))


def synthesize(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    question: str,
    spec: FilterSpec,
    max_documents: int | None = None,
    concurrency: int = 8,
    session_factory=None,
) -> SynthesisReport:
    """Filter, read every matched document, and synthesize an answer with citations.

    The report carries `matched_documents` alongside `documents_read` always, not only
    when they differ, so a reader never has to know to check whether a cap applied.
    """
    from corpus.db.session import tenant_session

    if session_factory is None:

        def session_factory():
            return tenant_session(tenant_id)

    documents, total = select_documents(
        session, tenant_id=tenant_id, spec=spec, max_documents=max_documents
    )
    report = SynthesisReport(
        question=question,
        answer="",
        matched_documents=total,
        documents_read=len(documents),
        capped=len(documents) < total,
        dropped_by_cap=total - len(documents),
    )
    if not documents:
        report.answer = "No documents in this corpus match that filter."
        return report

    if report.capped:
        # Never a silent truncation: this is the one thing that would make a
        # synthesis actively misleading rather than merely incomplete.
        log.warning(
            "synthesis_capped",
            matched=total,
            reading=len(documents),
            dropped=report.dropped_by_cap,
        )

    citations: list[Citation] = []
    marker = 0
    for ref, output, error in map_documents_concurrent(
        session_factory,
        tenant_id=tenant_id,
        documents=documents,
        question=question,
        concurrency=concurrency,
    ):
        if error is not None:
            report.documents_failed += 1
            log.warning("synthesis_map_failed", document_id=str(ref.document_id), error=error)
            continue
        if output is None or not output.addresses_question or not output.findings:
            continue
        report.documents_addressing += 1
        for finding in output.findings:
            marker += 1
            citations.append(
                Citation(
                    marker=marker,
                    document_id=ref.document_id,
                    title=ref.title,
                    url=ref.url,
                    published_at=ref.published_at,
                    source_title=ref.source_title,
                    claim=finding.claim,
                    quote=finding.quote,
                )
            )

    # Chronological, so the reduce sees the findings in the order the corpus produced
    # them and can speak to how a view developed rather than guessing at sequence.
    citations.sort(key=lambda c: (c.published_at or dt.date.min, c.marker))
    report.citations = citations

    if not citations:
        report.answer = (
            f"Read {report.documents_read} matching documents; none of them address "
            f"that question. The filter matched, but the content does not."
        )
        return report

    report.answer = reduce_findings(citations, question=question)

    # Validate rather than trust: a citation marker is the one part of this output a
    # reader is most likely to take on faith, and the least able to check.
    valid = {c.marker for c in citations}
    used = {int(m) for m in _MARKER.findall(report.answer)}
    report.invalid_markers = sorted(used - valid)
    if report.invalid_markers:
        log.warning("synthesis_invalid_markers", markers=report.invalid_markers)

    return report
