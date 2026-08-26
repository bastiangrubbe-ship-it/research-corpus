import { StatTile } from "@bastiangrubbe/ui-kit";
import { useCallback, useEffect, useState } from "react";

import {
  type SynthesisFilter,
  type SynthesisPlan,
  type SynthesisReport,
  fetchSynthesisPlan,
  runSynthesis,
} from "../api";
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

/** Rough, and labelled as rough in the UI. Measured: 12 documents took 58s at the
 * default concurrency, so ~5s/document is the honest order of magnitude. Presenting
 * this to the second would imply a precision the number does not have. */
function estimateMinutes(calls: number): string {
  const seconds = calls * 5;
  if (seconds < 90) return "under 2 min";
  return `~${Math.round(seconds / 60)} min`;
}

export default function SynthesisPanel() {
  const [question, setQuestion] = useState("");
  const [query, setQuery] = useState("");
  const [entityName, setEntityName] = useState("");
  const [domain, setDomain] = useState("");
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");
  const [maxDocuments, setMaxDocuments] = useState(60);

  const [plan, setPlan] = useState<SynthesisPlan | null>(null);
  const [planError, setPlanError] = useState<string | null>(null);
  const [report, setReport] = useState<SynthesisReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const filter: SynthesisFilter = {
    ...(query.trim() ? { query: query.trim() } : {}),
    ...(entityName.trim() ? { entityName: entityName.trim() } : {}),
    ...(domain ? { domain } : {}),
    ...(since ? { since } : {}),
    ...(until ? { until } : {}),
    maxDocuments,
  };
  const hasFilter = Boolean(query.trim() || entityName.trim() || domain || since || until);

  // Re-price on every filter change. The plan costs nothing, so there is no reason
  // to make the user ask for it — and seeing "1,055 documents" appear as you type is
  // what stops an accidental thousand-call run.
  const serialized = JSON.stringify(filter);
  const refreshPlan = useCallback(async () => {
    if (!hasFilter) {
      setPlan(null);
      setPlanError(null);
      return;
    }
    try {
      setPlanError(null);
      setPlan(await fetchSynthesisPlan(JSON.parse(serialized)));
    } catch (err) {
      setPlan(null);
      setPlanError(err instanceof Error ? err.message : String(err));
    }
  }, [serialized, hasFilter]);

  useEffect(() => {
    const timer = setTimeout(refreshPlan, 300);
    return () => clearTimeout(timer);
  }, [refreshPlan]);

  const run = async () => {
    if (!question.trim() || !hasFilter || busy) return;
    setBusy(true);
    setError(null);
    setReport(null);
    try {
      setReport(await runSynthesis({ ...filter, question: question.trim() }));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className={styles.panel}>
      <h2 className={styles.panelTitle}>Synthesize</h2>
      <p className={styles.hint}>
        Not search. Search ranks and returns its best ten; this reads <em>every</em> document
        matching the filter, in full, and answers from all of them with citations. Use it when
        the question is about a body of material rather than a passage in it — how an argument
        changed, where sources disagree.
      </p>
      <p className={styles.hint}>
        It costs one LLM call per matched document and runs for minutes, so the filter is
        priced below before you commit. Narrow with an entity or date range rather than raising
        the cap — a bigger cap reads more documents, it does not make the filter better.
      </p>

      <div className={styles.row}>
        <input
          className={styles.input}
          placeholder="Question — e.g. how has the case for on-device inference changed?"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
        />
      </div>

      <div className={styles.row}>
        <input
          className={styles.input}
          placeholder="text filter (title/description)"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <input
          className={styles.input}
          placeholder="entity name"
          value={entityName}
          onChange={(e) => setEntityName(e.target.value)}
        />
        <select
          className={styles.select}
          value={domain}
          onChange={(e) => setDomain(e.target.value)}
        >
          <option value="">any domain</option>
          {DOMAINS.map((d) => (
            <option key={d} value={d}>
              {d}
            </option>
          ))}
        </select>
      </div>

      <div className={styles.row}>
        <input
          className={styles.input}
          type="date"
          value={since}
          onChange={(e) => setSince(e.target.value)}
        />
        <input
          className={styles.input}
          type="date"
          value={until}
          onChange={(e) => setUntil(e.target.value)}
        />
        <input
          className={styles.input}
          type="number"
          min={1}
          max={2000}
          value={maxDocuments}
          onChange={(e) => setMaxDocuments(Number(e.target.value))}
        />
        <button
          className={styles.button}
          onClick={run}
          disabled={busy || !question.trim() || !hasFilter}
        >
          {busy ? "reading…" : "Synthesize"}
        </button>
      </div>

      {!hasFilter && (
        <p className={styles.hint}>
          Add at least one filter. An unfiltered synthesis would read the entire corpus.
        </p>
      )}
      {planError && <p className={styles.error}>{planError}</p>}

      {plan && (
        <div className={styles.statRow}>
          <StatTile label="matched" value={plan.matched_documents.toLocaleString()} />
          <StatTile
            label="will read"
            value={plan.documents_to_read.toLocaleString()}
            tone={plan.capped ? "hold" : "go"}
          />
          <StatTile label="LLM calls" value={plan.llm_calls.toLocaleString()} />
          <StatTile label="rough time" value={estimateMinutes(plan.llm_calls)} />
        </div>
      )}

      {plan?.capped && (
        <p className={styles.hint}>
          <strong>{plan.dropped_by_cap.toLocaleString()} matching documents will not be read.</strong>{" "}
          The answer will be drawn from {plan.documents_to_read} of {plan.matched_documents} — treat
          it as a sample, not as what the corpus collectively says. Narrow the filter to make it
          complete.
        </p>
      )}
      {plan && plan.escalated_documents > 0 && (
        <p className={styles.hint}>
          {plan.escalated_documents} document(s) exceed the default model&apos;s context and will
          escalate to Opus rather than being truncated.
        </p>
      )}

      {busy && (
        <p className={styles.hint}>
          Reading {plan?.documents_to_read ?? "…"} documents. This does not stream — findings are
          only meaningful once they are combined.
        </p>
      )}
      {error && <p className={styles.error}>{error}</p>}

      {report && (
        <div className={styles.detailBox}>
          <div className={styles.statRow}>
            <StatTile label="read" value={String(report.documents_read)} />
            <StatTile
              label="addressed it"
              value={String(report.documents_addressing)}
              tone={report.documents_addressing > 0 ? "go" : "stop"}
            />
            <StatTile label="citations" value={String(report.citations.length)} />
            {report.documents_failed > 0 && (
              <StatTile label="failed" value={String(report.documents_failed)} tone="stop" />
            )}
          </div>

          {report.capped && (
            <p className={styles.hint}>
              Read {report.documents_read} of {report.matched_documents} matching documents;{" "}
              {report.dropped_by_cap} were not read.
            </p>
          )}
          {report.invalid_markers.length > 0 && (
            <p className={styles.error}>
              The answer cited {report.invalid_markers.length} marker(s) that were never issued (
              {report.invalid_markers.join(", ")}). Those claims are unsupported — treat them as
              unsourced.
            </p>
          )}
          {report.documents_addressing < report.documents_read && (
            <p className={styles.hint}>
              {report.documents_read - report.documents_addressing} document(s) matched the filter
              but did not bear on the question. They were read and excluded, not skipped.
            </p>
          )}

          <p className={styles.answer}>{report.answer}</p>

          {report.citations.length > 0 && (
            <div className={styles.tableWrap}>
              <table className={styles.table}>
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Date</th>
                    <th>Source</th>
                    <th>Claim</th>
                  </tr>
                </thead>
                <tbody>
                  {report.citations.map((c) => (
                    <tr key={c.marker}>
                      <td className={styles.mono}>[{c.marker}]</td>
                      <td className={styles.mono}>{c.published_at?.slice(0, 10) ?? "—"}</td>
                      <td className={styles.muted}>
                        {c.url ? (
                          <a href={c.url} target="_blank" rel="noreferrer">
                            {c.source_title ?? c.title ?? "source"}
                          </a>
                        ) : (
                          (c.source_title ?? c.title ?? "source")
                        )}
                      </td>
                      <td>
                        {c.claim}
                        {c.quote && <div className={styles.quote}>“{c.quote}”</div>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
