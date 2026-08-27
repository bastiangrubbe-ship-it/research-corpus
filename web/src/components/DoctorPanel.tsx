import { StatTile } from "@bastiangrubbe/ui-kit";
import { useCallback, useEffect, useState } from "react";

import { type DoctorReport, type DoctorStage, fetchDoctor } from "../api";
import styles from "../App.module.css";

/** Status → StatTile tone. `tone` carries real meaning here (go/hold/stop), so it is
 * mapped from the actual verdict rather than chosen for looks. */
const TONE: Record<DoctorStage["status"], "go" | "hold" | "stop"> = {
  ok: "go",
  partial: "hold",
  empty: "stop",
  unknown: "hold",
};

export default function DoctorPanel() {
  const [report, setReport] = useState<DoctorReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      setReport(await fetchDoctor());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <section className={styles.panel}>
      <h2 className={styles.panelTitle}>Pipeline health</h2>
      <p className={styles.hint}>
        Which stages actually ran over the whole corpus. Worth checking before believing a
        &ldquo;nothing found&rdquo; result: this project&rsquo;s recurring failure is not that a
        stage breaks, it is that a stage runs over <em>part</em> of the corpus while everything
        downstream keeps working confidently over whatever fraction exists. A partially-built
        stage does not look broken — it looks decisive.
      </p>

      <div className={styles.row}>
        <button className={styles.button} onClick={load} disabled={busy}>
          {busy ? "checking…" : "Re-check"}
        </button>
      </div>

      {error && <p className={styles.error}>{error}</p>}

      {report && (
        <>
          <div className={styles.statRow}>
            <StatTile
              label="stages incomplete"
              value={String(report.incomplete)}
              tone={report.incomplete === 0 ? "go" : "hold"}
            />
            <StatTile label="stages checked" value={String(report.stages.length)} />
          </div>

          <div className={styles.tableWrap}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th>Stage</th>
                  <th>Built</th>
                  <th>%</th>
                  <th>What is degraded meanwhile</th>
                </tr>
              </thead>
              <tbody>
                {report.stages.map((s) => (
                  <tr key={s.stage}>
                    <td>
                      <StatTile label="" value={s.status} tone={TONE[s.status]} />
                    </td>
                    <td className={styles.mono}>
                      {s.done.toLocaleString()} / {s.total.toLocaleString()}
                    </td>
                    <td className={styles.mono}>
                      {s.share === null ? "—" : `${(s.share * 100).toFixed(1)}%`}
                    </td>
                    <td>
                      <strong>{s.stage}</strong>
                      {s.status !== "ok" && (
                        <div className={styles.quote}>
                          {s.missing.toLocaleString()} missing — {s.impact}
                          <br />
                          fix: <code>{s.remedy}</code>
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}
