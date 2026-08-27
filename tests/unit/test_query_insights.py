"""Pure logic in the query-log readings — the two judgements that would mislead.

Both properties tested here exist to stop a wrong conclusion being drawn from a real
number: an unfinished backfill read as a sourcing gap, and an empty log read as total
failure.
"""

from __future__ import annotations

import datetime as dt

from corpus.analytics.query_insights import BacklogItem, KeepReport


def make_item(indexed: int | None, total: int | None) -> BacklogItem:
    return BacklogItem(
        query_text="q",
        times_asked=3,
        times_weak=3,
        worst_grade="none",
        last_asked=dt.datetime(2026, 8, 27),
        indexed_documents=indexed,
        total_documents=total,
    )


class TestIndexArtefactDetection:
    """A weak grade computed against a half-built index is not a sourcing gap. This
    project has already shipped that mistake once: dense search ran over 3.9% of the
    corpus and confidently reported no coverage for a topic with 26 documents."""

    def test_partial_index_is_flagged(self):
        assert make_item(130, 3349).likely_index_artefact is True

    def test_complete_index_is_not_flagged(self):
        assert make_item(3349, 3349).likely_index_artefact is False

    def test_almost_complete_is_not_flagged(self):
        # 99% indexed — a real gap, not an artefact.
        assert make_item(3315, 3349).likely_index_artefact is False

    def test_just_below_threshold_is_flagged(self):
        assert make_item(3000, 3349).likely_index_artefact is True

    def test_unknown_completeness_does_not_claim_an_artefact(self):
        # Never coalesce unknown to a value — absence of the counts is not evidence
        # that the index was fine.
        assert make_item(None, None).likely_index_artefact is False
        assert make_item(100, None).likely_index_artefact is False


class TestHitRate:
    def test_none_when_nothing_graded(self):
        """None, not 0.0 — an empty log means 'no evidence', and 0.0 would read as
        'this corpus always fails'."""
        report = KeepReport(
            total_queries=5, graded_queries=0, answered_well=0, weak=0, since=None
        )
        assert report.hit_rate is None

    def test_ratio_is_over_graded_not_total(self):
        # 10 queries ran, only 4 produced a grade; 3 of those were answerable.
        report = KeepReport(
            total_queries=10, graded_queries=4, answered_well=3, weak=1, since=None
        )
        assert report.hit_rate == 0.75
