import { ProgressBar, StatTile } from "@bastiangrubbe/ui-kit";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  type EntityBackfillEvent,
  type EntityStatus,
  attributeSpeakers,
  fetchEntityStatus,
  fetchSpeakerStatus,
  restoreDocument,
  startEntityBackfill,
  streamEntityBackfill,
} from "../api";
import styles from "../App.module.css";

interface BackfillState {
  done: number;
  succeeded: number;
  failed: number;
  total: number;
  status: "running" | "done" | "failed";
  lastDetail: string;
}

export default function EnrichmentPanel() {
  const [entity, setEntity] = useState<EntityStatus | null>(null);
  const [limit, setLimit] = useState("");
  const [backfill, setBackfill] = useState<BackfillState | null>(null);
  const [entityError, setEntityError] = useState<string | null>(null);
  const closeStreamRef = useRef<(() => void) | null>(null);

  const [speakers, setSpeakers] = useState<Record<string, number>>({});
  const [speakerLimit, setSpeakerLimit] = useState("200");
  const [speakerStatus, setSpeakerStatus] = useState<string | null>(null);

  const [restoreId, setRestoreId] = useState("");
  const [restoreStatus, setRestoreStatus] = useState<string | null>(null);
  const [restoreBusy, setRestoreBusy] = useState(false);

  const reload = useCallback(() => {
    fetchEntityStatus()
      .then(setEntity)
      .catch(() => {
        /* backend not up yet — the seeds panel already surfaces that error */
      });
    fetchSpeakerStatus()
      .then((s) => setSpeakers(s.by_method))
      .catch(() => {
        /* same */
      });
  }, []);

  useEffect(() => {
    reload();
    return () => closeStreamRef.current?.();
  }, [reload]);

  const runBackfill = async () => {
    setEntityError(null);
    try {
      const { run_id } = await startEntityBackfill(limit ? { limit: Number(limit) } : {});
      setBackfill({
        done: 0,
        succeeded: 0,
        failed: 0,
        total: 0,
        status: "running",
        lastDetail: "",
      });
      closeStreamRef.current?.();
      closeStreamRef.current = streamEntityBackfill(
        run_id,
        (event: EntityBackfillEvent) =>
          setBackfill((prev) => {
            if (!prev) return prev;
            if (event.kind === "run_complete") {
              return {
                ...prev,
                status: event.status === "failed" ? "failed" : "done",
                succeeded: event.succeeded ?? prev.succeeded,
                failed: event.failed ?? prev.failed,
                total: event.total ?? prev.total,
              };
            }
            if (event.kind === "extracted") {
              return {
                ...prev,
                done: prev.done + 1,
                succeeded: prev.succeeded + 1,
                lastDetail: `${event.mentions} mentions`,
              };
            }
            if (event.kind === "failed") {
              return {
                ...prev,
                done: prev.done + 1,
                failed: prev.failed + 1,
                lastDetail: event.detail ?? "failed",
              };
            }
            return prev;
          }),
        reload
      );
    } catch (err) {
      setEntityError(err instanceof Error ? err.message : String(err));
    }
  };

  const runSpeakers = async () => {
    setSpeakerStatus("attributing…");
    try {
      const { processed } = await attributeSpeakers(
        speakerLimit ? Number(speakerLimit) : undefined
      );
      setSpeakerStatus(`attributed ${processed} document(s)`);
      reload();
    } catch (err) {
      setSpeakerStatus(err instanceof Error ? err.message : String(err));
    }
  };

  const runRestore = async () => {
    if (!restoreId.trim() || restoreBusy) return;
    setRestoreBusy(true);
    setRestoreStatus("restoring…");
    try {
      const { transcript_version_id } = await restoreDocument(restoreId.trim());
      setRestoreStatus(`new transcript version ${transcript_version_id.slice(0, 8)}… created`);
      setRestoreId("");
    } catch (err) {
      setRestoreStatus(err instanceof Error ? err.message : String(err));
    } finally {
      setRestoreBusy(false);
    }
  };

  const speakerTotal = Object.values(speakers).reduce((a, b) => a + b, 0);

  return (
    <section className={styles.panel}>
      <h2 className={styles.panelTitle}>Enrichment</h2>
      <p className={styles.hint}>
        The same passes the CLI flows run. Triggering one here is an extra ad-hoc run — it does
        not disable or replace any schedule. Entity extraction spends real subscription quota and
        is the only one that does; speaker attribution and restoration are local and free.
      </p>

      <h3 className={styles.subTitle}>Entity extraction</h3>
      <div className={styles.statRow}>
        <StatTile
          value={entity?.pending ?? "…"}
          label="documents pending"
          tone={entity && entity.pending > 0 ? "hold" : "go"}
        />
        <StatTile value={entity?.extractor_version.split(":")[1] ?? "…"} label="model" />
      </div>
      <p className={styles.hint} style={{ marginTop: 8 }}>
        Re-running is safe: documents already extracted at this version are skipped, so nothing
        is processed twice. Leave the limit blank to take everything pending.
      </p>
      <div className={styles.row}>
        <input
          className={styles.inputSmall}
          placeholder="limit"
          value={limit}
          onChange={(e) => setLimit(e.target.value)}
        />
        <button
          className={styles.button}
          onClick={runBackfill}
          disabled={backfill?.status === "running"}
        >
          {backfill?.status === "running" ? "running…" : "Extract"}
        </button>
      </div>
      {entityError && <p className={styles.error}>{entityError}</p>}
      {backfill && (
        <div className={styles.runPanel}>
          <ProgressBar
            value={backfill.done}
            max={backfill.total}
            label={`${backfill.done} / ${backfill.total || "?"} — ${backfill.lastDetail}`}
          />
          <div className={styles.statRow}>
            <StatTile value={backfill.succeeded} label="extracted" tone="go" />
            <StatTile
              value={backfill.failed}
              label="failed"
              tone={backfill.failed > 0 ? "stop" : "neutral"}
            />
            <StatTile
              value={backfill.status}
              label="status"
              tone={
                backfill.status === "failed"
                  ? "stop"
                  : backfill.status === "done"
                    ? "go"
                    : "hold"
              }
            />
          </div>
        </div>
      )}

      <h3 className={styles.subTitle} style={{ marginTop: 22 }}>
        Speaker attribution
      </h3>
      <p className={styles.hint}>
        Tier-1 heuristics over title, description and channel name — local, free, no model. Every
        guess is cross-checked against the entity table, so a channel named like a person but
        known to be a brand is rejected rather than asserted. {speakerTotal} document(s)
        attributed so far.
      </p>
      {speakerTotal > 0 && (
        <div className={styles.statRow}>
          {Object.entries(speakers)
            .sort((a, b) => b[1] - a[1])
            .map(([method, n]) => (
              <StatTile
                key={method}
                value={n}
                label={method}
                tone={method === "unknown" ? "neutral" : "go"}
              />
            ))}
        </div>
      )}
      <div className={styles.row} style={{ marginTop: 10 }}>
        <input
          className={styles.inputSmall}
          placeholder="limit"
          value={speakerLimit}
          onChange={(e) => setSpeakerLimit(e.target.value)}
        />
        <button className={styles.button} onClick={runSpeakers}>
          Attribute
        </button>
      </div>
      {speakerStatus && <p className={styles.hint}>{speakerStatus}</p>}

      <h3 className={styles.subTitle} style={{ marginTop: 22 }}>
        Transcript restoration
      </h3>
      <p className={styles.hint}>
        Adds punctuation and sentence casing to one document's transcript. Non-destructive: it
        writes a new transcript version pointing at the original, which is never modified. Paste a
        document_id from a search result.
      </p>
      <div className={styles.row}>
        <input
          className={styles.input}
          placeholder="document_id"
          value={restoreId}
          onChange={(e) => setRestoreId(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && runRestore()}
        />
        <button className={styles.button} onClick={runRestore} disabled={restoreBusy}>
          {restoreBusy ? "restoring…" : "Restore"}
        </button>
      </div>
      {restoreStatus && <p className={styles.hint}>{restoreStatus}</p>}
    </section>
  );
}
