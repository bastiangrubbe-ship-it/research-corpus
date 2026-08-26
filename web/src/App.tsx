import { ProgressBar, StatTile } from "@bastiangrubbe/ui-kit";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  type CreditSummary,
  type IngestEventPayload,
  type SeedRow,
  configureWatch,
  fetchCredits,
  fetchSeeds,
  fetchWatchStatus,
  manualAdd,
  startRun,
  stopWatch,
  streamRun,
} from "./api";
import styles from "./App.module.css";
import AnalyticsPanel from "./components/AnalyticsPanel";
import CoveragePanel from "./components/CoveragePanel";
import EnrichmentPanel from "./components/EnrichmentPanel";
import EvalHistoryPanel from "./components/EvalHistoryPanel";
import McpPanel from "./components/McpPanel";
import RssPanel from "./components/RssPanel";
import SearchPanel from "./components/SearchPanel";
import SynthesisPanel from "./components/SynthesisPanel";

interface RunState {
  runId: string;
  handle: string;
  current: number;
  total: number;
  fetched: number;
  skipped: number;
  failed: number;
  status: "running" | "done" | "failed";
  lastDetail: string;
  creditsSpent: number | undefined;
  creditsBudget: number | undefined;
  error: string | null | undefined;
}

function reduceEvent(prev: RunState, event: IngestEventPayload): RunState {
  switch (event.kind) {
    case "discovered":
      return { ...prev, total: event.total ?? prev.total };
    case "fetching":
      return { ...prev, current: event.current ?? prev.current, lastDetail: event.detail ?? "" };
    case "fetched":
      return {
        ...prev,
        current: event.current ?? prev.current,
        fetched: prev.fetched + 1,
        lastDetail: event.detail ?? "",
      };
    case "skipped":
      return { ...prev, current: event.current ?? prev.current, skipped: prev.skipped + 1 };
    case "failed":
      return {
        ...prev,
        current: event.current ?? prev.current,
        failed: prev.failed + 1,
        lastDetail: event.detail ?? prev.lastDetail,
      };
    case "run_complete":
      return {
        ...prev,
        status: event.status === "failed" ? "failed" : "done",
        error: event.error,
        creditsSpent: event.credits_spent,
        creditsBudget: event.credits_budget,
      };
    default:
      return prev;
  }
}

export default function App() {
  const [seeds, setSeeds] = useState<SeedRow[]>([]);
  const [seedsError, setSeedsError] = useState<string | null>(null);
  const [handle, setHandle] = useState("");
  const [limit, setLimit] = useState("3");
  const [run, setRun] = useState<RunState | null>(null);
  const [manualUrl, setManualUrl] = useState("");
  const [manualStatus, setManualStatus] = useState<string | null>(null);
  const [watchPath, setWatchPath] = useState("");
  const [watchedPath, setWatchedPath] = useState<string | null>(null);
  const [credits, setCredits] = useState<CreditSummary | null>(null);
  const closeStreamRef = useRef<(() => void) | null>(null);

  const reloadSeeds = useCallback(() => {
    fetchSeeds()
      .then(setSeeds)
      .catch((err: Error) => setSeedsError(err.message));
  }, []);

  const reloadCredits = useCallback(() => {
    fetchCredits()
      .then(setCredits)
      .catch(() => {
        /* backend not up yet — the seeds panel already surfaces that error */
      });
  }, []);

  useEffect(() => {
    reloadSeeds();
    reloadCredits();
    fetchWatchStatus()
      .then((s) => setWatchedPath(s.watched_path))
      .catch(() => {
        /* backend not up yet — the seeds panel already surfaces that error */
      });
    return () => closeStreamRef.current?.();
  }, [reloadSeeds, reloadCredits]);

  const handleStartRun = async () => {
    if (!handle.trim()) return;
    try {
      const { run_id } = await startRun({
        handle: handle.trim(),
        ...(limit ? { limit: Number(limit) } : {}),
      });
      setRun({
        runId: run_id,
        handle: handle.trim(),
        current: 0,
        total: 0,
        fetched: 0,
        skipped: 0,
        failed: 0,
        status: "running",
        lastDetail: "",
        creditsSpent: undefined,
        creditsBudget: undefined,
        error: undefined,
      });
      closeStreamRef.current?.();
      closeStreamRef.current = streamRun(
        run_id,
        (event) => setRun((prev) => (prev ? reduceEvent(prev, event) : prev)),
        () => {
          reloadSeeds();
          reloadCredits();
        }
      );
    } catch (err) {
      setRun({
        runId: "",
        handle: handle.trim(),
        current: 0,
        total: 0,
        fetched: 0,
        skipped: 0,
        failed: 0,
        status: "failed",
        lastDetail: "",
        creditsSpent: undefined,
        creditsBudget: undefined,
        error: err instanceof Error ? err.message : String(err),
      });
    }
  };

  const handleManualAdd = async () => {
    if (!manualUrl.trim()) return;
    setManualStatus("resolving…");
    try {
      const row = await manualAdd(manualUrl.trim());
      setManualStatus(`added ${row.handle} — ${row.name}`);
      setManualUrl("");
      reloadSeeds();
    } catch (err) {
      setManualStatus(err instanceof Error ? err.message : String(err));
    }
  };

  const handleConfigureWatch = async () => {
    if (!watchPath.trim()) return;
    try {
      const { watched_path } = await configureWatch(watchPath.trim());
      setWatchedPath(watched_path);
    } catch (err) {
      setManualStatus(err instanceof Error ? err.message : String(err));
    }
  };

  const handleStopWatch = async () => {
    await stopWatch();
    setWatchedPath(null);
  };

  return (
    <div className={styles.page}>
      <h1 className={styles.title}>research-corpus — dashboard</h1>

      <SearchPanel />
      <SynthesisPanel />
      <CoveragePanel />
      <AnalyticsPanel />
      <EvalHistoryPanel />
      <McpPanel />
      <EnrichmentPanel />
      <RssPanel />

      <section className={styles.panel}>
        <h2 className={styles.panelTitle}>Supadata credits</h2>
        {credits?.has_data ? (
          <>
            <div className={styles.statRow}>
              <StatTile value={credits.used_today} label="used today" tone="neutral" />
              <StatTile value={credits.used_this_month} label="used this month" tone="neutral" />
              <StatTile
                value={credits.avg_per_day_last_30_days.toFixed(1)}
                label="avg / day (30d)"
                tone="neutral"
              />
              <StatTile value={credits.budget} label="budget / month" tone="neutral" />
            </div>
            <p className={styles.hint}>
              No "remaining" figure — that would only be budget minus what we've logged, which goes
              silently wrong the moment any credit is spent outside this tool. These numbers are
              exactly what this tool has recorded spending, true regardless of that.
            </p>
          </>
        ) : (
          <p className={styles.hint}>
            No credit spend recorded yet. Budget configured: {credits?.budget ?? "…"}/month.
          </p>
        )}
      </section>

      <section className={styles.panel}>
        <h2 className={styles.panelTitle}>Run a channel</h2>
        <div className={styles.row}>
          <input
            className={styles.input}
            placeholder="@handle"
            value={handle}
            onChange={(e) => setHandle(e.target.value)}
          />
          <input
            className={styles.inputSmall}
            placeholder="limit"
            value={limit}
            onChange={(e) => setLimit(e.target.value)}
          />
          <button className={styles.button} onClick={handleStartRun}>
            Start
          </button>
        </div>

        {run && (
          <div className={styles.runPanel}>
            <ProgressBar
              value={run.current}
              max={run.total}
              label={`${run.handle} — ${run.current} / ${run.total || "?"} — ${run.lastDetail}`}
            />
            <div className={styles.statRow}>
              <StatTile value={run.fetched} label="fetched" tone="go" />
              <StatTile value={run.skipped} label="already had" tone="neutral" />
              <StatTile
                value={run.failed}
                label="failed"
                tone={run.failed > 0 ? "stop" : "neutral"}
              />
              <StatTile
                value={run.status}
                label="status"
                tone={run.status === "failed" ? "stop" : run.status === "done" ? "go" : "hold"}
              />
              {run.creditsSpent !== undefined && (
                <StatTile
                  value={`${run.creditsSpent} / ${run.creditsBudget}`}
                  label="credits this run"
                />
              )}
            </div>
            {run.error && <p className={styles.error}>{run.error}</p>}
          </div>
        )}
      </section>

      <section className={styles.panel}>
        <h2 className={styles.panelTitle}>Add a channel</h2>
        <p className={styles.hint}>
          Paste a channel URL, a bare @handle, or a single video URL — a video link resolves to its
          parent channel automatically. Lands in seeds/youtube_channels.yaml with domain/tier set to
          "unknown", pending your review.
        </p>
        <div className={styles.row}>
          <input
            className={styles.input}
            placeholder="https://www.youtube.com/@handle or a video URL"
            value={manualUrl}
            onChange={(e) => setManualUrl(e.target.value)}
          />
          <button className={styles.button} onClick={handleManualAdd}>
            Add
          </button>
        </div>
        {manualStatus && <p className={styles.hint}>{manualStatus}</p>}
      </section>

      <section className={styles.panel}>
        <h2 className={styles.panelTitle}>Watch a folder</h2>
        <p className={styles.hint}>
          Runs server-side — a browser page cannot continuously watch a filesystem path. A JSON file
          dropped here (a list of URLs, or objects with a url/handle field) is picked up
          automatically.
        </p>
        {watchedPath ? (
          <div className={styles.row}>
            <span className={styles.watchedPath}>{watchedPath}</span>
            <button className={styles.button} onClick={handleStopWatch}>
              Stop
            </button>
          </div>
        ) : (
          <div className={styles.row}>
            <input
              className={styles.input}
              placeholder="/absolute/path/to/watch"
              value={watchPath}
              onChange={(e) => setWatchPath(e.target.value)}
            />
            <button className={styles.button} onClick={handleConfigureWatch}>
              Watch
            </button>
          </div>
        )}
      </section>

      <section className={styles.panel}>
        <h2 className={styles.panelTitle}>Seed table — {seeds.length} channels</h2>
        {seedsError && <p className={styles.error}>{seedsError}</p>}
        <div className={styles.tableWrap}>
          <table className={styles.table}>
            <thead>
              <tr>
                <th>Handle</th>
                <th>Name</th>
                <th>Domain</th>
                <th>Tier</th>
                <th>Phase</th>
              </tr>
            </thead>
            <tbody>
              {seeds.map((s) => (
                <tr key={s.handle}>
                  <td>{s.handle}</td>
                  <td>{s.name}</td>
                  <td>{s.domain}</td>
                  <td>{s.authority_tier}</td>
                  <td>{s.phase}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
