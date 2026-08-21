"""Normalizers asserted against responses recorded from the live APIs.

These fixtures come from real calls (scripts/record_supadata.py and a real
youtube-transcript-api fetch), not from the OpenAPI spec. A spec tells you the
declared shape; only the service tells you what it actually sends.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from corpus.db.enums import ProvenanceConfidence
from corpus.sources.youtube.supadata import _to_segments as supadata_segments
from corpus.sources.youtube.ytapi import _to_segments as ytapi_segments

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "responses"


def _load(name: str) -> dict:
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"{name} not recorded; run scripts/record_supadata.py")
    return json.loads(path.read_text())


def test_supadata_offsets_are_milliseconds_and_pass_through() -> None:
    data = _load("supadata_captions_present.json")
    content = data["payload"]["content"]
    segments = supadata_segments(content)

    assert segments[0].offset_ms == content[0]["offset"], "supadata ms must not be rescaled"
    assert segments[0].duration_ms == content[0]["duration"]
    assert segments[0].text == content[0]["text"]
    assert [s.idx for s in segments] == list(range(len(segments)))


def test_ytapi_seconds_convert_to_milliseconds() -> None:
    data = _load("ytapi_both_manual_and_generated.json")
    snippets = data["snippets"]
    segments = ytapi_segments(snippets)

    assert segments[0].offset_ms == round(snippets[0]["start"] * 1000)
    assert segments[0].duration_ms == round(snippets[0]["duration"] * 1000)


def test_both_providers_agree_on_the_same_video() -> None:
    """Cross-validation. The two normalizers are independent code paths with
    different input units; agreeing on the same video is strong evidence both are
    right, in a way that asserting either against itself is not.
    """
    sd = _load("supadata_captions_present.json")
    yt = _load("ytapi_both_manual_and_generated.json")

    sd_first = supadata_segments(sd["payload"]["content"])[0]
    yt_first = ytapi_segments(yt["snippets"])[0]

    assert sd_first.text == yt_first.text
    assert sd_first.offset_ms == yt_first.offset_ms
    assert sd_first.duration_ms == yt_first.duration_ms


def test_supadata_records_no_provenance() -> None:
    """The defining limitation of this provider, pinned so a future refactor cannot
    quietly start claiming knowledge it does not have."""
    data = _load("supadata_captions_present.json")
    normalized = data["normalized"]

    assert normalized["is_auto_generated"] is None
    assert normalized["provenance_confidence"] == ProvenanceConfidence.UNKNOWN.value
    assert set(data["payload"]) <= {"content", "lang", "availableLangs", "_content_truncated_from"}


def test_ytapi_reports_both_manual_and_generated_english() -> None:
    """Why provenance is part of the selection key rather than a label applied after.

    Two 'en' tracks exist for this video with different provenance. Selecting by
    language alone returns whichever is listed first.
    """
    data = _load("ytapi_both_manual_and_generated.json")
    english = [t for t in data["available"] if t["language_code"] == "en"]

    assert len(english) == 2
    assert {t["is_generated"] for t in english} == {True, False}


def test_providers_disagree_on_language_code_format() -> None:
    """Recorded discrepancy, asserted so it is not mistaken for a bug later.

    Supadata normalises to two-letter codes ('de', 'pt'); youtube-transcript-api
    preserves regional codes ('de-DE', 'pt-BR'). available_langs therefore depends on
    which provider produced the row, and any query filtering on it must account for
    that.
    """
    sd = set(_load("supadata_captions_present.json")["payload"]["availableLangs"])
    yt = {t["language_code"] for t in _load("ytapi_both_manual_and_generated.json")["available"]}

    assert "de" in sd and "de-DE" in yt
    assert sd != yt


def test_error_cases_map_to_the_right_exception() -> None:
    """A 404 means nobody has it; a 400 means we asked wrongly. Only the first is a
    reason to stop, and neither is a reason to fail over."""
    assert _load("supadata_unavailable.json")["error"] == "TranscriptUnavailable"
    assert _load("supadata_wrong_language.json")["error"] == "InvalidRequest"
