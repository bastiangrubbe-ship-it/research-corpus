#!/usr/bin/env python
"""Extract entities only for documents relevant to a specific query.

    uv run python flows/query_entities.py "AI coding agents" [--top-k 10] [--dry-run]

The other half of entity extraction alongside flows/nightly_entities.py's FIFO
backfill: instead of processing the whole unenriched backlog in queue order, rank it
by relevance to a query via corpus.enrich.relevance_gate, and only run the expensive
`claude -p` call on documents actually relevant to what was just asked.

Two-stage by default: a cheap local embedding search casts a wide net
(--candidate-pool documents), then a local cross-encoder reranks that pool down to
--top-k. Pass --no-rerank to skip the second stage and use raw cosine-similarity
ranking alone (faster, less accurate — see docs/EVAL_RELEVANCE_GATE.md).

Requires document_summary rows to already exist for candidates to be found —
run `backfill_document_summaries` (or this script's --backfill-summaries flag) first
if the corpus hasn't been summarized/embedded yet. That step is local and free, so it
is safe to run eagerly and often, unlike the entity extraction itself.
"""

from __future__ import annotations

import argparse
import sys

import structlog

from corpus.config import get_settings
from corpus.db.session import tenant_session
from corpus.enrich.entities import enrich_documents_concurrent, extractor_version
from corpus.enrich.relevance_gate import (
    backfill_document_summaries,
    find_relevant_unenriched_documents,
    rerank_relevant_documents,
)
from corpus.ingest.runner import resolve_tenant_id

log = structlog.get_logger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", help="the question or topic driving this extraction")
    parser.add_argument(
        "--top-k", type=int, default=10, help="how many documents to actually extract"
    )
    parser.add_argument(
        "--candidate-pool",
        type=int,
        default=50,
        help="how many candidates the embedding gate hands to the reranker",
    )
    parser.add_argument(
        "--max-distance",
        type=float,
        default=None,
        help="drop candidates weaker than this cosine distance",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=0.01,
        help=(
            "if the single best reranked candidate scores below this, treat the query as "
            "having no relevant documents and extract nothing (default 0.01 — measured in "
            "docs/EVAL_RELEVANCE_GATE.md: real matches topped out at 0.068-0.82, a query with "
            "no real match in the backlog topped out at 0.0006). Pass 0 to disable."
        ),
    )
    parser.add_argument(
        "--no-rerank",
        action="store_true",
        help="skip the cross-encoder stage, use cosine ranking alone",
    )
    parser.add_argument(
        "--concurrency", type=int, default=5, help="parallel `claude -p` calls (default 5)"
    )
    parser.add_argument(
        "--backfill-summaries",
        type=int,
        default=0,
        metavar="N",
        help="summarize+embed up to N un-summarized documents first (local, no Claude)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the candidates, extract nothing"
    )
    args = parser.parse_args()

    settings = get_settings()
    tenant_id = resolve_tenant_id(settings)
    version = extractor_version()
    # See nightly_entities.py: None enables per-document model escalation.
    model = None

    if args.backfill_summaries:
        with tenant_session(tenant_id) as session:
            n = backfill_document_summaries(
                session, tenant_id=tenant_id, limit=args.backfill_summaries
            )
        print(f"summarized+embedded {n} document(s) (local, no Claude)")

    gate_top_k = args.top_k if args.no_rerank else args.candidate_pool
    with tenant_session(tenant_id) as session:
        candidates = find_relevant_unenriched_documents(
            session,
            tenant_id=tenant_id,
            query_text=args.query,
            extractor_version_str=version,
            top_k=gate_top_k,
            max_distance=args.max_distance,
        )

        if not args.no_rerank and candidates:
            candidates = rerank_relevant_documents(
                session,
                query_text=args.query,
                candidates=candidates,
                top_k=args.top_k,
                min_score=args.min_score,
            )

    label = "score" if not args.no_rerank else "dist"
    print(f"{len(candidates)} candidate(s) for {args.query!r} (extractor_version={version})")
    for document_id, _tv_id, value in candidates:
        print(f"  {label}={value:.3f}  {document_id}")
    if args.dry_run or not candidates:
        return 0

    pending = [(doc_id, tv_id) for doc_id, tv_id, _dist in candidates]
    ok, failed = 0, 0
    for document_id, mention_count, error in enrich_documents_concurrent(
        pending, tenant_id=tenant_id, model=model, concurrency=args.concurrency
    ):
        if error is not None:
            log.warning("entity_extraction_failed", document_id=str(document_id), error=str(error))
            print(f"  ! {document_id} failed: {error}", file=sys.stderr)
            failed += 1
            continue
        print(f"  {document_id} -> {mention_count} mention(s)")
        ok += 1

    print(f"\ndone: {ok} succeeded, {failed} failed")
    return 1 if failed and not ok else 0


if __name__ == "__main__":
    raise SystemExit(main())
