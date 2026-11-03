//! Invariant tests (brief section 8.4). Unit tests with simulated time and callers.
//! Note: in unit tests a panicking call does not roll back state the way the NEAR
//! runtime does, so every #[should_panic] test ends at the panic.

use super::*;
use near_sdk::test_utils::{get_logs, VMContextBuilder};
use near_sdk::testing_env;

const NEAR: u128 = 1_000_000_000_000_000_000_000_000;
const T0: u64 = 1_800_000_000;
const ONE_YOCTO: u128 = 1;

fn acc(s: &str) -> AccountId {
    format!("{s}.testnet").parse().unwrap()
}

fn ctx(pred: &str, deposit: u128, t: u64) {
    let mut b = VMContextBuilder::new();
    b.current_account_id(acc("kavren"))
        .signer_account_id(acc(pred))
        .predecessor_account_id(acc(pred))
        .attached_deposit(NearToken::from_yoctonear(deposit))
        .block_timestamp(t * 1_000_000_000)
        .account_balance(NearToken::from_near(10_000));
    testing_env!(b.build());
}

/// New contract on EMPTY storage (unit-test storage otherwise carries over).
fn fresh() -> Kavren {
    near_sdk::mock::with_mocked_blockchain(|b| {
        let _ = b.take_storage();
    });
    ctx("deployer", 0, T0);
    Kavren::new(acc("admin"), acc("evaluator"), None)
}

fn h(c: char) -> String {
    std::iter::repeat(c).take(64).collect()
}

fn spec(t: &str) -> String {
    format!(r#"{{"evaluator_version":"0.1.0","template_id":"{t}"}}"#)
}

fn policy() -> Policy {
    Policy {
        max_per_job: U128(2 * NEAR),
        max_open_jobs: 2,
        allowed_templates: vec!["contacts-v1".into()],
        allowed_providers: vec![acc("provider")],
        max_deadline_secs: 7 * 24 * 3600,
        max_attempts: 3,
        expires_at: T0 + 30 * 24 * 3600,
    }
}

fn job_args(amount: u128) -> CreateJobArgs {
    CreateJobArgs {
        owner_id: acc("owner"),
        provider_id: acc("provider"),
        amount: U128(amount),
        template_id: "contacts-v1".into(),
        spec_json: spec("contacts-v1"),
        draft_id: "draft-1".into(),
        input_hash: h('1'),
        work_hash: h('2'),
        canary_commitment: canary_commitment(),
        deadline: T0 + 3600,
        max_attempts: 3,
    }
}

const REVEAL: &[u8] = b"{\"canaries\":[],\"salt\":\"00\"}";

fn canary_commitment() -> String {
    to_hex(&env::sha256(REVEAL))
}

/// Contract with a 10 NEAR budget for (owner, agent).
fn setup() -> Kavren {
    let mut c = fresh();
    ctx("owner", 10 * NEAR, T0);
    c.fund_budget(acc("agent"), policy());
    c
}

fn budget(c: &Kavren) -> Budget {
    c.get_budget(acc("owner"), acc("agent")).unwrap()
}

fn create(c: &mut Kavren, amount: u128) -> u64 {
    ctx("agent", 0, T0);
    c.create_job(job_args(amount))
}

fn submit(c: &mut Kavren, id: u64, out: char, t: u64) -> u32 {
    ctx("provider", 0, t);
    c.submit_work(id, h(out))
}

fn verdict(c: &mut Kavren, id: u64, seq: u32, out: char, v: Verdict, t: u64) {
    ctx("evaluator", 0, t);
    c.submit_verdict(id, seq, h(out), v, h('e'));
}

/// deposited == withdrawn + budgets + escrow + claimable + retained, and the
/// running totals equal the sums recomputed from the records.
fn assert_conserved(c: &Kavren, budgets: &[(&str, &str)], jobs: u64, claimants: &[&str]) {
    let t = c.get_totals();
    assert_eq!(t.deposited.0, t.withdrawn.0 + t.budgets.0 + t.escrow.0 + t.claimable.0 + t.retained.0, "conservation");
    let b: u128 = budgets.iter().filter_map(|(o, a)| c.get_budget(acc(o), acc(a))).map(|b| b.balance.0).sum();
    assert_eq!(b, t.budgets.0, "budget total");
    let e: u128 = (0..jobs)
        .filter_map(|i| c.get_job(i))
        .filter(|j| matches!(j.status, JobStatus::Open | JobStatus::Submitted))
        .map(|j| j.amount.0)
        .sum();
    assert_eq!(e, t.escrow.0, "escrow total");
    let cl: u128 = claimants.iter().map(|a| c.get_claimable(acc(a)).0).sum();
    assert_eq!(cl, t.claimable.0, "claimable total");
}

// =================================================================== budget and funding

#[test]
fn fund_budget_charges_storage_and_credits_rest() {
    let c = setup();
    let b = budget(&c);
    let t = c.get_totals();
    assert!(b.balance.0 < 10 * NEAR && b.balance.0 > 9 * NEAR);
    assert_eq!(b.balance.0 + t.retained.0, 10 * NEAR);
    assert_conserved(&c, &[("owner", "agent")], 0, &[]);
}

#[test]
#[should_panic(expected = "allowed_providers must list 1-16 providers")]
fn policy_requires_explicit_providers() {
    let mut c = fresh();
    let mut p = policy();
    p.allowed_providers = vec![];
    ctx("owner", NEAR, T0);
    c.fund_budget(acc("agent"), p);
}

// =================================================================== I1: escrow only from budget, by agent, within policy

#[test]
fn create_job_locks_amount_and_storage_from_budget() {
    let mut c = setup();
    let before = budget(&c).balance.0;
    let id = create(&mut c, 3 * NEAR / 2);
    let j = c.get_job(id).unwrap();
    assert_eq!(j.status, JobStatus::Open);
    assert_eq!(j.evaluator_id, acc("evaluator"));
    assert!(j.storage_cost.0 > 0);
    assert_eq!(budget(&c).balance.0, before - 3 * NEAR / 2 - j.storage_cost.0);
    assert_eq!(budget(&c).open_jobs, 1);
    assert!(get_logs().iter().any(|l| l.starts_with("EVENT_JSON:") && l.contains("\"job_created\"")));
    assert_conserved(&c, &[("owner", "agent")], 1, &[]);
}

#[test]
#[should_panic(expected = "no budget for this owner and agent")]
fn stranger_cannot_create_job_from_budget() {
    let mut c = setup();
    ctx("stranger", 0, T0);
    c.create_job(job_args(NEAR));
}

#[test]
#[should_panic(expected = "no budget for this owner and agent")]
fn owner_is_not_the_agent() {
    let mut c = setup();
    ctx("owner", 0, T0);
    c.create_job(job_args(NEAR));
}

#[test]
#[should_panic(expected = "amount exceeds max_per_job")]
fn over_max_per_job_rejected() {
    let mut c = setup();
    create(&mut c, 3 * NEAR);
}

#[test]
#[should_panic(expected = "template not allowed by policy")]
fn template_outside_policy_rejected() {
    let mut c = setup();
    let mut a = job_args(NEAR);
    a.template_id = "leads-v1".into();
    a.spec_json = spec("leads-v1");
    ctx("agent", 0, T0);
    c.create_job(a);
}

#[test]
#[should_panic(expected = "provider not allowed by policy")]
fn provider_outside_policy_rejected() {
    let mut c = setup();
    let mut a = job_args(NEAR);
    a.provider_id = acc("stranger");
    ctx("agent", 0, T0);
    c.create_job(a);
}

#[test]
#[should_panic(expected = "spec template_id does not match")]
fn spec_must_match_template() {
    let mut c = setup();
    let mut a = job_args(NEAR);
    a.spec_json = spec("leads-v1");
    ctx("agent", 0, T0);
    c.create_job(a);
}

#[test]
#[should_panic(expected = "max_open_jobs reached")]
fn max_open_jobs_enforced() {
    let mut c = setup();
    create(&mut c, NEAR / 2);
    create(&mut c, NEAR / 2);
    create(&mut c, NEAR / 2);
}

#[test]
#[should_panic(expected = "insufficient budget for amount + storage")]
fn cannot_lock_more_than_budget() {
    let mut c = fresh();
    ctx("owner", NEAR, T0);
    c.fund_budget(acc("agent"), policy());
    create(&mut c, NEAR); // budget is 1 NEAR minus storage
}

#[test]
#[should_panic(expected = "budget expired")]
fn expired_budget_rejected() {
    let mut c = setup();
    ctx("agent", 0, T0 + 31 * 24 * 3600);
    c.create_job(job_args(NEAR));
}

#[test]
#[should_panic(expected = "agent revoked")]
fn revoked_agent_rejected() {
    let mut c = setup();
    ctx("owner", ONE_YOCTO, T0);
    c.revoke_agent(acc("agent"));
    create(&mut c, NEAR);
}

#[test]
#[should_panic(expected = "deadline too soon")]
fn deadline_too_soon_rejected() {
    let mut c = setup();
    let mut a = job_args(NEAR);
    a.deadline = T0 + 60;
    ctx("agent", 0, T0);
    c.create_job(a);
}

#[test]
#[should_panic(expected = "max_attempts out of range")]
fn attempts_above_policy_rejected() {
    let mut c = setup();
    let mut a = job_args(NEAR);
    a.max_attempts = 4;
    ctx("agent", 0, T0);
    c.create_job(a);
}

#[test]
#[should_panic(expected = "hashes must be 64 lowercase hex")]
fn malformed_hash_rejected() {
    let mut c = setup();
    let mut a = job_args(NEAR);
    a.work_hash = h('A');
    ctx("agent", 0, T0);
    c.create_job(a);
}

// =================================================================== I2 + I11: terms fixed; admin changes affect new jobs only

#[test]
fn admin_changes_do_not_touch_existing_jobs() {
    let mut c = setup();
    let id = create(&mut c, NEAR);
    let before = c.get_job(id).unwrap();
    ctx("admin", ONE_YOCTO, T0);
    c.set_evaluator(acc("evaluator2"));
    ctx("admin", ONE_YOCTO, T0);
    c.set_config(Config { eval_window_secs: 600, ..Config::default_mvp() });
    assert_eq!(c.get_job(id).unwrap(), before);
    // the snapshotted evaluator still decides the old job; the new one cannot
    let seq = submit(&mut c, id, 'a', T0 + 10);
    verdict(&mut c, id, seq, 'a', Verdict::Pass, T0 + 1000); // 1000s > new 600s window, < old 1800s window
    assert_eq!(c.get_job(id).unwrap().status, JobStatus::Passed);
    let id2 = create(&mut c, NEAR);
    assert_eq!(c.get_job(id2).unwrap().evaluator_id, acc("evaluator2"));
    assert_eq!(c.get_job(id2).unwrap().eval_window_secs, 600);
}

#[test]
#[should_panic(expected = "only the job's evaluator")]
fn new_evaluator_cannot_decide_old_job() {
    let mut c = setup();
    let id = create(&mut c, NEAR);
    ctx("admin", ONE_YOCTO, T0);
    c.set_evaluator(acc("evaluator2"));
    let seq = submit(&mut c, id, 'a', T0 + 10);
    ctx("evaluator2", 0, T0 + 20);
    c.submit_verdict(id, seq, h('a'), Verdict::Pass, h('e'));
}

#[test]
#[should_panic(expected = "only admin")]
fn non_admin_cannot_set_evaluator() {
    let mut c = setup();
    ctx("owner", ONE_YOCTO, T0);
    c.set_evaluator(acc("owner"));
}

#[test]
#[should_panic(expected = "Requires attached deposit of exactly 1 yoctoNEAR")]
fn admin_actions_need_full_access_key() {
    let mut c = setup();
    ctx("admin", 0, T0);
    c.set_evaluator(acc("evaluator2"));
}

// =================================================================== I3: PASS credits once, FAIL never credits

#[test]
fn pass_credits_provider_exactly_once_and_withdraw_pays_once() {
    let mut c = setup();
    let id = create(&mut c, 3 * NEAR / 2);
    let seq = submit(&mut c, id, 'a', T0 + 10);
    verdict(&mut c, id, seq, 'a', Verdict::Pass, T0 + 20);
    assert_eq!(c.get_claimable(acc("provider")).0, 3 * NEAR / 2);
    assert_eq!(budget(&c).open_jobs, 0);
    assert_conserved(&c, &[("owner", "agent")], 1, &["provider"]);
    ctx("provider", 0, T0 + 30);
    let _ = c.withdraw();
    assert_eq!(c.get_claimable(acc("provider")).0, 0);
    assert_eq!(c.get_totals().withdrawn.0, 3 * NEAR / 2);
    assert_conserved(&c, &[("owner", "agent")], 1, &["provider"]);
}

#[test]
#[should_panic(expected = "nothing to withdraw")]
fn second_withdraw_pays_nothing() {
    let mut c = setup();
    let id = create(&mut c, NEAR);
    let seq = submit(&mut c, id, 'a', T0 + 10);
    verdict(&mut c, id, seq, 'a', Verdict::Pass, T0 + 20);
    ctx("provider", 0, T0 + 30);
    let _ = c.withdraw();
    ctx("provider", 0, T0 + 31);
    let _ = c.withdraw();
}

#[test]
#[should_panic(expected = "no submission awaiting a verdict")]
fn second_pass_rejected() {
    let mut c = setup();
    let id = create(&mut c, NEAR);
    let seq = submit(&mut c, id, 'a', T0 + 10);
    verdict(&mut c, id, seq, 'a', Verdict::Pass, T0 + 20);
    verdict(&mut c, id, seq, 'a', Verdict::Pass, T0 + 21);
}

#[test]
fn fail_never_credits_and_reopens_job() {
    let mut c = setup();
    let id = create(&mut c, NEAR);
    let seq = submit(&mut c, id, 'a', T0 + 10);
    verdict(&mut c, id, seq, 'a', Verdict::Fail, T0 + 20);
    let j = c.get_job(id).unwrap();
    assert_eq!(j.status, JobStatus::Open);
    assert_eq!(j.attempts_used, 1);
    assert_eq!(c.get_claimable(acc("provider")).0, 0);
    assert_eq!(c.get_totals().escrow.0, NEAR);
    assert_conserved(&c, &[("owner", "agent")], 1, &["provider"]);
}

#[test]
fn fail_then_retry_then_pass() {
    let mut c = setup();
    let id = create(&mut c, NEAR);
    let s1 = submit(&mut c, id, 'a', T0 + 10);
    verdict(&mut c, id, s1, 'a', Verdict::Fail, T0 + 20);
    let s2 = submit(&mut c, id, 'b', T0 + 30);
    assert_eq!(s2, 2);
    verdict(&mut c, id, s2, 'b', Verdict::Pass, T0 + 40);
    let j = c.get_job(id).unwrap();
    assert_eq!(j.status, JobStatus::Passed);
    assert_eq!(j.verdicts.len(), 2);
    assert_eq!(c.get_claimable(acc("provider")).0, NEAR);
}

// =================================================================== I4: stale verdicts rejected

#[test]
#[should_panic(expected = "stale or unknown submission seq")]
fn stale_seq_rejected() {
    let mut c = setup();
    let id = create(&mut c, NEAR);
    let s1 = submit(&mut c, id, 'a', T0 + 10);
    verdict(&mut c, id, s1, 'a', Verdict::Fail, T0 + 20);
    submit(&mut c, id, 'b', T0 + 30);
    verdict(&mut c, id, s1, 'b', Verdict::Pass, T0 + 40); // replay of the old seq
}

#[test]
#[should_panic(expected = "output_hash does not match current submission")]
fn old_output_hash_rejected() {
    let mut c = setup();
    let id = create(&mut c, NEAR);
    let s1 = submit(&mut c, id, 'a', T0 + 10);
    verdict(&mut c, id, s1, 'a', Verdict::Fail, T0 + 20);
    let s2 = submit(&mut c, id, 'b', T0 + 30);
    verdict(&mut c, id, s2, 'a', Verdict::Pass, T0 + 40); // PASS for the old file
}

// =================================================================== I5 + I6: who may call what

#[test]
#[should_panic(expected = "only the job's evaluator")]
fn provider_cannot_pass_itself() {
    let mut c = setup();
    let id = create(&mut c, NEAR);
    let seq = submit(&mut c, id, 'a', T0 + 10);
    ctx("provider", 0, T0 + 20);
    c.submit_verdict(id, seq, h('a'), Verdict::Pass, h('e'));
}

#[test]
#[should_panic(expected = "only the job's evaluator")]
fn admin_cannot_pass_a_job() {
    let mut c = setup();
    let id = create(&mut c, NEAR);
    let seq = submit(&mut c, id, 'a', T0 + 10);
    ctx("admin", 0, T0 + 20);
    c.submit_verdict(id, seq, h('a'), Verdict::Pass, h('e'));
}

#[test]
#[should_panic(expected = "only the job's provider")]
fn only_provider_submits() {
    let mut c = setup();
    let id = create(&mut c, NEAR);
    ctx("stranger", 0, T0 + 10);
    c.submit_work(id, h('a'));
}

#[test]
#[should_panic(expected = "nothing to withdraw")]
fn stranger_cannot_withdraw_providers_payment() {
    let mut c = setup();
    let id = create(&mut c, NEAR);
    let seq = submit(&mut c, id, 'a', T0 + 10);
    verdict(&mut c, id, seq, 'a', Verdict::Pass, T0 + 20);
    ctx("stranger", 0, T0 + 30);
    let _ = c.withdraw();
}

#[test]
#[should_panic(expected = "provider cannot be the evaluator")]
fn evaluator_cannot_be_provider() {
    let mut c = setup();
    let mut p = policy();
    p.allowed_providers = vec![acc("evaluator")];
    ctx("owner", ONE_YOCTO, T0);
    c.update_policy(acc("agent"), p);
    let mut a = job_args(NEAR);
    a.provider_id = acc("evaluator");
    ctx("agent", 0, T0);
    c.create_job(a);
}

// =================================================================== I7: agent can never take budget or escrow

#[test]
#[should_panic(expected = "no budget for this owner and agent")]
fn agent_cannot_withdraw_budget() {
    let mut c = setup();
    ctx("agent", ONE_YOCTO, T0); // even if it could attach 1 yocto
    let _ = c.withdraw_budget(acc("agent"), U128(NEAR));
}

#[test]
#[should_panic(expected = "Requires attached deposit of exactly 1 yoctoNEAR")]
fn owner_methods_need_one_yocto() {
    // a function-call key cannot attach a deposit, so it can never reach these
    let mut c = setup();
    ctx("owner", 0, T0);
    let _ = c.withdraw_budget(acc("agent"), U128(NEAR));
}

#[test]
#[should_panic(expected = "insufficient unlocked budget")]
fn owner_cannot_withdraw_locked_funds() {
    let mut c = setup();
    create(&mut c, 2 * NEAR);
    create(&mut c, 2 * NEAR);
    let bal = budget(&c).balance.0;
    ctx("owner", ONE_YOCTO, T0);
    let _ = c.withdraw_budget(acc("agent"), U128(bal + 1));
}

#[test]
fn owner_withdraws_unlocked_budget() {
    let mut c = setup();
    create(&mut c, NEAR);
    let bal = budget(&c).balance.0;
    ctx("owner", ONE_YOCTO, T0);
    let _ = c.withdraw_budget(acc("agent"), U128(bal));
    assert_eq!(budget(&c).balance.0, 0);
    assert_eq!(c.get_totals().escrow.0, NEAR);
    assert_conserved(&c, &[("owner", "agent")], 1, &[]);
}

// =================================================================== I9: no permanent lock; refunds go to the budget

#[test]
fn refund_after_deadline_returns_funds_to_budget() {
    let mut c = setup();
    let id = create(&mut c, NEAR);
    let before = budget(&c).balance.0;
    ctx("stranger", 0, T0 + 3600); // permissionless; exactly at the deadline
    c.refund_job(id);
    assert_eq!(c.get_job(id).unwrap().status, JobStatus::Refunded);
    assert_eq!(budget(&c).balance.0, before + NEAR);
    assert_eq!(budget(&c).open_jobs, 0);
    assert!(get_logs().iter().any(|l| l.contains("deadline_passed")));
    assert_conserved(&c, &[("owner", "agent")], 1, &[]);
}

#[test]
#[should_panic(expected = "deadline passed")]
fn no_submission_at_deadline() {
    let mut c = setup();
    let id = create(&mut c, NEAR);
    submit(&mut c, id, 'a', T0 + 3600);
}

#[test]
#[should_panic(expected = "no refund condition holds")]
fn no_refund_before_deadline() {
    let mut c = setup();
    let id = create(&mut c, NEAR);
    ctx("agent", 0, T0 + 3599);
    c.refund_job(id);
}

#[test]
fn refund_after_attempts_exhausted() {
    let mut c = setup();
    let id = create(&mut c, NEAR);
    for (i, out) in ['a', 'b', 'c'].into_iter().enumerate() {
        let t = T0 + 10 + i as u64 * 100;
        let s = submit(&mut c, id, out, t);
        verdict(&mut c, id, s, out, Verdict::Fail, t + 5);
    }
    ctx("agent", 0, T0 + 400); // well before the deadline
    c.refund_job(id);
    assert!(get_logs().iter().any(|l| l.contains("attempts_exhausted")));
    assert_conserved(&c, &[("owner", "agent")], 1, &["provider"]);
}

#[test]
#[should_panic(expected = "no attempts left")]
fn no_fourth_attempt() {
    let mut c = setup();
    let id = create(&mut c, NEAR);
    for out in ['a', 'b', 'c'] {
        let s = submit(&mut c, id, out, T0 + 10);
        verdict(&mut c, id, s, out, Verdict::Fail, T0 + 11);
    }
    submit(&mut c, id, 'd', T0 + 20);
}

#[test]
fn refund_when_evaluator_times_out() {
    let mut c = setup();
    let id = create(&mut c, NEAR);
    submit(&mut c, id, 'a', T0 + 10);
    ctx("owner", 0, T0 + 10 + 1800);
    c.refund_job(id);
    assert!(get_logs().iter().any(|l| l.contains("evaluator_timeout")));
    assert_eq!(c.get_job(id).unwrap().status, JobStatus::Refunded);
}

#[test]
#[should_panic(expected = "no refund condition holds")]
fn no_timeout_refund_inside_window_even_after_deadline() {
    let mut c = setup();
    let id = create(&mut c, NEAR);
    submit(&mut c, id, 'a', T0 + 3500);
    ctx("owner", 0, T0 + 3700); // past deadline, but the submission is still being evaluated
    c.refund_job(id);
}

#[test]
fn verdict_after_deadline_inside_window_counts() {
    let mut c = setup();
    let id = create(&mut c, NEAR);
    let s = submit(&mut c, id, 'a', T0 + 3500);
    verdict(&mut c, id, s, 'a', Verdict::Pass, T0 + 3700);
    assert_eq!(c.get_job(id).unwrap().status, JobStatus::Passed);
}

#[test]
#[should_panic(expected = "evaluation window closed")]
fn verdict_at_window_end_rejected() {
    let mut c = setup();
    let id = create(&mut c, NEAR);
    let s = submit(&mut c, id, 'a', T0 + 10);
    verdict(&mut c, id, s, 'a', Verdict::Pass, T0 + 10 + 1800);
}

#[test]
#[should_panic(expected = "no refund condition holds")]
fn passed_job_cannot_be_refunded() {
    let mut c = setup();
    let id = create(&mut c, NEAR);
    let s = submit(&mut c, id, 'a', T0 + 10);
    verdict(&mut c, id, s, 'a', Verdict::Pass, T0 + 20);
    ctx("owner", 0, T0 + 99_999);
    c.refund_job(id);
}

#[test]
fn every_open_state_has_a_timed_exit() {
    // Open, Open-after-FAIL and Submitted each become refundable once time passes.
    for scenario in 0..3 {
        let mut c = setup();
        let id = create(&mut c, NEAR);
        if scenario >= 1 {
            let s = submit(&mut c, id, 'a', T0 + 10);
            if scenario == 1 {
                verdict(&mut c, id, s, 'a', Verdict::Fail, T0 + 20);
            }
        }
        ctx("stranger", 0, T0 + 3600 + 1800);
        c.refund_job(id);
        assert_eq!(c.get_job(id).unwrap().status, JobStatus::Refunded, "scenario {scenario}");
        assert_conserved(&c, &[("owner", "agent")], 1, &["provider"]);
    }
}

#[test]
fn revoked_agents_open_jobs_still_settle_and_refund_to_budget() {
    let mut c = setup();
    let id = create(&mut c, NEAR);
    ctx("owner", ONE_YOCTO, T0);
    c.revoke_agent(acc("agent"));
    ctx("stranger", 0, T0 + 3600);
    c.refund_job(id);
    let bal = budget(&c).balance.0;
    ctx("owner", ONE_YOCTO, T0 + 3601);
    let _ = c.withdraw_budget(acc("agent"), U128(bal));
    assert_eq!(budget(&c).balance.0, 0);
    assert_conserved(&c, &[("owner", "agent")], 1, &[]);
}

// =================================================================== I10: canary reveal

#[test]
fn reveal_opens_commitment_after_final_state() {
    let mut c = setup();
    let id = create(&mut c, NEAR);
    let s = submit(&mut c, id, 'a', T0 + 10);
    verdict(&mut c, id, s, 'a', Verdict::Pass, T0 + 20);
    ctx("evaluator", 0, T0 + 30);
    c.reveal_canaries(id, Base64VecU8(REVEAL.to_vec()));
    assert!(c.get_job(id).unwrap().canaries_revealed);
}

#[test]
#[should_panic(expected = "reveal does not match commitment")]
fn wrong_reveal_rejected() {
    let mut c = setup();
    let id = create(&mut c, NEAR);
    ctx("stranger", 0, T0 + 3600);
    c.refund_job(id);
    ctx("evaluator", 0, T0 + 3601);
    c.reveal_canaries(id, Base64VecU8(b"{\"canaries\":[],\"salt\":\"01\"}".to_vec()));
}

#[test]
#[should_panic(expected = "job not final")]
fn no_reveal_while_job_can_still_be_worked() {
    let mut c = setup();
    let id = create(&mut c, NEAR);
    ctx("evaluator", 0, T0 + 10);
    c.reveal_canaries(id, Base64VecU8(REVEAL.to_vec()));
}

#[test]
#[should_panic(expected = "already revealed")]
fn reveal_only_once() {
    let mut c = setup();
    let id = create(&mut c, NEAR);
    ctx("stranger", 0, T0 + 3600);
    c.refund_job(id);
    ctx("evaluator", 0, T0 + 3601);
    c.reveal_canaries(id, Base64VecU8(REVEAL.to_vec()));
    ctx("evaluator", 0, T0 + 3602);
    c.reveal_canaries(id, Base64VecU8(REVEAL.to_vec()));
}

#[test]
#[should_panic(expected = "only the job's evaluator")]
fn only_evaluator_reveals() {
    let mut c = setup();
    let id = create(&mut c, NEAR);
    ctx("stranger", 0, T0 + 3600);
    c.refund_job(id);
    c.reveal_canaries(id, Base64VecU8(REVEAL.to_vec()));
}

// =================================================================== I8: conservation under a long random run

/// Deterministic pseudo-random sequence of VALID operations across two budgets and
/// two providers; conservation and per-record sums are checked after every step.
#[test]
fn conservation_holds_over_random_valid_operations() {
    let mut c = fresh();
    let mut p = policy();
    p.allowed_providers = vec![acc("provider"), acc("provider2")];
    p.max_open_jobs = 5;
    ctx("owner", 20 * NEAR, T0);
    c.fund_budget(acc("agent"), p.clone());
    ctx("owner2", 20 * NEAR, T0);
    c.fund_budget(acc("agent2"), p);

    let budgets = [("owner", "agent"), ("owner2", "agent2")];
    let claimants = ["provider", "provider2"];
    let mut seed: u64 = 0x5eed_1234;
    let mut rnd = |n: u64| {
        seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        (seed >> 33) % n
    };
    let mut t = T0;
    let mut out = 0u8;
    let mut done = [0u32; 7];
    for _step in 0..600 {
        t += 1 + rnd(120);
        let n = c.next_job_id;
        match rnd(7) {
            0 => {
                let (o, a) = budgets[rnd(2) as usize];
                let b = c.get_budget(acc(o), acc(a)).unwrap();
                let amount = (1 + rnd(20)) as u128 * NEAR / 10;
                if b.open_jobs < b.policy.max_open_jobs && b.balance.0 > amount + NEAR / 10 && t < b.policy.expires_at {
                    let mut args = job_args(amount);
                    args.owner_id = acc(o);
                    args.provider_id = acc(claimants[rnd(2) as usize]);
                    args.deadline = t + 600 + rnd(3000);
                    ctx(a, 0, t);
                    c.create_job(args);
                    done[0] += 1;
                }
            }
            1 | 2 if n > 0 => {
                let start = rnd(n);
                let j = (0..n).map(|k| c.get_job((start + k) % n).unwrap()).find(|j| j.status == JobStatus::Open).unwrap_or_else(|| c.get_job(start).unwrap());
                if j.status == JobStatus::Open && t < j.deadline && j.attempts_used < j.max_attempts {
                    out = out.wrapping_add(1);
                    let o = char::from(b'a' + out % 6);
                    ctx(j.provider_id.as_str().trim_end_matches(".testnet"), 0, t);
                    c.submit_work(j.job_id, h(o));
                    done[1] += 1;
                }
            }
            3 if n > 0 => {
                let start = rnd(n);
                let j = (0..n).map(|k| c.get_job((start + k) % n).unwrap()).find(|j| j.status == JobStatus::Submitted).unwrap_or_else(|| c.get_job(start).unwrap());
                if j.status == JobStatus::Submitted && t < j.submitted_at.unwrap() + j.eval_window_secs {
                    let v = if rnd(2) == 0 { Verdict::Pass } else { Verdict::Fail };
                    ctx("evaluator", 0, t);
                    c.submit_verdict(j.job_id, j.submission_seq, j.current_output_hash.clone().unwrap(), v, h('e'));
                    done[3] += 1;
                }
            }
            4 if n > 0 => {
                let j = c.get_job(rnd(n)).unwrap();
                let ok = match j.status {
                    JobStatus::Open => t >= j.deadline || j.attempts_used >= j.max_attempts,
                    JobStatus::Submitted => t >= j.submitted_at.unwrap() + j.eval_window_secs,
                    _ => false,
                };
                if ok {
                    ctx("stranger", 0, t);
                    c.refund_job(j.job_id);
                    done[4] += 1;
                }
            }
            5 => {
                let who = claimants[rnd(2) as usize];
                if c.get_claimable(acc(who)).0 > 0 {
                    ctx(who, 0, t);
                    let _ = c.withdraw();
                    done[5] += 1;
                }
            }
            6 => {
                let (o, a) = budgets[rnd(2) as usize];
                let bal = c.get_budget(acc(o), acc(a)).unwrap().balance.0;
                if bal > NEAR && rnd(4) == 0 {
                    ctx(o, ONE_YOCTO, t);
                    let _ = c.withdraw_budget(acc(a), U128(bal / 3));
                    done[6] += 1;
                }
            }
            _ => {}
        }
        assert_conserved(&c, &budgets, c.next_job_id, &claimants);
        for i in 0..c.next_job_id {
            let j = c.get_job(i).unwrap();
            let passes = j.verdicts.iter().filter(|v| v.verdict == Verdict::Pass).count();
            assert!(passes <= 1, "job {i} passed more than once");
            assert_eq!(passes == 1, j.status == JobStatus::Passed);
        }
    }
    assert!(c.next_job_id >= 10, "random run should create a meaningful number of jobs");
    let t = c.get_totals();
    assert!(done.iter().enumerate().all(|(i, n)| i == 2 || *n > 0), "every operation kind must occur: {done:?}");
    assert!(t.withdrawn.0 > 0 && t.claimable.0 + t.withdrawn.0 > 0);
    println!("random run: ops={done:?} jobs={} totals={t:?}", c.next_job_id);
}
