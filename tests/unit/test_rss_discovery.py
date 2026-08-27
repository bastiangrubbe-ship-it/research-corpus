"""The Shorts guard, which is the whole reason RSS discovery is not a drop-in.

Measured on @danmartell: 5 of 6 recent RSS entries were Shorts (58s, 28s, 55s, 28s,
31s). yt-dlp's /videos discovery excludes those; RSS does not and carries no duration
field. Nothing downstream filtered by duration before this — the Shorts exclusion
lived entirely in the choice of discovery URL — so swapping in RSS without a guard
would have started ingesting Shorts corpus-wide.
"""

from __future__ import annotations

import pytest

from corpus.sources.youtube.rss_discovery import (
    SHORTS_MAX_DURATION_S,
    is_probable_short,
)


class TestShortsGuard:
    @pytest.mark.parametrize("duration", [28, 31, 55, 58, 180])
    def test_real_measured_shorts_are_caught(self, duration):
        assert is_probable_short(duration) is True

    @pytest.mark.parametrize("duration", [181, 1109, 1588, 2908])
    def test_real_measured_uploads_pass(self, duration):
        assert is_probable_short(duration) is False

    def test_unknown_duration_is_not_treated_as_a_short(self):
        """None means the metadata call did not report a duration. That is not
        evidence of a Short, and refusing to fetch on missing metadata would silently
        drop real uploads — the same reason `is_auto_generated` is nullable."""
        assert is_probable_short(None) is False

    def test_boundary_is_inclusive(self):
        assert is_probable_short(SHORTS_MAX_DURATION_S) is True
        assert is_probable_short(SHORTS_MAX_DURATION_S + 1) is False


class TestFeedLimitIsDocumented:
    def test_entry_limit_matches_what_youtube_actually_returns(self):
        from corpus.sources.youtube.rss_discovery import FEED_ENTRY_LIMIT

        # YouTube's channel feed returns ~15 entries and offers no paging. A channel
        # that posted more than this since the last run loses the older ones, which is
        # why yt-dlp has to stay available for backfill.
        assert FEED_ENTRY_LIMIT == 15
