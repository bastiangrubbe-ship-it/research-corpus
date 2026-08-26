"""Split a transcript into overlapping windows built from whole segments.

**Naming, deliberately not `late.py`.** The build plan specified a `chunking/late.py`
implementing late chunking — embed the full document with a long-context model, then
pool token embeddings per chunk so each chunk carries document context. That
technique needs the whole document to fit the encoder: `nomic-embed-text-v1.5` takes
8,192 tokens, and this corpus's median transcript is ~18,800 characters with a
maximum near 1,000,000. Late chunking is therefore not applicable to most documents
here, and a file named `late.py` containing ordinary windowing would be a lie in the
filename. If a long-context encoder is adopted later, real late chunking goes in
`late.py` beside this.

What this does instead: group consecutive `segment` rows into windows of roughly
`target_chars`, overlapping by `overlap_chars`, never splitting a segment. Two
consequences worth having:

* **Timestamps survive.** Each window inherits `start_ms` from its first segment and
  `end_ms` from its last, so a chunk hit can cite a position in the video rather than
  just naming the document.
* **No mid-sentence splits at ASR boundaries.** Segments are the provider's own
  units; respecting them avoids cutting through a phrase, which matters here because
  much of this corpus has no sentence punctuation to fall back on.

Overlap exists because a window boundary that lands mid-topic otherwise hides that
topic from both neighbours — the retrieval equivalent of a seam.
"""

from __future__ import annotations

from dataclasses import dataclass

#: ~400 tokens at roughly 4 characters per token, the size the build plan assumed.
#: Large enough to hold an argument, small enough that its embedding isn't an average
#: of several unrelated ones — the failure mode that makes whole-document vectors
#: weak in the first place.
TARGET_CHARS = 1600

#: ~10% of a window. Enough to carry a thought across a seam without materially
#: inflating the number of vectors.
OVERLAP_CHARS = 160


@dataclass(frozen=True, slots=True)
class SegmentInput:
    text: str
    offset_ms: int | None
    duration_ms: int | None


@dataclass(frozen=True, slots=True)
class Window:
    idx: int
    text: str
    start_ms: int | None
    end_ms: int | None
    #: Rough — `len(text) // 4`. Stored for capacity planning, never used to make a
    #: retrieval decision, so an approximation is honest here rather than lazy.
    token_estimate: int


def build_windows(
    segments: list[SegmentInput],
    *,
    target_chars: int = TARGET_CHARS,
    overlap_chars: int = OVERLAP_CHARS,
) -> list[Window]:
    """Consecutive windows over `segments`. Empty input yields no windows.

    A single segment longer than `target_chars` becomes its own oversized window
    rather than being split: segment boundaries are the one structural signal this
    corpus's unpunctuated text reliably has, and breaking them to hit a size target
    would trade real structure for a tidy number.
    """
    usable = [s for s in segments if s.text and s.text.strip()]
    if not usable:
        return []

    windows: list[Window] = []
    current: list[SegmentInput] = []
    current_len = 0

    def flush() -> None:
        nonlocal current, current_len
        if not current:
            return
        text = " ".join(s.text.strip() for s in current)
        start = next((s.offset_ms for s in current if s.offset_ms is not None), None)
        end = None
        for s in reversed(current):
            if s.offset_ms is not None:
                end = s.offset_ms + (s.duration_ms or 0)
                break
        windows.append(
            Window(
                idx=len(windows),
                text=text,
                start_ms=start,
                end_ms=end,
                token_estimate=len(text) // 4,
            )
        )

        # Seed the next window with the tail of this one, by whole segments.
        carried: list[SegmentInput] = []
        carried_len = 0
        for s in reversed(current):
            seg_len = len(s.text) + 1
            if carried_len + seg_len > overlap_chars:
                break
            carried.insert(0, s)
            carried_len += seg_len
        current = carried
        current_len = carried_len

    for segment in usable:
        seg_len = len(segment.text) + 1
        if current and current_len + seg_len > target_chars:
            flush()
        current.append(segment)
        current_len += seg_len

    # Final flush, but only if the tail holds something the previous window didn't
    # already cover — otherwise the carried overlap alone would become a duplicate.
    if current and (not windows or current_len > overlap_chars):
        text = " ".join(s.text.strip() for s in current)
        start = next((s.offset_ms for s in current if s.offset_ms is not None), None)
        end = None
        for s in reversed(current):
            if s.offset_ms is not None:
                end = s.offset_ms + (s.duration_ms or 0)
                break
        windows.append(
            Window(
                idx=len(windows),
                text=text,
                start_ms=start,
                end_ms=end,
                token_estimate=len(text) // 4,
            )
        )

    return windows
