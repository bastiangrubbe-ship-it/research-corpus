"""Reciprocal Rank Fusion — combines independently-ranked lists (lexical, dense)
into one ranking, without needing their scores to be on comparable scales.

`corpus.retrieval.lexical.lexical_search` returns `ts_rank_cd` scores (higher is
better); `corpus.retrieval.dense.dense_search` returns cosine distances (lower is
better). RRF sidesteps reconciling those two scales entirely — it only uses each
document's *rank position* within its own list, not the raw score, which is the
whole point of the technique and why it composes cleanly with lanes that have nothing
in common numerically.

`k=60` is the standard default from the original RRF paper, not measured against this
corpus — the build plan itself flags this exact parameter as an "unverified default —
the harness should test it." Still true here; nothing below changes that.
"""

from __future__ import annotations

import uuid
from collections import defaultdict


def reciprocal_rank_fusion(
    ranked_lists: list[list[tuple[uuid.UUID, float]]],
    *,
    k: int = 60,
    top_k: int = 20,
) -> list[tuple[uuid.UUID, float]]:
    """Each list in `ranked_lists` must already be sorted best-first — RRF reads
    position, not the score value, so it doesn't matter that lexical scores rank
    descending and dense distances rank ascending; each list's own ordering already
    encodes "better," which is all this function looks at.

    Returns (document_id, fused_score) sorted descending — higher fused_score means
    it ranked well across more lanes, or ranked very well in at least one. A document
    appearing in only one lane still competes; it just starts from a smaller
    contribution than one appearing near the top of two.
    """
    scores: dict[uuid.UUID, float] = defaultdict(float)
    for ranked in ranked_lists:
        for rank, (document_id, _score) in enumerate(ranked, start=1):
            scores[document_id] += 1.0 / (k + rank)

    fused = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return fused[:top_k]
