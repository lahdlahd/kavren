// Reading the KAVREN contract straight from NEAR RPC, in the browser.
// No server of ours sits between the page and the chain.

export const DEFAULT_TESTNET_RPCS = [
  "https://rpc.testnet.fastnear.com",
  "https://test.rpc.fastnear.com",
  "https://near-testnet.drpc.org",
  "https://rpc.testnet.near.org",
];

const YOCTO = 10n ** 24n;

/** "1234500000000000000000000" -> "1.2345" (trailing zeros trimmed, at most `digits` decimals). */
export function yoctoToNear(yocto, digits = 4) {
  const v = BigInt(yocto);
  const neg = v < 0n;
  const a = neg ? -v : v;
  const whole = a / YOCTO;
  let frac = (a % YOCTO).toString().padStart(24, "0").slice(0, digits).replace(/0+$/, "");
  return `${neg ? "-" : ""}${whole}${frac ? "." + frac : ""}`;
}

/** Exact decimal NEAR string -> yocto string. Throws on bad input or more than 24 decimals. */
export function nearToYocto(near) {
  const s = String(near).trim();
  if (!/^\d+(\.\d+)?$/.test(s)) throw new Error("Enter an amount like 0.5 or 2");
  const [w, f = ""] = s.split(".");
  if (f.length > 24) throw new Error("At most 24 decimal places");
  return (BigInt(w) * YOCTO + BigInt((f + "0".repeat(24)).slice(0, 24))).toString();
}

export function shortHash(h, n = 8) {
  return h && h.length > 2 * n + 1 ? `${h.slice(0, n)}…${h.slice(-4)}` : h || "";
}

/** Seconds since epoch -> "4 Nov 2026, 12:32:05 UTC". */
export function fmtTime(secs) {
  if (secs == null) return "—";
  const d = new Date(Number(secs) * 1000);
  const m = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][d.getUTCMonth()];
  const p = (x) => String(x).padStart(2, "0");
  return `${d.getUTCDate()} ${m} ${d.getUTCFullYear()}, ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())} UTC`;
}

export function fmtDuration(secs) {
  const s = Math.max(0, Math.round(secs));
  if (s < 90) return `${s} s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  if (s < 172800) return `${Math.round(s / 3600)} h`;
  return `${Math.round(s / 86400)} days`;
}

function b64(str) {
  return typeof btoa === "function" ? btoa(str) : Buffer.from(str, "binary").toString("base64");
}

/** A tiny RPC client with failover across providers. `fetchImpl` is injectable for tests. */
export function makeChain({ contractId, rpcs = DEFAULT_TESTNET_RPCS, fetchImpl = globalThis.fetch }) {
  let i = 0;
  async function post(body) {
    let last;
    for (let attempt = 0; attempt < rpcs.length * 2; attempt++) {
      const url = rpcs[i % rpcs.length];
      try {
        const r = await fetchImpl(url, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ jsonrpc: "2.0", id: "kavren-web", ...body }),
        });
        if (r.status === 429 || r.status >= 500) throw new Error(`${url} answered ${r.status}`);
        const j = await r.json();
        if (j.error && /TIMEOUT|UNAVAILABLE|INTERNAL/i.test(JSON.stringify(j.error))) throw new Error(JSON.stringify(j.error));
        return j;
      } catch (e) {
        last = e;
        i++;
      }
    }
    throw new Error(`No NEAR RPC provider answered: ${last && last.message}`);
  }
  async function view(method, args = {}) {
    const j = await post({
      method: "query",
      params: {
        request_type: "call_function", finality: "optimistic", account_id: contractId,
        method_name: method, args_base64: b64(JSON.stringify(args)),
      },
    });
    if (j.error) throw new Error(JSON.stringify(j.error));
    if (j.result && j.result.error) throw new Error(j.result.error);
    const bytes = Uint8Array.from(j.result.result);
    return JSON.parse(new TextDecoder().decode(bytes));
  }
  return {
    view,
    getJob: (id) => view("get_job", { job_id: Number(id) }),
    getBudget: (owner_id, agent_id) => view("get_budget", { owner_id, agent_id }),
    getConfig: () => view("get_config"),
    getEvaluator: () => view("get_evaluator"),
  };
}

/** Highest existing job id (ids are 0, 1, 2, ... with no gaps), or -1 if there are none. */
export async function findLatestJobId(getJob) {
  if (!(await getJob(0))) return -1;
  let lo = 0, hi = 1;
  while (await getJob(hi)) { lo = hi; hi *= 2; }
  while (hi - lo > 1) {
    const mid = Math.floor((lo + hi) / 2);
    if (await getJob(mid)) lo = mid; else hi = mid;
  }
  return lo;
}

export async function sha256Hex(bytes) {
  const d = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
