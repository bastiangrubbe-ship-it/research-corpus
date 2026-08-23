"""Supadata client.

Schema confirmed from the published OpenAPI spec — see docs/SUPADATA.md. Two
properties of this API drive the design here:

* **It reports no provenance.** The response has three fields and none says whether
  captions were human-authored or machine-generated. When captions are missing it
  silently substitutes Whisper and returns the identical shape. Every transcript from
  this provider is therefore recorded with `is_auto_generated=None` and
  `provenance_confidence='unknown'` — or `'inferred'` in the one case below.

* **Credits are finite.** 1 per batch request plus 1 per video. Spending is checked
  before the call, not discovered afterwards from a 429.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from corpus.db.enums import DatePrecision, ProvenanceConfidence, TranscriptProvider
from corpus.sources.base import (
    CreditBudgetExceeded,
    InvalidRequest,
    NormalizedDocument,
    NormalizedSegment,
    NormalizedTranscript,
    ProviderBlocked,
    RawResponse,
    TranscriptUnavailable,
)

PROVIDER = "supadata"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class RateLimiter:
    """Token bucket. The real limit is plan-dependent and undocumented, so the rate
    comes from configuration rather than being hardcoded to a number we cannot verify.
    """

    def __init__(self, rate_per_second: float) -> None:
        self._min_interval = 1.0 / rate_per_second if rate_per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._last = 0.0

    def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            elapsed = time.monotonic() - self._last
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last = time.monotonic()


#: (credits, endpoint, external_id) -> None. Called only after a reservation
#: succeeds — never for a call that raised CreditBudgetExceeded, since no credit
#: was actually spent in that case.
OnSpend = Callable[[int, str, str | None], None]


class CreditLedger:
    """Tracks spend against the configured budget and refuses to exceed it.

    Deliberately a preflight check. Discovering the limit by receiving a 429 wastes
    the request that hits it and gives no way to reason about a large backfill before
    starting one.

    `budget`/`spent` here are in-memory and reset on every process restart — they
    are not the durable record. `on_spend`, when supplied, is how a caller persists
    each spend event; without it, "credits used" and "credits remaining" cannot
    outlive this one process, because Supadata itself reports no consumption back
    to the caller (see docs/SUPADATA.md).
    """

    def __init__(self, budget: int, *, on_spend: OnSpend | None = None) -> None:
        self.budget = budget
        self.spent = 0
        self._on_spend = on_spend
        self._lock = threading.Lock()

    def reserve(self, credits: int, *, endpoint: str, external_id: str | None = None) -> None:
        with self._lock:
            if self.spent + credits > self.budget:
                raise CreditBudgetExceeded(
                    f"would spend {credits} credits; {self.remaining} of {self.budget} remain"
                )
            self.spent += credits
        if self._on_spend is not None:
            self._on_spend(credits, endpoint, external_id)

    @property
    def remaining(self) -> int:
        return max(0, self.budget - self.spent)


class SupadataClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.supadata.ai/v1",
        requests_per_second: float = 2.0,
        monthly_credits: int = 30_000,
        client: httpx.Client | None = None,
        on_spend: OnSpend | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._limiter = RateLimiter(requests_per_second)
        self.ledger = CreditLedger(monthly_credits, on_spend=on_spend)
        self._client = client or httpx.Client(
            base_url=self._base_url,
            headers={"x-api-key": api_key},
            timeout=httpx.Timeout(30.0, read=120.0),
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SupadataClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- transport ---------------------------------------------------------
    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=20),
        reraise=True,
    )
    def _get(self, path: str, params: dict[str, Any]) -> httpx.Response:
        self._limiter.acquire()
        response = self._client.get(path, params=params)
        self._raise_for_status(response)
        return response

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        code = response.status_code
        if code in (200, 202):
            return
        if code == 400:
            # Observed: an unsupported `lang` returns 400 with
            # {"error":"invalid-request"}. Treating this as a provider block
            # would trigger a pointless failover to a provider that will reject
            # the identical request.
            raise InvalidRequest(f"supadata: {response.text[:200]}")
        if code == 401:
            raise ProviderBlocked("supadata: missing or invalid API key (401)")
        if code == 402:
            raise ProviderBlocked("supadata: payment required (402)")
        if code == 429:
            raise ProviderBlocked("supadata: plan limit exceeded (429)")
        if code == 404:
            raise TranscriptUnavailable("supadata: not found (404)")
        if 500 <= code < 600:
            # Retryable; tenacity catches HTTPStatusError.
            response.raise_for_status()
        raise ProviderBlocked(f"supadata: unexpected status {code}: {response.text[:200]}")

    # -- endpoints ---------------------------------------------------------
    def fetch_transcript(
        self, video_id: str, *, lang: str = "en"
    ) -> tuple[NormalizedTranscript, RawResponse]:
        self.ledger.reserve(1, endpoint="/youtube/transcript", external_id=video_id)
        params = {"videoId": video_id, "lang": lang, "text": "false"}
        response = self._get("/youtube/transcript", params)
        payload = response.json()

        raw = RawResponse(
            provider=PROVIDER,
            endpoint="/youtube/transcript",
            external_id=video_id,
            fetched_at=_now(),
            payload=payload,
            http_status=response.status_code,
            headers=dict(response.headers),
            request_params=params,
        )

        content = payload.get("content")
        if content is None:
            raise TranscriptUnavailable(f"supadata returned no content for {video_id}")

        # INFERENCE, not documentation. The docs imply 202 accompanies the Whisper
        # path (captions absent, audio transcribed on the fly) but never state it.
        # Recorded as 'inferred' so it can be distinguished later from a fact, and
        # revisited cheaply if it turns out to be wrong.
        if response.status_code == 202:
            is_auto: bool | None = True
            confidence = ProvenanceConfidence.INFERRED
        else:
            is_auto = None
            confidence = ProvenanceConfidence.UNKNOWN

        transcript = NormalizedTranscript(
            provider=TranscriptProvider.SUPADATA,
            lang=payload.get("lang", lang),
            segments=_to_segments(content),
            available_langs=tuple(payload.get("availableLangs") or ()),
            is_auto_generated=is_auto,
            provenance_confidence=confidence,
        )
        return transcript, raw

    def list_channel_videos(self, channel_ref: str, *, limit: int | None = None) -> list[str]:
        """Video ids for a channel. Costs 1 credit regardless of how many come back."""
        self.ledger.reserve(1, endpoint="/youtube/channel/videos", external_id=channel_ref)
        params: dict[str, Any] = {"id": channel_ref}
        if limit is not None:
            params["limit"] = min(limit, 5000)  # documented ceiling
        response = self._get("/youtube/channel/videos", params)
        payload = response.json()
        ids = payload.get("videoIds") or payload.get("videos") or []
        return [v if isinstance(v, str) else v.get("id") for v in ids]

    def fetch_metadata(self, video_id: str) -> tuple[NormalizedDocument, RawResponse]:
        self.ledger.reserve(1, endpoint="/youtube/video", external_id=video_id)
        params = {"id": video_id}
        response = self._get("/youtube/video", params)
        payload = response.json()

        raw = RawResponse(
            provider=PROVIDER,
            endpoint="/youtube/video",
            external_id=video_id,
            fetched_at=_now(),
            payload=payload,
            http_status=response.status_code,
            headers=dict(response.headers),
            request_params=params,
        )
        return _to_document(video_id, payload), raw


def _to_segments(content: Any) -> list[NormalizedSegment]:
    """Supadata offsets are already milliseconds — no conversion, unlike ytapi.

    With `text=true` the API collapses `content` to a plain string, which loses all
    timestamps. We always request `text=false`; the string form is handled only so a
    misconfiguration degrades to one untimed segment instead of raising.
    """
    if isinstance(content, str):
        return [NormalizedSegment(idx=0, text=content, offset_ms=0, duration_ms=0)]
    return [
        NormalizedSegment(
            idx=i,
            text=chunk["text"],
            offset_ms=int(chunk["offset"]),
            duration_ms=int(chunk.get("duration", 0)),
        )
        for i, chunk in enumerate(content)
    ]


def _to_document(video_id: str, payload: dict[str, Any]) -> NormalizedDocument:
    """`uploadDate` is optional in the schema, hence the precision field."""
    upload = payload.get("uploadDate")
    published_at: dt.datetime | None = None
    precision = DatePrecision.UNKNOWN
    source = None
    if upload:
        try:
            published_at = dt.datetime.fromisoformat(str(upload).replace("Z", "+00:00"))
            precision = DatePrecision.EXACT
            source = "api"
        except ValueError:
            precision = DatePrecision.UNKNOWN

    channel = payload.get("channel") or {}
    return NormalizedDocument(
        external_id=payload.get("id") or video_id,
        url=f"https://www.youtube.com/watch?v={payload.get('id') or video_id}",
        title=payload.get("title"),
        description=payload.get("description"),
        duration_s=payload.get("duration"),
        published_at=published_at,
        published_at_precision=precision,
        published_at_source=source,
        extra={
            "channel_id": channel.get("id"),
            "channel_name": channel.get("name"),
            "tags": payload.get("tags"),
            "view_count": payload.get("viewCount"),
            "like_count": payload.get("likeCount"),
            "transcript_languages": payload.get("transcriptLanguages"),
        },
    )
