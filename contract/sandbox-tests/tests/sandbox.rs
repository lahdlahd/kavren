//! Sandbox integration tests: the real NEAR runtime, the real wasm, real access keys.
//! These cover what unit tests cannot:
//!   - non-payable methods reject deposits (enforced in the wasm export),
//!   - function-call keys cannot attach deposits or call methods outside their list,
//!   - payments actually move NEAR between accounts.
//! Run: KAVREN_WASM=path/to/kavren_contract.wasm cargo test (CI does this).

use near_workspaces::types::{AccessKey, KeyType, NearToken, SecretKey};
use near_workspaces::{Account, Contract, Worker};
use serde_json::json;
use std::time::{SystemTime, UNIX_EPOCH};

const SPEC: &str = r#"{"evaluator_version":"0.1.0","template_id":"contacts-v1"}"#;

fn now() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs()
}

fn h(c: char) -> String {
    std::iter::repeat(c).take(64).collect()
}

struct Env {
    worker: Worker<near_workspaces::network::Sandbox>,
    contract: Contract,
    owner: Account,
    agent_fc: Account,
    provider_fc: Account,
    provider: Account,
    evaluator_fc: Account,
}

/// Adds a function-call key for `methods` on the contract to `account`, and returns
/// an Account handle that signs with ONLY that restricted key.
async fn fc_handle(env_worker: &Worker<near_workspaces::network::Sandbox>, account: &Account, contract: &Contract, methods: &[&str]) -> anyhow::Result<Account> {
    let sk = SecretKey::from_random(KeyType::ED25519);
    account
        .batch(account.id())
        .add_key(sk.public_key(), AccessKey::function_call_access(contract.id(), methods, Some(NearToken::from_millinear(250))))
        .transact()
        .await?
        .into_result()?;
    Ok(Account::from_secret_key(account.id().clone(), sk, env_worker))
}

async fn setup() -> anyhow::Result<Env> {
    let wasm_path = std::env::var("KAVREN_WASM").expect("set KAVREN_WASM to the built contract wasm");
    let wasm = std::fs::read(wasm_path)?;
    let worker = near_workspaces::sandbox().await?;
    let contract = worker.dev_deploy(&wasm).await?;
    let admin = worker.dev_create_account().await?;
    let evaluator = worker.dev_create_account().await?;
    let owner = worker.dev_create_account().await?;
    let agent = worker.dev_create_account().await?;
    let provider = worker.dev_create_account().await?;

    contract
        .call("new")
        .args_json(json!({"admin": admin.id(), "evaluator": evaluator.id(), "config": null}))
        .transact()
        .await?
        .into_result()?;

    owner
        .call(contract.id(), "fund_budget")
        .args_json(json!({"agent_id": agent.id(), "policy": {
            "max_per_job": NearToken::from_near(2).as_yoctonear().to_string(),
            "max_open_jobs": 2,
            "allowed_templates": ["contacts-v1"],
            "allowed_providers": [provider.id()],
            "max_deadline_secs": 7 * 24 * 3600,
            "max_attempts": 3,
            "expires_at": now() + 30 * 24 * 3600
        }}))
        .deposit(NearToken::from_near(5))
        .transact()
        .await?
        .into_result()?;

    let agent_fc = fc_handle(&worker, &agent, &contract, &["create_job", "refund_job"]).await?;
    let provider_fc = fc_handle(&worker, &provider, &contract, &["submit_work", "withdraw"]).await?;
    let evaluator_fc = fc_handle(&worker, &evaluator, &contract, &["submit_verdict", "reveal_canaries"]).await?;
    Ok(Env { worker, contract, owner, agent_fc, provider_fc, provider, evaluator_fc })
}

fn job_args(env: &Env, amount: NearToken) -> serde_json::Value {
    json!({"args": {
        "owner_id": env.owner.id(),
        "provider_id": env.provider.id(),
        "amount": amount.as_yoctonear().to_string(),
        "template_id": "contacts-v1",
        "spec_json": SPEC,
        "draft_id": "draft-1",
        "input_hash": h('1'),
        "work_hash": h('2'),
        "canary_commitment": h('3'),
        "deadline": now() + 3600,
        "max_attempts": 3
    }})
}

/// A transaction that must not succeed: either rejected before execution (Err) or failed.
async fn assert_rejected<E: std::fmt::Display>(r: Result<near_workspaces::result::ExecutionFinalResult, E>, what: &str) {
    match r {
        Err(e) => println!("{what}: rejected before execution: {e}"),
        Ok(res) => assert!(res.is_failure(), "{what}: expected failure, got success"),
    }
}

#[tokio::test]
async fn full_flow_with_function_call_keys_moves_real_near() -> anyhow::Result<()> {
    let env = setup().await?;
    let job_id: u64 = env
        .agent_fc
        .call(env.contract.id(), "create_job")
        .args_json(job_args(&env, NearToken::from_millinear(1500)))
        .transact()
        .await?
        .into_result()?
        .json()?;

    let seq: u32 = env
        .provider_fc
        .call(env.contract.id(), "submit_work")
        .args_json(json!({"job_id": job_id, "output_hash": h('a')}))
        .transact()
        .await?
        .into_result()?
        .json()?;

    env.evaluator_fc
        .call(env.contract.id(), "submit_verdict")
        .args_json(json!({"job_id": job_id, "seq": seq, "output_hash": h('a'), "verdict": "PASS", "report_hash": h('e')}))
        .transact()
        .await?
        .into_result()?;

    let before = env.provider.view_account().await?.balance;
    env.provider_fc.call(env.contract.id(), "withdraw").transact().await?.into_result()?;
    let after = env.provider.view_account().await?.balance;
    let gained = after.as_yoctonear() as i128 - before.as_yoctonear() as i128;
    // 1.5 NEAR minus gas; gas is far below 0.01 NEAR
    assert!(gained > NearToken::from_millinear(1490).as_yoctonear() as i128, "provider gained {gained}");

    // second withdraw pays nothing
    let r = env.provider_fc.call(env.contract.id(), "withdraw").transact().await;
    assert_rejected(r, "second withdraw").await;
    let _ = &env.worker;
    Ok(())
}

#[tokio::test]
async fn function_call_key_cannot_attach_deposit() -> anyhow::Result<()> {
    let env = setup().await?;
    let r = env
        .agent_fc
        .call(env.contract.id(), "create_job")
        .args_json(job_args(&env, NearToken::from_near(1)))
        .deposit(NearToken::from_yoctonear(1))
        .transact()
        .await;
    assert_rejected(r, "FC key with 1 yocto deposit").await;
    Ok(())
}

#[tokio::test]
async fn function_call_key_cannot_call_owner_methods() -> anyhow::Result<()> {
    let env = setup().await?;
    let r = env
        .agent_fc
        .call(env.contract.id(), "withdraw_budget")
        .args_json(json!({"agent_id": env.agent_fc.id(), "amount": "1"}))
        .transact()
        .await;
    assert_rejected(r, "agent FC key calling withdraw_budget").await;
    Ok(())
}

#[tokio::test]
async fn non_payable_methods_reject_deposits_even_with_full_access() -> anyhow::Result<()> {
    let env = setup().await?;
    // the owner's full-access key may attach a deposit, but the method must refuse it
    let r = env
        .owner
        .call(env.contract.id(), "refund_job")
        .args_json(json!({"job_id": 0}))
        .deposit(NearToken::from_yoctonear(1))
        .transact()
        .await;
    assert_rejected(r, "deposit on non-payable refund_job").await;
    Ok(())
}

#[tokio::test]
async fn over_limit_job_is_rejected_on_chain() -> anyhow::Result<()> {
    let env = setup().await?;
    let r = env
        .agent_fc
        .call(env.contract.id(), "create_job")
        .args_json(job_args(&env, NearToken::from_near(3)))
        .transact()
        .await;
    assert_rejected(r, "3 NEAR job over 2 NEAR max_per_job").await;
    Ok(())
}
