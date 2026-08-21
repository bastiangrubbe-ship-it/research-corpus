# Supadata API — confirmed schema

Taken from `docs.supadata.ai/api-reference/v1-openapi.json` and the endpoint docs,
2026-08-20. Recorded here so it is never re-derived or guessed at.

Base URL `https://api.supadata.ai/v1`. Auth: `x-api-key` header.
Plan: **30,000 credits/month.**

## Transcript

`GET /youtube/transcript?url=|videoId=[&lang=&text=&chunkSize=]`

```json
{ "content": [ { "text": "...", "offset": 1234, "duration": 567, "lang": "en" } ],
  "lang": "en",
  "availableLangs": ["en", "es", "zh-TW"] }
```

`offset` and `duration` are **milliseconds**. With `text=true`, `content` collapses to
a plain string. Three top-level fields, and that is all.

## Metadata — a separate call

`id, title, description, duration` (seconds), `channel{id,name}`, `tags`,
`transcriptLanguages`; optional `thumbnail, uploadDate, viewCount, likeCount`.

Note `uploadDate` is **optional** — hence `document.published_at_precision`.

## Batch

`POST /youtube/transcript/batch` with `videoIds` | `playlistId` | `channelId`
(+ `limit` ≤ 5000, default 10) → `{"jobId": "..."}`.
Poll `GET /youtube/batch/{jobId}` → `queued | active | completed | failed`.

```json
{ "status": "completed",
  "results": [ { "videoId": "...", "transcript": { "content": "...", "lang": "en",
                                                   "availableLangs": ["en"] } } ],
  "stats": { "total": 2, "succeeded": 1, "failed": 1 },
  "completedAt": "2025-04-03T06:59:53.428Z" }
```

Failed videos appear in `results` with an `errorCode` (e.g. `transcript-unavailable`)
rather than failing the job.

**Credits: 1 for the batch + 1 per video.** A 10-video batch costs 11.

## Errors

`400` parameter validation · `401` missing key · `402` payment required ·
`404` not found · `429` plan limit exceeded · `5xx` infrastructure.

## What it does not provide

This is the part that shapes the build.

| Needed | Available? |
|---|---|
| Speaker attribution / diarization | **No.** Nothing, in any endpoint. |
| Auto-generated vs human-authored | **No.** Not in the response body at all. |
| Publication date | Optional `uploadDate` on the metadata call |
| Source authority | Derivable from `channel` |

When captions are missing, Supadata **silently substitutes Whisper** and returns the
identical shape.

### Corrections from live calls (2026-08-21)

Recorded against the real API. Two things the documentation implies are not true in
practice:

* **There is no `x-billable-requests` header.** The full response header set is
  Cloudflare boilerplate plus `content-type`. Nothing reports credit consumption.
  **Consequence:** our local `CreditLedger` is the only accounting that exists, and it
  can drift from Supadata's real count with no way to detect the divergence. Step 3
  should reconcile against the dashboard periodically rather than trusting the ledger
  indefinitely.
* **HTTP 202 has not been observed.** Both transcript calls returned 200, including a
  video chosen for having minimal captions. The `202 = Whisper` mapping remains
  **inferred and untested**; it is coded defensively and recorded as
  `provenance_confidence='inferred'` if it ever fires, but nothing yet confirms it
  fires at all. Do not treat it as a reliable provenance signal.

### Status codes, as observed

| Code | Meaning | Our mapping |
|---|---|---|
| 200 | transcript returned | success, provenance `unknown` |
| 202 | **never observed** — presumed Whisper path | success, provenance `inferred` |
| 400 | bad parameter, e.g. unsupported `lang` | `InvalidRequest` — do **not** fail over |
| 404 | no transcript for this video | `TranscriptUnavailable` — do not fail over |
| 401 / 402 / 429 | auth, payment, quota | `ProviderBlocked` — fail over |

The 400 case matters: an unsupported `lang` is a malformed request, and failing over
sends the identical malformed request to a second provider that rejects it the same
way. Mapping it to `ProviderBlocked` would waste a credit per call and hide the cause.

### Language codes differ between providers

Supadata normalises to two-letter codes; youtube-transcript-api preserves regional
ones. Same video, same moment:

| | Supadata | youtube-transcript-api |
|---|---|---|
| `availableLangs` | `en, de, ja, pt, es` | `en, de-DE, ja, pt-BR, es-419` |

`transcript_version.available_langs` therefore depends on which provider wrote the
row. Any query filtering on it has to account for both forms.

### Cross-validation

Both providers on `dQw4w9WgXcQ`, first segment: text `[♪♪♪]`, Supadata
`offset: 1360, duration: 1680` (ms), ytapi `start: 1.36, duration: 1.68` (s). The two
normalizers are independent code paths with different input units and they agree
exactly — which is stronger evidence than either asserted against itself.

## Consequence: two providers, not one

`youtube-transcript-api` exposes `is_generated` and is therefore the **only** source of
transcript provenance. Supadata does not have it at any price.

But YouTube blocks cloud-provider IPs, so `youtube-transcript-api` works from a laptop
on a residential connection and will fail on the Linux server this migrates to. The
provider mix is expected to invert on migration:

| | macOS (now) | Linux server (later) |
|---|---|---|
| Primary | youtube-transcript-api (gives provenance) | Supadata (survives the IP block) |
| Fallback | Supadata | — |

`transcript_version.provider` exists from commit one for exactly this reason. Neither
provider gives speaker attribution; that is ours to build, two-tier, in step 4.
