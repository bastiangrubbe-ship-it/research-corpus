# The inbox — how browser captures enter the corpus

This file is a **contract between two repositories**. `tab-capture` (a Chrome extension
and its native messaging host) writes files here; `flows/ingest_inbox.py` in this repo
reads them. Neither side may change the layout, the drop protocol, or the schemas
without changing this file and both implementations.

It exists because the seam between two repos in two languages is the one place where
"obvious" assumptions silently diverge.

## Why an inbox and not bronze

The extension does **not** write into `bronze/`. Three independent reasons, any one
sufficient:

1. Bronze keys are
   `sha256(provider|endpoint|external_id|sha256(canonical_json(payload)))`, derived by
   `BronzeStore.key_for()`. An external process in another language would have to
   reproduce that byte-for-byte — including `json.dumps(..., sort_keys=True,
   default=str)` — and keep reproducing it forever. Two implementations of one
   content-address that must agree is the worst available coupling.
2. `BronzeWriteConflict` and the tmp+rename atomicity assume exactly one writer. A
   browser killed mid-write inside bronze leaves something that *looks* like an
   archival record.
3. Bronze is the rebuild guarantee. The inbox is a staging area where partial writes,
   garbage and retries are **expected**. Those two things need opposite failure
   semantics.

Intake reads the inbox, derives canonical bronze records through `BronzeStore`, and only
then considers a capture ingested.

## Layout

```
$PROJECT_DATA_DIR/inbox/          # CORPUS_INBOX_DIR; defaults to <data>/inbox
  pending/                        # the extension writes here, and only here
  processed/YYYY/MM/DD/           # moved here after a successful ingest
  failed/                         # moved here, alongside a <name>.error.txt
```

`processed/` is **disposable**. Bronze already holds the verbatim payload and the PDF
blob, so nothing is lost by deleting it. Do not treat it as an archive.

Neither side ever creates the tree implicitly — see *Never mkdir* below.

## Drop protocol

The extension must honour all four rules. Intake relies on every one of them.

1. **Atomic rename, same directory.** Write `pending/<name>.part`, `fsync`, then
   `os.replace()` onto the final name. The temp file must be in the *same directory*:
   `os.replace` across filesystems raises `EXDEV`, and a temp in `/tmp` is on a
   different device than a `/Volumes/…` share.
2. **PDF first, JSON last.** For a page capture, rename the `.pdf` into place *before*
   the `.page.json`. The sidecar names the PDF, so a `.page.json` appearing means its
   PDF is already complete on disk. This is a second line of defence that does not
   depend on the filesystem: rename atomicity on SMB is a weaker guarantee than on APFS.
3. **The batch manifest is written last of all.** Its presence means the batch finished.
4. **Intake ignores** dotfiles and anything ending in `.part`.

## Idempotency

Three independent layers, and their independence is the point:

1. **Bronze content-addressing** — re-ingesting an identical capture produces an
   identical record, which `BronzeStore.write()` already treats as a silent no-op.
2. **`uq_document_tenant_source_external`** — the second attempt finds the document and
   skips before spending a transcript credit.
3. **The move out of `pending/`** — the file stops being seen.

**Intake must commit the database transaction first, then move the file.** If the move
fails (a crash in between), layers 1 and 2 make the retry harmless. The reverse ordering
can lose a capture entirely.

## Never mkdir the output directory

On macOS, if `/Volumes/research` is not mounted and something creates
`/Volumes/research/inbox`, it creates a **local** directory on the boot disk that then
**shadows the mountpoint**. Captures land there, report success, and disappear from view
the moment the share remounts on top of them.

So: the native host refuses to create the output directory, and there is **no local
fallback directory, ever**. Falling back would mean files silently landing somewhere
intake never looks — worse than not capturing at all. The extension pre-flights the path
before touching a single tab and aborts the whole batch if it is unavailable.

## File naming

```
<capture_id>_<NN>_<host>_<slug>.pdf
<capture_id>_<NN>_<host>_<slug>.page.json
<capture_id>.youtube.json
<capture_id>.capture.json
```

- `capture_id` — `20260824T134501Z-3f9a2c`: UTC ISO-basic timestamp of batch start plus
  6 hex random. Sortable, and collision-free for two batches in the same second.
- `NN` — two-digit index within the batch.
- `host` — URL hostname, leading `www.` stripped.
- `slug` — last meaningful path segment or the title, lowercased, non-`[a-z0-9-]`
  collapsed to `-`, capped at 60 chars.

Flat — no subdirectories in `pending/`. Filenames stay well under the 255-byte limit
SMB shares enforce.

A page's PDF and its sidecar are linked **twice**: by the shared filename stem and by
`capture_id`/`item_id` inside the JSON. A human renaming a file should not sever the
association.

There is deliberately **no user-configurable filename template**. This naming is a
contract another repo parses.

## Schemas

Every JSON file carries `schema` as its first key. Intake dispatches on it and refuses
anything it does not recognise, rather than guessing.

Absent values are `null`, **never** `""` and never omitted — consistent with this
project's rule that recording what is unknown is the point, and that "the source did not
tell us" is a different claim from "empty".

### `tab-capture/youtube-batch@1` — `<capture_id>.youtube.json`

One file per batch, listing every YouTube tab that was selected. These are **single
videos to ingest**, not channels to backfill.

```json
{
  "schema": "tab-capture/youtube-batch@1",
  "capture_id": "20260824T134501Z-3f9a2c",
  "captured_at": "2026-08-24T13:45:01.123Z",
  "tool": { "name": "tab-capture", "extension_version": "0.1.0", "host_version": "0.1.0" },
  "count": 1,
  "items": [
    {
      "item_id": "20260824T134501Z-3f9a2c-01",
      "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s&list=PLabc",
      "canonical_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
      "kind": "video",
      "video_id": "dQw4w9WgXcQ",
      "playlist_id": "PLabc",
      "channel_handle": null,
      "channel_url": null,
      "title": "Attention Is All You Need — explained",
      "tab_title_raw": "Attention Is All You Need — explained - YouTube",
      "favicon_url": "https://www.youtube.com/s/desktop/…/favicon_32x32.png",
      "tab": { "window_id": 1, "tab_id": 345, "index": 4, "incognito": false },
      "captured_at": "2026-08-24T13:45:01.180Z"
    }
  ]
}
```

`kind` ∈ `video | shorts | playlist | channel | search | unknown`. Intake ingests only
`video` and `shorts`; anything else is recorded and skipped.

**`title` and `tab_title_raw` are advisory only.** They are archived in bronze and must
never be written to `document.title` — yt-dlp is authoritative for YouTube metadata, and
the extension deliberately does not scrape the YouTube DOM. The extension's job is to
deliver a reliable **identifier**; enrichment happens here.

### `tab-capture/page@1` — `<stem>.page.json`

```json
{
  "schema": "tab-capture/page@1",
  "capture_id": "20260824T134501Z-3f9a2c",
  "item_id": "20260824T134501Z-3f9a2c-02",
  "captured_at": "2026-08-24T13:45:03.221Z",

  "url": "https://arxiv.org/abs/1706.03762",
  "final_url": "https://arxiv.org/abs/1706.03762",
  "canonical_url": "https://arxiv.org/abs/1706.03762",
  "http_status": null,

  "tab_title": "[1706.03762] Attention Is All You Need",
  "lang": "en",

  "pdf": {
    "filename": "20260824T134501Z-3f9a2c_02_arxiv.org_attention-is-all-you-need.pdf",
    "bytes": 482113,
    "sha256": "9f2c1e…",
    "print_options": {
      "printBackground": true, "landscape": false, "scale": 1,
      "paperWidth": 8.27, "paperHeight": 11.69,
      "marginTop": 0.4, "marginBottom": 0.4, "marginLeft": 0.4, "marginRight": 0.4,
      "emulated_media": "screen"
    }
  },

  "readability": {
    "ok": true,
    "error": null,
    "title": "Attention Is All You Need",
    "byline": null,
    "excerpt": "The dominant sequence transduction models…",
    "site_name": "arXiv.org",
    "dir": "ltr",
    "lang": "en",
    "length": 18422,
    "text": "paragraph one\n\nparagraph two\n\n…",
    "content_html": "<div id=\"readability-page-1\">…</div>",
    "library_version": "0.5.0"
  },

  "raw_html": { "outer_html": "<html>…</html>", "bytes": 942144, "truncated": false },

  "meta": {
    "description": null, "og_title": null, "og_type": null, "og_site_name": null,
    "article_published_time": null, "author": null, "keywords": null
  },

  "browser": { "user_agent": "Mozilla/5.0 …", "chrome_version": "…" },
  "tab": { "window_id": 1, "tab_id": 346, "index": 5, "incognito": false },
  "tool": { "name": "tab-capture", "extension_version": "0.1.0", "host_version": "0.1.0" }
}
```

Notes that are load-bearing:

- **`http_status` is always `null`.** The extension did not fetch the page and does not
  observe its response status. It is present as an explicit unknown rather than absent
  or invented.
- **`pdf.sha256` describes the bytes actually on disk.** The native host computes it
  after writing and injects it into the sidecar before writing that. Intake may verify
  rather than trust; a mismatch is corruption.
- **`readability.ok: false` is not a failed capture.** A dashboard or a search-results
  page legitimately has no article. The PDF is still written and still archived. Intake
  records `DocumentStatus.FAILED` for the *text*, while the PDF and `outer_html` remain
  in bronze so the text can be re-derived later by a better extractor.
- **`raw_html.truncated`** is set when `outer_html` was dropped to keep the native
  message under its ceiling. `bytes` still reports the real size.
- `readability.text` is the **only** field text is derived from. Every other field under
  `readability` is stored verbatim in bronze.

### `tab-capture/manifest@1` — `<capture_id>.capture.json`

```json
{
  "schema": "tab-capture/manifest@1",
  "capture_id": "20260824T134501Z-3f9a2c",
  "started_at": "2026-08-24T13:45:01.123Z",
  "finished_at": "2026-08-24T13:45:19.884Z",
  "output_dir": "/Volumes/research/inbox/pending",
  "counts": { "requested": 7, "written": 5, "failed": 1, "skipped": 1 },
  "items": [
    { "item_id": "…-02", "kind": "page", "url": "…", "status": "written",
      "files": ["….pdf", "….page.json"], "error": null },
    { "item_id": "…-06", "kind": "page", "url": "…", "status": "failed",
      "files": [], "error": { "code": "debugger_attach_failed",
                              "message": "DevTools is open on this tab" } }
  ]
}
```

Intake **ignores** this file for ingestion purposes and moves it to `processed/`
alongside the batch. It exists so a human can reconcile "did I get everything" without
diffing directories, and because it is the only record of the **failures**, which are
otherwise invisible on disk.

## Capture files are untrusted input

A capture's contents are derived from a web page, and a page controls its own HTML,
title, and metadata. Intake treats every field as hostile data:

- **`pdf.filename` is attacker-influenceable.** Resolve it strictly as a sibling of the
  JSON and reject anything containing a path separator, `..`, a NUL, or resolving
  outside `pending/`. Never `Path(pending) / capture["pdf"]["filename"]` without that
  check — that is a path-traversal write.
- **`url` must be validated as `http` or `https`** before it reaches `normalize_url` or
  `document.url`. Reject `file:`, `javascript:`, `data:`.
- Titles, bylines, excerpts and HTML are stored and indexed, never rendered or executed.
  The entity extractor already runs with `--allowedTools ""` for this reason.

## What intake does with a capture

Summarised here so both sides can see the whole path; the implementation lives in
`src/corpus/ingest/inbox.py`.

| Capture | `Source` | `Document` | Text |
|---|---|---|---|
| `youtube-batch@1` item | the video's **real channel**, `kind=YOUTUBE_CHANNEL`, `external_id=@handle` | via the normal yt-dlp + transcript path | the usual transcript providers |
| `page@1` | the **site**, `kind=WEB`, `external_id=<hostname>` | `external_id = sha256(normalize_url(url))`, `url` verbatim | `TranscriptVersion(provider=NATIVE)`, one `Segment` per paragraph, offsets `NULL` |

Two invariants worth stating explicitly, because both are non-obvious and a future
"helpful" change could silently reverse them:

- **Intake never calls `append_seed()`.** Creating a `source` row does *not* enroll a
  channel for backfill — only `seeds/youtube_channels.yaml` does that, and nothing reads
  the `source` table to decide what to ingest. This is precisely what makes "capture one
  video" mean one video.
- **Captured videos attach to their real channel**, not a synthetic `browser-capture`
  source. `uq_document_tenant_source_external` is scoped by `source_id`, so a synthetic
  source would produce a *second* document for a video later backfilled through its
  channel — double-counting every entity mention in the analytics this corpus exists for.
