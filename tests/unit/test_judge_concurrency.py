"""judge_pool runs its LLM calls concurrently, and fails safely.

It used to be a serial `for` loop, which on this corpus meant one eval query took six
hours: ~55 candidates, each a full-transcript `claude -p` call, one after another.
"""

from __future__ import annotations

import threading
import time
import uuid

import pytest

from corpus.eval import judge as judge_mod
from corpus.eval.judge import Judgment, Verdict, judge_pool


class FakeCandidate:
    def __init__(self, title="t"):
        self.document_id = uuid.uuid4()
        self.title = title


@pytest.fixture
def cache_dir(tmp_path):
    return tmp_path


def test_calls_run_concurrently(monkeypatch, cache_dir):
    """Ten 0.2s calls finish in well under the 2s a serial loop would take."""
    in_flight, peak, lock = 0, [0], threading.Lock()

    def slow_judge(query_text, *, document_id, title, text, model=None):
        nonlocal in_flight
        with lock:
            in_flight += 1
            peak[0] = max(peak[0], in_flight)
        time.sleep(0.2)
        with lock:
            in_flight -= 1
        return Judgment(str(document_id), Verdict.RELEVANT, "r")

    monkeypatch.setattr(judge_mod, "judge_one", slow_judge)
    candidates = [FakeCandidate() for _ in range(10)]

    started = time.monotonic()
    out = judge_pool("q", "query", candidates, cache_dir=cache_dir,
                     fetch_text=lambda d: "text", concurrency=10)
    elapsed = time.monotonic() - started

    assert len(out) == 10
    assert peak[0] > 1, "calls did not overlap — judging is still serial"
    assert elapsed < 1.5, f"took {elapsed:.2f}s; serial would be ~2s"


def test_a_failing_candidate_is_skipped_not_invented(monkeypatch, cache_dir):
    """An unjudged document is a known gap; a fabricated verdict silently corrupts
    ground truth. So a failure must drop the candidate, not substitute a value."""
    def flaky(query_text, *, document_id, title, text, model=None):
        if title == "bad":
            raise judge_mod.JudgeCallError("boom")
        return Judgment(str(document_id), Verdict.RELEVANT, "r")

    monkeypatch.setattr(judge_mod, "judge_one", flaky)
    candidates = [FakeCandidate("ok"), FakeCandidate("bad"), FakeCandidate("ok")]
    out = judge_pool("q", "query", candidates, cache_dir=cache_dir,
                     fetch_text=lambda d: "text", concurrency=4)

    assert len(out) == 2, "the failed candidate should be absent, not defaulted"


def test_cached_candidates_are_not_rejudged(monkeypatch, cache_dir):
    calls = []

    def counting(query_text, *, document_id, title, text, model=None):
        calls.append(document_id)
        return Judgment(str(document_id), Verdict.RELEVANT, "r")

    monkeypatch.setattr(judge_mod, "judge_one", counting)
    candidates = [FakeCandidate() for _ in range(3)]
    judge_pool("q", "query", candidates, cache_dir=cache_dir, fetch_text=lambda d: "t")
    assert len(calls) == 3

    judge_pool("q", "query", candidates, cache_dir=cache_dir, fetch_text=lambda d: "t")
    assert len(calls) == 3, "cached judgments were re-judged"


def test_fetch_text_runs_on_the_calling_thread(monkeypatch, cache_dir):
    """Callers pass a closure over an open SQLAlchemy Session, which is not
    thread-safe. Reading transcripts inside the pool would be a race that usually
    appears to work."""
    main = threading.get_ident()
    seen = []

    monkeypatch.setattr(
        judge_mod, "judge_one",
        lambda q, *, document_id, title, text, model=None: Judgment(
            str(document_id), Verdict.RELEVANT, "r"),
    )

    def fetch(doc_id):
        seen.append(threading.get_ident())
        return "text"

    judge_pool("q", "query", [FakeCandidate() for _ in range(5)],
               cache_dir=cache_dir, fetch_text=fetch, concurrency=5)
    assert set(seen) == {main}, "fetch_text ran off the main thread"
