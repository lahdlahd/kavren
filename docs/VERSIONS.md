# Tool versions (checked 3 Nov 2026)

Checked against the live package registries (crates.io, PyPI, npm) on 3 Nov 2026. Each dependency is pinned exactly where it's introduced. If a later phase needs to change a version, the change is recorded here with the reason.

| Component | Package | Version | Released / updated | Used for |
|---|---|---|---|---|
| Contract SDK | `near-sdk` (crates.io) | **5.30.0** | 2026-10-27 | Rust contract |
| Contract build | `cargo-near` (crates.io) | **0.22.0** | 2026-10-19 | reproducible wasm build + deploy |
| Contract integration tests | `near-workspaces` (crates.io) | **0.24.0** | 2026-10-26 | sandbox tests |
| Sandbox node | `near-sandbox` (crates.io) | 0.4.0 | 2026-10-26 | pulled in by near-workspaces |
| Rust API client | `near-api` (crates.io) | 0.8.7 | 2026-10-28 | not used (reference only) |
| Python NEAR client | `py-near` (PyPI) | **1.2.22** | — | agents + evaluator transaction signing |
| JS NEAR client | `near-api-js` (npm) | **7.4.0** | 2026-10-23 | signer cross-check; fallback signer; web |
| Wallet | `@near-wallet-selector/core` (npm) | 10.1.4 | 2025-12-04 | web owner console (check for a newer major before Phase 4) |
| Spec schema | `pydantic` (PyPI) | 2.13.5 | — | kavren_core |
| Tests | `pytest` (PyPI) | 9.2.0 | — | kavren_core |
| Secret scan | `gitleaks` | **v8.30.1** | latest tag | pre-commit hook |
| Toolchain in Claude's workspace | rustc / cargo | 1.97.0 | — | — |
| Toolchain in Claude's workspace | Python / Node | 3.13.18 / 22.23.4 | — | — |

## What was verified in Claude's workspace

- **`near-sdk` 5.30.0 compiles and its unit tests run** natively with rustc 1.97.0, using the `unit-testing` feature and a small throwaway contract.
- **py-near 1.2.22 builds a correct signed FunctionCall transaction.** Checked offline in `scripts/signer_check`:
  - near-api-js 7.4.0 decodes the transaction to identical bytes
  - the signature verifies against the public key
  - near-api-js produces the identical (deterministic ed25519) signature from the same key
- **py-near has no NEP-413 (signed message) helper for servers.** The API will implement NEP-413 verification directly in Phase 2: borsh payload with tag `2^31 + 413`, then sha256, then an ed25519 check, then an RPC check that the key belongs to the account.
- **Doc quirk:** py-near's docstring calls a function-call key `allowance` "gas units". On NEAR the allowance is denominated in **yoctoNEAR**. We'll always pass explicit yocto values.

## What could not be verified here, and why

Claude's workspace can only reach package registries and GitHub. These hosts are blocked:

| Blocked host | Consequence | Where it runs instead |
|---|---|---|
| `static.rust-lang.org` | Can't install the `wasm32-unknown-unknown` target, so no wasm build here | Owner's machine or GitHub Actions |
| `s3-us-west-1.amazonaws.com` | Can't download the sandbox node binary, so no near-workspaces integration tests here | Owner's machine or GitHub Actions |
| `rpc.testnet.near.org` | No live testnet calls, so the function-call-key live check can't run here | Owner runs `scripts/signer_check/testnet_fc_check.py` |

Contract invariant tests are therefore written as **near-sdk unit tests**, which run here with simulated block time and predecessor accounts. Sandbox integration tests are added for wasm-level behaviour and run in CI.
