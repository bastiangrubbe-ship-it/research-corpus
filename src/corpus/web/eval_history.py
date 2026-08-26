"""Eval run history routes.

Reads the timestamped JSON that `corpus.eval.run` writes to
`settings.eval_dir / "runs"`. Filesystem only — no database, no models, no
side effects; the eval itself is a CLI action, and nothing here can start one.

The diff endpoint calls `corpus.eval.report.compare_runs`, the same function the
CLI's `--diff` prints, so the dashboard and terminal cannot disagree about what
changed between two runs.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from corpus.config import get_settings
from corpus.eval.report import compare_runs

router = APIRouter(prefix="/api/eval", tags=["eval"])


def _runs_dir():
    return get_settings().eval_dir / "runs"


def _resolve(name: str):
    """Resolve a run file by name, refusing anything that isn't a plain filename
    inside the runs directory — this takes a caller-supplied string and turns it
    into a path, which is exactly where traversal gets in."""
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(status_code=400, detail=f"invalid run name {name!r}")
    path = _runs_dir() / name
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"no such run {name!r}")
    return path


@router.get("/runs")
def list_runs() -> list[dict[str, Any]]:
    """Newest first. Cheap — reads each run's header fields, not its full results."""
    runs_dir = _runs_dir()
    if not runs_dir.exists():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(runs_dir.glob("run_*.json"), reverse=True):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            # A partially-written run (killed mid-write) shouldn't break the list.
            continue
        out.append(
            {
                "name": path.name,
                "timestamp": data.get("timestamp"),
                "top_k": data.get("top_k"),
                "n_queries": len(data.get("queries", [])),
            }
        )
    return out


@router.get("/runs/{name}")
def get_run(name: str) -> dict[str, Any]:
    return json.loads(_resolve(name).read_text())


@router.get("/diff")
def diff(a: str = Query(...), b: str = Query(...)) -> list[dict[str, Any]]:
    """Per-query, per-lane precision deltas from run `a` to run `b`."""
    return compare_runs(_resolve(a), _resolve(b))
