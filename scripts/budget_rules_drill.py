"""Live drill of the owner's budget rules on testnet (gas only; no job can be created).

The buyer agent's function-call key tries four create_job calls that each break exactly ONE rule of the
owner's policy for buyer-agent.lahd-kavren.testnet (not the MCP agent's budget):
  1. amount above max_per_job        -> "amount exceeds max_per_job"
  2. a provider not on the allowlist -> "provider not allowed by policy"
  3. a job type not on the allowlist -> "template not allowed by policy"
  4. after the budget has expired    -> "budget expired"
Every call carries deliberately malformed hashes, so even if a rule check were missing the contract would
still refuse the call (with a DIFFERENT message, which this script reports as a problem). No escrow moves.

Step 4 needs the owner to shorten the budget's expiry for about 2 minutes and then restore the original
policy; the script prints both NEAR CLI commands (run them in Windows PowerShell) and checks the result.

Run (from ~/kavren):
  BUYER_OWNER_ACCOUNT_ID=lahd-kavren.testnet .venv/bin/python scripts/budget_rules_drill.py
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time

TX = "https://testnet.nearblocks.io/txns/"
BAD = "not-a-hash"  # fails the contract's hash check, which runs only AFTER every policy check


def main() -> None:
    from kavren_agents.common import AgentChain, ChainCallError, load_env_file
    from kavren_api.chain import quiet_rpc_race_noise

    quiet_rpc_race_noise()
    buyer = load_env_file("~/.kavren/buyer-agent.env")
    contract = buyer["KAVREN_CONTRACT_ID"]
    owner = os.environ.get("BUYER_OWNER_ACCOUNT_ID") or sys.exit("set BUYER_OWNER_ACCOUNT_ID")
    agent = buyer["BUYER_AGENT_ACCOUNT_ID"]
    bc = AgentChain(os.environ.get("NEAR_RPC_URL", ""), contract, agent, buyer["BUYER_AGENT_FC_PRIVATE_KEY"])

    budget = bc.view("get_budget", {"owner_id": owner, "agent_id": agent}) or sys.exit("no budget for this agent")
    policy = budget["policy"]
    if budget["open_jobs"] or budget.get("revoked"):
        sys.exit("this budget has open jobs or is revoked; run the drill on an idle budget")
    print(f"Budget rules drill on {agent}. Policy: max {int(policy['max_per_job']) / 1e24:g} NEAR per job, "
          f"providers {policy['allowed_providers']}, job types {policy['allowed_templates']}, "
          f"expires {time.strftime('%Y-%m-%d %H:%M', time.localtime(policy['expires_at']))}.")
    if input("Type 'go' to start: ").strip() != "go":
        sys.exit("cancelled")

    def args(**over):
        a = {"owner_id": owner, "provider_id": policy["allowed_providers"][0], "amount": "240000000000000000000000",
             "template_id": policy["allowed_templates"][0], "spec_json": "{}", "draft_id": "drill",
             "input_hash": BAD, "work_hash": BAD, "canary_commitment": BAD,
             "deadline": int(time.time()) + 3600, "max_attempts": 1}
        a.update(over)
        return {"args": a}

    results = []

    def attempt(label, call_args, want):
        try:
            res = bc.call("create_job", call_args)
            print(f"  {label}: NOT refused (tx {TX}{res['tx']}) -> PROBLEM")
            results.append({"rule": label, "refused": False, "tx": res["tx"]})
            return
        except ChainCallError as e:
            ok = want in e.detail
            msg = e.detail.split("Smart contract panicked: ")[-1].split('"')[0] if "panicked" in e.detail else e.detail[:120]
            print(f"  {label}: refused on-chain: \"{msg}\" -> {'OK' if ok else 'UNEXPECTED (another check stopped it)'}"
                  + (f"\n    tx {TX}{e.tx}" if e.tx else ""))
            results.append({"rule": label, "refused": True, "expected": ok, "message": msg, "tx": e.tx})

    over = str(int(policy["max_per_job"]) + 1)
    attempt("1. amount above max_per_job", args(amount=over), "amount exceeds max_per_job")
    attempt("2. provider not on the allowlist", args(provider_id="not-allowed.lahd-kavren.testnet"),
            "provider not allowed by policy")
    attempt("3. job type not on the allowlist", args(template_id="leads-v1"), "template not allowed by policy")

    # ---- 4. expired budget: needs the owner (full-access key) twice
    expiry = int(time.time()) + 150
    short = {**policy, "expires_at": expiry}
    b64 = lambda p: base64.b64encode(json.dumps({"agent_id": agent, "policy": p}).encode()).decode()
    cli = ("near contract call-function as-transaction {c} update_policy base64-args '{a}' prepaid-gas '30.0 Tgas' "
           "attached-deposit '1 yoctoNEAR' sign-as {o} network-config testnet sign-with-keychain send")
    print("\nStep 4 needs you. In Windows PowerShell, run this to make the budget expire about 2 minutes from now:\n")
    print(cli.format(c=contract, a=b64(short), o=owner))
    input("\nPress Enter once that transaction succeeded... ")
    p = bc.view("get_budget", {"owner_id": owner, "agent_id": agent})["policy"]
    if p["expires_at"] != expiry:
        sys.exit("the shortened expiry is not on-chain yet; nothing else was changed")
    while time.time() < expiry + 20:
        left = int(expiry + 20 - time.time())
        print(f"\r  waiting for the budget to expire: {left // 60:02d}:{left % 60:02d} ", end="", flush=True)
        time.sleep(5)
    print()
    attempt("4. budget expired", args(), "budget expired")

    print("\nNow restore the original rules. In Windows PowerShell, run:\n")
    print(cli.format(c=contract, a=b64(policy), o=owner))
    input("\nPress Enter once that transaction succeeded... ")
    restored = bc.view("get_budget", {"owner_id": owner, "agent_id": agent})["policy"] == policy
    print(f"  original policy restored: {'yes' if restored else 'NO - run the restore command again'}")
    after = bc.view("get_budget", {"owner_id": owner, "agent_id": agent})
    ok = all(r.get("refused") and r.get("expected") for r in results) and restored
    out = {"agent": agent, "results": results, "policy_restored": restored,
           "balance_before": budget["balance"], "balance_after": after["balance"], "open_jobs_after": after["open_jobs"],
           "result": "ALL FOUR RULES ENFORCED ON-CHAIN" if ok else "PROBLEM"}
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
