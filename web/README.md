# research-corpus dashboard

Local ingestion dashboard: live progress for a running channel backfill, the seed
table, manual channel/video add, and folder-watch configuration. Talks to the
FastAPI backend in `src/corpus/web/` (this project's Python side) over HTTP + SSE.

Built on [`@bastiangrubbe/ui-kit`](https://github.com/bastiangrubbe-ship-it/ui-kit) —
`ProgressBar` and `StatTile` are components from there, not local to this app.

## Run it

Two processes, both local-only:

```bash
# from the repo root
uv run uvicorn corpus.web.app:app --host 127.0.0.1 --port 8420

# from web/
pnpm install
pnpm dev
```

Open <http://localhost:5173>. The backend binds to `127.0.0.1` only and the two
ports are hardcoded on both sides (CORS in `corpus/web/app.py`, `server.port` in
`vite.config.ts`) — this is a local single-operator tool, not meant to be
network-reachable.

## What it does

- **Run a channel** — starts an ingestion run against one seed-table entry, streams
  live progress (discovering → fetching → fetched/skipped/failed → done) over SSE.
- **Add a channel** — paste a channel URL, a bare `@handle`, or a single video URL
  (auto-resolves to its parent channel). Appends to `seeds/youtube_channels.yaml`
  with `domain`/`authority_tier` set to `unknown`, pending review — never bypasses
  that file.
- **Watch a folder** — configures a server-side `watchdog` observer on a path you
  type in. Browsers cannot continuously watch a filesystem path themselves; see
  `docs/DECISIONS.md` in the repo root for why this couldn't be a browser directory
  picker.
- **Seed table** — a live read of the same YAML file everything else writes to.

## Develop

```bash
pnpm typecheck
pnpm lint
pnpm build
```
