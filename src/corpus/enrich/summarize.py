"""Extractive summarization — TextRank, no model, milliseconds per document.

This produces the text that `corpus.embedding.encode` turns into the corpus's first
dense lane (see `DocumentSummary` in db/models.py). Deliberately not an LLM pass: an
abstractive summary is the same cost profile as the contextual-embedding pass the
project's ingest-path rule was written against, and it isn't needed here — TextRank's
extractive sentences are enough signal for a document-level relevance gate.

NLTK's sentence tokenizer data is downloaded once into `settings.cache_dir`, never
into the repo or a user-global location.
"""

from __future__ import annotations

import os

from corpus.config import get_settings

_SENTENCE_COUNT = 5

#: Hard ceiling on a summary's length, independent of sentence count.
#:
#: TextRank returns "the top 5 sentences", which is only bounded if the text has
#: sentence boundaries to find. Raw ASR transcripts frequently have no terminal
#: punctuation at all, so NLTK sees the entire transcript as one sentence and the
#: "summary" comes back as the whole document — measured on this corpus before this
#: guard existed: a median summary of ~1,300 characters, but a maximum of 38,115
#: containing exactly one period (docs/DECISIONS.md, 2026-08-25).
#:
#: `corpus.enrich.restore` is the real fix for those transcripts and this cap is not
#: a substitute for it — but a summarizer that can silently emit a 38k-character
#: "summary" is a hazard to everything downstream (embedding, reranking, storage),
#: and it should be bounded at the source regardless of input quality.
_MAX_SUMMARY_CHARS = 4000


def _nltk_data_dir() -> str:
    path = str(get_settings().cache_dir / "nltk")
    os.makedirs(path, exist_ok=True)
    return path


def _ensure_punkt() -> None:
    import nltk

    data_dir = _nltk_data_dir()
    if data_dir not in nltk.data.path:
        nltk.data.path.insert(0, data_dir)
    for resource in ("tokenizers/punkt_tab", "tokenizers/punkt"):
        try:
            nltk.data.find(resource)
            return
        except LookupError:
            continue
    nltk.download("punkt_tab", download_dir=data_dir, quiet=True)


def summarize_extractive(text: str, *, sentence_count: int = _SENTENCE_COUNT) -> str:
    """TextRank over `text`, returns the top `sentence_count` sentences in
    their original order. Empty input returns an empty string rather than
    raising — callers decide whether an empty summary is worth storing."""
    if not text.strip():
        return ""

    _ensure_punkt()
    from sumy.nlp.tokenizers import Tokenizer
    from sumy.parsers.plaintext import PlaintextParser
    from sumy.summarizers.text_rank import TextRankSummarizer

    parser = PlaintextParser.from_string(text, Tokenizer("english"))
    summarizer = TextRankSummarizer()
    sentences = summarizer(parser.document, sentence_count)
    summary = " ".join(str(s) for s in sentences)
    if len(summary) <= _MAX_SUMMARY_CHARS:
        return summary
    # Cut on a word boundary so the stored text stays readable rather than ending
    # mid-token — this text is shown in the dashboard and fed to a cross-encoder.
    return summary[:_MAX_SUMMARY_CHARS].rsplit(" ", 1)[0]
