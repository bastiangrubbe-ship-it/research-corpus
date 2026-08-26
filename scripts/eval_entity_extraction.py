#!/usr/bin/env python
"""Score an entity extractor's output against hand-labeled ground truth.

    uv run python scripts/eval_entity_extraction.py

Rubric and process are in docs/EVAL.md — this is just the mechanical scoring half.
Ground truth and candidate lists are plain data (see `ScoredEntity`/`score` below),
not tied to any one extractor, so the same function scores Claude's output, GLiNER's,
or anything else against the same ground truth.

Matching is by canonical_name, case-insensitive exact match — deliberately not
fuzzy. A near-miss like "Codeex" vs "Codex" is exactly the hallucination this rubric
exists to catch; fuzzy-matching it to the real entity would hide the failure mode
under test.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class GroundTruthEntity:
    canonical_name: str
    kind: str


@dataclass(frozen=True, slots=True)
class CandidateEntity:
    canonical_name: str
    kind: str


@dataclass(slots=True)
class ScoreResult:
    correct: int = 0
    wrong_kind: int = 0
    hallucinated: int = 0
    missed: int = 0
    hallucinated_names: list[str] = field(default_factory=list)
    missed_names: list[str] = field(default_factory=list)

    @property
    def precision(self) -> float | None:
        denom = self.correct + self.hallucinated
        return self.correct / denom if denom else None

    @property
    def recall(self) -> float | None:
        denom = self.correct + self.missed
        return self.correct / denom if denom else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if not p or not r or (p + r) == 0:
            return None
        return 2 * p * r / (p + r)

    @property
    def kind_accuracy(self) -> float | None:
        denom = self.correct + self.wrong_kind
        return self.correct / denom if denom else None


def score(
    ground_truth: list[GroundTruthEntity], candidates: list[CandidateEntity]
) -> ScoreResult:
    gt_by_name = {g.canonical_name.lower(): g for g in ground_truth}
    matched_gt: set[str] = set()
    result = ScoreResult()

    for cand in candidates:
        key = cand.canonical_name.lower()
        gt = gt_by_name.get(key)
        if gt is None:
            result.hallucinated += 1
            result.hallucinated_names.append(cand.canonical_name)
            continue
        matched_gt.add(key)
        if cand.kind == gt.kind:
            result.correct += 1
        else:
            result.wrong_kind += 1

    for key, gt in gt_by_name.items():
        if key not in matched_gt:
            result.missed += 1
            result.missed_names.append(gt.canonical_name)

    return result


def print_report(label: str, r: ScoreResult) -> None:
    def fmt(x: float | None) -> str:
        return f"{x:.2f}" if x is not None else "n/a"

    print(f"  {label}")
    print(
        f"    correct={r.correct} wrong_kind={r.wrong_kind} "
        f"hallucinated={r.hallucinated} missed={r.missed}"
    )
    print(
        f"    precision={fmt(r.precision)} recall={fmt(r.recall)} "
        f"f1={fmt(r.f1)} kind_acc={fmt(r.kind_accuracy)}"
    )
    if r.hallucinated_names:
        print(f"    hallucinated: {r.hallucinated_names}")
    if r.missed_names:
        print(f"    missed: {r.missed_names}")
