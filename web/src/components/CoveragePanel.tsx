import { StatTile } from "@bastiangrubbe/ui-kit";
import { useState } from "react";

import { type CoverageGrade, type CoverageReport, fetchCoverage } from "../api";
import styles from "../App.module.css";

/** Mirrors corpus.db.enums.Domain — see the note in SearchPanel. */
const DOMAINS = [
  "ai_research",
  "ai_automation",
  "entrepreneurship",
  "personal_development",
  "regulatory",
  "general",
  "unknown",
] as const;

/** Grade → StatTile tone. `tone` carries real meaning in this design system
 * (go/hold/stop), so it is mapped from the actual verdict, never chosen for looks. */
const GRADE_TONE: Record<CoverageGrade, "go" | "hold" | "stop"> = {
  good: "go",
  partial: "hold",
  thin: "hold",
  none: "stop",
};

export default function CoveragePanel() {
  const [query, setQuery] = useState("");
  const [domain, setDomain] = useState("");
  const [report, setReport] = useState<CoverageReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const run = async () => {
    if (!query.trim() || busy) return;
    setBusy(true);
    setError(null);
    setReport(null);
    try {
      setReport(
        await fetchCoverage({ query: query.trim(), ...(domain ? { domain } : {}) })
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className={styles.panel}>
      <h2 className={styles.panelTitle}>Coverage check</h2>
      <p className={styles.hint}>
        Search always returns its ten best documents — whether those are ten strong matches or
        the ten least-bad in a corpus that holds nothing on the subject. This tells those two
        apart, and says what would fix weak coverage. Takes ~25s: it reranks a wide candidate
        pool, because breadth is the entire question.
      </p>

      <div className={styles.row}>
        <input
          className={styles.input}
          placeholder="a topic you want to rely on this corpus for…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && run()}
        />
        <select className={styles.select} value={domain} onChange={(e) => setDomain(e.target.value)}>
          <option value="">all domains</option>
          {DOMAINS.map((d) => (
            <option key={d} value={d}>
              {d}
            </option>
          ))}
        </select>
        <button className={styles.button} onClick={run} disabled={busy}>
          {busy ? "assessing…" : "Assess"}
        </button>
      </div>

      {busy && (
        <p className={styles.hint} style={{ marginTop: 12 }}>
          Reranking candidates — this is the slow, deliberate path, not a typeahead.
        </p>
      )}
      {error && <p className={styles.error}>{error}</p>}
      {report && <CoverageResult report={report} />}
    </section>
  );
}

function CoverageResult({ report }: { report: CoverageReport }) {
  const hasMatches = report.n_documents > 0;
  const indexPct = report.total_documents
    ? (100 * report.indexed_documents) / report.total_documents
    : 100;
  const indexIncomplete = indexPct < 95;

  return (
    <div className={styles.runPanel}>
      {indexIncomplete && (
        <p className={styles.error} style={{ margin: 0 }}>
          Semantic index is {indexPct.toFixed(0)}% complete (
          {report.indexed_documents.toLocaleString()} of{" "}
          {report.total_documents.toLocaleString()} documents). This verdict describes that
          subset, not the corpus.
        </p>
      )}
      <div className={styles.statRow}>
        <StatTile value={report.grade} label="coverage" tone={GRADE_TONE[report.grade]} />
        <StatTile value={report.n_documents} label="documents" tone="neutral" />
        <StatTile
          value={report.n_sources}
          label="distinct sources"
          tone={report.n_sources >= 3 ? "go" : report.n_sources > 0 ? "hold" : "stop"}
        />
        <StatTile
          value={report.span_days === null ? "—" : `${report.span_days}d`}
          label="time span"
          tone="neutral"
        />
        <StatTile value={report.best_score.toFixed(3)} label="best match" tone="neutral" />
      </div>

      <p className={styles.hint} style={{ margin: 0 }}>
        {report.headline}
        {hasMatches && report.date_earliest && report.date_latest && (
          <>
            {" "}
            Spanning {report.date_earliest} → {report.date_latest}.
          </>
        )}
      </p>

      {hasMatches && (
        <div className={styles.detailGrid}>
          <dt>top sources</dt>
          <dd>
            {report.top_sources.map(([name, n]) => `${name} (${n})`).join(" · ") || "—"}
          </dd>
          <dt>domains</dt>
          <dd>
            {Object.entries(report.domain_breakdown)
              .sort((a, b) => b[1] - a[1])
              .map(([d, n]) => `${d} ${n}`)
              .join(" · ") || "—"}
          </dd>
          <dt>authority</dt>
          <dd>
            {Object.entries(report.authority_breakdown)
              .sort((a, b) => b[1] - a[1])
              .map(([t, n]) => `${t} ${n}`)
              .join(" · ") || "—"}
          </dd>
        </div>
      )}

      {report.suggestions.length > 0 && (
        <div>
          <h3 className={styles.subTitle}>
            {report.grade === "good" ? "Worth knowing" : "How to improve this"}
          </h3>
          <ul className={styles.suggestionList}>
            {report.suggestions.map((s) => (
              <li key={s}>{s}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
