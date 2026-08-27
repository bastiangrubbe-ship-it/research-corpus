#!/usr/bin/env python
"""Paired A/B for a change that affects retrieval, scored against frozen judgments.

    uv run python scripts/paired_retrieval_ab.py --backup-table document_summary_backup_20260826

Why this exists rather than "run the eval harness twice": the harness judges its own
pool on every run, so a change that surfaces new documents gets them judged and the
relevant-set grows between runs. Recall's denominator then moves underneath the
measurement. On 2026-08-26 that produced a clean-looking reranked recall drop of
0.723 -> 0.566 which was mostly artefact (docs/DECISIONS.md).

Two rules this encodes:

1. **One frozen ground truth.** Both conditions are scored against the judgment cache
   exactly as it stands, so the relevant-set is identical for both and only retrieval
   differs.
2. **A real control lane.** `chunk_dense` reads no summary, so a summary-side change
   must move it by 0.000. If it moves, the harness changed and the rest of the table
   is not trustworthy. Note `lexical` is NOT a control for summary changes -- it
   reads `document_summary.text` (retrieval/lexical.py).

The "before" condition is produced by restoring a backup table inside a transaction
that is rolled back, so the live table is never persistently modified.
"""

from __future__ import annotations

import argparse
import glob
import json
import os

from sqlalchemy import text

from corpus.config import get_settings
from corpus.db.enums import Domain
from corpus.db.session import tenant_session
from corpus.eval.run import _load_queries
from corpus.ingest.runner import resolve_tenant_id
from corpus.retrieval.dense import chunk_dense_search, dense_search
from corpus.retrieval.lexical import lexical_search
from corpus.retrieval.search import hybrid_search

CONTROL_LANE = "chunk_dense"


def load_truth(eval_dir) -> dict[str, set[str]]:
    """Frozen ground truth: the judgment cache as it stands right now."""
    truth = {}
    for path in glob.glob(str(eval_dir / "judgments" / "*.json")):
        qid = os.path.basename(path)[:-5]
        with open(path) as fh:
            judged = json.load(fh)
        truth[qid] = {k for k, v in judged.items() if v["verdict"] == "relevant"}
    return truth


def measure(session, *, tenant_id, truth, top_k, with_rerank):
    out: dict[str, list] = {}
    for q in _load_queries():
        relevant = truth.get(q["id"], set())
        if not relevant:
            continue
        domain = Domain(q["domain"]) if q.get("domain") else None
        lanes = {
            "lexical": [str(d) for d, _ in lexical_search(
                session, tenant_id=tenant_id, query_text=q["text"], domain=domain, top_k=top_k)],
            "dense": [str(d) for d, _ in dense_search(
                session, tenant_id=tenant_id, query_text=q["text"], domain=domain, top_k=top_k)],
            "chunk_dense": [str(d) for d, _x, _c, _s in chunk_dense_search(
                session, tenant_id=tenant_id, query_text=q["text"], domain=domain, top_k=top_k)],
        }
        if with_rerank:
            lanes["reranked"] = [str(d) for d, _ in hybrid_search(
                session, tenant_id=tenant_id, query_text=q["text"], domain=domain,
                top_k=top_k, candidate_pool=top_k, rerank=True)]
        for lane, docs in lanes.items():
            hits = sum(1 for d in docs if d in relevant)
            out.setdefault(lane, []).append(
                (q["id"], hits / len(docs) if docs else None, hits / len(relevant))
            )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-table", required=True,
                        help="table holding the 'before' document_summary rows")
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument(
        "--rerank",
        action="store_true",
        help="also measure the reranked lane (slower: one cross-encoder pass per query)",
    )
    args = parser.parse_args()

    settings = get_settings()
    tenant_id = resolve_tenant_id(settings)
    truth = load_truth(settings.eval_dir)
    n_relevant = sum(len(v) for v in truth.values())
    print(f"  frozen ground truth: {n_relevant} relevant judgments over {len(truth)} queries")
    print(f"  top_k={args.top_k}, identical relevant-set for both conditions\n")

    with tenant_session(tenant_id) as session:
        after = measure(session, tenant_id=tenant_id, truth=truth,
                        top_k=args.top_k, with_rerank=args.rerank)
        session.execute(text(f"""
            update document_summary d
            set text=b.text, embedding=b.embedding, model_version=b.model_version
            from {args.backup_table} b
            where b.document_id=d.document_id and b.method=d.method"""))
        session.flush()
        before = measure(session, tenant_id=tenant_id, truth=truth,
                         top_k=args.top_k, with_rerank=args.rerank)
        session.rollback()  # live table is never persistently modified

    print(f"    {'lane':<13} {'P before':>9} {'P after':>9} {'ΔP':>8}   "
          f"{'R before':>9} {'R after':>9} {'ΔR':>8}")
    control_moved = False
    for lane in before:
        b = [x for x in before[lane] if x[1] is not None]
        a = [x for x in after[lane] if x[1] is not None]
        if not b or not a:
            continue
        pb = sum(x[1] for x in b) / len(b)
        pa = sum(x[1] for x in a) / len(a)
        rb = sum(x[2] for x in b) / len(b)
        ra = sum(x[2] for x in a) / len(a)
        flag = "  <- CONTROL" if lane == CONTROL_LANE else ""
        print(f"    {lane:<13} {pb:>9.3f} {pa:>9.3f} {pa - pb:>+8.3f}   "
              f"{rb:>9.3f} {ra:>9.3f} {ra - rb:>+8.3f}{flag}")
        if lane == CONTROL_LANE and (abs(pa - pb) > 1e-9 or abs(ra - rb) > 1e-9):
            control_moved = True

    if control_moved:
        print(f"\n  WARNING: the control lane ({CONTROL_LANE}) moved. It reads no summary, so it")
        print("  cannot move from a summary-side change. Something else differs between the two")
        print("  measurements -- do not trust the other rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
