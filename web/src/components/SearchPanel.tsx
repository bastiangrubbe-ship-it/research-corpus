import { Fragment, useState } from "react";

import { type Provenance, type SearchHit, fetchProvenance, fetchSearch } from "../api";
import styles from "../App.module.css";

/** Mirrors corpus.db.enums.Domain. Hardcoded rather than fetched: domain strings
 * are already handled as plain strings elsewhere in this dashboard (seed rows,
 * manual add). Cost of that choice — this list silently goes stale if the enum
 * gains a member, so it is worth a grep when Domain changes. */
const DOMAINS = [
  "ai_research",
  "ai_automation",
  "entrepreneurship",
  "personal_development",
  "regulatory",
  "general",
  "unknown",
] as const;

function formatDate(iso: string | null): string {
  if (!iso) return "—";
  return iso.slice(0, 10);
}

export default function SearchPanel() {
  const [query, setQuery] = useState("");
  const [domain, setDomain] = useState("");
  const [rerank, setRerank] = useState(true);
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [provenance, setProvenance] = useState<Record<string, Provenance>>({});
  const [provenanceError, setProvenanceError] = useState<string | null>(null);

  const runSearch = async () => {
    if (!query.trim() || busy) return;
    setBusy(true);
    setError(null);
    setExpanded(null);
    try {
      setHits(
        await fetchSearch({ query: query.trim(), rerank, ...(domain ? { domain } : {}) })
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setHits(null);
    } finally {
      setBusy(false);
    }
  };

  const toggleExpand = async (documentId: string) => {
    if (expanded === documentId) {
      setExpanded(null);
      return;
    }
    setExpanded(documentId);
    setProvenanceError(null);
    if (provenance[documentId]) return;
    try {
      const p = await fetchProvenance(documentId);
      setProvenance((prev) => ({ ...prev, [documentId]: p }));
    } catch (err) {
      setProvenanceError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <section className={styles.panel}>
      <h2 className={styles.panelTitle}>Search the corpus</h2>
      <p className={styles.hint}>
        The same hybrid retrieval an MCP client gets — lexical + dense, RRF-fused, then
        cross-encoder reranked. A result that looks wrong here is a real retrieval result, not a
        dashboard artifact. Reranking costs about 10s per query (and ~30s more on the very first
        search, while model weights load); unchecking it returns RRF-fused order in well under a
        second. This panel also searches a narrower candidate pool than the agent path does, so
        its ranking can differ from the same query asked through MCP.
      </p>

      <div className={styles.row}>
        <input
          className={styles.input}
          placeholder="what are people saying about…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && runSearch()}
        />
        <select
          className={styles.select}
          value={domain}
          onChange={(e) => setDomain(e.target.value)}
        >
          <option value="">all domains</option>
          {DOMAINS.map((d) => (
            <option key={d} value={d}>
              {d}
            </option>
          ))}
        </select>
        <label className={styles.checkLabel}>
          <input type="checkbox" checked={rerank} onChange={(e) => setRerank(e.target.checked)} />
          rerank
        </label>
        <button className={styles.button} onClick={runSearch} disabled={busy}>
          {busy ? "searching…" : "Search"}
        </button>
      </div>

      {error && <p className={styles.error}>{error}</p>}

      {hits && hits.length === 0 && (
        <p className={styles.hint}>
          No results. With reranking on, a query whose best candidate scores too low returns
          nothing rather than the least-bad noise — that is deliberate.
        </p>
      )}

      {hits && hits.length > 0 && (
        <div className={styles.tableWrap}>
          <table className={styles.table}>
            <thead>
              <tr>
                <th>Score</th>
                <th>Title</th>
                <th>Published</th>
              </tr>
            </thead>
            <tbody>
              {hits.map((hit) => (
                <Fragment key={hit.document_id}>
                  <tr
                    className={styles.clickableRow}
                    onClick={() => toggleExpand(hit.document_id)}
                  >
                    <td className={styles.mono}>{hit.score.toFixed(3)}</td>
                    <td>{hit.title ?? <span className={styles.muted}>(untitled)</span>}</td>
                    <td className={styles.mono}>{formatDate(hit.published_at)}</td>
                  </tr>
                  {expanded === hit.document_id && (
                    <tr>
                      <td colSpan={3}>
                        <ProvenanceDetail
                          hit={hit}
                          data={provenance[hit.document_id]}
                          error={provenanceError}
                        />
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function ProvenanceDetail({
  hit,
  data,
  error,
}: {
  hit: SearchHit;
  data: Provenance | undefined;
  error: string | null;
}) {
  if (error) return <p className={styles.error}>{error}</p>;
  if (!data) return <p className={styles.hint}>loading provenance…</p>;

  /* "unknown" provenance is the common, honest case for Supadata-sourced
   * transcripts — the provider never reports whether captions were auto-generated.
   * Showing it as "unknown" rather than defaulting to "human" is the whole point of
   * the nullable column. */
  const generated =
    data.is_auto_generated === null
      ? "unknown — provider did not say"
      : data.is_auto_generated
        ? "yes (ASR)"
        : "no (real captions)";

  return (
    <div className={styles.detailBox}>
      <dl className={styles.detailGrid}>
        <dt>document_id</dt>
        <dd className={styles.mono}>{hit.document_id}</dd>
        <dt>source</dt>
        <dd>
          {data.source_title ?? "—"} <span className={styles.muted}>({data.source_kind})</span>
        </dd>
        <dt>domain / tier</dt>
        <dd>
          {data.domain} · {data.authority_tier}
        </dd>
        <dt>published</dt>
        <dd>
          {formatDate(data.published_at)}{" "}
          <span className={styles.muted}>({data.published_at_precision} precision)</span>
        </dd>
        <dt>transcript</dt>
        <dd>
          {data.transcript_provider ?? "—"} · auto-generated: {generated}
        </dd>
      </dl>
      {data.url && (
        <a className={styles.link} href={data.url} target="_blank" rel="noreferrer">
          open source ↗
        </a>
      )}
    </div>
  );
}
