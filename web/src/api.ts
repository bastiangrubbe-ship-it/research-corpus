const API_BASE = "http://127.0.0.1:8420";

export interface SeedRow {
  handle: string;
  name: string;
  domain: string;
  authority_tier: string;
  phase: string;
  videos_at_survey?: number;
  subscribers_at_survey?: number;
  note?: string;
}

export async function fetchSeeds(): Promise<SeedRow[]> {
  const res = await fetch(`${API_BASE}/api/seeds`);
  if (!res.ok) throw new Error(`GET /api/seeds failed: ${res.status}`);
  return res.json();
}

export async function startRun(args: {
  phase?: string;
  handle?: string;
  limit?: number;
}): Promise<{ run_id: string }> {
  const res = await fetch(`${API_BASE}/api/ingest/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(args),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail ?? `POST /api/ingest/start failed: ${res.status}`);
  }
  return res.json();
}

/** One event as emitted by corpus.ingest.pipelines.IngestEvent, plus the
 * synthetic "run_complete" envelope the run manager appends at the end. */
export interface IngestEventPayload {
  kind:
    | "discovering"
    | "discovered"
    | "fetching"
    | "fetched"
    | "skipped"
    | "failed"
    | "budget_exceeded"
    | "done"
    | "run_complete"
    | "error";
  source_handle?: string;
  detail?: string;
  current?: number;
  total?: number;
  extra?: Record<string, unknown>;
  status?: string;
  error?: string | null;
  credits_spent?: number;
  credits_budget?: number;
}

/** Opens an SSE connection for one run. Returns a close function — callers must
 * invoke it (e.g. in a React effect cleanup) or the connection outlives the
 * component that opened it. */
export function streamRun(
  runId: string,
  onEvent: (event: IngestEventPayload) => void,
  onDone: () => void
): () => void {
  const source = new EventSource(`${API_BASE}/api/ingest/stream/${runId}`);
  source.onmessage = (message) => {
    const payload = JSON.parse(message.data) as IngestEventPayload;
    onEvent(payload);
    if (payload.kind === "run_complete") {
      source.close();
      onDone();
    }
  };
  source.onerror = () => {
    source.close();
    onDone();
  };
  return () => source.close();
}

export async function manualAdd(url: string): Promise<SeedRow> {
  const res = await fetch(`${API_BASE}/api/seeds/manual`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail ?? `manual add failed: ${res.status}`);
  }
  return res.json();
}

export async function fetchWatchStatus(): Promise<{ watched_path: string | null }> {
  const res = await fetch(`${API_BASE}/api/watch`);
  if (!res.ok) throw new Error(`GET /api/watch failed: ${res.status}`);
  return res.json();
}

export async function configureWatch(path: string): Promise<{ watched_path: string | null }> {
  const res = await fetch(`${API_BASE}/api/watch`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail ?? `configure watch failed: ${res.status}`);
  }
  return res.json();
}

export async function stopWatch(): Promise<void> {
  await fetch(`${API_BASE}/api/watch`, { method: "DELETE" });
}

export interface CreditSummary {
  budget: number;
  used_today: number;
  used_this_month: number;
  used_last_30_days: number;
  avg_per_day_last_30_days: number;
  remaining_estimate: number;
  has_data: boolean;
}

export async function fetchCredits(): Promise<CreditSummary> {
  const res = await fetch(`${API_BASE}/api/credits`);
  if (!res.ok) throw new Error(`GET /api/credits failed: ${res.status}`);
  return res.json();
}
