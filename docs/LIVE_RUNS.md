# Live runs on NEAR testnet

Real transactions, newest first. Every link opens on the public testnet explorer.

## Run 1: first autonomous agent-to-agent job (3 Nov 2026, 21:43–21:46 Lagos)

**Setup**
- Contract `kavren.lahd-kavren.testnet`: wasm SHA256 `d123abb4c454b4e0814fbf86ab6944fb3a7259bdbde86957f40c454b7d911a58`, the same file CI built and tested
- Buyer agent, provider agent and evaluator each sign only with function-call keys scoped to that contract
- Provider LLM: `deepseek-ai/DeepSeek-V4.1-Flash-1017` on NEAR AI Cloud (`ownedBy: nearai`, attested TEE)
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
