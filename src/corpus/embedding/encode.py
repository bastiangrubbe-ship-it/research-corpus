"""Local document-level embeddings — nomic-embed-text-v1.5 (Apache-2.0), the model
docs/DECISIONS.md and the build plan settled on for the corpus's first dense lane.

One vector per document (over its extractive summary, see corpus.enrich.summarize),
not per chunk — ~30-50x fewer vectors than chunk-level embedding, and matched to the
document-level arguments the only queries that need vectors actually make. This module
does not touch chunk-level embeddings; that is a later build step, added only if the
eval harness shows document-level dense missing things.

nomic-embed-text-v1.5 requires task prefixes baked into the input text — this is a
model-specific contract, not decoration: "search_document: " for indexed text,
"search_query: " for the query side of a similarity search. Mixing them up degrades
retrieval quality silently rather than raising, so `embed_documents`/`embed_query`
exist as separate functions specifically to make that mistake harder to make.

Model weights land in `settings.cache_dir`, never the repo or a user-global HF cache.
"""

from __future__ import annotations

import os
from functools import lru_cache

from corpus.config import get_settings

MODEL_NAME = "nomic-ai/nomic-embed-text-v1.5"
EMBEDDING_DIM = 768


def embedding_model_version() -> str:
    return f"sentence-transformers:{MODEL_NAME}"


def _configure_hf_cache() -> None:
    cache_dir = str(get_settings().cache_dir / "huggingface")
    os.makedirs(cache_dir, exist_ok=True)
    os.environ.setdefault("HF_HOME", cache_dir)


@lru_cache(maxsize=1)
def _get_model():
    _configure_hf_cache()
    import torch
    from sentence_transformers import SentenceTransformer

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    return SentenceTransformer(MODEL_NAME, trust_remote_code=True, device=device)


def embed_documents(texts: list[str]) -> list[list[float]]:
    """Embeds text destined for the index (`document_summary.text`)."""
    model = _get_model()
    prefixed = [f"search_document: {t}" for t in texts]
    return model.encode(prefixed, normalize_embeddings=True).tolist()


def embed_query(text: str) -> list[float]:
    """Embeds a query for cosine similarity search against `document_summary.embedding`."""
    model = _get_model()
    vec = model.encode([f"search_query: {text}"], normalize_embeddings=True)
    return vec[0].tolist()
