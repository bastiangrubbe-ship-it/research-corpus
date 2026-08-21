"""Provider failover.

The distinction that matters: `TranscriptUnavailable` means *no* provider will have it,
so trying the next one only burns a credit. `ProviderBlocked` means *this* provider is
refusing us, and the next one may not be. Collapsing the two would either waste the
credit budget or silently lose recoverable documents.
"""

from __future__ import annotations

import datetime as dt

import pytest

from corpus.db.enums import ProvenanceConfidence, TranscriptProvider
from corpus.sources.base import (
    NormalizedDocument,
    NormalizedSegment,
    NormalizedTranscript,
    ProviderBlocked,
    RawResponse,
    TranscriptUnavailable,
)
from corpus.sources.youtube.adapter import YouTubeAdapter


def _transcript(provider: TranscriptProvider) -> NormalizedTranscript:
    known = provider is TranscriptProvider.YTAPI
    return NormalizedTranscript(
        provider=provider,
        lang="en",
        segments=[NormalizedSegment(idx=0, text="hi", offset_ms=0, duration_ms=100)],
        is_auto_generated=False if known else None,
        provenance_confidence=ProvenanceConfidence.KNOWN if known else ProvenanceConfidence.UNKNOWN,
    )


class FakeTranscriptClient:
    def __init__(self, provider: TranscriptProvider, raises: Exception | None = None) -> None:
        self.provider = provider
        self.raises = raises
        self.calls = 0

    def fetch_transcript(self, video_id: str, *, lang: str = "en"):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        raw = RawResponse(
            provider=self.provider.value,
            endpoint="fake",
            external_id=video_id,
            fetched_at=dt.datetime.now(dt.UTC),
            payload={},
        )
        return _transcript(self.provider), raw


class FakeSupadata(FakeTranscriptClient):
    def __init__(self, raises: Exception | None = None) -> None:
        super().__init__(TranscriptProvider.SUPADATA, raises)

    def fetch_metadata(self, video_id: str):
        return (
            NormalizedDocument(external_id=video_id, title="t"),
            RawResponse(
                provider="supadata",
                endpoint="/youtube/video",
                external_id=video_id,
                fetched_at=dt.datetime.now(dt.UTC),
                payload={},
            ),
        )


def test_ytapi_is_preferred_and_supadata_is_not_called() -> None:
    """The local ordering. ytapi first because it is the only source of provenance."""
    yt = FakeTranscriptClient(TranscriptProvider.YTAPI)
    sd = FakeSupadata()
    adapter = YouTubeAdapter(ytapi=yt, supadata=sd, provider_order=("ytapi", "supadata"))

    result = adapter.fetch("vid1")

    assert result.transcript is not None
    assert result.transcript.provider is TranscriptProvider.YTAPI
    assert result.transcript.provenance_confidence is ProvenanceConfidence.KNOWN
    assert yt.calls == 1
    assert sd.calls == 0, "supadata should not be called when ytapi succeeds"


def test_blocked_provider_falls_through_to_the_next() -> None:
    """What happens on the Linux server: YouTube blocks the IP, Supadata takes over."""
    yt = FakeTranscriptClient(TranscriptProvider.YTAPI, raises=ProviderBlocked("ip blocked"))
    sd = FakeSupadata()
    adapter = YouTubeAdapter(ytapi=yt, supadata=sd, provider_order=("ytapi", "supadata"))

    result = adapter.fetch("vid1")

    assert result.transcript is not None
    assert result.transcript.provider is TranscriptProvider.SUPADATA
    # Provenance is genuinely lost on this path, and is recorded as unknown rather
    # than guessed.
    assert result.transcript.is_auto_generated is None
    assert result.transcript.provenance_confidence is ProvenanceConfidence.UNKNOWN
    assert sd.calls == 1


def test_unavailable_transcript_does_not_burn_a_second_provider() -> None:
    """A video with no captions has none for anybody. Failing over wastes a credit."""
    yt = FakeTranscriptClient(TranscriptProvider.YTAPI, raises=TranscriptUnavailable("none"))
    sd = FakeSupadata()
    adapter = YouTubeAdapter(ytapi=yt, supadata=sd, provider_order=("ytapi", "supadata"))

    result = adapter.fetch("vid1")

    assert result.transcript is None
    assert result.error_code is not None and "transcript-unavailable" in result.error_code
    assert sd.calls == 0, "unavailable must not trigger failover"


def test_document_survives_when_every_provider_fails() -> None:
    """A document with no transcript is still worth storing — its metadata feeds
    entity analytics, and the transcript can be retried later."""
    yt = FakeTranscriptClient(TranscriptProvider.YTAPI, raises=ProviderBlocked("blocked"))
    sd = FakeSupadata(raises=ProviderBlocked("quota"))
    adapter = YouTubeAdapter(ytapi=yt, supadata=sd, provider_order=("ytapi", "supadata"))

    result = adapter.fetch("vid1")

    assert result.transcript is None
    assert result.document.external_id == "vid1"
    assert result.document.title == "t", "metadata should survive transcript failure"
    assert result.error_code is not None and "provider-blocked" in result.error_code


def test_server_ordering_puts_supadata_first() -> None:
    """The migration case: order is configuration, not a code change."""
    yt = FakeTranscriptClient(TranscriptProvider.YTAPI)
    sd = FakeSupadata()
    adapter = YouTubeAdapter(ytapi=yt, supadata=sd, provider_order=("supadata", "ytapi"))

    result = adapter.fetch("vid1")

    assert result.transcript is not None
    assert result.transcript.provider is TranscriptProvider.SUPADATA
    assert yt.calls == 0


def test_unknown_provider_name_is_rejected_loudly() -> None:
    adapter = YouTubeAdapter(ytapi=None, supadata=None, provider_order=("nonsense",))
    with pytest.raises(ValueError, match="unknown provider"):
        adapter.fetch("vid1")
