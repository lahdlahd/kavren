# KAVREN (codename)

Acceptance and payment assurance for AI agents hiring AI agents, on NEAR. Being built for NEAR HACKATHON 2026 (tracks: Agent Economy, AI Security & Defense).

**Status: Phase 0, work in progress.** This README will become the judge guide ("do X → see Y") once the contract, evaluator and agents exist. Nothing below claims more than the tests currently show.

## What exists today

| Path | What it is | How to check it |
|---|---|---|
| `kavren_core/` | Spec schema, canonical JSON, value rules (text/email/date), job templates | `cd kavren_core && pip install -e '.[test]' && pytest` |
| `fixtures/contacts/` | A hand-written messy input, canaries, a pass submission, 8 attack submissions, and a hand-checked answer key | `python3 fixtures/build_fixtures.py` regenerates identical bytes; `pytest` cross-checks the answer key |
| `docs/EVALUATION_RULES.md` | The normative definition of checks C1–C8 and H1 | — |
| `docs/VERSIONS.md` | Pinned tool versions and what was or wasn't verified | — |
| `scripts/signer_check/` | Proof that py-near signs correct function-call transactions (offline cross-check with near-api-js), plus a live testnet check | see its README |

## Secrets

Configuration comes only from environment variables (see `.env.example`). `.env*` files are gitignored, and gitleaks runs as a pre-commit hook (`pre-commit install`). Never commit keys.
