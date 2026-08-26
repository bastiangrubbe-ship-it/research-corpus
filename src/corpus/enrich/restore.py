"""Punctuation restoration for raw ASR transcripts — `oliverguhr/fullstop-
punctuation-multilang-large`, the model the build plan named for this step.

Not needed for entity extraction: Claude's extractor already handles raw,
unpunctuated ASR text fine (docs/EVAL.md measured ~0.90 F1 with no restoration at
all) — that was this step's original justification, written against a GLiNER-based
extractor that never shipped. What actually still needs this: sentence-boundary-
dependent processing. `corpus.enrich.summarize.summarize_extractive` tokenizes into
sentences via NLTK before running TextRank, and sentence tokenization has nothing to
work with on text with no periods or question marks at all — this is the real,
still-live reason to build this now.

Produces a new `transcript_version` row (`provider=RESTORED`,
`derived_from_id` pointing at the raw parent) plus its own `segment` rows — the raw
transcript is never mutated, both stay queryable, per the schema's own design.

Only punctuation is model-based here. Truecasing is a simple sentence-boundary
heuristic (capitalize the first word of the text and the word after each `.`/`?`) —
real truecasing (proper nouns mid-sentence) needs its own model or an NER pass and is
a separate, larger task, not attempted here rather than faked with a heuristic that
would silently under-deliver on its own name.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from corpus.config import get_settings
from corpus.db.enums import TranscriptProvider
from corpus.db.models import Segment, TranscriptVersion

MODEL_NAME = "oliverguhr/fullstop-punctuation-multilang-large"
_SENTENCE_END_LABELS = {".", "?"}
_WORDS_PER_CHUNK = 200  # this model's practical context; chunked independently,
# so restoration quality dips slightly at each chunk boundary — acceptable for a
# tier-1 heuristic step, not for anything treated as ground truth.


def _configure_hf_cache() -> None:
    import os

    cache_dir = str(get_settings().cache_dir / "huggingface")
    os.makedirs(cache_dir, exist_ok=True)
    os.environ.setdefault("HF_HOME", cache_dir)


@lru_cache(maxsize=1)
def _get_pipeline():
    _configure_hf_cache()
    from transformers import pipeline

    return pipeline("token-classification", model=MODEL_NAME)


def _merge_into_words(tokens: list[dict]) -> list[tuple[str, str]]:
    """(word, punctuation_label) pairs — this model's tokenizer marks the start of
    each real word with a SentencePiece '▁' prefix; tokens without it are
    continuations of the previous word. The last sub-token's label is the word's
    punctuation call, matching how this model was trained to be read."""
    words: list[tuple[str, str]] = []
    current_word = ""
    current_label = "0"
    for tok in tokens:
        piece = tok["word"]
        if piece.startswith("▁"):
            if current_word:
                words.append((current_word, current_label))
            current_word = piece[1:]
        else:
            current_word += piece
        current_label = tok["entity"]
    if current_word:
        words.append((current_word, current_label))
    return words


def _reconstruct(words: list[tuple[str, str]]) -> str:
    out = []
    capitalize_next = True
    for word, label in words:
        if capitalize_next and word:
            word = word[0].upper() + word[1:]
            capitalize_next = False
        # Don't double up on punctuation the source already had. Most of this
        # corpus is unpunctuated ASR, but not all of it — a partly-punctuated
        # transcript otherwise comes back with "NVIDIA DRIVE.." where the model
        # predicted a stop the text had already written.
        punct = "" if (label == "0" or word[-1:] in ".,?-:") else label
        out.append(word + punct)
        if label in _SENTENCE_END_LABELS:
            capitalize_next = True
    return " ".join(out)


def restore_punctuation(text: str) -> str:
    """Punctuation-restored, sentence-boundary-truecased text. Empty input returns
    empty output rather than raising."""
    if not text.strip():
        return ""

    pipe = _get_pipeline()
    words = text.split()
    restored_chunks = []
    for i in range(0, len(words), _WORDS_PER_CHUNK):
        chunk_text = " ".join(words[i : i + _WORDS_PER_CHUNK])
        tokens = pipe(chunk_text)
        restored_chunks.append(_reconstruct(_merge_into_words(tokens)))
    return " ".join(restored_chunks)


@dataclass(frozen=True, slots=True)
class _RestoredSegment:
    idx: int
    text: str
    offset_ms: int
    duration_ms: int


def _realign_to_segments(
    restored_text: str, segment_rows, total_duration_ms: int
) -> list[_RestoredSegment]:
    """Spread restored text back over the parent's segment boundaries, keeping each
    segment's original offset_ms and duration_ms.

    Restoration is punctuation and capitalization only — it never inserts or deletes a
    word — so the restored word sequence lines up 1:1 with the input and can be cut at
    the same points. Restoring the document as one string first and re-cutting
    afterwards is deliberate: restoring segment by segment would give the model a few
    seconds of speech at a time and no sentence context across a boundary, which is
    where punctuation decisions are hardest.

    This has to preserve segments because everything downstream picks a document's
    *latest* transcript version (`DISTINCT ON (document_id) ... created_at DESC` in
    chunking, the relevance gate, and entity extraction alike). An earlier version
    wrote the whole restored document as one segment at offset_ms=0; that made every
    chunk built from a restored version report a start_ms near zero, so
    `search_with_timestamps` would have degraded corpus-wide the moment restoration
    ran broadly — quietly, since a timestamp of 0 looks like a value rather than a
    missing one.

    If the word counts ever fail to line up, fall back to one segment spanning the
    document rather than distributing text against offsets it does not match: losing
    timestamps is recoverable and obvious, misattributed timestamps are neither.

    The known cause of a mismatch is `<unk>`: where the transcript holds a character
    outside the model's vocabulary, the tokenizer emits an unknown token and
    reconstruction splits the word around it, so `Roo<unk>3` comes back as
    `Roo<unk> 3` and the count grows. Measured over the full corpus this fired on 4 of
    3,269 versions (0.12%). Left to the fallback rather than fixed with a
    sequence-alignment pass: those documents keep their timestamps in the raw parent
    version, which is never mutated, so the loss is confined to the derived copy.
    """
    restored_words = restored_text.split()
    original_counts = [len(row.text.split()) for row in segment_rows]

    if sum(original_counts) != len(restored_words):
        return [_RestoredSegment(0, restored_text, 0, total_duration_ms)]

    out: list[_RestoredSegment] = []
    cursor = 0
    for idx, (row, count) in enumerate(zip(segment_rows, original_counts, strict=True)):
        text = " ".join(restored_words[cursor : cursor + count])
        cursor += count
        if text:
            out.append(_RestoredSegment(idx, text, row.offset_ms, row.duration_ms))
    return out


def restore_transcript_version(
    session: Session, *, tenant_id: uuid.UUID, transcript_version_id: uuid.UUID
) -> uuid.UUID | None:
    """Restores one transcript_version's segments into a new derived version.
    Returns the new version's id, or None if the source has no text to restore.
    """
    parent = session.execute(
        select(TranscriptVersion).where(
            TranscriptVersion.id == transcript_version_id, TranscriptVersion.tenant_id == tenant_id
        )
    ).scalar_one()

    segment_rows = session.execute(
        select(Segment.text, Segment.offset_ms, Segment.duration_ms)
        .where(Segment.transcript_version_id == transcript_version_id)
        .order_by(Segment.idx)
    ).all()
    full_text = " ".join(row.text for row in segment_rows)
    if not full_text.strip():
        return None
    total_duration_ms = (
        max(row.offset_ms + row.duration_ms for row in segment_rows) if segment_rows else 0
    )

    restored_text = restore_punctuation(full_text)

    new_version = TranscriptVersion(
        tenant_id=tenant_id,
        document_id=parent.document_id,
        provider=TranscriptProvider.RESTORED,
        is_auto_generated=parent.is_auto_generated,
        provenance_confidence=parent.provenance_confidence,
        lang=parent.lang,
        available_langs=parent.available_langs,
        derived_from_id=parent.id,
    )
    session.add(new_version)
    session.flush()  # need new_version.id before inserting its segments

    for segment in _realign_to_segments(restored_text, segment_rows, total_duration_ms):
        session.add(
            Segment(
                tenant_id=tenant_id,
                transcript_version_id=new_version.id,
                idx=segment.idx,
                text=segment.text,
                offset_ms=segment.offset_ms,
                duration_ms=segment.duration_ms,
            )
        )
    session.commit()
    return new_version.id
