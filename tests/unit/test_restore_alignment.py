"""Restored transcripts must keep their parent's segment boundaries and offsets.

This is the test for a bug that would have been invisible: restoration used to write
the whole document as one segment at offset_ms=0. Since chunking picks a document's
*latest* transcript version, restoring the corpus would have made every chunk report a
start_ms near zero — and a timestamp of 0 reads as a value, not as missing data, so
`search_with_timestamps` would have degraded corpus-wide without anything failing.
"""

from __future__ import annotations

from dataclasses import dataclass

from corpus.enrich.restore import _realign_to_segments


@dataclass(frozen=True)
class FakeSegment:
    text: str
    offset_ms: int
    duration_ms: int


SEGMENTS = [
    FakeSegment("so basically what we", 0, 1000),
    FakeSegment("are doing here is", 1000, 900),
    FakeSegment("building an agent", 1900, 1100),
]
RAW_WORDS = sum(len(s.text.split()) for s in SEGMENTS)


def test_segment_count_and_offsets_are_preserved():
    restored = "So basically, what we are doing here is building an agent."
    out = _realign_to_segments(restored, SEGMENTS, 3000)

    assert len(out) == len(SEGMENTS)
    assert [s.offset_ms for s in out] == [0, 1000, 1900]
    assert [s.duration_ms for s in out] == [1000, 900, 1100]
    assert [s.idx for s in out] == [0, 1, 2]


def test_words_are_cut_at_the_original_boundaries():
    restored = "So basically, what we are doing here is building an agent."
    out = _realign_to_segments(restored, SEGMENTS, 3000)

    # Same word counts as the parent, so each segment still covers the audio its
    # offset claims it does.
    assert [len(s.text.split()) for s in out] == [len(s.text.split()) for s in SEGMENTS]
    assert out[0].text == "So basically, what we"
    assert out[2].text == "building an agent."


def test_punctuation_is_actually_carried_through():
    restored = "So basically, what we are doing here is building an agent."
    out = _realign_to_segments(restored, SEGMENTS, 3000)
    assert "".join(s.text for s in out) != "".join(s.text for s in SEGMENTS)
    assert out[0].text.startswith("So")  # truecased


def test_word_count_mismatch_falls_back_to_one_segment():
    """If the counts ever drift, losing timestamps is recoverable and obvious;
    silently misattributing them is neither. The fallback is the safe direction."""
    dropped_a_word = "So basically what we are doing here is building an"
    assert len(dropped_a_word.split()) != RAW_WORDS

    out = _realign_to_segments(dropped_a_word, SEGMENTS, 3000)
    assert len(out) == 1
    assert out[0].idx == 0
    assert out[0].offset_ms == 0
    assert out[0].duration_ms == 3000


def test_empty_segments_are_dropped_not_written_blank():
    segments = [FakeSegment("hello there", 0, 500), FakeSegment("", 500, 100)]
    out = _realign_to_segments("Hello there.", segments, 600)
    assert len(out) == 1
    assert out[0].text == "Hello there."
