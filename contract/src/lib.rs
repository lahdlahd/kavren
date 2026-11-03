//! KAVREN contract: budgets, job escrow, verdict-gated pull payments.
//!
//! Trust model (see README): the contract enforces budget policy, job terms,
//! caller permissions, verdict binding and the payment ledger. It does NOT check
//! that the evaluator ran the checks honestly; it only accepts verdicts from the
//! evaluator snapshotted on the job, bound to the exact current submission.
//!
//! Time: all public times are UNIX seconds. Boundaries partition cleanly:
//!   submit allowed      iff now <  deadline
//!   deadline refund     iff now >= deadline
//!   verdict allowed     iff now <  submitted_at + eval_window_secs
//!   evaluator-timeout   iff now >= submitted_at + eval_window_secs

use near_sdk::json_types::{Base64VecU8, U128};
use near_sdk::store::LookupMap;
use near_sdk::{
    assert_one_yocto, env, near, require, AccountId, BorshStorageKey, NearToken, PanicOnDefault,
    Promise,
};

pub const MAX_SPEC_BYTES: usize = 2048;
pub const MAX_REVEAL_BYTES: usize = 16 * 1024;
pub const MAX_LIST_ITEMS: usize = 16;
pub const MAX_ID_BYTES: usize = 64;
pub const MAX_ATTEMPTS_CAP: u8 = 5;

// ------------------------------------------------------------------ types

#[near(serializers = [json, borsh])]
#[derive(Clone, Debug, PartialEq)]
pub struct Config {
    pub eval_window_secs: u64,
    pub min_deadline_secs: u64,
    pub max_deadline_secs: u64,
    pub max_attempts_cap: u8,
}

impl Config {
    pub fn default_mvp() -> Self {
        Self { eval_window_secs: 1800, min_deadline_secs: 600, max_deadline_secs: 7 * 24 * 3600, max_attempts_cap: 5 }
    }
    fn validate(&self) {
        require!(self.eval_window_secs >= 60 && self.eval_window_secs <= 24 * 3600, "eval_window_secs out of range");
        require!(self.min_deadline_secs >= 60, "min_deadline_secs too small");
        require!(self.max_deadline_secs >= self.min_deadline_secs, "max_deadline_secs < min_deadline_secs");
        require!(self.max_deadline_secs <= 30 * 24 * 3600, "max_deadline_secs too large");
        require!(self.max_attempts_cap >= 1 && self.max_attempts_cap <= MAX_ATTEMPTS_CAP, "max_attempts_cap out of range");
    }
}

#[near(serializers = [json, borsh])]
#[derive(Clone, Debug, PartialEq)]
pub struct Policy {
    pub max_per_job: U128,
    pub max_open_jobs: u32,
    pub allowed_templates: Vec<String>,
    pub allowed_providers: Vec<AccountId>,
    pub max_deadline_secs: u64,
    pub max_attempts: u8,
    pub expires_at: u64,
}

#[near(serializers = [json, borsh])]
#[derive(Clone, Debug, PartialEq)]
pub struct Budget {
    pub owner_id: AccountId,
    pub agent_id: AccountId,
    /// Unlocked funds the agent may commit to jobs and the owner may withdraw.
    pub balance: U128,
    pub open_jobs: u32,
    pub revoked: bool,
    pub policy: Policy,
}

#[near(serializers = [json, borsh])]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum JobStatus {
    Open,
    Submitted,
    Passed,
    Refunded,
}

#[near(serializers = [json, borsh])]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Verdict {
    #[serde(rename = "PASS")]
    Pass,
    #[serde(rename = "FAIL")]
    Fail,
}

#[near(serializers = [json, borsh])]
#[derive(Clone, Debug, PartialEq)]
pub struct VerdictRecord {
    pub seq: u32,
    pub verdict: Verdict,
    pub output_hash: String,
    pub report_hash: String,
    pub at: u64,
}

#[near(serializers = [json, borsh])]
#[derive(Clone, Debug, PartialEq)]
pub struct Job {
    pub job_id: u64,
    pub owner_id: AccountId,
    pub buyer_agent: AccountId,
    pub provider_id: AccountId,
    pub evaluator_id: AccountId,
    pub amount: U128,
    pub template_id: String,
    pub spec_json: String,
    pub draft_id: String,
    pub input_hash: String,
    pub work_hash: String,
    pub canary_commitment: String,
    pub created_at: u64,
    pub deadline: u64,
    pub max_attempts: u8,
    pub attempts_used: u8,
    pub eval_window_secs: u64,
    pub submission_seq: u32,
    pub current_output_hash: Option<String>,
    pub submitted_at: Option<u64>,
    pub status: JobStatus,
    pub verdicts: Vec<VerdictRecord>,
    pub canaries_revealed: bool,
    pub storage_cost: U128,
}

#[near(serializers = [json])]
pub struct CreateJobArgs {
    pub owner_id: AccountId,
    pub provider_id: AccountId,
    pub amount: U128,
    pub template_id: String,
    pub spec_json: String,
    pub draft_id: String,
    pub input_hash: String,
    pub work_hash: String,
    pub canary_commitment: String,
    pub deadline: u64,
    pub max_attempts: u8,
}

/// Running totals used for the conservation invariant.
#[near(serializers = [json, borsh])]
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Totals {
    pub deposited: U128,
    pub withdrawn: U128,
    pub budgets: U128,
    pub escrow: U128,
    pub claimable: U128,
    /// Kept by the contract: storage costs and 1-yocto confirmations.
    pub retained: U128,
}

#[near(event_json(standard = "kavren"))]
pub enum Event {
    #[event_version("1.0.0")]
    BudgetFunded { owner_id: AccountId, agent_id: AccountId, amount: U128, storage_cost: U128 },
    #[event_version("1.0.0")]
    PolicyUpdated { owner_id: AccountId, agent_id: AccountId },
    #[event_version("1.0.0")]
    AgentRevoked { owner_id: AccountId, agent_id: AccountId },
    #[event_version("1.0.0")]
    BudgetWithdrawn { owner_id: AccountId, agent_id: AccountId, amount: U128 },
    #[event_version("1.0.0")]
    JobCreated { job_id: u64, owner_id: AccountId, buyer_agent: AccountId, provider_id: AccountId, evaluator_id: AccountId, amount: U128, template_id: String, deadline: u64 },
    #[event_version("1.0.0")]
    WorkSubmitted { job_id: u64, seq: u32, output_hash: String },
    #[event_version("1.0.0")]
    VerdictSubmitted { job_id: u64, seq: u32, verdict: Verdict, output_hash: String, report_hash: String },
    #[event_version("1.0.0")]
    JobRefunded { job_id: u64, reason: String, amount: U128 },
    #[event_version("1.0.0")]
    Withdrawn { account_id: AccountId, amount: U128 },
    #[event_version("1.0.0")]
    CanariesRevealed { job_id: u64, canary_commitment: String },
    #[event_version("1.0.0")]
    EvaluatorChanged { evaluator_id: AccountId },
    #[event_version("1.0.0")]
    ConfigChanged { config: Config },
}

#[derive(BorshStorageKey)]
#[near]
enum Prefix {
    Budgets,
    Jobs,
    Claimable,
    ProviderJobs,
}

// ------------------------------------------------------------------ contract

#[near(contract_state)]
#[derive(PanicOnDefault)]
pub struct Kavren {
    admin: AccountId,
    evaluator: AccountId,
    config: Config,
    budgets: LookupMap<(AccountId, AccountId), Budget>,
    jobs: LookupMap<u64, Job>,
    claimable: LookupMap<AccountId, u128>,
    provider_jobs: LookupMap<AccountId, Vec<u64>>,
    next_job_id: u64,
    totals: Totals,
}

fn now() -> u64 {
    env::block_timestamp() / 1_000_000_000
}

fn is_hash(s: &str) -> bool {
    s.len() == 64 && s.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn to_hex(bytes: &[u8]) -> String {
    const H: &[u8; 16] = b"0123456789abcdef";
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push(H[(b >> 4) as usize] as char);
        s.push(H[(b & 0xf) as usize] as char);
    }
    s
}

fn add(a: U128, b: u128) -> U128 {
    U128(a.0.checked_add(b).unwrap_or_else(|| env::panic_str("overflow")))
}
fn sub(a: U128, b: u128) -> U128 {
    U128(a.0.checked_sub(b).unwrap_or_else(|| env::panic_str("underflow")))
}

fn validate_template_id(t: &str) {
    require!(!t.is_empty() && t.len() <= MAX_ID_BYTES, "bad template id");
    require!(t.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-'), "bad template id");
}

#[near]
impl Kavren {
    #[init]
    pub fn new(admin: AccountId, evaluator: AccountId, config: Option<Config>) -> Self {
        let config = config.unwrap_or_else(Config::default_mvp);
        config.validate();
        Self {
            admin,
            evaluator,
            config,
            budgets: LookupMap::new(Prefix::Budgets),
            jobs: LookupMap::new(Prefix::Jobs),
            claimable: LookupMap::new(Prefix::Claimable),
            provider_jobs: LookupMap::new(Prefix::ProviderJobs),
            next_job_id: 0,
            totals: Totals::default(),
        }
    }

    // -------------------------------------------------------------- admin (new jobs only)

    #[payable]
    pub fn set_evaluator(&mut self, evaluator_id: AccountId) {
        assert_one_yocto();
        require!(env::predecessor_account_id() == self.admin, "only admin");
        self.count_one_yocto();
        self.evaluator = evaluator_id.clone();
        Event::EvaluatorChanged { evaluator_id }.emit();
    }

    #[payable]
    pub fn set_config(&mut self, config: Config) {
        assert_one_yocto();
        require!(env::predecessor_account_id() == self.admin, "only admin");
        config.validate();
        self.count_one_yocto();
        self.config = config.clone();
        Event::ConfigChanged { config }.emit();
    }

    // -------------------------------------------------------------- owner

    /// Create or top up the (owner, agent) budget and set its policy.
    /// The storage cost of a new budget record is taken from the deposit.
    #[payable]
    pub fn fund_budget(&mut self, agent_id: AccountId, policy: Policy) {
        let owner = env::predecessor_account_id();
        let deposit = env::attached_deposit().as_yoctonear();
        require!(deposit > 0, "attach a deposit");
        require!(agent_id != owner, "agent must differ from owner");
        self.validate_policy(&policy);

        let before = env::storage_usage();
        let key = (owner.clone(), agent_id.clone());
        let mut budget = self.budgets.get(&key).cloned().unwrap_or(Budget {
            owner_id: owner.clone(),
            agent_id: agent_id.clone(),
            balance: U128(0),
            open_jobs: 0,
            revoked: false,
            policy: policy.clone(),
        });
        budget.policy = policy;
        budget.revoked = false;
        self.budgets.insert(key.clone(), budget);
        self.budgets.flush();
        let storage_cost = self.storage_cost_since(before);
        require!(deposit > storage_cost, "deposit does not cover storage");
        let credited = deposit - storage_cost;

        let b = self.budgets.get_mut(&key).unwrap();
        b.balance = add(b.balance, credited);
        self.totals.deposited = add(self.totals.deposited, deposit);
        self.totals.retained = add(self.totals.retained, storage_cost);
        self.totals.budgets = add(self.totals.budgets, credited);
        Event::BudgetFunded { owner_id: owner, agent_id, amount: U128(credited), storage_cost: U128(storage_cost) }.emit();
    }

    #[payable]
    pub fn update_policy(&mut self, agent_id: AccountId, policy: Policy) {
        assert_one_yocto();
        self.validate_policy(&policy);
        let owner = env::predecessor_account_id();
        let b = self.budget_mut(&owner, &agent_id);
        b.policy = policy;
        self.count_one_yocto();
        Event::PolicyUpdated { owner_id: owner, agent_id }.emit();
    }

    #[payable]
    pub fn revoke_agent(&mut self, agent_id: AccountId) {
        assert_one_yocto();
        let owner = env::predecessor_account_id();
        let b = self.budget_mut(&owner, &agent_id);
        b.revoked = true;
        self.count_one_yocto();
        Event::AgentRevoked { owner_id: owner, agent_id }.emit();
    }

    /// Withdraw UNLOCKED budget funds to the owner. Funds locked in jobs are not touchable.
    #[payable]
    pub fn withdraw_budget(&mut self, agent_id: AccountId, amount: U128) -> Promise {
        assert_one_yocto();
        let owner = env::predecessor_account_id();
        require!(amount.0 > 0, "amount must be > 0");
        let b = self.budget_mut(&owner, &agent_id);
        require!(b.balance.0 >= amount.0, "insufficient unlocked budget");
        b.balance = sub(b.balance, amount.0);
        self.totals.budgets = sub(self.totals.budgets, amount.0);
        self.totals.withdrawn = add(self.totals.withdrawn, amount.0);
        self.count_one_yocto();
        Event::BudgetWithdrawn { owner_id: owner.clone(), agent_id, amount }.emit();
        Promise::new(owner).transfer(NearToken::from_yoctonear(amount.0))
    }

    // -------------------------------------------------------------- buyer agent

    /// Called by the buyer agent (function-call key, no deposit). Locks `amount`
    /// from the owner's budget; job storage is charged to the budget too.
    pub fn create_job(&mut self, args: CreateJobArgs) -> u64 {
        let agent = env::predecessor_account_id();
        let t = now();
        let key = (args.owner_id.clone(), agent.clone());
        let budget = self.budgets.get(&key).cloned().unwrap_or_else(|| env::panic_str("no budget for this owner and agent"));
        let p = &budget.policy;

        require!(!budget.revoked, "agent revoked");
        require!(t < p.expires_at, "budget expired");
        require!(budget.open_jobs < p.max_open_jobs, "max_open_jobs reached");
        require!(p.allowed_templates.contains(&args.template_id), "template not allowed by policy");
        require!(p.allowed_providers.contains(&args.provider_id), "provider not allowed by policy");
        require!(args.provider_id != agent && args.provider_id != args.owner_id, "provider must be a different account");
        require!(args.provider_id != self.evaluator, "provider cannot be the evaluator");
        require!(args.amount.0 > 0, "amount must be > 0");
        require!(args.amount.0 <= p.max_per_job.0, "amount exceeds max_per_job");
        let max_dl = p.max_deadline_secs.min(self.config.max_deadline_secs);
        require!(args.deadline >= t + self.config.min_deadline_secs, "deadline too soon");
        require!(args.deadline <= t + max_dl, "deadline too far");
        let max_att = p.max_attempts.min(self.config.max_attempts_cap);
        require!(args.max_attempts >= 1 && args.max_attempts <= max_att, "max_attempts out of range");
        require!(is_hash(&args.input_hash) && is_hash(&args.work_hash) && is_hash(&args.canary_commitment), "hashes must be 64 lowercase hex");
        require!(!args.draft_id.is_empty() && args.draft_id.len() <= MAX_ID_BYTES, "bad draft_id");
        validate_template_id(&args.template_id);
        self.validate_spec(&args.spec_json, &args.template_id);

        let job_id = self.next_job_id;
        self.next_job_id += 1;
        let before = env::storage_usage();
        let job = Job {
            job_id,
            owner_id: args.owner_id.clone(),
            buyer_agent: agent.clone(),
            provider_id: args.provider_id.clone(),
            evaluator_id: self.evaluator.clone(),
            amount: args.amount,
            template_id: args.template_id.clone(),
            spec_json: args.spec_json,
            draft_id: args.draft_id,
            input_hash: args.input_hash,
            work_hash: args.work_hash,
            canary_commitment: args.canary_commitment,
            created_at: t,
            deadline: args.deadline,
            max_attempts: args.max_attempts,
            attempts_used: 0,
            eval_window_secs: self.config.eval_window_secs,
            submission_seq: 0,
            current_output_hash: None,
            submitted_at: None,
            status: JobStatus::Open,
            verdicts: vec![],
            canaries_revealed: false,
            storage_cost: U128(0),
        };
        self.jobs.insert(job_id, job);
        let mut list = self.provider_jobs.get(&args.provider_id).cloned().unwrap_or_default();
        list.push(job_id);
        self.provider_jobs.insert(args.provider_id.clone(), list);
        self.jobs.flush();
        self.provider_jobs.flush();
        // room for verdict records and the current hash written later
        let storage_cost = self.storage_cost_since(before) + 512 * env::storage_byte_cost().as_yoctonear();

        let total = args.amount.0.checked_add(storage_cost).unwrap();
        require!(budget.balance.0 >= total, "insufficient budget for amount + storage");
        let b = self.budgets.get_mut(&key).unwrap();
        b.balance = sub(b.balance, total);
        b.open_jobs += 1;
        self.jobs.get_mut(&job_id).unwrap().storage_cost = U128(storage_cost);
        self.totals.budgets = sub(self.totals.budgets, total);
        self.totals.escrow = add(self.totals.escrow, args.amount.0);
        self.totals.retained = add(self.totals.retained, storage_cost);

        Event::JobCreated {
            job_id,
            owner_id: args.owner_id,
            buyer_agent: agent,
            provider_id: args.provider_id,
            evaluator_id: self.evaluator.clone(),
            amount: args.amount,
            template_id: args.template_id,
            deadline: args.deadline,
        }
        .emit();
        job_id
    }

    /// Permissionless: anyone may trigger a refund once a refund condition holds.
    /// Funds always return to the owner's budget, never to the caller.
    pub fn refund_job(&mut self, job_id: u64) {
        let t = now();
        let job = self.jobs.get(&job_id).cloned().unwrap_or_else(|| env::panic_str("no such job"));
        let reason = match job.status {
            JobStatus::Open if t >= job.deadline => "deadline_passed",
            JobStatus::Open if job.attempts_used >= job.max_attempts => "attempts_exhausted",
            JobStatus::Submitted if t >= job.submitted_at.unwrap() + job.eval_window_secs => "evaluator_timeout",
            _ => env::panic_str("no refund condition holds"),
        };
        let key = (job.owner_id.clone(), job.buyer_agent.clone());
        let b = self.budgets.get_mut(&key).unwrap();
        b.balance = add(b.balance, job.amount.0);
        b.open_jobs -= 1;
        self.totals.escrow = sub(self.totals.escrow, job.amount.0);
        self.totals.budgets = add(self.totals.budgets, job.amount.0);
        self.jobs.get_mut(&job_id).unwrap().status = JobStatus::Refunded;
        Event::JobRefunded { job_id, reason: reason.to_string(), amount: job.amount }.emit();
    }

    // -------------------------------------------------------------- provider

    pub fn submit_work(&mut self, job_id: u64, output_hash: String) -> u32 {
        let t = now();
        require!(is_hash(&output_hash), "output_hash must be 64 lowercase hex");
        let job = self.jobs.get_mut(&job_id).unwrap_or_else(|| env::panic_str("no such job"));
        require!(env::predecessor_account_id() == job.provider_id, "only the job's provider");
        require!(job.status == JobStatus::Open, "job not open");
        require!(t < job.deadline, "deadline passed");
        require!(job.attempts_used < job.max_attempts, "no attempts left");
        job.submission_seq += 1;
        job.current_output_hash = Some(output_hash.clone());
        job.submitted_at = Some(t);
        job.status = JobStatus::Submitted;
        let seq = job.submission_seq;
        Event::WorkSubmitted { job_id, seq, output_hash }.emit();
        seq
    }

    /// Pull payment: pays the caller's whole claimable balance to the caller.
    pub fn withdraw(&mut self) -> Promise {
        let who = env::predecessor_account_id();
        let amount = self.claimable.get(&who).copied().unwrap_or(0);
        require!(amount > 0, "nothing to withdraw");
        self.claimable.remove(&who);
        self.totals.claimable = sub(self.totals.claimable, amount);
        self.totals.withdrawn = add(self.totals.withdrawn, amount);
        Event::Withdrawn { account_id: who.clone(), amount: U128(amount) }.emit();
        Promise::new(who).transfer(NearToken::from_yoctonear(amount))
    }

    // -------------------------------------------------------------- evaluator

    pub fn submit_verdict(&mut self, job_id: u64, seq: u32, output_hash: String, verdict: Verdict, report_hash: String) {
        let t = now();
        require!(is_hash(&report_hash), "report_hash must be 64 lowercase hex");
        let job = self.jobs.get(&job_id).cloned().unwrap_or_else(|| env::panic_str("no such job"));
        require!(env::predecessor_account_id() == job.evaluator_id, "only the job's evaluator");
        require!(job.status == JobStatus::Submitted, "no submission awaiting a verdict");
        require!(seq == job.submission_seq, "stale or unknown submission seq");
        require!(Some(&output_hash) == job.current_output_hash.as_ref(), "output_hash does not match current submission");
        require!(t < job.submitted_at.unwrap() + job.eval_window_secs, "evaluation window closed");

        let record = VerdictRecord { seq, verdict, output_hash: output_hash.clone(), report_hash: report_hash.clone(), at: t };
        match verdict {
            Verdict::Pass => {
                let prev = self.claimable.get(&job.provider_id).copied().unwrap_or(0);
                self.claimable.insert(job.provider_id.clone(), prev + job.amount.0);
                self.totals.escrow = sub(self.totals.escrow, job.amount.0);
                self.totals.claimable = add(self.totals.claimable, job.amount.0);
                let key = (job.owner_id.clone(), job.buyer_agent.clone());
                self.budgets.get_mut(&key).unwrap().open_jobs -= 1;
                let j = self.jobs.get_mut(&job_id).unwrap();
                j.status = JobStatus::Passed;
                j.verdicts.push(record);
            }
            Verdict::Fail => {
                let j = self.jobs.get_mut(&job_id).unwrap();
                j.attempts_used += 1;
                j.status = JobStatus::Open;
                j.verdicts.push(record);
            }
        }
        Event::VerdictSubmitted { job_id, seq, verdict, output_hash, report_hash }.emit();
    }

    /// Open the canary commitment after the job is final. The blob is checked
    /// against the commitment and NOT stored (it remains public in the tx args).
    pub fn reveal_canaries(&mut self, job_id: u64, blob: Base64VecU8) {
        let job = self.jobs.get_mut(&job_id).unwrap_or_else(|| env::panic_str("no such job"));
        require!(env::predecessor_account_id() == job.evaluator_id, "only the job's evaluator");
        require!(matches!(job.status, JobStatus::Passed | JobStatus::Refunded), "job not final");
        require!(!job.canaries_revealed, "already revealed");
        require!(blob.0.len() <= MAX_REVEAL_BYTES, "reveal too large");
        require!(to_hex(&env::sha256(&blob.0)) == job.canary_commitment, "reveal does not match commitment");
        job.canaries_revealed = true;
        Event::CanariesRevealed { job_id, canary_commitment: job.canary_commitment.clone() }.emit();
    }

    // -------------------------------------------------------------- views

    pub fn get_config(&self) -> Config {
        self.config.clone()
    }
    pub fn get_evaluator(&self) -> AccountId {
        self.evaluator.clone()
    }
    pub fn get_admin(&self) -> AccountId {
        self.admin.clone()
    }
    pub fn get_job(&self, job_id: u64) -> Option<Job> {
        self.jobs.get(&job_id).cloned()
    }
    pub fn get_budget(&self, owner_id: AccountId, agent_id: AccountId) -> Option<Budget> {
        self.budgets.get(&(owner_id, agent_id)).cloned()
    }
    pub fn get_claimable(&self, account_id: AccountId) -> U128 {
        U128(self.claimable.get(&account_id).copied().unwrap_or(0))
    }
    pub fn get_jobs_for_provider(&self, provider_id: AccountId, from_index: Option<u32>, limit: Option<u32>) -> Vec<Job> {
        let ids = self.provider_jobs.get(&provider_id).cloned().unwrap_or_default();
        ids.iter()
            .skip(from_index.unwrap_or(0) as usize)
            .take(limit.unwrap_or(50).min(100) as usize)
            .filter_map(|id| self.jobs.get(id).cloned())
            .collect()
    }
    pub fn get_totals(&self) -> Totals {
        self.totals.clone()
    }

    // -------------------------------------------------------------- internal

    fn validate_policy(&self, p: &Policy) {
        require!(p.max_per_job.0 > 0, "max_per_job must be > 0");
        require!(p.max_open_jobs >= 1 && p.max_open_jobs <= 100, "max_open_jobs out of range");
        require!(!p.allowed_templates.is_empty() && p.allowed_templates.len() <= MAX_LIST_ITEMS, "allowed_templates must list 1-16 templates");
        require!(!p.allowed_providers.is_empty() && p.allowed_providers.len() <= MAX_LIST_ITEMS, "allowed_providers must list 1-16 providers");
        for t in &p.allowed_templates {
            validate_template_id(t);
        }
        require!(p.max_deadline_secs >= self.config.min_deadline_secs, "policy max_deadline_secs below protocol minimum");
        require!(p.max_attempts >= 1 && p.max_attempts <= self.config.max_attempts_cap, "policy max_attempts out of range");
        require!(p.expires_at > now(), "policy already expired");
    }

    /// The contract enforces only what it relies on: size, JSON object, and that the
    /// spec's template_id matches the job's template. Full schema validation is done
    /// off-chain by kavren_core and re-checked by the provider agent.
    fn validate_spec(&self, spec_json: &str, template_id: &str) {
        require!(!spec_json.is_empty() && spec_json.len() <= MAX_SPEC_BYTES, "spec_json size out of range");
        let v: near_sdk::serde_json::Value =
            near_sdk::serde_json::from_str(spec_json).unwrap_or_else(|_| env::panic_str("spec_json is not valid JSON"));
        require!(v.get("template_id").and_then(|x| x.as_str()) == Some(template_id), "spec template_id does not match");
        require!(v.get("evaluator_version").and_then(|x| x.as_str()).is_some(), "spec missing evaluator_version");
    }

    fn budget_mut(&mut self, owner: &AccountId, agent: &AccountId) -> &mut Budget {
        self.budgets
            .get_mut(&(owner.clone(), agent.clone()))
            .unwrap_or_else(|| env::panic_str("no budget for this owner and agent"))
    }

    fn count_one_yocto(&mut self) {
        self.totals.deposited = add(self.totals.deposited, 1);
        self.totals.retained = add(self.totals.retained, 1);
    }

    fn storage_cost_since(&self, before: u64) -> u128 {
        let after = env::storage_usage();
        (after.saturating_sub(before) as u128) * env::storage_byte_cost().as_yoctonear()
    }
}

#[cfg(test)]
mod tests;
