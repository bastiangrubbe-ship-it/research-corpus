#!/usr/bin/env python
"""Record real Supadata responses as test fixtures.

Fixtures for this provider must come from the live API, not from the OpenAPI spec.
A spec tells you the declared shape; it does not tell you what the service actually
sends — optional fields that are always present, fields that are documented but
absent, the exact status code on the Whisper path. Testing against a hand-written
approximation of a spec verifies our reading of the documentation, which is not the
thing that breaks.

Run once, with SUPADATA_API_KEY set:

    uv run python scripts/record_supadata.py

Costs ~5 credits of 30,000.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import sys

from corpus.config import get_settings
from corpus.sources.base import SourceError
from corpus.sources.youtube.supadata import SupadataClient

OUT = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "responses"

# Chosen to exercise the branches that matter, not to be representative.
CASES = [
    ("captions_present", "dQw4w9WgXcQ", "en"),  # human captions exist -> expect 200
    ("no_captions_whisper", "aqz-KE-bpKQ", "en"),  # sparse captions -> may hit 202/Whisper
    ("unavailable", "xxxxxxxxxxx", "en"),  # bad id -> expect 404
    ("wrong_language", "dQw4w9WgXcQ", "xx"),  # unsupported lang
]


def main() -> int:
    settings = get_settings()
    key = (
        settings.supadata_api_key.get_secret_value()
        if settings.supadata_api_key is not None
        else ""
    ).strip()
    # An empty string is not None. Without this check the script cheerfully spends
    # credits collecting four identical 401s.
    if not key:
        print(
            "SUPADATA_API_KEY is not set.\nAdd it to .env (which is gitignored) and re-run.",
            file=sys.stderr,
        )
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    client = SupadataClient(
        api_key=key,
        base_url=settings.supadata_base_url,
        requests_per_second=settings.supadata_requests_per_second,
        monthly_credits=settings.supadata_monthly_credits,
    )

    with client:
        for name, video_id, lang in CASES:
            record: dict = {"_case": name, "video_id": video_id, "lang": lang}
            try:
                transcript, raw = client.fetch_transcript(video_id, lang=lang)
                record |= {
                    "http_status": raw.http_status,
                    # Truncated: enough to assert on, small enough to read in a diff.
                    "payload": _truncate(raw.payload),
                    "headers": {
                        k: v
                        for k, v in raw.headers.items()
                        if k.lower().startswith("x-") or k.lower() == "content-type"
                    },
                    "normalized": {
                        "lang": transcript.lang,
                        "segments": len(transcript.segments),
                        "first_segment": dataclasses.asdict(transcript.segments[0])
                        if transcript.segments
                        else None,
                        "is_auto_generated": transcript.is_auto_generated,
                        "provenance_confidence": transcript.provenance_confidence.value,
                    },
                }
            except SourceError as exc:
                record |= {"error": type(exc).__name__, "message": str(exc)}

            path = OUT / f"supadata_{name}.json"
            path.write_text(json.dumps(record, indent=2, ensure_ascii=False))
            print(f"  {path.name}: {record.get('http_status') or record.get('error')}")

    print(f"\ncredits spent: {client.ledger.spent} (of {client.ledger.budget})")
    return 0


def _truncate(payload: object, keep: int = 5) -> object:
    if isinstance(payload, dict) and isinstance(payload.get("content"), list):
        content = payload["content"]
        return payload | {
            "content": content[:keep],
            "_content_truncated_from": len(content),
        }
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
