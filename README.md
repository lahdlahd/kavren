# KAVREN (codename)

Acceptance and payment assurance for AI agents hiring AI agents, on NEAR. Being built for NEAR HACKATHON 2026 (tracks: Agent Economy, AI Security & Defense).

**Status: contract, evaluator and API done (local); agents and web in progress; not yet deployed.** This README will become the judge guide ("do X → see Y") once the contract, evaluator and agents exist. Nothing below claims more than the tests currently show.

## What exists today

| Path | What it is | How to check it |
|---|---|---|
| `kavren_core/` | Spec schema, canonical JSON, value rules (text/email/date), job templates | `cd kavren_core && pip install -e '.[test]' && pytest` |
| `fixtures/contacts/` | A hand-written messy input, canaries, a pass submission, 8 attack submissions, and a hand-checked answer key | `python3 fixtures/build_fixtures.py` regenerates identical bytes; `pytest` cross-checks the answer key |
| `contract/` | Rust contract: budgets, escrow, verdict-gated pull payments, timed refunds | `cd contract && cargo test` (54 invariant tests); sandbox tests run in CI. See `contract/README.md` |
| `kavren_core/…/evaluator.py` + `kavren` CLI | Deterministic checks C1–C8 and H1, including **rejection truth** and **merge truth**; canonical report and `report_hash`; deliverable builder | `kavren verify fixtures/contacts --submission pass` → `PASS`; `--submission fail_false_reject` → `FAIL` with C2 `REASON_NOT_TRUE` and C8. Add `--show-violations` for detail |
| `api/` | FastAPI service: NEP-413 wallet-signature login (key checked on-chain), drafts with canary generation, per-party private file access decided from the **on-chain** job, hash-checked uploads, the evaluator worker (verdicts, retries, canary reveal, 7-day file retention) | `cd api && pip install -e '.[test]' && pytest` (16 tests against a fake chain). Runs with `uvicorn kavren_api.main:create_app --factory` + `python -m kavren_api.worker` |
| `agents/` | **Buyer agent** (rule-based, autonomous within the owner's on-chain policy): inbox → template → policy check → draft → `create_job` with its function-call key → monitor → refund, or **re-run the evaluation itself** before accepting a PASS | `cd agents && pip install -e '.[test]' && pytest` (10 tests, including catching a lying evaluator) |
| `docs/EVALUATION_RULES.md` | The normative definition of checks C1–C8 and H1 | — |
| `docs/VERSIONS.md` | Pinned tool versions and what was or wasn't verified | — |
| `scripts/signer_check/` | Proof that py-near signs correct function-call transactions (offline cross-check with near-api-js), plus a live testnet check | see its README |

## Limitations (honest, kept current)

- **The evaluator is a trusted off-chain service.** The contract binds each verdict to the exact submitted file, the exact job and the evaluator recorded on that job, but it can't prove the checks were run correctly. Anyone holding the files can re-run them with `kavren verify`. For now it reads the job's recorded hashes from `job.json`; reading them from the chain comes with the API milestone.
- **Rejection reasons are checked, not trusted (rule C2f, approved).** When a provider rejects a record as `INVALID_EMAIL`, `MISSING_REQUIRED` or `INVALID_DATE`, the evaluator re-derives that reason from the **work file's own values** and the spec's rules, and fails the submission if the reason isn't true. Merges are checked the same way: both records must share the same dedupe key in the input. This is defined in `docs/EVALUATION_RULES.md`. The evaluator implements it (`reason_is_true` in `evaluator.py`). The `fail_false_reject`, `fail_fake_merge` and wrong-reason fixtures show it catching false labels, and deliberately disabling the check makes those tests fail.
- **For CSV cleaning, canaries are a second layer.** Because the public checks fully determine correctness for this job type, every canary failure in our fixtures is also caught by a public check. Canaries stay in as the protocol's mechanism for future job types where full checking isn't possible, and as an independent spot-check. They raise the cost of gaming; they don't guarantee against it.
- **If the evaluator misses its 30-minute window, the buyer can refund,** so the provider carries the risk of evaluator downtime.
- **Drafts that no honest provider could pass are refused.** If the buyer's file has more unfixable rows than the rejection cap allows, the API says so instead of creating a job nobody can pass.
- **The API is a trusted service.** It generates the canaries and holds them until the job is final, and it stores the private files. The contract and the commitments limit what it can change after funding, but it isn't trustless.
- **Testnet only, native NEAR only.**

## Secrets

Configuration comes only from environment variables (see `.env.example`). `.env*` files are gitignored, and gitleaks runs as a pre-commit hook (`pre-commit install`). Never commit keys.
