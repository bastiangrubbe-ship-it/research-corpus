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
import subprocess
import uuid
from collections.abc import Iterable, Sequence

import structlog
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from corpus.config import get_settings
from corpus.db.enums import EntityKind
from corpus.db.models import Entity, EntityMention, Segment, TranscriptVersion

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
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()


@retry(
    retry=retry_if_exception_type(ClaudeCodeCallError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    reraise=True,
)
def _run_claude_code(transcript_text: str, *, model: str, timeout_s: float) -> str:
    try:
        proc = subprocess.run(
            [
                "claude",
                "-p",
                _PROMPT,
                "--model",
                model,
                "--output-format",
                "json",
                "--allowedTools",
                "",
            ],
            input=transcript_text,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClaudeCodeCallError(f"claude CLI timed out after {timeout_s}s") from exc
    except FileNotFoundError as exc:
        raise ClaudeCodeCallError("claude CLI not found on PATH") from exc

    if proc.returncode != 0:
        raise ClaudeCodeCallError(f"claude CLI exited {proc.returncode}: {proc.stderr[:500]}")

    return proc.stdout


def extract_entities(
    text: str,
    *,
    model: str | None = None,
    timeout_s: float | None = None,
) -> DocumentEntities:
    """One call per document. Not per chunk — see module docstring."""
    settings = get_settings()
    model = model or settings.entity_extraction_model
    timeout_s = timeout_s or settings.entity_extraction_timeout_s

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
    already_enriched = select(EntityMention.document_id).where(
        EntityMention.tenant_id == tenant_id,
        EntityMention.extractor_version == extractor_version_str,
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
    text = reconstruct_transcript_text(session, transcript_version_id)
    if not text.strip():
        return 0

    doc_entities = extract_entities(text, model=model)
    version = extractor_version(model or get_settings().entity_extraction_model)
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
    return len(mention_rows)
