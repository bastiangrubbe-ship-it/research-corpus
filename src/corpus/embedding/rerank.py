"""Cross-encoder reranking — Qwen3-Reranker-4B (Apache-2.0, confirmed 2026-08-24).

Sits downstream of corpus.enrich.relevance_gate's cheap embedding-based candidate
search. That gate casts a wide net cheaply: cosine similarity between two
independently-computed vectors (the query's, and a document summary's), which is fast
enough to search the whole unenriched backlog but structurally can't let the query and
document actually attend to each other. A cross-encoder does exactly that — query and
document text go through the model together — which is why it catches things cosine
similarity misses (docs/EVAL_RELEVANCE_GATE.md's marketing/SEO query is the concrete
example: generic "AI + get rich" content embedded deceptively close to real marketing
content; a cross-encoder reading both texts together should tell those apart).

This is why reranking only ever runs over a narrow candidate pool (tens of documents),
never the whole backlog directly: a cross-encoder scores one (query, document) pair at
a time — there's no precomputed index the way there is for embeddings, so it doesn't
scale the way the bi-encoder gate does.

Chosen over bge-reranker-v2-m3 (the build plan's original, lighter pick) because the
corpus's actual hardware (48GB unified memory) comfortably runs the larger model, and
public benchmarks put it ahead — see docs/DECISIONS.md for the full comparison. Model
weights land in settings.cache_dir, same convention as corpus.embedding.encode.
"""

from __future__ import annotations

import os
from functools import lru_cache

from corpus.config import get_settings

MODEL_NAME = "Qwen/Qwen3-Reranker-4B"

# Qwen3-Reranker's own model card: not customizing this instruction costs ~1-5%
# retrieval performance, and the wrapper's built-in default ("Given a web search
# query, retrieve relevant passages that answer the query") is tuned for web search,
# not this corpus's actual content — podcast/video transcript summaries being judged
# against a research question, not a page being judged against a search box query.
# docs/EVAL_RELEVANCE_GATE.md's reranker test ran against the untuned default and got
# a mixed result; this is the untested fix that test flagged as the first thing to try.
_INSTRUCTION = (
    "Given a research question about topics discussed in podcast and video "
    "transcripts, determine whether this transcript summary is relevant to the "
    "question"
)


def reranker_model_version() -> str:
    return f"sentence-transformers:{MODEL_NAME}:instructed-v1:fp32"


def _configure_hf_cache() -> None:
    cache_dir = str(get_settings().cache_dir / "huggingface")
    os.makedirs(cache_dir, exist_ok=True)
    os.environ.setdefault("HF_HOME", cache_dir)


@lru_cache(maxsize=1)
def _get_model():
    _configure_hf_cache()
    import torch
    from sentence_transformers import CrossEncoder

    return CrossEncoder(
        MODEL_NAME,
        prompts={"corpus_relevance": _INSTRUCTION},
        default_prompt_name="corpus_relevance",
        # Default load dtype (bfloat16) is a confirmed, serious bug source: two
        # genuinely different documents can quantize to the exact same logit
        # difference and produce bit-for-bit identical scores. Verified directly —
        # a real 38-document batch had 42% of scores collide in bfloat16 (16/38, 7
        # distinct duplicate groups, one at a non-extreme value of 0.4378...
        # matched exactly); the identical batch scored fp32 had zero collisions
        # (38/38 unique). See docs/DECISIONS.md for the full isolation trail.
        model_kwargs={"dtype": torch.float32},
    )


#: Hard cap on how much of each document the cross-encoder actually reads.
#:
#: Not a tuning knob — a robustness fix for a measured pathology. Document summaries
#: on this corpus have a median length of ~1,300 characters and a p90 of ~4,700, but
#: the longest is 38,115: transcripts whose ASR text contains no sentence
#: punctuation at all defeat NLTK's sentence tokenizer, so TextRank's "top 5
#: sentences" returns the entire transcript (see docs/DECISIONS.md, 2026-08-25).
#:
#: Because batching pads every sequence to the longest one in its batch, a single
#: such outlier taxes its batch-mates too. Measured: a 21-document rerank containing
#: one of these took 306s, while a 40-document rerank of normal summaries took 24s —
#: fewer documents, 12x slower. Capping bounds that worst case without touching the
#: ~90% of documents that are already shorter than the cap.
#:
#: Truncation is also not much of a loss for the affected documents specifically:
#: what gets cut is the tail of an un-summarized transcript, and the opening of a
#: video transcript ("today we're going to look at X") is a fair topical signal.
_MAX_DOC_CHARS = 4000


def rerank(query: str, documents: list[str]) -> list[float]:
    """Relevance score per document, same order as input, each in [0, 1] via sigmoid.
    Higher means more relevant — the opposite sense of relevance_gate's cosine
    distance, which is why the two are never mixed into one return contract.

    `batch_size=4` is deliberate: fp32 fixes the score-collision bug (see
    docs/DECISIONS.md) but roughly doubles activation memory versus the library's
    bfloat16 default, and this model's weights alone are already ~16GB. A batch of
    50 candidates at the library's default internal batch size hit a real MPS OOM
    (48GB unified memory exceeded) during exactly this kind of candidate-pool-sized
    call. Smaller internal batches bound peak activation memory without touching
    weight memory or precision — slower per call, but correct and OOM-safe, which a
    faster wrong answer isn't.
    """
    if not documents:
        return []
    import torch

    model = _get_model()
    pairs = [(query, doc[:_MAX_DOC_CHARS]) for doc in documents]
    scores = model.predict(pairs, activation_fn=torch.nn.Sigmoid(), batch_size=4)
    return [float(s) for s in scores]
