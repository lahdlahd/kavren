# KAVREN contract (Rust, near-sdk 5.30.0)

Holds owner budgets and job escrow, and releases payment only on a PASS verdict from the evaluator recorded on the job. Every rule below is enforced on-chain.

## Who can call what

| Method | Caller | Key that can reach it | Notes |
|---|---|---|---|
| `new(admin, evaluator, config?)` | deployer | — | `config` defaults to 30-min eval window, 10 min to 7 day deadlines, at most 5 attempts |
| `fund_budget(agent_id, policy)` | owner | full access (it's payable) | storage for a new budget is taken from the deposit |
| `update_policy`, `revoke_agent`, `withdraw_budget` | owner | full access (exactly 1 yocto attached) | `withdraw_budget` touches **unlocked** funds only |
| `create_job({args})` | the budget's agent | function-call key | must satisfy the policy; amount + measured storage locked from the budget |
| `refund_job(job_id)` | **anyone** | any | only when a refund condition holds; money goes back to the **budget** |
| `submit_work(job_id, output_hash)` | the job's provider | function-call key | returns the new `seq` |
| `withdraw()` | anyone with a claimable balance | function-call key | pull payment to the caller |
| `submit_verdict(job_id, seq, output_hash, verdict, report_hash)` | the job's evaluator | function-call key | must match the **current** seq and hash, inside the window |
| `reveal_canaries(job_id, blob)` | the job's evaluator | function-call key | only in a final state; `sha256(blob)` must equal the commitment |
| `set_evaluator`, `set_config` | admin | full access (1 yocto) | apply to **new** jobs only; existing jobs keep their snapshot |
| views | anyone | — | `get_job`, `get_budget`, `get_claimable`, `get_jobs_for_provider`, `get_config`, `get_evaluator`, `get_admin`, `get_totals` |

## Time rules (UNIX seconds; the boundaries never overlap)

```
submit allowed          now <  deadline
refund: deadline        now >= deadline              (status Open)
refund: attempts used   attempts_used == max_attempts (status Open)
verdict allowed         now <  submitted_at + eval_window_secs
refund: evaluator late  now >= submitted_at + eval_window_secs (status Submitted)
```

A submission made before the deadline is still evaluated after the deadline, as long as it's inside its window.

## Invariants and the tests that prove them

`cargo test`: 54 unit tests with simulated time and callers.

| # | Invariant | Tests |
|---|---|---|
| 1 | Escrow is funded only from a budget, by its agent, within policy | `create_job_locks_amount_and_storage_from_budget`, `stranger_cannot_create_job_from_budget`, `owner_is_not_the_agent`, `over_max_per_job_rejected`, `template_outside_policy_rejected`, `provider_outside_policy_rejected`, `spec_must_match_template`, `max_open_jobs_enforced`, `cannot_lock_more_than_budget`, `expired_budget_rejected`, `revoked_agent_rejected`, `deadline_too_soon_rejected`, `attempts_above_policy_rejected`, `malformed_hash_rejected` |
| 2, 11 | Job terms, evaluator and timing are fixed at creation; admin changes affect new jobs only; admin has no path to funds | `admin_changes_do_not_touch_existing_jobs`, `new_evaluator_cannot_decide_old_job`, `non_admin_cannot_set_evaluator`, `admin_actions_need_full_access_key`, `admin_cannot_pass_a_job` |
| 3 | PASS credits exactly once; FAIL never credits | `pass_credits_provider_exactly_once_and_withdraw_pays_once`, `second_withdraw_pays_nothing`, `second_pass_rejected`, `fail_never_credits_and_reopens_job`, `fail_then_retry_then_pass` |
| 4 | Stale verdicts are rejected | `stale_seq_rejected`, `old_output_hash_rejected` |
| 5, 6 | Only the job's evaluator decides; only the provider submits; only the holder withdraws | `provider_cannot_pass_itself`, `only_provider_submits`, `stranger_cannot_withdraw_providers_payment`, `evaluator_cannot_be_provider` |
| 7 | An agent can never take budget or escrow | `agent_cannot_withdraw_budget`, `owner_methods_need_one_yocto`, `owner_cannot_withdraw_locked_funds`, `owner_withdraws_unlocked_budget` |
| 8 | Conservation: `deposited = withdrawn + budgets + escrow + claimable + retained`, with each total equal to the sum of its records | checked inside most tests, plus `conservation_holds_over_random_valid_operations` (600 random valid steps across two budgets and two providers) |
| 9 | No permanent lock | `every_open_state_has_a_timed_exit`, `refund_after_deadline_returns_funds_to_budget`, `refund_after_attempts_exhausted`, `refund_when_evaluator_times_out`, `no_timeout_refund_inside_window_even_after_deadline`, `verdict_after_deadline_inside_window_counts`, `revoked_agents_open_jobs_still_settle_and_refund_to_budget`, plus boundary tests |
| 10 | Bad reveals are rejected | `reveal_opens_commitment_after_final_state`, `wrong_reveal_rejected`, `no_reveal_while_job_can_still_be_worked`, `reveal_only_once`, `only_evaluator_reveals` |

**Mutation check:** six deliberate bugs were introduced one at a time, and the suite failed every time:
- removing the seq check
- removing the evaluator check
- refunding one second before the deadline
- crediting one extra yocto
- removing the `max_per_job` check
- refunding only half

**`sandbox-tests/`** covers what unit tests can't. It uses the real runtime, the real wasm and real access keys:
- a full flow signed only with function-call keys, in which NEAR actually moves to the provider
- a function-call key can't attach a deposit
- a function-call key can't call owner methods
- non-payable methods reject deposits even from a full-access key
- an over-limit job is rejected

These tests need the wasm build and the sandbox binary, so they run in GitHub Actions (`.github/workflows/ci.yml`). Locally they only compile in Claude's workspace (see `docs/VERSIONS.md`).

## Where this departs from the brief, with reasons

- **The verdict window is `now < submitted_at + window`** (the brief said `≤`). This way verdict and timeout refund never overlap, even for one second.
- **A provider can't be the protocol evaluator.** Otherwise one account could approve its own work.
- **Job storage is measured, plus a fixed 512-byte allowance** for verdict records written later. Both are charged to the budget.
- **The contract checks only what it relies on in `spec_json`:** size ≤ 2 KB, valid JSON, `template_id` matches the job, `evaluator_version` present. Full schema validation is done by `kavren_core` and re-checked by the provider agent before it works.
- **A failed transfer in `withdraw` / `withdraw_budget`.** The balance is zeroed before the transfer. The transfer always goes to the calling account, so it can only fail if that account deletes itself in the same block. In that case the NEAR is returned to the contract and becomes untracked surplus. Conservation is stated as "≥" on the live contract for this reason.

## Limitations (stated plainly)

- **The contract can't check the evaluator's work.** It accepts a verdict from the evaluator recorded on the job, bound to the exact current submission, inside the window. Whether the checks were actually run correctly is verified off-chain by replaying them (`kavren verify`, coming in the evaluator milestone).
- **If the evaluator is late, the buyer can refund.** The provider carries the risk of the evaluator being unavailable.
- **Upgradeability.** Whoever holds the contract account's full-access key can redeploy the code. It stays upgradeable during the build; see brief section 18, decision A.
