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
  /** run_complete only, YouTube runs: Supadata credit accounting. */
  credits_spent?: number;
  credits_budget?: number;
  /** run_complete only, RSS runs: no credits exist for a plain feed fetch, so the
   * terminal envelope reports counts instead. The per-item events above are
   * identical across both source kinds — ingest_source is adapter-agnostic — but
   * the closing envelope is not, because the two have genuinely different things
   * worth reporting at the end. */
  fetched?: number;
  skipped?: number;
  failed?: number;
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

/** One hit from corpus.mcp.tools.corpus_search. `title` and `url` are genuinely
 * nullable — some ingested documents have no title (metadata fetch failed, or the
 * source never provided one), so every render path must handle null rather than
 * assuming a string. */
export interface SearchHit {
  document_id: string;
  title: string | null;
  url: string | null;
  published_at: string | null;
  score: number;
}

export async function fetchSearch(args: {
  query: string;
  domain?: string;
  topK?: number;
  candidatePool?: number;
  rerank?: boolean;
}): Promise<SearchHit[]> {
  const params = new URLSearchParams({ query: args.query });
  if (args.domain) params.set("domain", args.domain);
  if (args.topK !== undefined) params.set("top_k", String(args.topK));
  if (args.candidatePool !== undefined) params.set("candidate_pool", String(args.candidatePool));
  if (args.rerank !== undefined) params.set("rerank", String(args.rerank));

  const res = await fetch(`${API_BASE}/api/search?${params}`);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail ?? `search failed: ${res.status}`);
  }
  return res.json();
}

/** Full provenance for one document. `is_auto_generated: null` means the provider
 * never told us — it does not mean "human-authored"; `provenance_confidence`
 * records which of those two situations applies. */
export interface Provenance {
  document_id: string;
  title: string | null;
  url: string | null;
  source_title: string | null;
  source_kind: string;
  authority_tier: string;
  domain: string;
  published_at: string | null;
  published_at_precision: string;
  transcript_provider: string | null;
  is_auto_generated: boolean | null;
  provenance_confidence: string | null;
}

export async function fetchProvenance(documentId: string): Promise<Provenance> {
  const res = await fetch(`${API_BASE}/api/documents/${documentId}/provenance`);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail ?? `provenance failed: ${res.status}`);
  }
  return res.json();
}

/* --- analytics -------------------------------------------------------------
 * Five analyses behind one route. The response shape differs per analysis, so
 * these are a discriminated union on `analysis` rather than one loose type —
 * that way the panel can't read `.timeline` off a rising-entities response. */

export type AnalysisType =
  | "rising_entities"
  | "emerging_entities"
  | "saturated_entities"
  | "co_occurrence_drift"
  | "diffusion_timeline";

export interface RisingRow {
  canonical_name: string;
  kind: string;
  recent_count: number;
  prior_count: number;
  /** null when prior_count is 0 — growth from nothing isn't a percentage, it's a
   * new entrant. Render it as "new", never as 0% or Infinity. */
  pct_change: number | null;
}

export interface EmergingRow {
  canonical_name: string;
  kind: string;
  first_mention_date: string;
  mention_count_since: number;
}

export interface SaturationRow {
  canonical_name: string;
  kind: string;
  distinct_sources: number;
  total_mentions: number;
  mentions_per_source: number;
}

export interface DriftPeriod {
  start: string;
  end: string;
  co_occurring_kinds: Record<string, number>;
}

export interface DiffusionRow {
  source_title: string | null;
  first_mention_date: string;
}

export type AnalyticsResponse =
  | { analysis: "rising_entities"; results: RisingRow[] }
  | { analysis: "emerging_entities"; since: string; results: EmergingRow[] }
  | { analysis: "saturated_entities"; results: SaturationRow[] }
  | { analysis: "co_occurrence_drift"; entity: string; periods: DriftPeriod[] }
  | { analysis: "diffusion_timeline"; entity: string; timeline: DiffusionRow[] };

export async function fetchAnalytics(args: {
  analysis: AnalysisType;
  domain?: string;
  entityName?: string;
}): Promise<AnalyticsResponse> {
  const params = new URLSearchParams({ analysis: args.analysis });
  if (args.domain) params.set("domain", args.domain);
  if (args.entityName) params.set("entity_name", args.entityName);

  const res = await fetch(`${API_BASE}/api/analytics?${params}`);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail ?? `analytics failed: ${res.status}`);
  }
  return res.json();
}

/* --- coverage --------------------------------------------------------------
 * Whether the corpus actually covers a topic, as opposed to merely returning its
 * ten best guesses for it. `n_documents` is 0 for grade "none" even though
 * `related_entities` may be populated — that is deliberate, not inconsistent: no
 * documents answer the query, but the nearest ones still say what the corpus
 * does hold. */

export type CoverageGrade = "none" | "thin" | "partial" | "good";

export interface CoverageReport {
  query: string;
  grade: CoverageGrade;
  headline: string;
  best_score: number;
  /** How much of the corpus the semantic lane can actually see. A verdict
   * computed against a partial index is a claim about that subset only. */
  indexed_documents: number;
  total_documents: number;
  n_documents: number;
  n_sources: number;
  date_earliest: string | null;
  date_latest: string | null;
  span_days: number | null;
  /** [source title, matching document count] */
  top_sources: [string, number][];
  domain_breakdown: Record<string, number>;
  absent_domains: string[];
  authority_breakdown: Record<string, number>;
  /** [entity name, kind, mention count] */
  related_entities: [string, string, number][];
  suggestions: string[];
}

export async function fetchCoverage(args: {
  query: string;
  domain?: string;
}): Promise<CoverageReport> {
  const params = new URLSearchParams({ query: args.query });
  if (args.domain) params.set("domain", args.domain);

  const res = await fetch(`${API_BASE}/api/coverage?${params}`);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail ?? `coverage failed: ${res.status}`);
  }
  return res.json();
}

/* --- eval run history ------------------------------------------------------ */

export interface EvalRunSummary {
  name: string;
  timestamp: string;
  top_k: number;
  n_queries: number;
}

export interface EvalLane {
  lane: string;
  n_candidates: number;
  /** null means the lane returned no candidates for this query — it did not
   * compete, which is different from scoring zero. Render as "n/a", never 0. */
  precision_strict: number | null;
  precision_lenient: number | null;
  recall_strict: number | null;
  recall_lenient: number | null;
}

export interface EvalQueryResult {
  query_id: string;
  query_text: string;
  category: string;
  pool_size: number;
  n_relevant_strict: number;
  n_relevant_lenient: number;
  lanes: EvalLane[];
}

export interface EvalRun {
  timestamp: string;
  top_k: number;
  queries: EvalQueryResult[];
}

export interface EvalDiffLane {
  lane: string;
  before: number;
  after: number;
  delta: number;
}

export interface EvalDiffRow {
  query_id: string;
  only_in_one_run: boolean;
  category: string | null;
  lanes: EvalDiffLane[];
}

export async function fetchEvalRuns(): Promise<EvalRunSummary[]> {
  const res = await fetch(`${API_BASE}/api/eval/runs`);
  if (!res.ok) throw new Error(`GET /api/eval/runs failed: ${res.status}`);
  return res.json();
}

export async function fetchEvalRun(name: string): Promise<EvalRun> {
  const res = await fetch(`${API_BASE}/api/eval/runs/${encodeURIComponent(name)}`);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail ?? `eval run failed: ${res.status}`);
  }
  return res.json();
}

export async function fetchEvalDiff(a: string, b: string): Promise<EvalDiffRow[]> {
  const params = new URLSearchParams({ a, b });
  const res = await fetch(`${API_BASE}/api/eval/diff?${params}`);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail ?? `eval diff failed: ${res.status}`);
  }
  return res.json();
}

/* --- MCP tool catalog / tester -------------------------------------------- */

export interface McpTool {
  name: string;
  description: string;
  /** JSON-Schema properties for the tool's arguments. */
  properties: Record<string, { type?: string; description?: string; default?: unknown }>;
  required: string[];
}

export interface McpCallResult {
  name: string;
  is_error: boolean;
  text: string;
  /** Populated only when the tool returns a shape MCP could structure; the text
   * block is always present, so render that and treat this as a bonus. */
  structured: Record<string, unknown> | null;
}

export async function fetchMcpTools(): Promise<McpTool[]> {
  const res = await fetch(`${API_BASE}/api/mcp/tools`);
  if (!res.ok) throw new Error(`GET /api/mcp/tools failed: ${res.status}`);
  return res.json();
}

export async function callMcpTool(
  name: string,
  args: Record<string, unknown>
): Promise<McpCallResult> {
  const res = await fetch(`${API_BASE}/api/mcp/call`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, arguments: args }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail ?? `mcp call failed: ${res.status}`);
  }
  return res.json();
}

/* --- enrichment triggers ---------------------------------------------------
 * All three also exist as CLI flows; the dashboard runs an ad-hoc extra pass and
 * never disables a schedule. */

export interface EntityStatus {
  pending: number;
  extractor_version: string;
}

export interface EntityBackfillEvent {
  kind: "extracted" | "failed" | "run_complete" | "error";
  document_id?: string;
  mentions?: number;
  detail?: string;
  status?: string;
  error?: string | null;
  succeeded?: number;
  failed?: number;
  total?: number;
}

export async function fetchEntityStatus(): Promise<EntityStatus> {
  const res = await fetch(`${API_BASE}/api/entities/status`);
  if (!res.ok) throw new Error(`GET /api/entities/status failed: ${res.status}`);
  return res.json();
}

export async function startEntityBackfill(args: {
  limit?: number;
  concurrency?: number;
}): Promise<{ run_id: string }> {
  const res = await fetch(`${API_BASE}/api/entities/backfill/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(args),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail ?? `start failed: ${res.status}`);
  }
  return res.json();
}

/** Same contract as streamRun: returns a close function the caller must invoke. */
export function streamEntityBackfill(
  runId: string,
  onEvent: (event: EntityBackfillEvent) => void,
  onDone: () => void
): () => void {
  const source = new EventSource(`${API_BASE}/api/entities/stream/${runId}`);
  source.onmessage = (message) => {
    const payload = JSON.parse(message.data) as EntityBackfillEvent;
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

export async function fetchSpeakerStatus(): Promise<{ by_method: Record<string, number> }> {
  const res = await fetch(`${API_BASE}/api/speakers/status`);
  if (!res.ok) throw new Error(`GET /api/speakers/status failed: ${res.status}`);
  return res.json();
}

export async function attributeSpeakers(limit?: number): Promise<{ processed: number }> {
  const res = await fetch(`${API_BASE}/api/speakers/attribute`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(limit === undefined ? {} : { limit }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail ?? `attribute failed: ${res.status}`);
  }
  return res.json();
}

export async function restoreDocument(
  documentId: string
): Promise<{ transcript_version_id: string }> {
  const res = await fetch(`${API_BASE}/api/documents/${documentId}/restore`, { method: "POST" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail ?? `restore failed: ${res.status}`);
  }
  return res.json();
}

/* --- RSS feeds -------------------------------------------------------------
 * Adds go through seeds/rss_feeds.yaml, never straight to the database — same
 * reviewed-source-of-truth rule as YouTube channels. */

export interface FeedPreview {
  url: string;
  title: string;
  entry_count: number;
  sample_titles: string[];
  /** Entries carry only a link, no real body (a link-aggregator feed). It will
   * ingest fine but produce documents with almost no text — worth knowing first. */
  link_only: boolean;
}

export interface FeedRow {
  url: string;
  title: string;
  domain: string;
  authority_tier: string;
  added_at: string;
}

export async function fetchFeeds(): Promise<FeedRow[]> {
  const res = await fetch(`${API_BASE}/api/rss/feeds`);
  if (!res.ok) throw new Error(`GET /api/rss/feeds failed: ${res.status}`);
  return res.json();
}

async function rssPost<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}/api/rss/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail ?? `rss ${path} failed: ${res.status}`);
  }
  return res.json();
}

export function previewFeed(url: string): Promise<FeedPreview> {
  return rssPost<FeedPreview>("preview", { url });
}

export function addFeed(args: {
  url: string;
  domain: string;
  authority_tier: string;
}): Promise<FeedRow> {
  return rssPost<FeedRow>("add", args);
}

export function startFeedRun(args: {
  url: string;
  domain: string;
  authority_tier: string;
  limit?: number;
}): Promise<{ run_id: string }> {
  return rssPost<{ run_id: string }>("start", args);
}

/** Per-item events match the YouTube run stream exactly (ingest_source is
 * adapter-agnostic); only the closing run_complete envelope differs — counts
 * instead of credits. */
export function streamFeedRun(
  runId: string,
  onEvent: (event: IngestEventPayload) => void,
  onDone: () => void
): () => void {
  const source = new EventSource(`${API_BASE}/api/rss/stream/${runId}`);
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

export interface CreditSummary {
  budget: number;
  used_today: number;
  used_this_month: number;
  used_last_30_days: number;
  avg_per_day_last_30_days: number;
  has_data: boolean;
}

export async function fetchCredits(): Promise<CreditSummary> {
  const res = await fetch(`${API_BASE}/api/credits`);
  if (!res.ok) throw new Error(`GET /api/credits failed: ${res.status}`);
  return res.json();
}

// --- Synthesis -------------------------------------------------------------
//
// Two calls on purpose. `plan` prices a filter without spending anything; `run`
// is one LLM call per matched document and takes minutes. The panel is expected
// to plan freely and run only on an explicit click.

export interface SynthesisPlan {
  dry_run: true;
  matched_documents: number;
  documents_to_read: number;
  escalated_documents: number;
  total_chars: number;
  capped: boolean;
  dropped_by_cap: number;
  llm_calls: number;
}

export interface SynthesisCitation {
  marker: number;
  document_id: string;
  title: string | null;
  url: string | null;
  published_at: string | null;
  source_title: string | null;
  claim: string;
  quote: string;
}

export interface SynthesisReport {
  question: string;
  answer: string;
  citations: SynthesisCitation[];
  matched_documents: number;
  documents_read: number;
  documents_addressing: number;
  documents_failed: number;
  capped: boolean;
  dropped_by_cap: number;
  invalid_markers: number[];
  prompt_version: string;
}

export interface SynthesisFilter {
  question?: string;
  query?: string;
  domain?: string;
  entityName?: string;
  since?: string;
  until?: string;
  maxDocuments?: number;
}

function synthesisParams(args: SynthesisFilter): URLSearchParams {
  const params = new URLSearchParams();
  if (args.question) params.set("question", args.question);
  if (args.query) params.set("query", args.query);
  if (args.domain) params.set("domain", args.domain);
  if (args.entityName) params.set("entity_name", args.entityName);
  if (args.since) params.set("since", args.since);
  if (args.until) params.set("until", args.until);
  if (args.maxDocuments !== undefined) params.set("max_documents", String(args.maxDocuments));
  return params;
}

export async function fetchSynthesisPlan(args: SynthesisFilter): Promise<SynthesisPlan> {
  const res = await fetch(`${API_BASE}/api/synthesis/plan?${synthesisParams(args)}`);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail ?? `synthesis plan failed: ${res.status}`);
  }
  return res.json();
}

export async function runSynthesis(args: SynthesisFilter): Promise<SynthesisReport> {
  const res = await fetch(`${API_BASE}/api/synthesis/run?${synthesisParams(args)}`, {
    method: "POST",
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail ?? `synthesis failed: ${res.status}`);
  }
  return res.json();
}
