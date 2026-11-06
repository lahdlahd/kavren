# Live runs on NEAR testnet

Real transactions, newest first. Every link opens on the public testnet explorer.

## Run 1: first autonomous agent-to-agent job (3 Nov 2026, 21:43–21:46 Lagos)

**Setup**
- Contract `kavren.lahd-kavren.testnet`: wasm SHA256 `d123abb4c454b4e0814fbf86ab6944fb3a7259bdbde86957f40c454b7d911a58`, the same file CI built and tested
- Buyer agent, provider agent and evaluator each sign only with function-call keys scoped to that contract
- Provider LLM: `deepseek-ai/DeepSeek-V4.1-Flash-1017` on NEAR AI Cloud (`ownedBy: nearai`). NEAR AI serves it from TEE hardware, but KAVREN does not yet verify that attestation itself; since 5 Nov each submission records the model and service as *reported by the provider*
- Input: `fixtures/contacts/input.csv`, 20 messy rows; the protocol added 8 hidden canaries

**What happened.** No human acted between dropping the file into the inbox and the payment.

| Step | Who | Transaction |
|---|---|---|
| Owner funds a 3 NEAR budget with policy (max 1 NEAR per job, provider allowlist, `contacts-v1` only) | owner `lahd-kavren.testnet` (the only human action) | [GMMqFqsM…6ddM](https://testnet.nearblocks.io/txns/GMMqFqsMqngV6BnySQ5usbFSXF8ea4vtQ6zyZ2wR6ddM) |
| Buyer agent picks the template, checks the policy, prices the job at 0.24 NEAR, creates **job 0** | `buyer-agent` (function-call key) | [HGKofCAU…KGU3](https://testnet.nearblocks.io/txns/HGKofCAUqE93wNxysDS9vnsBRRcJRse2hcSvfQe9KGU3) |
| Provider agent checks the draft and the work hash, plans with DeepSeek (passed the public checks on the first try), submits its output hash | `provider-agent` (function-call key) | [7H7MUADC…ZzcQ](https://testnet.nearblocks.io/txns/7H7MUADCw9C2nHkquRLCu9yJo51ZSy5ji228wpppZzcQ) |
| Evaluator runs C1–C8 and the hidden canaries: **PASS**, bound to seq 1 and output hash `33fdfb4a…f6df` | `evaluator` (function-call key) | [3Gm6WfHo…Cv6A](https://testnet.nearblocks.io/txns/3Gm6WfHoDXphuJc2Nv4iqPLWxzJ9QLvC9otFtDuhCv6A) |
| Canary commitment opened on-chain (the contract checks sha256) | `evaluator` | [2u6bAmUN…x5PP](https://testnet.nearblocks.io/txns/2u6bAmUN77usgPfEWCidZaz7nfdXdgwHiVzKrGk3x5PP) |
| Buyer agent re-runs the whole evaluation itself: work hash ✓, submission hash ✓, verdict PASS ✓, report hash matches the chain ✓. Then it accepts the cleaned file | `buyer-agent` (off-chain check) | — |
| Provider withdraws its 0.24 NEAR (pull payment) | `provider-agent` | [AXzA6guX…LAL](https://testnet.nearblocks.io/txns/AXzA6guXCAUHH4CyFn6vn53xiPBT2dxdfxssmhRYLAL) |

**Timing:** about 3 minutes from file to payment. The LLM planning took about 70 s; the rest is the 15-second polling of each process.

## Incident: read-after-write lag (jobs 1 and 2)

**What went wrong.** On a second run the API read the chain at `final` finality, a few blocks behind the agents' own transactions.
- The buyer created **job 1**. Its `register` call got 404 ("no such job") because the API's read had not caught up. The tick failed before the agent saved its state or moved the file, so on the next tick it hired again (**job 2**). The owner's on-chain policy (`max_open_jobs = 2`) stopped a third hire.
- The provider submitted job 1's hash on-chain. Its upload got 409 because the API still saw the job as Open, and the file was not kept, so the evaluator had nothing to judge.

**What protected the funds.** Neither job could be paid without an evaluator PASS, and the contract lets anyone refund a job once its timer runs out. Job 1 became the live test of the evaluator-timeout refund path (see below).

**Fix** (commit after `253d1cf`):
- Chain reads use `optimistic` finality. The contract still re-checks every write.
- Agents retry 404/409 for a short time after their own transactions.
- The buyer records a pending hire *before* `create_job`. After the transaction it saves the job and moves the file, and only then registers. Registering is retried on later ticks.
- On restart the buyer adopts any live job it created that its state file does not know about.
- The provider writes each submission to disk before `submit_work` and re-uploads it while the job is Submitted with a matching hash.
- Regression tests cover each step, using a lagging chain view.

**After the fix (restart at 22:31 WAT).** In its first ticks the buyer adopted jobs 1 and 2 on its own. Every `register` and upload then succeeded on the first try.

| Job | What happened | Transaction |
|---|---|---|
| 1 | No file was ever stored, so no verdict could be given. When the 30-minute evaluation window ran out, the buyer agent called `refund_job` and 0.24 NEAR went back to the owner's budget: **the evaluator-timeout refund path, live** | [2YQQNdfm…t4x3](https://testnet.nearblocks.io/txns/2YQQNdfmdZdFXL72f5jR4N6KL9ouP1qF93hcR1tot4x3) |
| 2 | Provider re-planned, submitted and uploaded; PASS; buyer re-verified it independently | — |
| 3 | Hired from the duplicate file still in the inbox, once job 1's refund freed a slot under `max_open_jobs`; PASS, re-verified | [C5fJm55A…tfVN](https://testnet.nearblocks.io/txns/C5fJm55AFdbspQNDcf3Hdr9zgMGZixNqe2icVqnZtfVN) (create) |
| 4 | A further copy of the file put in the inbox; PASS, canaries revealed, provider withdrew 0.24 NEAR | [GbMZDXZs…DDhf](https://testnet.nearblocks.io/txns/GbMZDXZsdRaU8vARW1qSbXWY1TVPoH3CCMyWAcKZDDhf) (create) · [ByvW8wY7…WjHm](https://testnet.nearblocks.io/txns/ByvW8wY7wV1QjAFPo6vmUHCCyFFdVS1tyi2hDh2UWjHm) (PASS) · [HFk3RqZP…zGQ9](https://testnet.nearblocks.io/txns/HFk3RqZPmX4oNi5tsDLQaBZzkEKMqqXk21z7DW32zGQ9) (reveal) · [EBpMFcUq…xajM](https://testnet.nearblocks.io/txns/EBpMFcUq7A3UaxdrgrLij4dkoyFssxnr1HmHnY2hxajM) (withdraw) |

**Totals across jobs 0–4:** 1.20 NEAR escrowed. 0.24 NEAR was refunded to the budget and 0.96 NEAR was paid to the provider for four verified PASSes. Nothing is stuck.

Because jobs 0, 3 and 4 came from the same file name, their receipts overwrote each other in the outbox. That has been fixed: outputs are now named `job<N>-<file>`.

Jobs 5–8 were the attack gallery; see [ATTACKS.md](ATTACKS.md).

## Run 2: the hosted evaluator (5 Nov 2026, job 9)

From job 9 on, only the evaluator on Render (https://kavren.onrender.com) judges; the laptop's API and worker are stopped. Job 9 was hired by the buyer agent and passed about 80 seconds after the provider submitted. The proof page's canary check is green: the browser hashed the canary set the evaluator published and it matched the on-chain commitment. Proof: [job 9](https://kavren.onrender.com/job/?id=9).

## Run 3: an independent AI agent hires through MCP (5 Nov 2026, job 10)

Claude Desktop, connected to KAVREN's buyer-side MCP server and acting as `claude-buyer.lahd-kavren.testnet` (a function-call key that can only `create_job` and `refund_job`), was given one instruction: get a contacts file cleaned, within the owner's rules. On its own it checked the budget and rules, quoted the file, hired job 10 for 0.24 NEAR, polled the status, re-ran the evaluation itself and confirmed the work, submission and report hashes against the chain before accepting. Submission 1 passed with 0 of 3 attempts used. Proof: [job 10](https://kavren.onrender.com/job/?id=10).

**Incident: two provider agents running.** The provider log showed a `submit_work` failing with "job not open". Two `kavren-provider` processes were running on the same key: one submitted and uploaded first, and the contract refused the other's late submission. No money or work was lost. Fix (01c9e28): `kavren-provider` takes a per-account lock and refuses to start if another copy is running.

**Wording fixed.** Claude reported the provider "was paid", and the proof page said the same. A PASS makes the escrow *claimable* by the provider, which collects it with `withdraw`. The page and `kavren_accept` now say "released to the provider".

## Run 4: attested planning, verified by the evaluator (5 Nov 2026, job 11)

Since eba0697 the provider plans with a non-streamed call through NEAR AI's official SDK and uploads the exact request and response bytes with NEAR AI's `provider_tee` signature. The evaluator on Render, with its own NEAR AI key, fetches a fresh attestation for the signing key (Intel TDX quote UpToDate, NVIDIA GPU evidence verified), checks the signature over those bytes, and re-runs the signed plan on the job's work file: it reproduced the submitted file. Not checked: which software build the TEE runs.

Claude Desktop hired job 11 through MCP in the same way (0.242 NEAR, PASS on the first submission, independently re-checked, then accepted). `GET /jobs/11/provenance` returns `"verification": "attestation_verified"` for `deepseek-ai/DeepSeek-V4.1-Flash-1017` on `cloud-api.near.ai`, and the proof page shows it. Proof: [job 11](https://kavren.onrender.com/job/?id=11).

Cost of the attested call: the planning step took about 108 seconds in the local check, against a few seconds when streamed. The SDK verifies the attestation before sending, and the reply arrives in one piece. Well inside the 60-minute deadline. If the attested call fails, the provider falls back to streaming and the submission is labelled "reported by the provider".

## Run 5: deadline refund drill (5 Nov 2026, job 12)

An isolated job, opened from the buyer-agent's budget (not the MCP agent's) with a 20-minute deadline, with the provider agent stopped so no work was done (`scripts/deadline_refund_drill.py`). Result: **DEADLINE REFUND OK**.

| Step | Result | Transaction |
|---|---|---|
| Open job 12, 0.24 NEAR into escrow | Open | [FHMu3UhW…8cws](https://testnet.nearblocks.io/txns/FHMu3UhWVuZmpsY9tbCNnphkjYUpNJn7du4n76815cws) |
| Refund before the deadline | refused: "no refund condition holds" | [75bXMbNn…qDHi](https://testnet.nearblocks.io/txns/75bXMbNns3Z8nEC5axsvKWD4EJCDivaVrZkd3tD8qDHi) |
| Provider submits after the deadline | refused: "deadline passed" | [Bt4buQwL…sist](https://testnet.nearblocks.io/txns/Bt4buQwLbqh49f7iJhsD6kCBiN349C6zYHxAgrXosist) |
| Refund after the deadline | Refunded; 0.24 NEAR back in the budget | [9X52GJVz…2D19](https://testnet.nearblocks.io/txns/9X52GJVzet9KK9WtXR8sxMzqYk86bBWHgHdB7LXy2D19) |

Budget 2.1410 → 2.1235 NEAR: the job's 0.24 NEAR returned, and only the 0.0175 NEAR storage fee for the job record was kept. Proof: [job 12](https://kavren.onrender.com/job/?id=12).

All three refund paths have now run live: evaluator timeout (job 1), attempts exhausted (jobs 7 and 8, attack gallery) and deadline passed (job 12).

## Run 6: the owner's budget rules, enforced on-chain (5 Nov 2026)

`scripts/budget_rules_drill.py`: the buyer agent's function-call key made four `create_job` calls, each breaking exactly one rule of the owner's policy for `buyer-agent.lahd-kavren.testnet` (max 1 NEAR per job, provider `provider-agent.lahd-kavren.testnet`, job type `contacts-v1`). Every call also carried deliberately invalid hashes, so no job could have been created even if a rule check were missing: the contract would then have refused with a different message. Result: **ALL FOUR RULES ENFORCED ON-CHAIN**.

| Rule broken | Contract's answer | Transaction |
|---|---|---|
| Amount above max per job (1 NEAR + 1 yocto) | refused: "amount exceeds max_per_job" | [HUTSe1Tv…9Rae](https://testnet.nearblocks.io/txns/HUTSe1TvkhAesKp6gTXhGYZTPCPM5SZZ2GGaPiAz9Rae) |
| Provider not on the allowlist | refused: "provider not allowed by policy" | [E6Stam8o…NycY](https://testnet.nearblocks.io/txns/E6Stam8otsWDACzTB5dC5r9XkVfpbKvy4LtFCiWMNycY) |
| Job type not on the allowlist (`leads-v1`) | refused: "template not allowed by policy" | [B2GmpQL1…2iLJ](https://testnet.nearblocks.io/txns/B2GmpQL1PWyn59p5voXo7oyntRyfJ1swYVKt3xCp2iLJ) |
| Budget expired (the owner shortened the expiry to about 2 minutes) | refused: "budget expired" | [8TgLY4uq…Hpmm](https://testnet.nearblocks.io/txns/8TgLY4uqp9UExqGGzCTPKh5LH3txjdjqDuYNJ1UnHpmm) |

The owner then restored the original policy with `update_policy` ([HZGoxz2z…Kz7R](https://testnet.nearblocks.io/txns/HZGoxz2zGgJGtMC88PThGQ2KyJM6tVUPRTSB2ErcKz7R)); the script confirmed it on-chain. The budget balance was 2.12347 NEAR before and after, with 0 open jobs: only gas was spent.

Still proven by tests only, not live: a real-provider FAIL → retry → PASS loop (live, the AI provider has always passed first time), a second `withdraw` paying nothing, and the 7-day file deletion (no job has reached 7 days yet).

## Run 7: job 13, recorded for the demo (5 Nov 2026)

Claude Desktop hired job 13 through MCP in the same way as jobs 10 and 11 (0.242 NEAR; PASS on the first submission, re-checked by the agent before accepting). Proof: [job 13](https://kavren.onrender.com/job/?id=13).

| Step | Transaction | Time (UTC, 5 Nov) |
|---|---|---|
| `create_job` by `claude-buyer.lahd-kavren.testnet` (the independent agent hires) | [CWC5ELbZ…pcK8](https://testnet.nearblocks.io/txns/CWC5ELbZYPX6zF4gxFhRiQW3fwhAfuigtBKF3bP7pcK8) | 16:57:16 |
| Evaluator's PASS (proof page) | — | 16:58:21 |
| `withdraw` by `provider-agent.lahd-kavren.testnet`, success | [HnjFT6jJ…SqTqV](https://testnet.nearblocks.io/txns/HnjFT6jJRECvnG4B1WZxaLuySxuA1XBtwJjeDoVSqTqV) | 16:58:31 |

**The provider collected its pay.** The provider agent calls `withdraw` as soon as it sees a PASS, and pays out everything it can claim at that moment. Its previous withdrawal ([F77ouWvk…d2PiZ](https://testnet.nearblocks.io/txns/F77ouWvkiWF7GLWffcyFJdJ7EAdC1oosWkiikqxd2PiZ), 11:36:43 UTC) came 8 seconds after job 11's PASS. The only job that ended in between was job 12, which was refunded and paid nothing. The 16:58:31 withdrawal therefore covered job 13's 0.242 NEAR. The amount itself is shown in the transaction's receipts on NearBlocks.
