import { useState } from "react";

import { type AnalysisType, type AnalyticsResponse, fetchAnalytics } from "../api";
import styles from "../App.module.css";

const ANALYSES: { value: AnalysisType; label: string; needsEntity: boolean; blurb: string }[] = [
  {
    value: "rising_entities",
    label: "rising — discussed more than before",
    needsEntity: false,
    blurb:
      "Mentions in the last six months against the six before that. Entities with no prior mentions show as “new” rather than an infinite percentage.",
  },
  {
    value: "emerging_entities",
    label: "emerging — newly in the discourse",
    needsEntity: false,
    blurb:
      "Entities whose entire mention history starts inside the window — not merely mentioned recently, but absent before it. Top 20 by mention count.",
  },
  {
    value: "saturated_entities",
    label: "saturated — widest spread of sources",
    needsEntity: false,
    blurb:
      "Ranked by how many distinct sources mention it, not how often. A high mentions-per-source with few sources is one loud channel, not consensus — the two columns are separated so you can see which you're looking at.",
  },
  {
    value: "co_occurrence_drift",
    label: "drift — what it's discussed alongside",
    needsEntity: true,
    blurb:
      "Which kinds of entity co-occur with this one, per period. A shift from regulation-heavy to vendor-heavy company is a framing change, measured distributionally rather than judged by a model.",
  },
  {
    value: "diffusion_timeline",
    label: "diffusion — who covered it first",
    needsEntity: true,
    blurb:
      "One row per source, ordered by when that source first mentioned it. Deliberately cross-domain: which kind of source picked something up first is part of the story.",
  },
];

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

function formatPct(pct: number | null): string {
  if (pct === null) return "new";
  return `${pct >= 0 ? "+" : ""}${(pct * 100).toFixed(0)}%`;
}

export default function AnalyticsPanel() {
  const [analysis, setAnalysis] = useState<AnalysisType>("rising_entities");
  const [domain, setDomain] = useState("");
  const [entityName, setEntityName] = useState("");
  const [data, setData] = useState<AnalyticsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const spec = ANALYSES.find((a) => a.value === analysis)!;

  const run = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      setData(
        await fetchAnalytics({
          analysis,
          ...(domain ? { domain } : {}),
          ...(entityName.trim() ? { entityName: entityName.trim() } : {}),
        })
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setData(null);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className={styles.panel}>
      <h2 className={styles.panelTitle}>Analytics</h2>
      <p className={styles.hint}>
        Plain aggregate queries over extracted entity mentions — no model inference, so these
        return in well under a second. Only documents with a known publication date are counted:
        an unknown date is excluded rather than guessed, because a guess would quietly bias every
        trend toward whatever was backfilled most recently.
      </p>

      <div className={styles.row}>
        <select
          className={styles.select}
          value={analysis}
          onChange={(e) => {
            setAnalysis(e.target.value as AnalysisType);
            // Drop the previous analysis's results rather than leaving them under
            // a heading that now describes something else — the columns differ per
            // analysis, so stale rows read as if they answered the new question.
            setData(null);
            setError(null);
          }}
        >
          {ANALYSES.map((a) => (
            <option key={a.value} value={a.value}>
              {a.label}
            </option>
          ))}
        </select>
        <select className={styles.select} value={domain} onChange={(e) => setDomain(e.target.value)}>
          <option value="">all domains</option>
          {DOMAINS.map((d) => (
            <option key={d} value={d}>
              {d}
            </option>
          ))}
        </select>
        {spec.needsEntity && (
          <input
            className={styles.input}
            placeholder="entity name (e.g. Claude Code)"
            value={entityName}
            onChange={(e) => setEntityName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && run()}
          />
        )}
        <button className={styles.button} onClick={run} disabled={busy}>
          {busy ? "running…" : "Run"}
        </button>
      </div>

      <p className={styles.hint} style={{ marginTop: 10 }}>
        {spec.blurb}
      </p>

      {error && <p className={styles.error}>{error}</p>}
      {data && <AnalyticsResult data={data} />}
    </section>
  );
}

function AnalyticsResult({ data }: { data: AnalyticsResponse }) {
  if (data.analysis === "rising_entities") {
    return (
      <Table headers={["Entity", "Kind", "Recent", "Prior", "Change"]}>
        {data.results.map((r) => (
          <tr key={`${r.canonical_name}-${r.kind}`}>
            <td>{r.canonical_name}</td>
            <td className={styles.muted}>{r.kind}</td>
            <td className={styles.mono}>{r.recent_count}</td>
            <td className={styles.mono}>{r.prior_count}</td>
            <td className={styles.mono}>{formatPct(r.pct_change)}</td>
          </tr>
        ))}
      </Table>
    );
  }

  if (data.analysis === "emerging_entities") {
    return (
      <>
        <p className={styles.hint}>since {data.since}</p>
        <Table headers={["Entity", "Kind", "First seen", "Mentions"]}>
          {data.results.map((r) => (
            <tr key={`${r.canonical_name}-${r.kind}`}>
              <td>{r.canonical_name}</td>
              <td className={styles.muted}>{r.kind}</td>
              <td className={styles.mono}>{r.first_mention_date}</td>
              <td className={styles.mono}>{r.mention_count_since}</td>
            </tr>
          ))}
        </Table>
      </>
    );
  }

  if (data.analysis === "saturated_entities") {
    return (
      <Table headers={["Entity", "Kind", "Sources", "Mentions", "Per source"]}>
        {data.results.map((r) => (
          <tr key={`${r.canonical_name}-${r.kind}`}>
            <td>{r.canonical_name}</td>
            <td className={styles.muted}>{r.kind}</td>
            <td className={styles.mono}>{r.distinct_sources}</td>
            <td className={styles.mono}>{r.total_mentions}</td>
            <td className={styles.mono}>{r.mentions_per_source.toFixed(1)}</td>
          </tr>
        ))}
      </Table>
    );
  }

  if (data.analysis === "co_occurrence_drift") {
    return (
      <>
        <p className={styles.hint}>{data.entity}</p>
        <Table headers={["Period", "Co-occurring entity kinds"]}>
          {data.periods.map((p) => (
            <tr key={p.start}>
              <td className={styles.mono}>
                {p.start} → {p.end}
              </td>
              <td>
                {Object.entries(p.co_occurring_kinds)
                  .sort((a, b) => b[1] - a[1])
                  .map(([kind, n]) => `${kind} ${n}`)
                  .join(" · ")}
              </td>
            </tr>
          ))}
        </Table>
      </>
    );
  }

  return (
    <>
      <p className={styles.hint}>
        {data.entity} — {data.timeline.length} sources
      </p>
      <Table headers={["First mention", "Source"]}>
        {data.timeline.map((t, i) => (
          <tr key={`${t.source_title ?? "?"}-${i}`}>
            <td className={styles.mono}>{t.first_mention_date}</td>
            <td>{t.source_title ?? <span className={styles.muted}>(unnamed source)</span>}</td>
          </tr>
        ))}
      </Table>
    </>
  );
}

function Table({ headers, children }: { headers: string[]; children: React.ReactNode }) {
  return (
    <div className={styles.tableWrap}>
      <table className={styles.table}>
        <thead>
          <tr>
            {headers.map((h) => (
              <th key={h}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}
