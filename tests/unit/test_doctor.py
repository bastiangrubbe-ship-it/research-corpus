"""Stage-status thresholds — the judgements that decide whether you get warned.

The threshold is the whole design question here. Too strict and the check cries wolf
on documents that legitimately have no transcript, gets ignored, and stops working as
a check. Too loose and it calls a half-built stage healthy, which is the failure this
module exists to catch.
"""

from __future__ import annotations

from corpus.ops.doctor import StageReport


def stage(done: int, total: int, status: str = "ok") -> StageReport:
    return StageReport(
        stage="s", done=done, total=total, status=status, impact="i", remedy="r"
    )


class TestShare:
    def test_share_is_a_fraction(self):
        assert stage(50, 100).share == 0.5

    def test_share_is_none_when_nothing_to_do(self):
        """None, not 0.0 or 1.0 — an empty stage has no completeness to report, and
        either number would be a claim the data does not support."""
        assert stage(0, 0).share is None

    def test_missing_never_goes_negative(self):
        # chunk_embedding can exceed its own chunk count mid-backfill across
        # partitions; a negative "missing" would be nonsense in the report.
        assert stage(120, 100).missing == 0


class TestStatusThresholds:
    """`_stage` assigns these; the values are asserted here as the contract the
    formatter and the CLI exit code both depend on."""

    def test_complete_enough_is_below_one(self):
        from corpus.ops.doctor import COMPLETE_ENOUGH

        # Deliberately not 1.0: a handful of documents legitimately have no
        # transcript, and a check that fires on those gets ignored.
        assert 0.9 < COMPLETE_ENOUGH < 1.0


class TestFormatting:
    def test_impact_shown_only_for_incomplete_stages(self):
        from corpus.ops.doctor import format_report

        out = format_report([stage(100, 100, "ok")])
        assert "impact" not in out.lower() or "i" not in out.split("\n")[1]
        assert "All stages complete." in out

    def test_incomplete_stage_names_its_impact_and_remedy(self):
        from corpus.ops.doctor import format_report

        out = format_report([stage(50, 100, "partial")])
        assert "50 missing" in out
        assert "fix: r" in out
        assert "looks decisive" in out

    def test_unknown_total_does_not_crash_the_formatter(self):
        from corpus.ops.doctor import format_report

        assert "??" in format_report([stage(0, 0, "unknown")])
