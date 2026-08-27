#!/usr/bin/env python
"""Run the retrieval eval harness: pool candidates per query, judge them
(LLM-assisted, cached), score each lane's pooled recall/precision, write one
timestamped result file per run.

    uv run python -m corpus.eval.run [--top-k 20] [--force-rejudge]

Verification standard this satisfies (build plan, step 7): one command runs the full
query set against a corpus snapshot and writes comparable, timestamped results;
re-running an unchanged corpus reproduces scores exactly, because judgments are
cached (corpus.eval.judge) and retrieval itself has no randomness — same model
weights, same SQL, same answer every time.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from corpus.config import get_settings
from corpus.db.enums import Domain
from corpus.db.session import tenant_session
from corpus.enrich.entities import reconstruct_transcript_text
from corpus.eval.judge import Verdict, judge_pool
from corpus.eval.pool import LANES, build_pool
from corpus.ingest.runner import resolve_tenant_id

_QUERIES_PATH = Path(__file__).parent / "queries.yaml"

_RELEVANT_VERDICTS = (Verdict.RELEVANT,)
_LENIENT_VERDICTS = (Verdict.RELEVANT, Verdict.MARGINAL)


@dataclass(frozen=True, slots=True)
class LaneMetrics:
    lane: str
    n_candidates: int
    precision_strict: float | None
    precision_lenient: float | None
    recall_strict: float | None
    recall_lenient: float | None


@dataclass(frozen=True, slots=True)
class QueryResult:
    query_id: str
    query_text: str
    category: str
    pool_size: int
    n_relevant_strict: int
    n_relevant_lenient: int
    lanes: list[LaneMetrics]


def _load_queries() -> list[dict]:
    return yaml.safe_load(_QUERIES_PATH.read_text())["queries"]


def _score_lane(
    lane: str,
    pool,
    judgments,
    total_relevant_strict: int,
    total_relevant_lenient: int,
) -> LaneMetrics:
    lane_candidates = [c for c in pool if lane in c.surfaced_by]
    n = len(lane_candidates)
    if n == 0:
        return LaneMetrics(lane, 0, None, None, None, None)

    verdicts = [judgments[str(c.document_id)].verdict for c in lane_candidates]
    hits_strict = sum(1 for v in verdicts if v in _RELEVANT_VERDICTS)
    hits_lenient = sum(1 for v in verdicts if v in _LENIENT_VERDICTS)

    return LaneMetrics(
        lane=lane,
        n_candidates=n,
        precision_strict=hits_strict / n,
        precision_lenient=hits_lenient / n,
        recall_strict=(hits_strict / total_relevant_strict) if total_relevant_strict else None,
        recall_lenient=(hits_lenient / total_relevant_lenient) if total_relevant_lenient else None,
    )


def run_eval(
    *, top_k: int = 20, force_rejudge: bool = False, judge_concurrency: int | None = None
) -> list[QueryResult]:
    settings = get_settings()
    tenant_id = resolve_tenant_id(settings)
    cache_dir = settings.eval_dir / "judgments"
    queries = _load_queries()

    results: list[QueryResult] = []
    with tenant_session(tenant_id) as session:
        for q in queries:
            domain = Domain(q["domain"]) if q.get("domain") else None
            pool = build_pool(
                session, tenant_id=tenant_id, query_text=q["text"], domain=domain, top_k=top_k
            )

            def fetch_text(document_id, _session=session):
                from sqlalchemy import select

                from corpus.db.models import TranscriptVersion

                tv_id = _session.execute(
                    select(TranscriptVersion.id)
                    .where(TranscriptVersion.document_id == document_id)
                    .order_by(TranscriptVersion.created_at.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if tv_id is None:
                    return ""
                return reconstruct_transcript_text(_session, tv_id)

            judge_kwargs = {}
            if judge_concurrency is not None:
                judge_kwargs["concurrency"] = judge_concurrency
            judgments = judge_pool(
                q["id"],
                q["text"],
                pool,
                cache_dir=cache_dir,
                fetch_text=fetch_text,
                force=force_rejudge,
                **judge_kwargs,
            )

            total_strict = sum(1 for j in judgments.values() if j.verdict in _RELEVANT_VERDICTS)
            total_lenient = sum(1 for j in judgments.values() if j.verdict in _LENIENT_VERDICTS)

            lane_metrics = [
                _score_lane(lane, pool, judgments, total_strict, total_lenient) for lane in LANES
            ]
            results.append(
                QueryResult(
                    query_id=q["id"],
                    query_text=q["text"],
                    category=q.get("category", "unspecified"),
                    pool_size=len(pool),
                    n_relevant_strict=total_strict,
                    n_relevant_lenient=total_lenient,
                    lanes=lane_metrics,
                )
            )
    return results


def _write_run(results: list[QueryResult], *, top_k: int) -> Path:
    settings = get_settings()
    runs_dir = settings.eval_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    path = runs_dir / f"run_{timestamp}.json"
    payload = {
        "timestamp": timestamp,
        "top_k": top_k,
        "queries": [{**asdict(r), "lanes": [asdict(lane) for lane in r.lanes]} for r in results],
    }
    path.write_text(json.dumps(payload, indent=2))
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top-k", type=int, default=20, help="per-lane candidate pool size")
    parser.add_argument(
        "--judge-concurrency",
        type=int,
        default=None,
        help=(
            "parallel judge calls (default 12). Judging used to be serial, which made "
            "a single query take six hours on this corpus."
        ),
    )
    parser.add_argument(
        "--force-rejudge", action="store_true", help="ignore cached judgments, re-run the LLM judge"
    )
    args = parser.parse_args()

    results = run_eval(
        top_k=args.top_k,
        judge_concurrency=args.judge_concurrency,
        force_rejudge=args.force_rejudge,
    )
    path = _write_run(results, top_k=args.top_k)
    print(f"wrote {path}")
    for r in results:
        print(f"\n{r.query_id} ({r.category}): pool={r.pool_size} relevant={r.n_relevant_strict}")
        for lane in r.lanes:
            p = f"{lane.precision_strict:.2f}" if lane.precision_strict is not None else "n/a"
            rc = f"{lane.recall_strict:.2f}" if lane.recall_strict is not None else "n/a"
            print(f"  {lane.lane:10} n={lane.n_candidates:3} precision={p} recall={rc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
