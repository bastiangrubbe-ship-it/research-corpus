"""Entity extraction: Claude Code (headless) for judgment, string-matching for spans.

Two concerns that don't belong to the same step:

1. *What is an entity, what kind is it, what is its canonical name* — this is a
   judgment call (is "the Snowflake platform" the vendor Snowflake or a person's
   metaphor?) that a gazetteer or GLiNER pass handles worse on jargon-heavy,
   ASR-derived transcripts than an LLM does. One call per document, not per chunk:
   at this corpus's near-term scale (tens of thousands of documents, not the 14M-chunk
   ceiling the "no LLM in the ingest path" rule was written against for contextual
   embeddings) this is a tractable number of calls under a Claude subscription login.
2. *Where exactly does that entity appear, character-for-character* — this is not a
   judgment call, and LLMs are unreliable at exact offset arithmetic. Once Claude has
   named the entities and the surface forms it used for them, finding every span is a
   deterministic string search against the transcript text.

Splitting the work this way means a bad character offset is a bug in
`find_mentions_in_text`, never a hallucination to chase in a prompt.

Extraction itself shells out to the Claude Code CLI in headless mode (`claude -p`),
the same way the project's other Claude-subscription-backed batch tooling runs it —
authenticated via `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) rather than a
metered `ANTHROPIC_API_KEY`, since this is meant to run as an unattended daily job, not
billed per token. `--allowedTools ""` is passed explicitly: transcript text is external,
untrusted content (see docs/DECISIONS.md), and this call has no legitimate reason to
ever execute a tool, so that capability is removed rather than assumed unreachable.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed

import structlog
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from corpus.config import get_settings
from corpus.db.enums import EntityKind
from corpus.db.models import Entity, EntityExtractionRun, EntityMention, Segment, TranscriptVersion
from corpus.db.session import tenant_session
from corpus.llm.headless import ClaudeCliError, run_claude_headless

log = structlog.get_logger(__name__)

# Bumped whenever the prompt or output schema changes — entity_mention.extractor_version
# records this so an LLM pass is never silently blended with a future GLiNER pass as if
# equally reliable (see docs/DECISIONS.md).
PROMPT_VERSION = "v1"

_PROMPT = """\
You extract named entities from a podcast/video transcript for a research corpus. \
The transcript may be auto-generated (ASR) and can contain minor mistranscriptions.

Extract only: vendors/companies, named people, named techniques or methodologies, \
named regulations or standards, and named products. Do not extract generic terms \
("the cloud", "machine learning" as a general field) — only specific, named entities.

For each entity, record:
- canonical_name: the standard/full name (e.g. "Snowflake", not "the Snowflake platform")
- kind: one of vendor, person, technique, regulation, product, organization
- surface_forms: every distinct way this entity is actually written or spoken in THIS \
transcript (e.g. ["Snowflake", "snowflake's platform"]) — used later for exact string \
matching, so include real variants seen in the text, not paraphrases you're inferring
- confidence: your confidence (0.0-1.0) that this is a real, correctly-identified entity

Skip anything you are not reasonably confident about rather than guessing.

Respond with ONLY a JSON object of this exact shape, no markdown fence, no prose \
before or after it:
{"entities": [{"canonical_name": str, "kind": str, "surface_forms": [str], \
"confidence": float}]}

Transcript:
"""


class ExtractedEntity(BaseModel):
    canonical_name: str
    kind: EntityKind
    surface_forms: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)


class DocumentEntities(BaseModel):
    entities: list[ExtractedEntity]


class ClaudeCodeCallError(Exception):
    """The `claude` CLI invocation itself failed — non-zero exit, timeout, not found.

    Retried by `_run_claude_code`; if it still fails after retries, transient.
    """


class ExtractionError(Exception):
    """Claude Code returned output that isn't valid, schema-matching JSON.

    Not retried — a schema mismatch is a prompt/parsing problem, not a transient one.
    """


def extractor_version(model: str = "") -> str:
    model = model or get_settings().entity_extraction_model
    return f"claude-code:{model}:entities-{PROMPT_VERSION}"


def _strip_markdown_fence(text: str) -> str:
    """Strips a wrapping ``` fence, extracting only what's inside it.

    Deliberately matches the *first* fenced block rather than anchoring the closing
    fence to end-of-string: despite the prompt asking for bare JSON, Claude sometimes
    closes the fence and then adds explanatory prose after it (e.g. "```\\n\\nThis
    transcript contains no identifiable named entities..."). An end-anchored strip
    leaves that trailing prose attached to the JSON and breaks parsing entirely,
    discarding an otherwise-correct `{"entities": []}` result as a schema failure.
    """
    text = text.strip()
    if text.startswith("```"):
        match = re.match(r"^```[a-zA-Z]*\n?(.*?)\n?```", text, re.DOTALL)
        if match:
            return match.group(1).strip()
    return text.strip()


@retry(
    retry=retry_if_exception_type(ClaudeCodeCallError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    reraise=True,
)
def _run_claude_code(transcript_text: str, *, model: str, timeout_s: float) -> str:
    """Delegates to corpus.llm.headless, which carries this call's hard-won detail:
    the CLI reports failures in the stdout envelope rather than on stderr. That was
    diagnosed here and then found sitting unfixed in corpus.eval.judge, which is why
    the subprocess call now lives in one module instead of three.

    Transcript text goes over stdin, never interpolated into the prompt — it is
    untrusted external content.
    """
    try:
        return run_claude_headless(
            _PROMPT, model=model, timeout_s=timeout_s, stdin_text=transcript_text
        )
    except ClaudeCliError as exc:
        raise ClaudeCodeCallError(str(exc)) from exc


#: Above this transcript size, extract with `LARGE_DOCUMENT_MODEL` instead.
#:
#: The configured model is `haiku` (200k tokens, ~800k characters). Exactly one
#: transcript in this corpus is 1,023,757 characters (~256k tokens) and simply does
#: not fit — it failed every backfill attempt, and docs/DECISIONS.md recorded it as a
#: permanent limit on the grounds that chunked multi-call extraction wasn't worth
#: building for one document.
#:
#: That reasoning was sound and the conclusion was still wrong: the fix isn't
#: chunking, it's a model with a bigger context. Opus takes 1M tokens, which fits
#: that document with room to spare — verified directly by judging the same document
#: end to end (corpus.eval.judge uses the identical escalation).
#:
#: 200,000 characters covers 25 documents (0.76%), so the escalation stays bounded
#: while removing the "permanently un-extractable" category entirely.
LARGE_DOCUMENT_CHARS = 200_000
LARGE_DOCUMENT_MODEL = "opus"

#: Escalated documents are both larger and on a slower model. Measured: the single
#: 1M-character transcript took 230s of API time against a 240s default.
LARGE_DOCUMENT_TIMEOUT_MULTIPLIER = 4


def extract_entities(
    text: str,
    *,
    model: str | None = None,
    timeout_s: float | None = None,
) -> DocumentEntities:
    """One call per document. Not per chunk — see module docstring.

    Model escalates by size when `model` isn't given explicitly: the configured
    default for the 99%+ of documents that fit it, Opus for the handful that don't.
    """
    settings = get_settings()
    if model is None:
        escalated = len(text) > LARGE_DOCUMENT_CHARS
        model = LARGE_DOCUMENT_MODEL if escalated else settings.entity_extraction_model
        if escalated:
            # Logged rather than folded into `extractor_version`, deliberately.
            # `find_unenriched_documents` and `EntityExtractionRun` both key off that
            # version string, so varying it per document by size would make escalated
            # documents look permanently unenriched and re-queue them on every run —
            # the exact failure this table was added to fix. The prompt, schema and
            # prompt version are identical on both paths; only context capacity
            # differs, so one version string is honest. This log line is how the
            # escalation stays visible.
            log.info(
                "entity_extraction_escalated",
                chars=len(text),
                model=model,
                reason="exceeds configured model's context",
            )
    if timeout_s is None:
        # An escalated document is ~250k tokens; the API call alone measured 230s
        # against a configured 240s ceiling, so the default is not merely tight for
        # this path, it is below the observed cost. Scaled rather than raised
        # globally: the 99% of documents that stay on the small model should keep a
        # short timeout, because for them a long wait means something is wrong.
        timeout_s = settings.entity_extraction_timeout_s * (
            LARGE_DOCUMENT_TIMEOUT_MULTIPLIER if len(text) > LARGE_DOCUMENT_CHARS else 1
        )

    stdout = _run_claude_code(text, model=model, timeout_s=timeout_s)

    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ExtractionError(
            f"claude CLI did not return a JSON envelope: {stdout[:300]!r}"
        ) from exc

    result_text = _strip_markdown_fence(envelope.get("result", ""))
    try:
        payload = json.loads(result_text)
        return DocumentEntities.model_validate(payload)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ExtractionError(
            f"model output did not match the entity schema: {result_text[:300]!r}"
        ) from exc


def _iter_span_matches(haystack: str, needle: str) -> Iterable[tuple[int, int]]:
    """Case-insensitive, word-boundary-aware matches of `needle` in `haystack`."""
    if not needle.strip():
        return
    pattern = re.compile(rf"(?<!\w){re.escape(needle)}(?!\w)", re.IGNORECASE)
    for match in pattern.finditer(haystack):
        yield match.start(), match.end()


def find_mentions_in_text(
    *,
    document_id: uuid.UUID,
    text: str,
    entities: Sequence[ExtractedEntity],
    chunk_id: uuid.UUID | None = None,
) -> list[dict]:
    """Deterministic span-finding against one block of text.

    `chunk_id` is None when this runs before chunking exists for the document (the
    current state of the pipeline — chunking is a later build step) — offsets are then
    relative to the full reconstructed transcript. Once chunking lands, the same
    function runs per chunk with `chunk_id` set and offsets relative to chunk text.
    """
    rows: list[dict] = []
    for entity in entities:
        # Surface forms of one entity overlap under case-insensitive matching (e.g.
        # "Snowflake" and "snowflake" both match "snowflake handles..."), so spans
        # are deduped per entity before becoming rows.
        spans: dict[tuple[int, int], None] = {}
        for surface in entity.surface_forms:
            for start, end in _iter_span_matches(text, surface):
                spans.setdefault((start, end), None)
        for start, end in spans:
            rows.append(
                {
                    "chunk_id": chunk_id,
                    "document_id": document_id,
                    "surface": text[start:end],
                    "start_char": start,
                    "end_char": end,
                    "confidence": entity.confidence,
                    "canonical_name": entity.canonical_name,
                    "kind": entity.kind,
                }
            )
    return rows


def persist_entities(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    entities: Sequence[ExtractedEntity],
    mention_rows: Sequence[dict],
    extractor_version_str: str,
) -> None:
    """Upsert `entity` rows, then insert `entity_mention` rows against them.

    Entities are shared across documents (unique on tenant_id, kind, canonical_name),
    so this upserts rather than inserts, merging aliases rather than overwriting them.
    """
    entity_ids: dict[tuple[EntityKind, str], uuid.UUID] = {}
    for entity in entities:
        existing = session.execute(
            select(Entity.id, Entity.aliases).where(
                Entity.tenant_id == tenant_id,
                Entity.kind == entity.kind,
                Entity.canonical_name == entity.canonical_name,
            )
        ).one_or_none()
        existing_aliases = existing.aliases or [] if existing else []
        merged_aliases = sorted(set(entity.surface_forms) | set(existing_aliases))

        stmt = (
            pg_insert(Entity)
            .values(
                tenant_id=tenant_id,
                kind=entity.kind,
                canonical_name=entity.canonical_name,
                aliases=merged_aliases,
            )
            .on_conflict_do_update(
                index_elements=[Entity.tenant_id, Entity.kind, Entity.canonical_name],
                set_={"aliases": merged_aliases},
            )
            .returning(Entity.id)
        )
        entity_id = session.execute(stmt).scalar_one()
        entity_ids[(entity.kind, entity.canonical_name)] = entity_id

    mention_dicts = [
        {
            "tenant_id": tenant_id,
            "entity_id": entity_ids[(row["kind"], row["canonical_name"])],
            "document_id": document_id,
            "chunk_id": row["chunk_id"],
            "surface": row["surface"],
            "start_char": row["start_char"],
            "end_char": row["end_char"],
            "confidence": row["confidence"],
            "extractor_version": extractor_version_str,
        }
        for row in mention_rows
    ]
    if mention_dicts:
        session.execute(pg_insert(EntityMention), mention_dicts)
    session.commit()


def mark_extraction_run(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    extractor_version_str: str,
    mention_count: int,
) -> None:
    """Records that `document_id` was checked at `extractor_version_str`, regardless
    of whether any entities were found. This is the completion marker
    `find_unenriched_documents` filters against — see `EntityExtractionRun`'s
    docstring for why `entity_mention` alone can't serve this purpose. Upserts so
    re-running an already-processed document (e.g. a manual re-check) updates the
    count and timestamp rather than erroring on the unique constraint.
    """
    stmt = (
        pg_insert(EntityExtractionRun)
        .values(
            tenant_id=tenant_id,
            document_id=document_id,
            extractor_version=extractor_version_str,
            mention_count=mention_count,
        )
        .on_conflict_do_update(
            index_elements=[EntityExtractionRun.document_id, EntityExtractionRun.extractor_version],
            set_={"mention_count": mention_count, "processed_at": func.now()},
        )
    )
    session.execute(stmt)
    session.commit()


def reconstruct_transcript_text(session: Session, transcript_version_id: uuid.UUID) -> str:
    """Segments in idx order, joined — the plain text Claude Code sees."""
    segments = (
        session.execute(
            select(Segment.text)
            .where(Segment.transcript_version_id == transcript_version_id)
            .order_by(Segment.idx)
        )
        .scalars()
        .all()
    )
    return " ".join(segments)


def find_unenriched_documents(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    extractor_version_str: str,
    limit: int | None = None,
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """(document_id, transcript_version_id) pairs not yet enriched at this version.

    Picks each document's most recent transcript_version — today that's whichever
    provider fetched it; once restoration (enrich/restore.py) lands, the restored
    version is written later and so becomes "most recent" automatically, with no
    change needed here. Filtering on extractor_version means a prompt/model bump
    reprocesses everything, the same provenance discipline entity_mention already
    has for telling an LLM pass apart from a future GLiNER pass.
    """
    latest_versions = (
        select(
            TranscriptVersion.document_id,
            TranscriptVersion.id.label("transcript_version_id"),
        )
        .where(TranscriptVersion.tenant_id == tenant_id)
        .distinct(TranscriptVersion.document_id)
        .order_by(TranscriptVersion.document_id, TranscriptVersion.created_at.desc())
        .subquery()
    )
    already_enriched = select(EntityExtractionRun.document_id).where(
        EntityExtractionRun.tenant_id == tenant_id,
        EntityExtractionRun.extractor_version == extractor_version_str,
    )
    stmt = select(latest_versions.c.document_id, latest_versions.c.transcript_version_id).where(
        latest_versions.c.document_id.notin_(already_enriched)
    )
    if limit:
        stmt = stmt.limit(limit)
    return [tuple(row) for row in session.execute(stmt).all()]


def enrich_document(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    transcript_version_id: uuid.UUID,
    model: str | None = None,
) -> int:
    """Extract + persist entities for one document. Returns the mention count."""
    version = extractor_version(model or get_settings().entity_extraction_model)
    text = reconstruct_transcript_text(session, transcript_version_id)
    if not text.strip():
        mark_extraction_run(
            session,
            tenant_id=tenant_id,
            document_id=document_id,
            extractor_version_str=version,
            mention_count=0,
        )
        return 0

    doc_entities = extract_entities(text, model=model)
    mention_rows = find_mentions_in_text(
        document_id=document_id, text=text, entities=doc_entities.entities
    )
    persist_entities(
        session,
        tenant_id=tenant_id,
        document_id=document_id,
        entities=doc_entities.entities,
        mention_rows=mention_rows,
        extractor_version_str=version,
    )
    mark_extraction_run(
        session,
        tenant_id=tenant_id,
        document_id=document_id,
        extractor_version_str=version,
        mention_count=len(mention_rows),
    )
    return len(mention_rows)


def enrich_documents_concurrent(
    pending: Sequence[tuple[uuid.UUID, uuid.UUID]],
    *,
    tenant_id: uuid.UUID,
    model: str | None = None,
    concurrency: int = 1,
) -> Iterator[tuple[uuid.UUID, int | None, Exception | None]]:
    """Concurrent version of `enrich_document` over a batch of (document_id,
    transcript_version_id) pairs. Yields (document_id, mention_count, error) as each
    document finishes — mention_count is None and error is set on failure.

    The slow part (`extract_entities`, the `claude -p` call) runs in a thread pool and
    touches no database; only the fast span-finding + persist step opens a
    `tenant_session`, one per document, on the thread that just finished extracting —
    so `concurrency` workers never hold that many DB connections open at once, only
    ever as many as are between "Claude answered" and "wrote the rows". Measured safe
    up to 25 concurrent `claude -p` calls with zero errors and near-linear wall-clock
    speedup (see docs/DECISIONS.md).
    """
    # `version` is always the *configured* extractor, even when an individual
    # document escalates to a larger model — see the note in `extract_entities`.
    version = extractor_version(model or get_settings().entity_extraction_model)

    def _extract_one(document_id, transcript_version_id):
        with tenant_session(tenant_id) as session:
            text = reconstruct_transcript_text(session, transcript_version_id)
        if not text.strip():
            return document_id, text, None, None
        try:
            # model=None here is meaningful: it lets extract_entities pick by
            # document size. Resolving it to the configured default first would
            # silently disable escalation for oversized transcripts.
            doc_entities = extract_entities(text, model=model)
            return document_id, text, doc_entities, None
        except (ClaudeCodeCallError, ExtractionError) as exc:
            return document_id, text, None, exc

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(_extract_one, doc_id, tv_id) for doc_id, tv_id in pending]
        for future in as_completed(futures):
            document_id, text, doc_entities, error = future.result()
            if error is not None:
                yield document_id, None, error
                continue
            if doc_entities is None:
                with tenant_session(tenant_id) as session:
                    mark_extraction_run(
                        session,
                        tenant_id=tenant_id,
                        document_id=document_id,
                        extractor_version_str=version,
                        mention_count=0,
                    )
                yield document_id, 0, None
                continue
            mention_rows = find_mentions_in_text(
                document_id=document_id, text=text, entities=doc_entities.entities
            )
            with tenant_session(tenant_id) as session:
                persist_entities(
                    session,
                    tenant_id=tenant_id,
                    document_id=document_id,
                    entities=doc_entities.entities,
                    mention_rows=mention_rows,
                    extractor_version_str=version,
                )
                mark_extraction_run(
                    session,
                    tenant_id=tenant_id,
                    document_id=document_id,
                    extractor_version_str=version,
                    mention_count=len(mention_rows),
                )
            yield document_id, len(mention_rows), None
