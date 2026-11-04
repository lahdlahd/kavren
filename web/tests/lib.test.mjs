import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { parseAttacks } from "../src/lib/attacks.mjs";
import { findLatestJobId, makeChain, nearToYocto, yoctoToNear } from "../src/lib/near.mjs";
import { formToPolicy, policyToForm } from "../src/lib/policy.mjs";
import { buildTimeline, sealFor } from "../src/lib/timeline.mjs";

const JOB = {
  job_id: 7, owner_id: "lahd-kavren.testnet", buyer_agent: "buyer-agent.lahd-kavren.testnet",
  provider_id: "provider-agent.lahd-kavren.testnet", evaluator_id: "evaluator.lahd-kavren.testnet",
  amount: "240000000000000000000000", input_hash: "a".repeat(64), work_hash: "b".repeat(64),
  canary_commitment: "c".repeat(64), created_at: 1000, deadline: 4600, max_attempts: 3, attempts_used: 3,
  eval_window_secs: 1800, submission_seq: 3, current_output_hash: "d".repeat(64), submitted_at: 1500,
  status: "Refunded", canaries_revealed: true,
  verdicts: [1, 2, 3].map((s) => ({ seq: s, verdict: "FAIL", output_hash: String(s).repeat(64), report_hash: "e".repeat(64), at: 1000 + s * 60 })),
};

test("yocto <-> NEAR is exact", () => {
  assert.equal(yoctoToNear("240000000000000000000000"), "0.24");
  assert.equal(yoctoToNear("1"), "0");
  assert.equal(yoctoToNear("1", 24), "0.000000000000000000000001");
  assert.equal(nearToYocto("0.24"), "240000000000000000000000");
  assert.equal(nearToYocto("2"), "2000000000000000000000000");
  assert.throws(() => nearToYocto("1e3"));
  assert.throws(() => nearToYocto("-1"));
});

test("timeline tells the refunded attack job exactly as stored", () => {
  const ev = buildTimeline(JOB, 5000);
  assert.deepEqual(ev.map((e) => e.title), [
    "Job created by buyer-agent.lahd-kavren.testnet", "Submission 1 judged: FAIL", "Submission 2 judged: FAIL",
    "Submission 3 judged: FAIL", "Refunded", "Hidden control records revealed"]);
  assert.match(ev[0].detail, /0\.24 NEAR moved from lahd-kavren\.testnet's budget into escrow/);
  assert.equal(sealFor(JOB, 5000).word, "Refunded");
});

test("seal follows status and refund conditions", () => {
  assert.equal(sealFor({ ...JOB, status: "Passed" }, 0).word, "Pass");
  assert.equal(sealFor({ ...JOB, status: "Open", attempts_used: 0 }, 2000).word, "Open");
  assert.equal(sealFor({ ...JOB, status: "Open", attempts_used: 0 }, 5000).word, "Refundable");
  const sub = buildTimeline({ ...JOB, status: "Submitted", verdicts: [] }, 1600);
  assert.match(sub.at(-1).detail, /has until 28 min from now/);
});

test("latest job id is found by probing", async () => {
  for (const n of [0, 1, 7, 8, 100]) {
    let calls = 0;
    const got = await findLatestJobId(async (id) => { calls++; return id <= n ? { job_id: id } : null; });
    assert.equal(got, n);
    assert.ok(calls < 20);
  }
  assert.equal(await findLatestJobId(async () => null), -1);
});

test("RPC client fails over and decodes view results", async () => {
  const seen = [];
  const fetchImpl = async (url, init) => {
    seen.push(url);
    if (url.includes("bad")) return { status: 429, json: async () => ({}) };
    const body = JSON.parse(init.body);
    const args = JSON.parse(Buffer.from(body.params.args_base64, "base64").toString());
    const out = [...Buffer.from(JSON.stringify({ job_id: args.job_id }))];
    return { status: 200, json: async () => ({ result: { result: out } }) };
  };
  const c = makeChain({ contractId: "k.testnet", rpcs: ["https://bad", "https://good"], fetchImpl });
  assert.deepEqual(await c.getJob(7), { job_id: 7 });
  assert.deepEqual(seen, ["https://bad", "https://good"]);
});

test("policy form round trip and validation", () => {
  const p = { max_per_job: "1000000000000000000000000", max_open_jobs: 2, allowed_templates: ["contacts-v1"],
    allowed_providers: ["provider-agent.lahd-kavren.testnet"], max_deadline_secs: 86400, max_attempts: 3, expires_at: 1900000000 };
  const f = policyToForm(p);
  const back = formToPolicy(f, { min_deadline_secs: 600, max_attempts_cap: 5 }, 1800000000);
  assert.deepEqual(back.policy, { ...p, expires_at: Math.floor(Date.parse(`${f.expiresAt}T23:59:59Z`) / 1000) });
  const bad = formToPolicy({ ...f, maxPerJob: "abc", providers: "Not An Account!", maxAttempts: "9" }, { min_deadline_secs: 600, max_attempts_cap: 5 }, 0);
  assert.deepEqual(Object.keys(bad.errors).sort(), ["maxAttempts", "maxPerJob", "providers"]);
});

test("attack report parses into 21 rows with real links", () => {
  const md = readFileSync(new URL("./fixtures/ATTACKS.md", import.meta.url), "utf8");
  const a = parseAttacks(md);
  assert.equal(a.rows.length, 21);
  assert.equal(a.matched, 21);
  assert.equal(a.money.paid, "0.0000");
  assert.equal(a.rows.filter((r) => r.txs.length).length, 15);
  assert.equal(a.rows[2].layer, "Access key (NEAR runtime)");
});
