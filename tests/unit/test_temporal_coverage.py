"""Coverage has a shape in time, not just a size.

`span_days` was the only temporal signal and cannot tell sustained coverage apart from
two clusters eighteen months apart — both report the same span. For a corpus whose
stated value is comparative and temporal, that is measuring the wrong thing: a topic
can be intensely covered for a fortnight and then fade, and the corpus genuinely holds
good coverage *of that fortnight* and nothing about now.
"""

from __future__ import annotations

import datetime as dt

from corpus.analytics.coverage import build_temporal_shape

AS_OF = dt.date(2026, 8, 27)
STALE = 120


def dated(*pairs):
    return [(dt.date(y, m, d), src) for y, m, d, src in pairs]


def spread(months, per_month=2, year=2026, src="s"):
    out = []
    for m in months:
        for i in range(per_month):
            out.append((dt.date(year, m, 1 + i), f"{src}{i}"))
    return out


class TestPatterns:
    def test_burst_is_one_month_holding_half(self):
        """The case that motivated this: a topic hot for a fortnight."""
        items = spread([8], per_month=9) + spread([6, 7], per_month=1)
        shape = build_temporal_shape(items, as_of=AS_OF, stale_after_days=STALE)
        assert shape.pattern == "burst"
        assert shape.peak_share >= 0.5
        assert shape.is_concentrated

    def test_faded_is_concentrated_and_old(self):
        items = spread([1, 2], per_month=5, year=2025)
        shape = build_temporal_shape(items, as_of=AS_OF, stale_after_days=STALE)
        assert shape.pattern == "faded"
        assert shape.days_since_latest > STALE
        assert shape.is_concentrated

    def test_sustained_is_spread_out(self):
        items = spread([1, 2, 3, 4, 5, 6, 7], per_month=3)
        shape = build_temporal_shape(items, as_of=AS_OF, stale_after_days=STALE)
        assert shape.pattern == "sustained"
        assert not shape.is_concentrated

    def test_sparse_refuses_to_claim_a_pattern(self):
        """Below a handful of documents the distribution is noise."""
        shape = build_temporal_shape(
            dated((2026, 3, 1, "a"), (2026, 5, 2, "b")), as_of=AS_OF, stale_after_days=STALE
        )
        assert shape.pattern == "sparse"

    def test_empty_is_sparse_not_a_crash(self):
        shape = build_temporal_shape([], as_of=AS_OF, stale_after_days=STALE)
        assert shape.pattern == "sparse"
        assert shape.buckets == []
        assert shape.days_since_latest is None


class TestGaps:
    def test_quiet_months_inside_the_span_are_counted(self):
        """Two clusters far apart report a wide span; the gap is the real finding."""
        items = spread([1], per_month=4, year=2025) + spread([8], per_month=4)
        shape = build_temporal_shape(items, as_of=AS_OF, stale_after_days=STALE)
        assert shape.active_months == 2
        assert shape.quiet_months >= 17
        assert shape.span_months >= 19

    def test_contiguous_coverage_has_no_quiet_months(self):
        shape = build_temporal_shape(
            spread([4, 5, 6, 7]), as_of=AS_OF, stale_after_days=STALE
        )
        assert shape.quiet_months == 0


class TestBuckets:
    def test_buckets_are_monthly_and_ordered(self):
        shape = build_temporal_shape(spread([3, 1, 2]), as_of=AS_OF, stale_after_days=STALE)
        assert [b.period.month for b in shape.buckets] == [1, 2, 3]
        assert all(b.period.day == 1 for b in shape.buckets)

    def test_distinct_sources_counted_per_bucket(self):
        items = dated(
            (2026, 5, 1, "a"), (2026, 5, 2, "a"), (2026, 5, 3, "b"), (2026, 6, 1, "c")
        )
        shape = build_temporal_shape(items, as_of=AS_OF, stale_after_days=STALE)
        may = shape.buckets[0]
        assert may.n_documents == 3
        assert may.n_sources == 2, "same source twice in a month is one source"
