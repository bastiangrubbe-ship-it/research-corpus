"""The bronze store: raw provider responses, written once, never mutated.

This is the corpus's rebuildability guarantee. If a parser turns out to be wrong,
the parser is fixed and every document re-derived from bronze — re-fetching from the
provider is often impossible (rate limits, expired credentials, deleted upstream
records, spent quota). It is a plain filesystem, deliberately outside the database and
outside row-level security: RLS protects query access to derived data, not the archival
copy that makes derivation redeemable in the first place.

Keys are content-addressed by SHA-256 over (provider, endpoint, external_id, a stable
hash of the payload). This makes a second identical fetch a no-op rather than a
duplicate: the same request against the same upstream state produces the same key,
so `exists()` is the whole "have we already got this" check ingestion needs.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from corpus.sources.base import RawResponse


class BronzeWriteConflict(Exception):
    """Raised when a write would overwrite an existing bronze file with different
    content. Bronze is append-only; a second write to the same key is only safe when
    it is byte-identical to the first (a harmless retry), never a silent overwrite.
    """


@dataclass(frozen=True, slots=True)
class BronzeRef:
    key: str
    path: Path

    def __str__(self) -> str:
        return self.key


class BronzeStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def key_for(self, raw: RawResponse) -> str:
        """Deterministic key: same provider + endpoint + external_id + payload
        content always produces the same key, so re-fetching identical upstream
        state is a verifiable no-op rather than a duplicate write.
        """
        payload_bytes = json.dumps(raw.payload, sort_keys=True, default=str).encode("utf-8")
        payload_hash = hashlib.sha256(payload_bytes).hexdigest()
        material = f"{raw.provider}|{raw.endpoint}|{raw.external_id}|{payload_hash}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _path_for(self, key: str, provider: str) -> Path:
        # Two levels of fan-out (key[:2]/key[2:4]/) keep any one directory from
        # accumulating hundreds of thousands of entries as the corpus grows.
        return self.root / provider / key[:2] / key[2:4] / f"{key}.json"

    def exists(self, raw: RawResponse) -> bool:
        return self._path_for(self.key_for(raw), raw.provider).exists()

    def write(self, raw: RawResponse) -> BronzeRef:
        """Write once. A second write of byte-identical content is a silent no-op —
        the common case of re-running an idempotent fetch. A second write of
        *different* content under the same key would mean the key derivation is
        broken (the same request produced two different payloads) and is refused
        loudly rather than silently overwriting archival data.
        """
        key = self.key_for(raw)
        path = self._path_for(key, raw.provider)

        record = {
            "provider": raw.provider,
            "endpoint": raw.endpoint,
            "external_id": raw.external_id,
            "fetched_at": raw.fetched_at.isoformat(),
            "http_status": raw.http_status,
            "headers": raw.headers,
            "request_params": raw.request_params,
            "payload": raw.payload,
        }
        new_bytes = json.dumps(record, indent=2, sort_keys=True, default=str).encode("utf-8")

        if path.exists():
            existing = path.read_bytes()
            if existing == new_bytes:
                return BronzeRef(key=key, path=path)
            raise BronzeWriteConflict(
                f"bronze key {key} already exists with different content at {path}"
            )

        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file in the same directory, then atomic rename. A process
        # killed mid-write leaves the .tmp file behind, never a half-written .json
        # that a later read would parse as valid and corrupt.
        tmp = path.with_suffix(".json.tmp")
        tmp.write_bytes(new_bytes)
        tmp.replace(path)
        return BronzeRef(key=key, path=path)

    def read(self, ref: BronzeRef) -> dict[str, Any]:
        return json.loads(ref.path.read_text())
