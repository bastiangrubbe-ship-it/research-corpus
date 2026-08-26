#!/usr/bin/env python
"""Format an eval run's results, or diff two runs against each other.

uv run python -m corpus.eval.report                    # latest run
uv run python -m corpus.eval.report --run run_20260825T140000.json
uv run python -m corpus.eval.report --diff run_A.json run_B.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from corpus.config import get_settings


def _runs_dir() -> Path:
    return get_settings().eval_dir / "runs"


def _latest_run() -> Path:
    runs = sorted(_runs_dir().glob("run_*.json"))
    if not runs:
        raise FileNotFoundError(f"no eval runs found in {_runs_dir()}")
    return runs[-1]


def _resolve(name_or_path: str) -> Path:
    path = Path(name_or_path)
    if path.exists():
        return path
    candidate = _runs_dir() / name_or_path
    if candidate.exists():
        return candidate
    raise FileNotFoundError(name_or_path)


def print_run(path: Path) -> None:
    data = json.loads(path.read_text())
    print(f"run: {path.name}  (top_k={data['top_k']}, timestamp={data['timestamp']})")
    for q in data["queries"]:
        print(f'\n{q["query_id"]} [{q["category"]}]  "{q["query_text"]}"')
        print(f"  pool={q['pool_size']}  relevant_strict={q['n_relevant_strict']}")
        for lane in q["lanes"]:
            p = f"{lane['precision_strict']:.2f}" if lane["precision_strict"] is not None else "n/a"
            r = f"{lane['recall_strict']:.2f}" if lane["recall_strict"] is not None else "n/a"
            print(f"    {lane['lane']:10} n={lane['n_candidates']:3}  precision={p}  recall={r}")


def diff_runs(path_a: Path, path_b: Path) -> None:
    """Print what `compare_runs` computes — a thin CLI wrapper, so the numbers here
    and the dashboard's cannot drift apart."""
    print(f"diff: {path_a.name} -> {path_b.name}")
    for row in compare_runs(path_a, path_b):
        if row["only_in_one_run"]:
            print(f"\n{row['query_id']}: present in only one run, skipping")
            continue
        print(f"\n{row['query_id']} [{row['category']}]")
        for lane in row["lanes"]:
            marker = "+" if lane["delta"] > 0 else ("-" if lane["delta"] < 0 else "=")
            print(
                f"  {lane['lane']:12} precision {lane['before']:.2f} -> {lane['after']:.2f} "
                f" ({marker}{abs(lane['delta']):.2f})"
            )


def compare_runs(path_a: Path, path_b: Path) -> list[dict]:
    """Per-query, per-lane precision deltas between two runs, as plain data.

    Extracted from `diff_runs` so the dashboard renders the same comparison the CLI
    prints rather than reimplementing the dict-walking — one definition of "what
    changed between two runs", used by both.
    """
    a = {q["query_id"]: q for q in json.loads(path_a.read_text())["queries"]}
    b = {q["query_id"]: q for q in json.loads(path_b.read_text())["queries"]}

    out: list[dict] = []
    for query_id in sorted(set(a) | set(b)):
        if query_id not in a or query_id not in b:
            out.append(
                {"query_id": query_id, "only_in_one_run": True, "category": None, "lanes": []}
            )
            continue
        lanes_a = {lane["lane"]: lane for lane in a[query_id]["lanes"]}
        lanes_b = {lane["lane"]: lane for lane in b[query_id]["lanes"]}
        lanes: list[dict] = []
        for lane_name in sorted(set(lanes_a) | set(lanes_b)):
            pa, pb = lanes_a.get(lane_name), lanes_b.get(lane_name)
            if pa is None or pb is None:
                continue
            before, after = pa["precision_strict"], pb["precision_strict"]
            # None means the lane returned no candidates at all for that query —
            # not zero precision. Skipped rather than coerced to 0.0, which would
            # read as "it scored badly" instead of "it did not compete".
            if before is None or after is None:
                continue
            lanes.append(
                {"lane": lane_name, "before": before, "after": after, "delta": after - before}
            )
        out.append(
            {
                "query_id": query_id,
                "only_in_one_run": False,
                "category": b[query_id]["category"],
                "lanes": lanes,
            }
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=str, default=None, help="run file to print (default: latest)")
    parser.add_argument("--diff", nargs=2, metavar=("RUN_A", "RUN_B"), help="compare two runs")
    args = parser.parse_args()

    if args.diff:
        diff_runs(_resolve(args.diff[0]), _resolve(args.diff[1]))
    else:
        path = _resolve(args.run) if args.run else _latest_run()
        print_run(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
