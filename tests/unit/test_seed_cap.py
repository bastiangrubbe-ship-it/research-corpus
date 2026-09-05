"""The seed-table video cap must fire regardless of how the note was capitalised.

Regression: `cap_for` matched the literal "CAP at" case-sensitively. It was written
for two rows spelled that way; every row added afterwards spelled it "cap at", so 15
of 17 caps silently did nothing. Nothing failed -- the ingest just paid for the whole
back catalogue of channels holding 1,000-2,400 videos each.
"""

from corpus.ingest.runner import cap_for


def test_lowercase_cap_is_honoured():
    assert cap_for({"note": "cap at 400 most recent — SaaS founder interviews"}) == 400


def test_legacy_uppercase_cap_still_honoured():
    assert cap_for({"note": "CAP at 400 most recent — legacy row"}) == 400


def test_cap_found_after_other_note_text():
    note = "reclassified to performance_marketing 2026-08-27 — cap at 250 most recent"
    assert cap_for({"note": note}) == 250


def test_no_cap_returns_none():
    assert cap_for({"note": "Established entrepreneurship interview podcast"}) is None
    assert cap_for({"note": ""}) is None
    assert cap_for({}) is None
    assert cap_for({"note": None}) is None
