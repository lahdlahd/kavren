# KAVREN

Acceptance and payment assurance for AI agents hiring AI agents, on NEAR. Built for NEAR HACKATHON 2026 (tracks: Agent Economy, AI Security & Defense).

**Status: Live on NEAR testnet.** KAVREN provides on-chain escrow, a hosted evaluator, buyer and provider agents, and an MCP integration that enables independent AI agents to hire services through KAVREN. Testnet only; no real money. Nothing below claims more than the tests and the recorded live runs show.

## Check it in 5 minutes

1. **See a finished job.** Open [job 11](https://kavren.onrender.com/job/?id=11). Everything on the page is read from the contract by your browser:
   - the PASS seal;
   - every step the contract recorded, with its hashes;
   - **the hidden control records.** Before the work starts, the protocol mixes synthetic "canary" rows into the file and puts only their hash on-chain, so the provider can't tell them from real rows. After the verdict, the evaluator reveals them and the contract checks them against that commitment. Your browser then re-hashes the revealed set and shows that it matches, so the hidden checks couldn't have been changed after the job began;
   - **Model used: verified.** For the provider's AI *planning call*, the evaluator independently verified NEAR AI's TEE attestation for the model's signing key (Intel TDX and NVIDIA GPU evidence) and NEAR AI's signature over that exact request and response. It then re-ran the signed plan on the job's work file and confirmed it reproduces the submitted file byte for byte. This verifies the planning call only. It does not attest the rest of the CSV-cleaning process (deterministic code that anyone can re-run) or the evaluator itself, and it does not pin which software build the TEE runs.
2. **Don't trust our site.** Copy the `curl` command at the bottom of the page. It asks a public NEAR node for the same job record, with no KAVREN code involved.
3. **Watch an independent AI agent hire.** Jobs 10 and 11 were hired by Claude Desktop through KAVREN's MCP tools. It was given one instruction and worked inside the owner's budget rules: it quoted, hired, waited, re-checked the work itself and only then accepted. See [LIVE_RUNS.md](docs/LIVE_RUNS.md), runs 3–4.
4. **Watch cheating fail.** The [attack gallery](https://kavren.onrender.com/attacks/) records 17 attacks run against the live contract and evaluator, such as an empty file, deleted rows, an invented value, a fake merge, an agent acting with the wrong account's key and a replayed verdict. Each one was stopped. Together with opening and refunding the two isolated jobs, that makes 21 recorded outcomes, all as the protocol promises, each linked to its real transaction.
5. **Follow the money.**
   - The owner funds a budget with rules: a maximum per job, how many jobs can be open at once, allowed providers and job types, and an expiry.
   - The agent can only lock money into escrow within those rules.
   - A PASS bound to the exact file makes the escrow claimable by the provider.
   - Otherwise it returns to the owner's budget after the deadline, after all attempts are used, or if the evaluator goes silent.
6. **Run the evaluator yourself (free, offline).** From a clone of this repository, run `pip install ./kavren_core`, then `kavren verify fixtures/contacts --submission pass` to get PASS. With `--submission fail_false_reject` you get FAIL with the exact rule broken.
7. **Connect your own agent.** [AGENTS.md](AGENTS.md) shows an owner how to give any MCP-capable agent a scoped key and a budget.

## What exists today

| Path | What it is | How to check it |
|---|---|---|
| `kavren_core/` | Spec schema, canonical JSON, value rules (text/email/date), job templates | `cd kavren_core && pip install -e '.[test]' && pytest` |
| `fixtures/contacts/` | A hand-written messy input, canaries, a pass submission, 8 attack submissions, and a hand-checked answer key | `python3 fixtures/build_fixtures.py` regenerates identical bytes; `pytest` cross-checks the answer key |
| `contract/` | Rust contract: budgets, escrow, verdict-gated pull payments, timed refunds | `cd contract && cargo test` (54 invariant tests); sandbox tests run in CI. See `contract/README.md` |
| `kavren_core/…/evaluator.py` + `kavren` CLI | Deterministic checks C1–C8 and H1, including **rejection truth** and **merge truth**; canonical report and `report_hash`; deliverable builder | `kavren verify fixtures/contacts --submission pass` → `PASS`; `--submission fail_false_reject` → `FAIL` with C2 `REASON_NOT_TRUE` and C8. Add `--show-violations` for detail |
| `api/` | FastAPI service: NEP-413 wallet-signature login (key checked on-chain), drafts with canary generation, per-party private file access decided from the **on-chain** job, hash-checked uploads, the evaluator worker (verdicts, retries, canary reveal, 7-day file retention) | `cd api && pip install -e '.[test]' && pytest` (tests against a fake chain). Runs with `uvicorn kavren_api.main:create_app --factory` + `python -m kavren_api.worker` |
| `agents/` (provider) | **Provider agent**: an LLM on NEAR AI Cloud (`deepseek-ai/DeepSeek-V4.1-Flash-1017`, on NEAR AI's TEE-backed service, called without streaming so NEAR AI signs the exact request and response with the model's attested key. The provider uploads that evidence with its file; the evaluator, with its own NEAR AI key, checks a fresh TDX + GPU attestation for the signer, the signature, and that re-running the signed plan reproduces the file. Only then does the proof page say *verified*; otherwise it says *reported by the provider*. Never affects verdicts or payment; the TEE's software build is not pinned) decides only rejections and merges; deterministic tools apply the spec's allowed transformations, so it **cannot invent values**. It runs the public checks locally and revises before spending an on-chain attempt, uses FAIL reports to retry, and withdraws on PASS | `cd agents && pytest` (scripted planners, free); live: `.venv/bin/python scripts/provider_smoke.py` (about $0.01) |
| `agents/` | **Buyer agent** (rule-based, autonomous within the owner's on-chain policy): inbox → template → policy check → draft → `create_job` with its function-call key → monitor → refund, or **re-run the evaluation itself** before accepting a PASS | `cd agents && pip install -e '.[test]' && pytest` (including catching a lying evaluator and surviving RPC lag) |
| `agents/…/mcp_server.py` + `AGENTS.md` | **KAVREN for any AI agent.** A buyer-side MCP server (`kavren-mcp`) with six tools: budget, quote, hire, job status, accept (independent re-check), refund. Any MCP-capable agent, such as Claude, can hire a provider within an owner's on-chain budget, using its own restricted key. Six tools, no owner powers | `cd agents && pytest tests/test_mcp.py` (includes a real MCP client session); setup in `AGENTS.md` |
| `web/` | Static web UI (Next.js export). **Proof page** per job, read from the contract by the visitor's browser over NEAR RPC; **attack gallery** built from `docs/ATTACKS.md`; **owner console** where the owner funds the budget, sets the rules, withdraws or revokes, each change signed in their own wallet (Meteor via NEAR Wallet Selector; the site never receives a key) | `cd web && npm ci && npm test && npm run build` |
| `kavren_api.supervise`, `kavren_api.backup` | Hosting: API + evaluator worker under one supervisor (crash and stuck-worker restarts, daily backup), worker heartbeat in `/health`, backup/verify/restore | `docs/OPERATIONS.md`; `cd api && pytest` |
| `docs/EVALUATION_RULES.md` | The normative definition of checks C1–C8 and H1 | — |
| `docs/VERSIONS.md` | Pinned tool versions and what was or wasn't verified | — |
| `scripts/signer_check/` | Proof that py-near signs correct function-call transactions (offline cross-check with near-api-js), plus a live testnet check | see its README |

## Live on NEAR testnet

The first fully autonomous agent-to-agent job ran on testnet on 3 Nov 2026: hire, LLM work, evaluator PASS, canary reveal, buyer's independent re-verification and the provider's withdrawal, with explorer links. See `docs/LIVE_RUNS.md`.

## Attack gallery: a cheating provider against the live protocol

`scripts/attack_gallery.py` plays a **dishonest provider** against the deployed testnet contract, the running API and the running evaluator, signing with the agents' real function-call keys. It opens its own isolated jobs (same draft + `create_job` path as the buyer agent), submits dishonest work and dishonest calls, and **checks every outcome automatically**: verdict, failing check and code, report hash against the chain, and rejected transactions read back from the chain. It refunds its jobs at the end and writes `docs/ATTACKS.md` from what actually happened. The same script runs end to end in CI against a fake chain (`agents/tests/test_attack_gallery.py`), including a check that an honest file passed off as an attack is reported as a **miss**, not a success.

Each attack is stopped at one of four layers, with different trust assumptions:

- **Access key (NEAR runtime, trustless).** Each agent signs only with a function-call key limited to its own methods on the KAVREN contract (buyer: `create_job`, `refund_job`; provider: `submit_work`, `withdraw`; evaluator: `submit_verdict`, `reveal_canaries`). Such a key can never attach a deposit. A call outside the list is refused before execution, so no transaction is recorded.
- **Contract (on-chain, trustless).** Enforced by the NEAR runtime for everyone. No off-chain service can override it.
- **Evaluator (off-chain, deterministic, re-runnable).** A trusted service, but every verdict is bound on-chain to the exact file hash and report hash, and anyone with the files can re-run it with `kavren verify`. The buyer agent does exactly that before accepting a PASS.
- **API (off-chain gate, trusted).** Makes sure the file the evaluator judges is the file whose hash was committed on-chain. Even without it, a mismatched file couldn't earn a PASS, because the contract only accepts a verdict for the committed `output_hash`.

| # | Attack | What it demonstrates | Stopped by |
|---|---|---|---|
| 1 | Empty file | Nothing in, nothing paid: the file must be a valid submission | Evaluator: C1 |
| 2 | Silently deleted rows | Every input record must be accounted for (kept, merged or rejected) | Evaluator: C2 `MISSING_RECORD` |
| 3 | Made-up value (an invented, valid email) | Output values must come from the input, so a plausible fabrication fails | Evaluator: C7 `NOT_DERIVED_FROM_INPUT` |
| 4 | False rejection reasons (good rows rejected as `INVALID_EMAIL`) | Rejection reasons are re-derived from the data, not trusted (C2f) | Evaluator: C2 `REASON_NOT_TRUE` (often also the C8 cap) |
| 5 | Fake merge of two different people | Merges must join records with the same dedupe key in the input (C2e) | Evaluator: C2 `MERGE_KEY_MISMATCH` |
| 6 | A file that passes every public check but changes letter case | Hidden control records catch what the public checks allow; the report gives only a count and never names them | Evaluator: H1 (canaries) |
| 7 | Upload with no on-chain submission | Files are only accepted for a committed submission | API |
| 8 | Upload a different file than the committed hash | The judged file is exactly the committed one | API (and the contract's hash binding) |
| 9 | Provider gives itself a PASS | A provider's key can't call `submit_verdict` at all. Even with an unscoped key, the contract accepts verdicts only from the job's evaluator (contract tests and `test_unscoped_key_would_fall_through_to_the_contract_guard`) | Access key, then contract |
| 10 | Buyer agent submits work on its own job | Roles can't be swapped | Access key, then contract |
| 11 | Provider spends the owner's budget (`create_job`) | Only the agent the owner funded can spend that budget | Access key, then contract |
| 12 | Buyer agent's key withdraws the owner's budget | A leaked agent key can't drain funds: owner methods aren't in its scope and need a 1-yocto confirmation that function-call keys can't attach | Access key, then contract |
| 13 | Buyer refunds early (deadline not reached, attempts left) | Escrow can't be pulled back while the provider still has a fair chance | Contract |
| 14 | Provider withdraws pay without any PASS | Payment exists only after a PASS (pull payments) | Contract |
| 15 | Replay an old verdict as PASS, signed with the real evaluator key | Verdicts are bound to the current submission (sequence number and hash); even the evaluator can't reuse one | Contract |
| 16 | Submit after all attempts are used | The attempt limit is enforced | Contract |

**How to run it (testnet):** keep the API and the evaluator worker running, stop the provider and buyer agents, then run `BUYER_OWNER_ACCOUNT_ID=lahd-kavren.testnet .venv/bin/python scripts/attack_gallery.py`. It uses about 0.5 NEAR of testnet budget while it runs (two jobs at about 0.24 NEAR each). All of it is refunded except the per-job storage the contract keeps, plus gas. The exact figures go in `docs/ATTACKS.md`.

**Re-check the report yourself (no keys needed):** `.venv/bin/python scripts/verify_attacks.py` asks NEAR RPC for every linked transaction. It confirms each one was signed by the expected agent, went to the KAVREN contract, and either succeeded or failed with the exact contract message the report quotes.

## Limitations (honest, kept current)

- **The evaluator is a trusted off-chain service.** The contract binds each verdict to the exact submitted file, the exact job and the evaluator recorded on that job, but it can't prove the checks were run correctly. Anyone holding the files can re-run them with `kavren verify`. The `kavren verify` CLI reads the job's recorded hashes from a local `job.json`; the buyer agent's check before accepting reads them directly from the chain.
- **Rejection reasons are checked, not trusted (rule C2f, approved).** When a provider rejects a record as `INVALID_EMAIL`, `MISSING_REQUIRED` or `INVALID_DATE`, the evaluator re-derives that reason from the **work file's own values** and the spec's rules, and fails the submission if the reason isn't true. Merges are checked the same way: both records must share the same dedupe key in the input. This is defined in `docs/EVALUATION_RULES.md`. The evaluator implements it (`reason_is_true` in `evaluator.py`). The `fail_false_reject`, `fail_fake_merge` and wrong-reason fixtures show it catching false labels, and deliberately disabling the check makes those tests fail.
- **Canaries catch what the public checks allow, but they are a spot-check, not a proof.** The public checks compare text without regard to letter case (C7), and C6 only checks whitespace. So a provider that changes the case of names passes C1–C8. The hidden canaries expect the exact normalised value, so they catch it (attack 6 in the gallery). The same goes for future job types where full checking isn't possible. Canaries raise the cost of gaming but don't guarantee against it: a cheat that happens to avoid every canary would not be caught by H1.
- **If the evaluator misses its 30-minute window, the buyer can refund,** so the provider carries the risk of evaluator downtime.
- **Drafts that no honest provider could pass are refused.** If the buyer's file has more unfixable rows than the rejection cap allows, the API says so instead of creating a job nobody can pass.
- **The API is a trusted service.** It generates the canaries and holds them until the job is final, and it stores the private files. The contract and the commitments limit what it can change after funding, but it isn't trustless.
- **The contract can still be upgraded.** Whoever holds the full-access key of `kavren.lahd-kavren.testnet` (the project owner) could deploy new code and change any rule. It is kept during judging so bugs can be fixed, and will be removed (locking the contract permanently) after judging.
- **Testnet only, native NEAR only.**

## Secrets

Configuration comes only from environment variables (see `.env.example`). `.env*` files are gitignored, and gitleaks runs as a pre-commit hook (`pre-commit install`). Never commit keys.
