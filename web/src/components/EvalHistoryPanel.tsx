import { useCallback, useEffect, useState } from "react";

import {
  type EvalDiffRow,
  type EvalRun,
  type EvalRunSummary,
  fetchEvalDiff,
  fetchEvalRun,
  fetchEvalRuns,
} from "../api";
import styles from "../App.module.css";

function pct(v: number | null): string {
  return v === null ? "n/a" : v.toFixed(2);
}

export default function EvalHistoryPanel() {
  const [runs, setRuns] = useState<EvalRunSummary[]>([]);
  const [selected, setSelected] = useState<string>("");
  const [run, setRun] = useState<EvalRun | null>(null);
  const [compareTo, setCompareTo] = useState<string>("");
  const [diff, setDiff] = useState<EvalDiffRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(() => {
    fetchEvalRuns()
      .then((rows) => {
        setRuns(rows);
        if (rows.length && !selected) setSelected(rows[0]!.name);
      })
      .catch(() => {
        /* backend not up yet — the seeds panel already surfaces that error */
      });
  }, [selected]);

  useEffect(reload, [reload]);

  useEffect(() => {
    if (!selected) return;
    setDiff(null);
    fetchEvalRun(selected)
      .then(setRun)
      .catch((err: Error) => setError(err.message));
  }, [selected]);

  const runDiff = async () => {
    if (!compareTo || !selected) return;
    setError(null);
    try {
      // Older run first so a positive delta reads as "improved since".
      setDiff(await fetchEvalDiff(compareTo, selected));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <section className={styles.panel}>
      <h2 className={styles.panelTitle}>Eval runs</h2>
      <p className={styles.hint}>
        Retrieval quality per lane, from <code>uv run python -m corpus.eval.run</code>. Judgments
        are cached per (query, document), so re-running an unchanged corpus reproduces scores
        exactly — a changed number means retrieval changed, not that the judge wandered. Precision
        counts only strict "relevant" verdicts; a lane showing "n/a" returned no candidates for
        that query at all, which is different from scoring zero.
      </p>

      {runs.length === 0 && (
        <p className={styles.hint}>
          No eval runs on disk yet. This panel is read-only — runs are started from the CLI.
        </p>
      )}

      {runs.length > 0 && (
        <div className={styles.row}>
          <select
            className={styles.select}
            value={selected}
            onChange={(e) => setSelected(e.target.value)}
          >
            {runs.map((r) => (
              <option key={r.name} value={r.name}>
                {r.timestamp} · {r.n_queries} queries · top_k={r.top_k}
              </option>
            ))}
          </select>
          <select
            className={styles.select}
            value={compareTo}
            onChange={(e) => setCompareTo(e.target.value)}
          >
            <option value="">compare against…</option>
            {runs
              .filter((r) => r.name !== selected)
              .map((r) => (
                <option key={r.name} value={r.name}>
                  {r.timestamp}
                </option>
              ))}
          </select>
          <button className={styles.button} onClick={runDiff} disabled={!compareTo}>
            Diff
          </button>
        </div>
      )}

      {error && <p className={styles.error}>{error}</p>}

      {diff && <DiffTable rows={diff} />}
      {!diff && run && <RunTable run={run} />}
    </section>
  );
}

function RunTable({ run }: { run: EvalRun }) {
  return (
    <div className={styles.tableWrap}>
      <table className={styles.table}>
        <thead>
          <tr>
            <th>Query</th>
            <th>Lane</th>
            <th>n</th>
            <th>Precision</th>
            <th>Recall</th>
          </tr>
        </thead>
        <tbody>
          {run.queries.flatMap((q) =>
            q.lanes.map((lane, i) => (
              <tr key={`${q.query_id}-${lane.lane}`}>
                <td>
                  {i === 0 ? (
                    <>
                      {q.query_id}{" "}
                      <span className={styles.muted}>
                        ({q.n_relevant_strict} relevant of {q.pool_size})
                      </span>
                    </>
                  ) : (
                    ""
                  )}
                </td>
                <td className={styles.muted}>{lane.lane}</td>
                <td className={styles.mono}>{lane.n_candidates}</td>
                <td className={styles.mono}>{pct(lane.precision_strict)}</td>
                <td className={styles.mono}>{pct(lane.recall_strict)}</td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}

function DiffTable({ rows }: { rows: EvalDiffRow[] }) {
  return (
    <div className={styles.tableWrap}>
      <table className={styles.table}>
        <thead>
          <tr>
            <th>Query</th>
            <th>Lane</th>
            <th>Before</th>
            <th>After</th>
            <th>Change</th>
          </tr>
        </thead>
        <tbody>
          {rows.flatMap((row) =>
            row.only_in_one_run
              ? [
                  <tr key={row.query_id}>
                    <td>{row.query_id}</td>
                    <td colSpan={4} className={styles.muted}>
                      present in only one run
                    </td>
                  </tr>,
                ]
              : row.lanes.map((lane, i) => (
                  <tr key={`${row.query_id}-${lane.lane}`}>
                    <td>{i === 0 ? row.query_id : ""}</td>
                    <td className={styles.muted}>{lane.lane}</td>
                    <td className={styles.mono}>{lane.before.toFixed(2)}</td>
                    <td className={styles.mono}>{lane.after.toFixed(2)}</td>
                    <td
                      className={styles.mono}
                      style={{
                        color:
                          lane.delta > 0
                            ? "var(--uik-go)"
                            : lane.delta < 0
                              ? "var(--uik-stop)"
                              : "var(--uik-muted)",
                      }}
                    >
                      {lane.delta > 0 ? "+" : ""}
                      {lane.delta.toFixed(2)}
                    </td>
                  </tr>
                ))
          )}
        </tbody>
      </table>
    </div>
  );
}
