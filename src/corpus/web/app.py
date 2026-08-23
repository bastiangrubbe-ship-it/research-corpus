"""FastAPI dashboard backend.

Local, single-operator tool: no auth, binds to localhost only (see
scripts/run_web.py), CORS opened for the dev-server origin. None of this is meant
to be internet-facing.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from corpus.config import get_settings
from corpus.db.session import tenant_session
from corpus.ingest.runner import resolve_tenant_id
from corpus.ops.credit_usage import summarize
from corpus.web.runs import manager
from corpus.web.seeds import SeedInputError, append_seed, load_all_seeds, resolve_input
from corpus.web.watch import watcher

app = FastAPI(title="research-corpus dashboard")

# Local dev only: the Vite dev server runs on a different port than this API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/seeds")
def get_seeds() -> list[dict[str, Any]]:
    return load_all_seeds()


@app.get("/api/credits")
def get_credits() -> dict[str, Any]:
    """remaining_estimate is our own figure against the configured budget, never
    a number confirmed by Supadata — it has no endpoint that reports usage back.
    """
    settings = get_settings()
    tenant_id = resolve_tenant_id(settings)
    with tenant_session(tenant_id) as session:
        summary = summarize(
            session,
            tenant_id=tenant_id,
            provider="supadata",
            budget=settings.supadata_monthly_credits,
        )
    return {
        "budget": summary.budget,
        "used_today": summary.used_today,
        "used_this_month": summary.used_this_month,
        "used_last_30_days": summary.used_last_30_days,
        "avg_per_day_last_30_days": round(summary.avg_per_day_last_30_days, 2),
        "remaining_estimate": summary.remaining_estimate,
        "has_data": summary.has_data,
    }


class StartRunRequest(BaseModel):
    phase: str | None = None
    handle: str | None = None
    limit: int | None = None


@app.post("/api/ingest/start")
def start_run(body: StartRunRequest) -> dict[str, str]:
    try:
        run_id = manager.start(phase=body.phase, handle=body.handle, limit=body.limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"run_id": run_id}


@app.get("/api/ingest/stream/{run_id}")
async def stream_run(run_id: str) -> EventSourceResponse:
    return EventSourceResponse(manager.stream(run_id))


class ManualAddRequest(BaseModel):
    url: str
    domain: str = "unknown"
    authority_tier: str = "unknown"


@app.post("/api/seeds/manual")
def manual_add(body: ManualAddRequest) -> dict[str, Any]:
    try:
        resolved = resolve_input(body.url)
        return append_seed(resolved, domain=body.domain, authority_tier=body.authority_tier)
    except SeedInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class WatchConfigRequest(BaseModel):
    path: str


@app.get("/api/watch")
def get_watch_status() -> dict[str, Any]:
    return {"watched_path": watcher.watched_path}


@app.post("/api/watch")
def configure_watch(body: WatchConfigRequest) -> dict[str, Any]:
    def on_event(event: dict[str, Any]) -> None:
        # Folder-watch events are logged (see watch.py); a future iteration could
        # forward them to their own SSE stream if the dashboard needs to show
        # "channel X was just added by the watcher" live rather than on next poll.
        pass

    try:
        watcher.start(body.path, on_event)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"watched_path": watcher.watched_path}


@app.delete("/api/watch")
def stop_watch() -> dict[str, Any]:
    watcher.stop()
    return {"watched_path": None}
