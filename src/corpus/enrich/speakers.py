"""Speaker attribution, tier 1 — cheap heuristics over title/description/channel
name, never an LLM call. Tier 2 (real diarization: yt-dlp + WhisperX + pyannote) is
a separate, opt-in flow for a curated subset, never in the main ingest path — this
module is only ever tier 1.

Calibrated against a real 25-document sample of this corpus before writing any
pattern, and revised again after that same sample caught two real false positives
once actually tested (see docs/DECISIONS.md, 2026-08-25):

1. A title's "with X" almost always names a *tool* in this AI-tutorial-heavy corpus
   ("How to Build a WhatsApp AI Agent with Claude Code"), not a guest — the opposite
   of what that phrase means in general podcast/interview conventions. Dropped
   entirely rather than kept and hoped it stays rare.
2. Channel names shaped like a person's name aren't always one — "Better Stack" is a
   real two-word devtools company, indistinguishable by shape alone from "Dan
   Martell". No regex fixes this; every raw guess is instead validated against this
   corpus's own entity table before being trusted: a match on a VENDOR/PRODUCT/
   ORGANIZATION/TECHNIQUE/REGULATION entity rejects the guess outright (something
   this corpus's independent entity-extraction pipeline already knows isn't a
   person), a match on PERSON confirms it and links `canonical_entity_id`. An
   unmatched guess keeps its original, deliberately modest confidence — validated
   neither way, not asserted as fact.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from corpus.db.enums import AttributionMethod, EntityKind
from corpus.db.models import Document, Entity, Source, Speaker

# Allows an optional lowercase surname particle (van, de, der, von, la, ...) — plain
# title-case-only missed real names like "Leon van Zyl" in the calibration sample.
#
# Separators are [ \t]+, never \s+: \s crosses newlines, and descriptions are full of
# them. "...videos\n\nSpecial Thanks" was captured as a two-word name because the gap
# between an outro line and a new heading looked exactly like the gap between a first
# and last name.
_PARTICLE = r"(?:[a-z]{2,3}[ \t]+)?"
_NAME = rf"[A-Z][a-z]+(?:\.[a-z]*)?(?:[ \t]+{_PARTICLE}[A-Z][a-z]+){{1,2}}"

# Case-insensitivity is scoped to the literal cue with (?i:...), never applied to the
# whole pattern. Under a blanket re.IGNORECASE the [A-Z][a-z]+ in _NAME also matches
# lowercase words, which destroys the only thing separating a name from ordinary
# prose: "both guests would remove by fiat" yielded the speaker "would remove by
# fiat", and "ft. Ryan Carson from" yielded "Ryan Carson from". That was 5 of 84
# parsed labels on this corpus (docs/DECISIONS.md, 2026-08-26).
_DESCRIPTION_GUEST_EXPLICIT = re.compile(rf"(?i:\bguests?:?)[ \t]+({_NAME})")
_DESCRIPTION_GUEST_BIO = re.compile(
    rf"^({_NAME})[ \t]+(?:is|was|has|spent|founded|leads|runs|works?|worked|co-founded)\b"
)
# "with X" deliberately absent — see module docstring, point 1.
_TITLE_GUEST = re.compile(rf"(?i:\b(?:ft\.?|feat\.?))[ \t]+({_NAME})\b")

_SOURCE_NAME_EMBEDDED = re.compile(rf"^({_NAME})\s+(?:on|of|'s)\b")
_SOURCE_IS_JUST_A_NAME = re.compile(rf"^{_NAME}$")

_NON_PERSON_KINDS = (
    EntityKind.VENDOR,
    EntityKind.PRODUCT,
    EntityKind.ORGANIZATION,
    EntityKind.TECHNIQUE,
    EntityKind.REGULATION,
)


@dataclass(frozen=True, slots=True)
class SpeakerGuess:
    label: str
    method: AttributionMethod
    confidence: float
    canonical_entity_id: uuid.UUID | None = None


def _raw_candidates(
    *, title: str, description: str, source_title: str | None
) -> list[tuple[str, AttributionMethod, float]]:
    """Pure pattern matching, no DB access — every candidate in priority order,
    highest confidence first. `guess_speaker` validates these against the entity
    table before accepting any of them; this function only proposes.
    """
    candidates = []

    match = _DESCRIPTION_GUEST_EXPLICIT.search(description)
    if match:
        candidates.append((match.group(1).strip(), AttributionMethod.DESCRIPTION_PARSED, 0.8))

    match = _DESCRIPTION_GUEST_BIO.search(description.strip())
    if match:
        candidates.append((match.group(1).strip(), AttributionMethod.DESCRIPTION_PARSED, 0.6))

    match = _TITLE_GUEST.search(title)
    if match:
        candidates.append((match.group(1).strip(), AttributionMethod.TITLE_PARSED, 0.6))

    if source_title:
        embedded = _SOURCE_NAME_EMBEDDED.match(source_title)
        if embedded:
            candidates.append((embedded.group(1).strip(), AttributionMethod.CHANNEL_DEFAULT, 0.5))
        elif _SOURCE_IS_JUST_A_NAME.match(source_title):
            candidates.append((source_title.strip(), AttributionMethod.CHANNEL_DEFAULT, 0.4))

    return candidates


def _entity_kind_for(
    session: Session, *, tenant_id: uuid.UUID, name: str
) -> tuple[EntityKind, uuid.UUID] | None:
    row = session.execute(
        select(Entity.kind, Entity.id).where(
            Entity.tenant_id == tenant_id, Entity.canonical_name.ilike(name)
        )
    ).first()
    return (row.kind, row.id) if row else None


def guess_speaker(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    title: str | None,
    description: str | None,
    source_title: str | None,
) -> SpeakerGuess:
    """Best validated tier-1 guess. Each raw candidate (highest-confidence first) is
    checked against this corpus's own entity table: a known non-person entity
    rejects it outright, a known PERSON entity confirms and links it, no match
    keeps the original modest confidence unvalidated either way.
    """
    for label, method, confidence in _raw_candidates(
        title=title or "", description=description or "", source_title=source_title
    ):
        found = _entity_kind_for(session, tenant_id=tenant_id, name=label)
        if found is not None:
            kind, entity_id = found
            if kind in _NON_PERSON_KINDS:
                continue  # known not to be a person — try the next candidate
            if kind == EntityKind.PERSON:
                return SpeakerGuess(label, method, min(confidence + 0.2, 0.95), entity_id)
        return SpeakerGuess(label, method, confidence)

    return SpeakerGuess(source_title or "unknown", AttributionMethod.UNKNOWN, 0.0)


def attribute_speakers(session: Session, *, tenant_id: uuid.UUID, limit: int | None = None) -> int:
    """Tier-1 speaker attribution for documents that don't have a speaker row yet.
    Returns the number of documents processed."""
    already_attributed = select(Speaker.document_id).where(Speaker.tenant_id == tenant_id)
    stmt = (
        select(Document.id, Document.title, Document.description, Source.title)
        .join(Source, Source.id == Document.source_id)
        .where(Document.tenant_id == tenant_id, Document.id.notin_(already_attributed))
    )
    if limit:
        stmt = stmt.limit(limit)
    rows = session.execute(stmt).all()

    for document_id, title, description, source_title in rows:
        guess = guess_speaker(
            session,
            tenant_id=tenant_id,
            title=title,
            description=description,
            source_title=source_title,
        )
        session.add(
            Speaker(
                tenant_id=tenant_id,
                document_id=document_id,
                label=guess.label,
                canonical_entity_id=guess.canonical_entity_id,
                attribution_method=guess.method,
                confidence=guess.confidence,
            )
        )
    session.commit()
    return len(rows)
