"""Synthesis internals that must hold without spending quota.

The two properties worth protecting here are the ones a reader cannot check for
themselves: that a citation marker points at the document it claims to, and that a
capped run is never presented as a complete one.
"""

from __future__ import annotations

import datetime as dt
import uuid

from corpus.synthesis.mapreduce import (
    _MARKER,
    Citation,
    SynthesisPlan,
    SynthesisReport,
    _format_findings,
)


def make_citation(marker: int, claim: str = "a claim", **kw) -> Citation:
    defaults = dict(
        document_id=uuid.uuid4(),
        title="A title",
        url="https://example.com",
        published_at=dt.date(2026, 3, 1),
        source_title="A source",
        quote="a quote",
    )
    defaults.update(kw)
    return Citation(marker=marker, claim=claim, **defaults)


class TestMarkerValidation:
    def test_marker_regex_finds_every_form_used_in_prose(self):
        text = "Claim one [1]. Claim two [2][13]. Not a marker [x]."
        assert [int(m) for m in _MARKER.findall(text)] == [1, 2, 13]

    def test_invalid_markers_are_detectable(self):
        citations = [make_citation(1), make_citation(2)]
        answer = "First [1], second [2], and a fabricated one [9]."
        valid = {c.marker for c in citations}
        used = {int(m) for m in _MARKER.findall(answer)}
        assert sorted(used - valid) == [9]


class TestFindingFormatting:
    def test_markers_and_dates_reach_the_prompt(self):
        out = _format_findings([make_citation(3, "ASML is a bottleneck")])
        assert "[3]" in out
        assert "2026-03-01" in out
        assert "A source" in out
        assert "ASML is a bottleneck" in out

    def test_quote_is_included_when_present(self):
        assert 'quote: "a quote"' in _format_findings([make_citation(1)])

    def test_missing_date_is_labelled_not_blank(self):
        out = _format_findings([make_citation(1, published_at=None)])
        assert "date unknown" in out

    def test_markers_are_not_renumbered_per_batch(self):
        """Hierarchical reduce keeps original markers; renumbering would make the
        same [3] mean a different document in each partial."""
        out = _format_findings([make_citation(41), make_citation(42)])
        assert "[41]" in out and "[42]" in out


class TestCapIsVisible:
    def test_plan_reports_what_a_cap_would_drop(self):
        plan = SynthesisPlan(
            matched_documents=1055,
            documents_to_read=60,
            escalated_documents=0,
            total_chars=1_953_066,
            capped=True,
        )
        assert plan.dropped_by_cap == 995

    def test_report_carries_matched_alongside_read_even_when_equal(self):
        """Always present, so a reader never has to know to check for a cap."""
        report = SynthesisReport(
            question="q", answer="a", matched_documents=12, documents_read=12
        )
        payload = report.to_dict()
        assert payload["matched_documents"] == 12
        assert payload["documents_read"] == 12
        assert payload["capped"] is False

    def test_to_dict_serializes_ids_and_dates(self):
        report = SynthesisReport(question="q", answer="a", citations=[make_citation(1)])
        payload = report.to_dict()
        citation = payload["citations"][0]
        assert isinstance(citation["document_id"], str)
        assert citation["published_at"] == "2026-03-01"
        uuid.UUID(citation["document_id"])  # round-trips
