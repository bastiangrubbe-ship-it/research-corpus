import { useEffect, useState } from "react";

import { type McpCallResult, type McpTool, callMcpTool, fetchMcpTools } from "../api";
import styles from "../App.module.css";

export default function McpPanel() {
  const [tools, setTools] = useState<McpTool[]>([]);
  const [selected, setSelected] = useState<string>("");
  const [argsText, setArgsText] = useState<string>("{}");
  const [result, setResult] = useState<McpCallResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    fetchMcpTools()
      .then((t) => {
        setTools(t);
        if (t.length) setSelected(t[0]!.name);
      })
      .catch(() => {
        /* backend not up yet — the seeds panel already surfaces that error */
      });
  }, []);

  const tool = tools.find((t) => t.name === selected);

  // Prefill the argument editor with the tool's required keys, so the shape is
  // obvious without reading the schema table underneath.
  useEffect(() => {
    if (!tool) return;
    const skeleton: Record<string, string> = {};
    for (const key of tool.required) skeleton[key] = "";
    setArgsText(JSON.stringify(skeleton, null, 2));
    setResult(null);
    setError(null);
  }, [tool]);

  const run = async () => {
    if (!tool || busy) return;
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(argsText || "{}");
    } catch {
      setError("arguments must be valid JSON");
      return;
    }
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      setResult(await callMcpTool(tool.name, parsed));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className={styles.panel}>
      <h2 className={styles.panelTitle}>MCP tools</h2>
      <p className={styles.hint}>
        The tools Claude Code sees, and a way to exercise one without leaving the browser — calls
        go through the same server singleton an MCP client reaches, so a result here is what the
        agent gets for the same arguments. This is a catalog and tester, not a process monitor:
        the MCP server is a subprocess started per client, so there is no "is it running" to show.
      </p>

      {tools.length === 0 && <p className={styles.hint}>No tools registered.</p>}

      {tools.length > 0 && (
        <>
          <div className={styles.row}>
            <select
              className={styles.select}
              value={selected}
              onChange={(e) => setSelected(e.target.value)}
            >
              {tools.map((t) => (
                <option key={t.name} value={t.name}>
                  {t.name}
                </option>
              ))}
            </select>
            <button className={styles.button} onClick={run} disabled={busy}>
              {busy ? "calling…" : "Call"}
            </button>
          </div>

          {tool && (
            <>
              <p className={styles.hint} style={{ marginTop: 10 }}>
                {tool.description}
              </p>

              <div className={styles.tableWrap} style={{ maxHeight: 160 }}>
                <table className={styles.table}>
                  <thead>
                    <tr>
                      <th>Argument</th>
                      <th>Type</th>
                      <th>Required</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(tool.properties).map(([key, spec]) => (
                      <tr key={key}>
                        <td className={styles.mono}>{key}</td>
                        <td className={styles.muted}>{spec.type ?? "any"}</td>
                        <td className={styles.mono}>
                          {tool.required.includes(key) ? "yes" : ""}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <textarea
                className={styles.textarea}
                rows={5}
                value={argsText}
                onChange={(e) => setArgsText(e.target.value)}
                spellCheck={false}
              />
            </>
          )}
        </>
      )}

      {error && <p className={styles.error}>{error}</p>}
      {result && (
        <pre className={styles.pre}>
          {result.is_error ? "tool reported an error:\n\n" : ""}
          {result.text}
        </pre>
      )}
    </section>
  );
}
