# KAVREN (codename)

Acceptance and payment assurance for AI agents hiring AI agents, on NEAR. Being built for NEAR HACKATHON 2026 (tracks: Agent Economy, AI Security & Defense).

**Status: contract deployed on NEAR testnet; evaluator, API, buyer agent and provider agent running live against it; web UI in progress.** This README will become the judge guide ("do X → see Y"). Nothing below claims more than the tests and the recorded live runs show.

## What exists today

| Path | What it is | How to check it |
|---|---|---|
| `kavren_core/` | Spec schema, canonical JSON, value rules (text/email/date), job templates | `cd kavren_core && pip install -e '.[test]' && pytest` |
| `fixtures/contacts/` | A hand-written messy input, canaries, a pass submission, 8 attack submissions, and a hand-checked answer key | `python3 fixtures/build_fixtures.py` regenerates identical bytes; `pytest` cross-checks the answer key |
| `contract/` | Rust contract: budgets, escrow, verdict-gated pull payments, timed refunds | `cd contract && cargo test` (54 invariant tests); sandbox tests run in CI. See `contract/README.md` |
| `kavren_core/…/evaluator.py` + `kavren` CLI | Deterministic checks C1–C8 and H1, including **rejection truth** and **merge truth**; canonical report and `report_hash`; deliverable builder | `kavren verify fixtures/contacts --submission pass` → `PASS`; `--submission fail_false_reject` → `FAIL` with C2 `REASON_NOT_TRUE` and C8. Add `--show-violations` for detail |
| `api/` | FastAPI service: NEP-413 wallet-signature login (key checked on-chain), drafts with canary generation, per-party private file access decided from the **on-chain** job, hash-checked uploads, the evaluator worker (verdicts, retries, canary reveal, 7-day file retention) | `cd api && pip install -e '.[test]' && pytest` (tests against a fake chain). Runs with `uvicorn kavren_api.main:create_app --factory` + `python -m kavren_api.worker` |
| `agents/` (provider) | **Provider agent**: an LLM on NEAR AI Cloud (`deepseek-ai/DeepSeek-V4.1-Flash-1017`, served from NEAR AI's attested TEE) decides only rejections and merges; deterministic tools apply the spec's allowed transformations, so it **cannot invent values**. It runs the public checks locally and revises before spending an on-chain attempt, uses FAIL reports to retry, and withdraws on PASS | `cd agents && pytest` (scripted planners, free); live: `.venv/bin/python scripts/provider_smoke.py` (about $0.01) |
| `agents/` | **Buyer agent** (rule-based, autonomous within the owner's on-chain policy): inbox → template → policy check → draft → `create_job` with its function-call key → monitor → refund, or **re-run the evaluation itself** before accepting a PASS | `cd agents && pip install -e '.[test]' && pytest` (including catching a lying evaluator and surviving RPC lag) |
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

- **The evaluator is a trusted off-chain service.** The contract binds each verdict to the exact submitted file, the exact job and the evaluator recorded on that job, but it can't prove the checks were run correctly. Anyone holding the files can re-run them with `kavren verify`. For now it reads the job's recorded hashes from `job.json`; reading them from the chain comes with the API milestone.
- **Rejection reasons are checked, not trusted (rule C2f, approved).** When a provider rejects a record as `INVALID_EMAIL`, `MISSING_REQUIRED` or `INVALID_DATE`, the evaluator re-derives that reason from the **work file's own values** and the spec's rules, and fails the submission if the reason isn't true. Merges are checked the same way: both records must share the same dedupe key in the input. This is defined in `docs/EVALUATION_RULES.md`. The evaluator implements it (`reason_is_true` in `evaluator.py`). The `fail_false_reject`, `fail_fake_merge` and wrong-reason fixtures show it catching false labels, and deliberately disabling the check makes those tests fail.
- **Canaries catch what the public checks allow, but they are a spot-check, not a proof.** The public checks compare text without regard to letter case (C7), and C6 only checks whitespace. So a provider that changes the case of names passes C1–C8. The hidden canaries expect the exact normalised value, so they catch it (attack 6 in the gallery). The same goes for future job types where full checking isn't possible. Canaries raise the cost of gaming but don't guarantee against it: a cheat that happens to avoid every canary would not be caught by H1.
- **If the evaluator misses its 30-minute window, the buyer can refund,** so the provider carries the risk of evaluator downtime.
- **Drafts that no honest provider could pass are refused.** If the buyer's file has more unfixable rows than the rejection cap allows, the API says so instead of creating a job nobody can pass.
- **The API is a trusted service.** It generates the canaries and holds them until the job is final, and it stores the private files. The contract and the commitments limit what it can change after funding, but it isn't trustless.
- **Testnet only, native NEAR only.**

## Secrets

Configuration comes only from environment variables (see `.env.example`). `.env*` files are gitignored, and gitleaks runs as a pre-commit hook (`pre-commit install`). Never commit keys.
