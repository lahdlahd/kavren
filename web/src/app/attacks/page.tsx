import data from "@/generated/attacks.json";
import type { AttackReport } from "@/lib/attacks.mjs";

const report = data as AttackReport;

const NO_TX: Record<string, string> = {
  Access: "None: NEAR refused it before it reached the chain",
  API: "None: an answer from the API, not a transaction",
};

const LAYERS = [
  ["Access key", "Each agent signs with a key limited to its own contract methods. A call outside that list is refused by NEAR before it runs, and no transaction is recorded."],
  ["Contract", "Rules enforced on-chain for everyone: who may submit, who may judge, when money can move. No server of ours can override them."],
  ["Evaluator", "Deterministic checks on the delivered file, re-runnable by anyone with the files. Each verdict is bound on-chain to the exact file and report."],
  ["API", "Accepts a file only if its hash is the one committed on-chain, so the evaluator judges exactly what was committed."],
] as const;

export default function AttacksPage() {
  if (report.missing) {
    return <><h1>Attack gallery</h1><p className="lede">No live run has been recorded yet. Run <code>scripts/attack_gallery.py</code>.</p></>;
  }
  return (
    <>
      <h1>A cheating provider against the live protocol</h1>
      <p className="lede">
        A script played a dishonest provider on NEAR testnet: bad files, forged calls, a replayed verdict.
        Each attempt below really happened, and each outcome was checked automatically and read back from the chain.
      </p>
      <div className="score">
        <b>{report.matched}/{report.total}</b>
        <p>outcomes went exactly as the protocol promises. The cheater was paid {report.money.paid ?? "0"} NEAR.</p>
      </div>

      <section className="section">
        <h2>Four layers, different trust</h2>
        <div className="layers">
          {LAYERS.map(([t, d]) => (<div key={t}><h3>{t}</h3><p>{d}</p></div>))}
        </div>
      </section>

      <section className="section">
        <h2>Every attempt</h2>
        <p className="muted small">Run {report.generatedAt} against <span className="mono">{report.contractId}</span>.</p>
        <div className="table-wrap">
          <table>
            <thead><tr><th>#</th><th>Attempt</th><th>Stopped by</th><th>What happened</th><th>Proof</th></tr></thead>
            <tbody>
              {report.rows.map((r) => (
                <tr key={r.n}>
                  <td className="num">{r.n}</td>
                  <td>{r.title}{r.job !== null && <> (<a href={`/job/?id=${r.job}`}>job {r.job}</a>)</>}</td>
                  <td>{r.layer}</td>
                  <td><span className="status" data-tone={r.ok ? "pass" : "fail"}>{r.ok ? "As promised" : "Not as promised"}</span><br /><span className="small">{r.actual}</span></td>
                  <td>{r.txs.length ? r.txs.map((t) => (<div key={t.hash}><a href={t.url}>{t.label}</a></div>)) : <span className="muted small">{NO_TX[r.layer.split(" ")[0]] ?? "No transaction"}</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="section">
        <h2>What it cost</h2>
        <p>
          {report.money.escrowed} NEAR was escrowed and {report.money.refunded} NEAR refunded. The contract kept {report.money.kept} NEAR
          for storing the two jobs, and the agents paid {report.money.gas} NEAR in gas. Testnet tokens only.
        </p>
      </section>
    </>
  );
}
