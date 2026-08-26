"""Speaker attribution patterns — the cases that were wrong in production.

These are regression tests, not coverage. Each one corresponds to a real label this
corpus actually produced (docs/DECISIONS.md, 2026-08-26): running attribution over all
3,349 documents put 5 prose fragments in the `speaker.label` column, and the cause was
one flag.
"""

from __future__ import annotations

import pytest

from corpus.db.enums import AttributionMethod
from corpus.enrich.speakers import _raw_candidates


def labels(title="", description="", source_title=None) -> list[str]:
    return [label for label, _method, _conf in _raw_candidates(
        title=title, description=description, source_title=source_title
    )]


class TestIgnorecaseLeak:
    """re.IGNORECASE on the whole pattern also makes _NAME's [A-Z][a-z]+ match
    lowercase words, which removes the only thing distinguishing a name from prose.
    Every string here produced a real bogus speaker row."""

    @pytest.mark.parametrize(
        "description",
        [
            "and the one bottleneck both guests would remove by fiat is compute",
            "our guests argue that the biggest constraint is talent",
        ],
    )
    def test_lowercase_prose_after_guest_cue_is_not_a_name(self, description):
        assert labels(description=description) == []

    def test_lowercase_word_after_feat_cue_is_not_part_of_a_name(self):
        # Produced the label "Ryan Carson from" — the trailing "from" matched
        # [A-Z][a-z]+ only because the flag was global.
        assert labels(title="AI Agents ft. Ryan Carson from Treehouse") == ["Ryan Carson"]

    def test_cue_itself_stays_case_insensitive(self):
        # The flag was there for a reason; scoping it must not break that reason.
        for cue in ("Guest:", "guest:", "GUEST:"):
            assert labels(description=f"{cue} Dylan Field joins us") == ["Dylan Field"]


class TestNewlineSpanning:
    r"""\s+ crosses newlines and descriptions are full of them, so an outro line
    followed by a heading looked exactly like "First Last"."""

    def test_name_does_not_span_a_line_break(self):
        assert labels(description="check out my videos\n\nSpecial Thanks to everyone") == []

    def test_name_still_matches_across_a_plain_space(self):
        assert labels(description="Gabor Mayer is a PM at Google") == ["Gabor Mayer"]


class TestStillWorks:
    """The fix must not cost the matches the patterns were calibrated for."""

    def test_bio_pattern(self):
        assert labels(description="Eric Ries is the NYT bestselling author") == ["Eric Ries"]

    def test_particle_surname(self):
        # "Leon van Zyl" — the lowercase particle is why _PARTICLE exists.
        assert labels(description="Leon van Zyl is a developer") == ["Leon van Zyl"]

    def test_channel_named_after_a_person(self):
        got = _raw_candidates(title="", description="", source_title="Dan Martell")
        assert got and got[0][0] == "Dan Martell"
        assert got[0][1] is AttributionMethod.CHANNEL_DEFAULT

    def test_with_x_is_still_deliberately_not_a_guest(self):
        # In this AI-tutorial-heavy corpus "with X" names a tool, not a person.
        assert labels(title="How to Build a WhatsApp AI Agent with Claude Code") == []
