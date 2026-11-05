"""Live drill of the DEADLINE refund on testnet (about 20 minutes, 0.24 NEAR locked then returned).

An isolated job is opened from the buyer-agent's budget (not the MCP agent's), with a 20-minute deadline,
and the provider never works on it. The drill shows, on-chain:
  1. refunding BEFORE the deadline is refused by the contract ("no refund condition holds");
  2. after the deadline, the provider can no longer submit ("deadline passed");
  3. after the deadline, refund_job succeeds and the escrow returns to the owner's budget.

The provider agent MUST be stopped for the whole drill (otherwise it would do the job).
Run (from ~/kavren, provider stopped):
  BUYER_OWNER_ACCOUNT_ID=lahd-kavren.testnet .venv/bin/python scripts/deadline_refund_drill.py [MINUTES]
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from kavren_core.canonical import sha256_hex

FX = Path(__file__).resolve().parents[1] / "fixtures" / "contacts"
TX = "https://testnet.nearblocks.io/txns/"


def near(y) -> str:
    return f"{int(y) / 1e24:.4f}"


def expect_refused(fn, label: str, want: str) -> dict:
    from kavren_agents.common import ChainCallError
    try:
        res = fn()
    except ChainCallError as e:
        ok = want in e.detail
        print(f"  {label}: refused by the contract ({e.detail[:120]}) -> {'OK' if ok else 'UNEXPECTED MESSAGE'}"
              + (f"\n    tx {TX}{e.tx}" if e.tx else ""))
        return {"step": label, "refused": True, "expected": ok, "detail": e.detail[:200], "tx": e.tx}
    print(f"  {label}: NOT refused (tx {res['tx']}) -> PROBLEM")
    return {"step": label, "refused": False, "tx": res["tx"]}


def main() -> None:
    from kavren_agents.common import AgentChain, ApiClient, load_env_file
    from kavren_api.chain import quiet_rpc_race_noise

    quiet_rpc_race_noise()
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
    buyer = load_env_file("~/.kavren/buyer-agent.env")
    prov = load_env_file("~/.kavren/provider-agent.env")
    rpc, contract = os.environ.get("NEAR_RPC_URL", ""), buyer["KAVREN_CONTRACT_ID"]
    api_url = os.environ.get("KAVREN_API_URL", "https://kavren.onrender.com")
    owner = os.environ.get("BUYER_OWNER_ACCOUNT_ID") or sys.exit("set BUYER_OWNER_ACCOUNT_ID")
    b_id, p_id = buyer["BUYER_AGENT_ACCOUNT_ID"], prov["PROVIDER_ACCOUNT_ID"]
    bc = AgentChain(rpc, contract, b_id, buyer["BUYER_AGENT_FC_PRIVATE_KEY"])
    pc = AgentChain(rpc, contract, p_id, prov["PROVIDER_FC_PRIVATE_KEY"])
    bapi = ApiClient(api_url, b_id, buyer["BUYER_AGENT_FC_PRIVATE_KEY"])

    before = bc.view("get_budget", {"owner_id": owner, "agent_id": b_id})
    if not before:
        sys.exit(f"no budget for {b_id}; fund it in the owner console first")
    print(f"Deadline-refund drill, {minutes:.0f} min. Budget of {b_id}: {near(before['balance'])} NEAR.")
    print("The provider agent must be STOPPED now (Ctrl+C in its tab) and stay stopped until this ends.")
    if input("Type 'go' to start: ").strip() != "go":
        sys.exit("cancelled")

    r = bapi.request("POST", "/drafts", files={"file": ("deadline-drill.csv", (FX / "input.csv").read_bytes(), "text/csv")},
                     data={"template_id": "contacts-v1"})
    r.raise_for_status()
    d = r.json()
    if int(d["price_yocto"]) > int(before["balance"]):
        sys.exit("budget too small for one job")
    deadline = int(time.time()) + int(minutes * 60)
    res = bc.call("create_job", {"args": {
        "owner_id": owner, "provider_id": p_id, "amount": d["price_yocto"], "template_id": d["template_id"],
        "spec_json": d["spec_json"], "draft_id": d["draft_id"], "input_hash": d["input_hash"],
        "work_hash": d["work_hash"], "canary_commitment": d["canary_commitment"], "deadline": deadline,
        "max_attempts": min(3, before["policy"]["max_attempts"])}})
    job_id = int(res["value"])
    bapi.request_after_write("POST", f"/jobs/{job_id}/register")
    j = bc.view("get_job", {"job_id": job_id})
    print(f"job {job_id} opened: {near(j['amount'])} NEAR in escrow, deadline {time.strftime('%H:%M:%S', time.localtime(j['deadline']))}"
          f"\n  tx {TX}{res['tx']}")
    out = {"job_id": job_id, "create_tx": res["tx"], "amount": near(j["amount"]), "deadline": j["deadline"],
           "budget_before": near(before["balance"]), "steps": []}

    out["steps"].append(expect_refused(lambda: bc.call("refund_job", {"job_id": job_id}),
                                       "1. refund before the deadline", "no refund condition holds"))

    while time.time() < j["deadline"] + 30:  # +30 s: let the chain's block time pass the deadline too
        left = int(j["deadline"] + 30 - time.time())
        print(f"\r  waiting for the deadline: {left // 60:02d}:{left % 60:02d} ", end="", flush=True)
        time.sleep(5)
    print()

    out["steps"].append(expect_refused(lambda: pc.call("submit_work", {"job_id": job_id, "output_hash": sha256_hex(b"late")}),
                                       "2. provider submits after the deadline", "deadline passed"))

    for attempt in range(8):  # block time can trail the wall clock by a few seconds
        try:
            ref = bc.call("refund_job", {"job_id": job_id})
            break
        except Exception as e:  # noqa: BLE001
            if attempt == 7:
                raise
            print(f"  refund not accepted yet ({str(e)[:80]}); retrying in 15 s")
            time.sleep(15)
    j = bc.view("get_job", {"job_id": job_id})
    after = bc.view("get_budget", {"owner_id": owner, "agent_id": b_id})
    ok = j["status"] == "Refunded" and int(before["balance"]) - int(after["balance"]) < int(j["amount"])
    print(f"  3. refund after the deadline: job {j['status']}, tx {TX}{ref['tx']}")
    print(f"     budget {near(before['balance'])} -> {near(after['balance'])} NEAR "
          f"(the job's {near(j['amount'])} NEAR came back; only the storage fee is kept)")
    out.update(refund_tx=ref["tx"], status=j["status"], budget_after=near(after["balance"]),
               result="DEADLINE REFUND OK" if ok and all(s.get("refused") and s.get("expected") for s in out["steps"])
               else "PROBLEM")
    print(f"\nproof page: https://kavren.onrender.com/job/?id={job_id}")
    print(json.dumps(out, indent=2))
    print("You can start the provider agent again now.")


if __name__ == "__main__":
    main()
