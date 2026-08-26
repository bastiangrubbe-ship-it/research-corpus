import { ProgressBar } from "@bastiangrubbe/ui-kit";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  type FeedPreview,
  type FeedRow,
  type IngestEventPayload,
  addFeed,
  fetchFeeds,
  previewFeed,
  startFeedRun,
  streamFeedRun,
} from "../api";
import styles from "../App.module.css";

const DOMAINS = [
  "ai_research",
  "ai_automation",
  "entrepreneurship",
  "personal_development",
  "regulatory",
  "general",
  "unknown",
] as const;

export default function RssPanel() {
  const [feeds, setFeeds] = useState<FeedRow[]>([]);
  const [url, setUrl] = useState("");
  const [domain, setDomain] = useState("unknown");
  const [preview, setPreview] = useState<FeedPreview | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [run, setRun] = useState<{ current: number; total: number; detail: string } | null>(null);
  const closeRef = useRef<(() => void) | null>(null);

  const reload = useCallback(() => {
    fetchFeeds()
      .then(setFeeds)
      .catch(() => {
        /* backend not up yet — the seeds panel already surfaces that error */
      });
  }, []);

  useEffect(() => {
    reload();
    return () => closeRef.current?.();
  }, [reload]);

  const doPreview = async () => {
    if (!url.trim() || busy) return;
    setBusy(true);
    setError(null);
    setStatus(null);
    setPreview(null);
    try {
      setPreview(await previewFeed(url.trim()));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const doAddAndRun = async () => {
    if (!preview || busy) return;
    setBusy(true);
    setError(null);
    try {
      try {
        await addFeed({ url: preview.url, domain, authority_tier: "unknown" });
        setStatus(`added to seeds/rss_feeds.yaml — commit it to keep the review trail`);
      } catch (err) {
        // Already present is fine; ingesting an existing feed is a normal re-run.
        const message = err instanceof Error ? err.message : String(err);
        if (!message.includes("already in")) throw err;
        setStatus("already in seeds/rss_feeds.yaml — re-ingesting");
      }
      reload();

      const { run_id } = await startFeedRun({
        url: preview.url,
        domain,
        authority_tier: "unknown",
      });
      setRun({ current: 0, total: 0, detail: "" });
      closeRef.current?.();
      closeRef.current = streamFeedRun(
        run_id,
        (e: IngestEventPayload) =>
          setRun((prev) =>
            prev
              ? {
                  current: e.current ?? prev.current,
                  total: e.total ?? prev.total,
                  detail:
                    e.kind === "run_complete"
                      ? `done — ${e.fetched ?? 0} fetched, ${e.skipped ?? 0} already had`
                      : (e.detail ?? prev.detail),
                }
              : prev
          ),
        reload
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className={styles.panel}>
      <h2 className={styles.panelTitle}>RSS feeds</h2>
      <p className={styles.hint}>
        Preview first — this isn't ceremony. A typo'd or dead feed URL makes feedparser return
        zero entries rather than fail, so without previewing you'd add a broken feed and see a run
        report "0 fetched" with no way to tell that from a feed with nothing new. Adds land in
        seeds/rss_feeds.yaml, never straight in the database, so a feed arrives with the same
        review trail a YouTube channel gets.
      </p>

      <div className={styles.row}>
        <input
          className={styles.input}
          placeholder="https://example.com/feed.xml"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && doPreview()}
        />
        <select className={styles.select} value={domain} onChange={(e) => setDomain(e.target.value)}>
          {DOMAINS.map((d) => (
            <option key={d} value={d}>
              {d}
            </option>
          ))}
        </select>
        <button className={styles.button} onClick={doPreview} disabled={busy}>
          {busy && !preview ? "checking…" : "Preview"}
        </button>
      </div>

      {error && <p className={styles.error}>{error}</p>}

      {preview && (
        <div className={styles.detailBox}>
          <dl className={styles.detailGrid}>
            <dt>feed</dt>
            <dd>{preview.title}</dd>
            <dt>entries</dt>
            <dd className={styles.mono}>{preview.entry_count}</dd>
            <dt>sample</dt>
            <dd>{preview.sample_titles.slice(0, 3).join(" · ")}</dd>
          </dl>
          {preview.link_only && (
            <p className={styles.hint} style={{ margin: 0 }}>
              Entries look link-only — this feed publishes links rather than article text, so the
              documents it produces will have very little content to search.
            </p>
          )}
          <div className={styles.row}>
            <button className={styles.button} onClick={doAddAndRun} disabled={busy}>
              Add &amp; ingest
            </button>
          </div>
        </div>
      )}

      {status && <p className={styles.hint}>{status}</p>}

      {run && (
        <div className={styles.runPanel}>
          <ProgressBar
            value={run.current}
            max={run.total}
            label={`${run.current} / ${run.total || "?"} — ${run.detail}`}
          />
        </div>
      )}

      {feeds.length > 0 && (
        <div className={styles.tableWrap} style={{ marginTop: 12 }}>
          <table className={styles.table}>
            <thead>
              <tr>
                <th>Feed</th>
                <th>Domain</th>
                <th>Added</th>
              </tr>
            </thead>
            <tbody>
              {feeds.map((f) => (
                <tr key={f.url}>
                  <td>{f.title}</td>
                  <td className={styles.muted}>{f.domain}</td>
                  <td className={styles.mono}>{f.added_at}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
