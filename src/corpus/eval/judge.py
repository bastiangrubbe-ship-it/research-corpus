"""LLM-assisted relevance judging, with a persistent on-disk cache — this is what
makes "re-running an unchanged corpus reproduces scores exactly" (the plan's step 7
verification standard) actually true. `claude -p` judgments are not perfectly
deterministic across calls; a cached judgment is. Once a (query, document) pair is
judged, it stays judged until the cache file is deleted or `force=True`.

Same conflict-of-interest caveat this project already states for entity extraction
(docs/EVAL.md) and the relevance gate (docs/EVAL_RELEVANCE_GATE.md): the judge here is
Claude, evaluating output that other Claude calls (extraction, reranking) produced.
Not a substitute for an independent human check — `EvalJudgment.verified_by` records
"llm_assisted" precisely so a later audit pass can find and re-check these, not so the
label blends in as if it were unquestioned ground truth.
"""

from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path

import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from corpus.eval.pool import PooledCandidate
from corpus.llm.headless import ClaudeCliError, run_claude_headless

log = structlog.get_logger(__name__)

JUDGE_MODEL = "haiku"
JUDGE_TIMEOUT_S = 600.0

#: Above this transcript size, judge with `JUDGE_MODEL_LARGE` instead.
#:
#: Two separate reasons, and only the first is a hard limit:
#:
#: 1. **Context.** Haiku takes 200k tokens (~800k characters). Exactly one transcript
#:    in this corpus (995,860 chars, ~249k tokens) does not fit at all.
#: 2. **Long-context recall.** Well below the hard ceiling, finding one relevant
#:    passage inside tens of thousands of tokens is a harder task than judging a
#:    short document, and it is precisely the case where a wrong "irrelevant" verdict
#:    is most likely — the needle is there, buried.
#:
#: 200,000 characters (~50k tokens) covers **25 documents, 0.76% of the corpus**, so
#: the escalation is bounded and cheap while putting the strongest model exactly
#: where judgments are hardest to get right.
LARGE_DOCUMENT_CHARS = 200_000

#: 1M-token context, so even the 249k-token outlier fits with room to spare.
JUDGE_MODEL_LARGE = "opus"

#: The judge reads the **whole transcript**. There is deliberately no truncation
#: constant here any more.
#:
#: There were two, and both were wrong for the same reason. 6,000 characters meant
#: judging most documents on their opening 10-30% — their introduction — so a video
#: addressing the query twenty minutes in was marked irrelevant on evidence that
#: could not possibly have shown it. Raising it to 30,000 narrowed the problem
#: without fixing it. Any cap manufactures false negatives in one direction only,
#: and is the most likely explanation for the judge-audit pattern in
#: docs/EVAL_RELEVANCE_GATE.md where every disagreement was `irrelevant → marginal`
#: and never the reverse.
#:
#: The point of a ground truth is to be more thorough than the thing it grades. A
#: judge reading less of the document than the retrieval system indexes cannot serve
#: as ground truth for that system.
#:
#: Deliberately *not* solved by feeding the judge the best-matching chunks: that
#: would show the judge exactly the passages retrieval already liked, and an
#: evaluation that grades retrieval using retrieval's own notion of relevance
#: measures nothing at all.

_PROMPT_TEMPLATE = """\
You are judging whether a document is relevant to a research query, for a retrieval \
evaluation. Be skeptical rather than generous — default to "irrelevant" unless the \
document's actual content, not just its title, genuinely addresses the query.

Query: {query}

Document title: {title}

Document text (transcript excerpt):
{text}

Respond with ONLY a JSON object of this exact shape, no markdown fence, no prose \
before or after it:
{{"verdict": "relevant" | "marginal" | "irrelevant", "reasoning": str}}

"reasoning" is one sentence explaining the verdict from the document's actual \
content, not its title.
"""


class Verdict(StrEnum):
    RELEVANT = "relevant"
    MARGINAL = "marginal"
    IRRELEVANT = "irrelevant"


@dataclass(frozen=True, slots=True)
class Judgment:
    document_id: str
    verdict: Verdict
    reasoning: str
    verified_by: str = "llm_assisted"


class JudgeCallError(Exception):
    """The `claude` CLI invocation itself failed. Retried; see entities.py's
    identically-named error for why this is the retried class and schema errors
    aren't."""


class JudgeParseError(Exception):
    """Claude Code returned output that isn't valid, schema-matching JSON. Not
    retried — a schema mismatch is a prompt problem, not a transient one."""


@retry(
    retry=retry_if_exception_type(JudgeCallError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    reraise=True,
)
def _run_claude_code(prompt: str, *, model: str, timeout_s: float) -> str:
    """Delegates to corpus.llm.headless. This used to be its own copy of the
    subprocess call, and it reported `proc.stderr` only -- so every judge failure
    surfaced as `claude CLI exited 1:` with nothing after the colon. entities.py had
    already diagnosed and fixed exactly that, but the fix never crossed over; the
    shared runner is what stops the two from drifting again.
    """
    try:
        return run_claude_headless(prompt, model=model, timeout_s=timeout_s)
    except ClaudeCliError as exc:
        raise JudgeCallError(str(exc)) from exc


def _strip_fence(text: str) -> str:
    import re

    text = text.strip()
    if text.startswith("```"):
        match = re.match(r"^```[a-zA-Z]*\n?(.*?)\n?```", text, re.DOTALL)
        if match:
            return match.group(1).strip()
    return text


def judge_one(
    query_text: str,
    *,
    document_id: uuid.UUID,
    title: str | None,
    text: str,
    model: str | None = None,
) -> Judgment:
    """Judge one document against one query, reading the document in full.

    Model is chosen by document size unless `model` is given explicitly: Haiku for
    the 99%+ that are short, Opus for anything over `LARGE_DOCUMENT_CHARS` — those
    both exceed Haiku's context in the extreme case and are the documents where a
    buried-needle miss is most likely. `verified_by` records which model judged,
    so a later audit can tell whether a verdict came from the cheap or the strong
    path rather than having to guess.
    """
    is_large = len(text) > LARGE_DOCUMENT_CHARS
    chosen = model or (JUDGE_MODEL_LARGE if is_large else JUDGE_MODEL)

    prompt = _PROMPT_TEMPLATE.format(query=query_text, title=title or "(untitled)", text=text)
    stdout = _run_claude_code(prompt, model=chosen, timeout_s=JUDGE_TIMEOUT_S)
    try:
        envelope = json.loads(stdout)
        payload = json.loads(_strip_fence(envelope.get("result", "")))
        return Judgment(
            document_id=str(document_id),
            verdict=Verdict(payload["verdict"]),
            reasoning=payload["reasoning"],
            verified_by=f"llm_assisted:{chosen}",
        )
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        raise JudgeParseError(f"unparseable judge output: {stdout[:300]!r}") from exc


def _cache_path(cache_dir: Path, query_id: str) -> Path:
    return cache_dir / f"{query_id}.json"


def load_cache(cache_dir: Path, query_id: str) -> dict[str, Judgment]:
    path = _cache_path(cache_dir, query_id)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {
        doc_id: Judgment(
            document_id=doc_id,
            verdict=Verdict(row["verdict"]),
            reasoning=row["reasoning"],
            verified_by=row.get("verified_by", "llm_assisted"),
        )
        for doc_id, row in raw.items()
    }


def save_cache(cache_dir: Path, query_id: str, judgments: dict[str, Judgment]) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(cache_dir, query_id)
    path.write_text(json.dumps({k: asdict(v) for k, v in judgments.items()}, indent=2))


#: Parallel `claude -p` judge calls. Matches what entity extraction measured safe
#: (up to 25, near-linear speedup, zero errors — docs/DECISIONS.md); 12 is the
#: conservative default because a judge call sends a whole untruncated transcript and
#: escalates large ones to Opus, so each is heavier than an extraction call.
DEFAULT_JUDGE_CONCURRENCY = 12


def judge_pool(
    query_id: str,
    query_text: str,
    candidates: list[PooledCandidate],
    *,
    cache_dir: Path,
    fetch_text: callable,
    force: bool = False,
    concurrency: int = DEFAULT_JUDGE_CONCURRENCY,
) -> dict[str, Judgment]:
    """Judges every candidate not already cached, then persists the merged result.
    `fetch_text(document_id) -> str` is injected rather than imported directly so
    this module has no database dependency of its own — callers already have a
    session open and know how to reconstruct a document's text.

    Judging runs concurrently. It used to be a serial loop, which on this corpus meant
    a single query took **six hours**: ~55 candidates, each a full-transcript
    `claude -p` call, one after another (docs/DECISIONS.md, 2026-08-27). The calls are
    independent and network-bound, so this is the same shape as
    `enrich_documents_concurrent`.

    `fetch_text` is called on the main thread, deliberately. Callers pass a closure
    over an open SQLAlchemy session, and a Session is not thread-safe — reading
    transcripts inside the pool would be a data race that usually appears to work.
    Only the LLM call, which touches no database, is parallelised.

    A candidate that fails is skipped rather than failing the run, and is simply not
    cached: an unjudged document is a known gap, whereas a judgement invented to keep
    the loop going is a silent corruption of ground truth.
    """
    cached = {} if force else load_cache(cache_dir, query_id)
    judgments = dict(cached)

    pending = [c for c in candidates if str(c.document_id) not in judgments]
    if not pending:
        return judgments

    # Sequential fetch (see docstring), then a concurrent judge over the results.
    texts = [(c, fetch_text(c.document_id)) for c in pending]

    def judge(item: tuple[PooledCandidate, str]) -> tuple[str, Judgment | None]:
        candidate, text = item
        try:
            return str(candidate.document_id), judge_one(
                query_text,
                document_id=candidate.document_id,
                title=candidate.title,
                text=text,
            )
        except (JudgeCallError, JudgeParseError) as exc:
            log.warning(
                "judge_failed", document_id=str(candidate.document_id), error=str(exc)
            )
            return str(candidate.document_id), None

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        for key, judgment in pool.map(judge, texts):
            if judgment is not None:
                judgments[key] = judgment

    if judgments != cached:
        save_cache(cache_dir, query_id, judgments)
    return judgments
