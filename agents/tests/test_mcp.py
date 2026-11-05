"""The buyer-side MCP tools, used by an outside agent with its OWN account and budget,
against the fake contract (with the real function-call key scopes), the real API and
the real evaluator. One test drives the tools through a real MCP client session."""
import sys
from pathlib import Path

import anyio
import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from fake_contract import BUYER, NEAR, OWNER, PROVIDER, AgentChainFake, Reject  # noqa: E402
from new_agent_key import ROLES  # noqa: E402
from test_buyer import INPUT, fc_key, policy, provider_does_work, world  # noqa: E402,F401

from kavren_agents.buyer import BuyerAgent  # noqa: E402
from kavren_agents.common import ApiClient  # noqa: E402
from kavren_agents.mcp_server import BuyerTools, build_server  # noqa: E402

CLAUDE = "claude-buyer.lahd-kavren.testnet"


def tools_for(w, fund=True, **pol):
    c = w["contract"]
    if fund:
        c.fund_budget(OWNER, CLAUDE, 2 * NEAR, policy(**pol))
    api = ApiClient("unused", CLAUDE, fc_key(c, CLAUDE), http=w["agent"].api.http)
    api.sleep = lambda s: None
    chain = AgentChainFake(c, CLAUDE, ROLES["claude-buyer"][2])  # the real key scope
    agent = BuyerAgent(CLAUDE, OWNER, chain, api, w["tmp"] / "claude" / "inbox", w["tmp"] / "claude" / "outbox", w["clock"])
    return BuyerTools(agent, "https://kavren.onrender.com")


def csv_file(w, name="leads.csv", data=INPUT):
    p = w["tmp"] / name
    p.write_bytes(data)
    return str(p)


def test_budget_shows_owner_rules_or_says_unfunded(world):
    t = tools_for(world, fund=False)
    assert t.budget()["funded"] is False
    world["contract"].fund_budget(OWNER, CLAUDE, 2 * NEAR, policy())
    b = t.budget()
    assert b["funded"] and b["balance_near"] == "2" and b["rules"]["allowed_providers"] == [PROVIDER]
    assert b["rules"]["max_per_job_near"] == "2" and b["jobs_running"] == 0


def test_quote_prices_without_spending(world):
    t = tools_for(world)
    q = t.quote(file_path=csv_file(world))
    assert q == {"can_hire_now": True, "job_type": "contacts-v1", "price_near": "0.24", "provider": PROVIDER,
                 "max_attempts": 3, "note": "Nothing has been spent. Call kavren_hire with the same input to hire."}
    assert world["contract"].jobs == {}


def test_outside_agent_hires_waits_verifies_and_accepts(world):
    w = world
    t = tools_for(w)
    h = t.hire(csv_text=INPUT.decode())
    assert h["hired"] and h["price_near"] == "0.24" and h["proof_page"].endswith(f"/job/?id={h['job_id']}")
    jid = h["job_id"]
    assert w["contract"].jobs[jid]["buyer_agent"] == CLAUDE  # its own budget, not the house buyer's
    assert w["contract"].budgets[(OWNER, BUYER)]["open_jobs"] == 0
    assert t.job_status(jid)["status"] == "Open"
    assert t.accept(jid) == {"accepted": False, "reason": "job is Open, not Passed"}

    provider_does_work(w, jid, good=True)
    assert t.job_status(jid)["status"] == "Submitted"
    w["svc"].process_pending()
    s = t.job_status(jid)
    assert s["status"] == "Passed" and s["verdicts"] == [{"submission": 1, "verdict": "PASS"}]
    assert "kavren_accept" in s["next_step"]

    a = t.accept(jid)
    assert a["accepted"] and all(a["independent_checks"].values())
    assert "claimable" in a["payment"] and "withdraw" in a["payment"]  # never claims the provider was paid
    assert a["rows"] > 0 and a["preview"][0] == ["full_name", "email", "signup_date"]
    assert Path(a["cleaned_file"]).read_text().startswith("full_name,email,signup_date\n")
    assert t.accept(jid)["already_accepted"] is True
    assert t.job_status(jid)["next_step"] == "Already accepted."


def test_retrying_a_hire_that_landed_never_hires_twice(world):
    w = world
    t = tools_for(w)
    real = t.a.chain.call

    def lands_then_raises(method, args):
        real(method, args)
        raise RuntimeError("RPC timed out waiting for the result")
    t.a.chain.call = lands_then_raises
    with pytest.raises(RuntimeError):
        t.hire(csv_text=INPUT.decode())
    t.a.chain.call = real
    again = t.hire(csv_text=INPUT.decode())
    assert again["hired"] and again["job_id"] == 0
    assert len(w["contract"].jobs) == 1


def test_owner_rules_bind_the_outside_agent(world):
    t = tools_for(world, max_per_job=str(NEAR // 10))
    q = t.quote(csv_text=INPUT.decode())
    assert q["can_hire_now"] is False and "max_per_job" in q["reason"]
    h = t.hire(csv_text=INPUT.decode())
    assert h["hired"] is False and world["contract"].jobs == {}
    # and even if the tool's own check were bypassed, the contract and the key scope refuse
    with pytest.raises(Reject):
        t.a.chain.call("withdraw_budget", {"agent_id": CLAUDE, "amount": "1"})


def test_refund_only_when_the_contract_allows_it(world):
    w = world
    t = tools_for(w)
    jid = t.hire(csv_text=INPUT.decode())["job_id"]
    r = t.refund(jid)
    assert r["refunded"] is False and "no refund condition" in r["reason"]
    bal = int(w["contract"].budgets[(OWNER, CLAUDE)]["balance"])
    w["clock"].t += 3700
    assert t.job_status(jid)["refundable_now"] is True
    r = t.refund(jid)
    assert r["refunded"] and r["amount_near"] == "0.24"
    assert int(w["contract"].budgets[(OWNER, CLAUDE)]["balance"]) == bal + 240 * 10**21


def test_accept_refuses_a_pass_that_does_not_reproduce(world):
    """A lying evaluator (or a stolen evaluator key) PASSes bad work: the agent's own re-check says no."""
    w = world
    t = tools_for(w)
    jid = t.hire(csv_text=INPUT.decode())["job_id"]
    provider_does_work(w, jid, good=False)
    j = w["contract"].jobs[jid]
    w["contract"].submit_verdict(jid, j["submission_seq"], j["current_output_hash"], "PASS", "e" * 64)
    a = t.accept(jid)
    assert a["accepted"] is False and a["checks"]["independent_verdict_is_pass"] is False
    assert "FAILED" in a["reason"]
    assert not list(t.a.outbox.glob("*.cleaned.csv"))


def test_cannot_act_on_another_agents_job(world):
    w = world
    (w["agent"].inbox / "house.csv").write_bytes(INPUT)
    house_job = w["agent"].tick()[0]["job_id"]
    t = tools_for(w)
    assert t.accept(house_job)["reason"] == "not a job created by this agent"
    assert t.refund(house_job)["reason"] == "not a job created by this agent"
    assert t.job_status(house_job)["created_by_this_agent"] is False


def test_bad_inputs_are_explained(world):
    t = tools_for(world)
    with pytest.raises(ValueError, match="exactly one"):
        t.quote()
    with pytest.raises(ValueError, match="no such file"):
        t.quote(file_path="/nope.csv")
    q = t.quote(csv_text="name,phone\nA,1\n")
    assert q["can_hire_now"] is False and "template" in q["reason"]


def test_tools_work_through_a_real_mcp_client(world):
    from mcp import Client

    t = tools_for(world)
    server = build_server(t)

    async def go():
        async with Client(server) as c:
            listed = await c.list_tools()
            names = sorted(x.name for x in listed.tools)
            ann = {x.name: x.annotations for x in listed.tools}
            res = await c.call_tool("kavren_quote", {"csv_text": INPUT.decode()})
            bud = await c.call_tool("kavren_budget", {})
            return names, ann, res, bud

    names, ann, res, bud = anyio.run(go)
    assert names == ["kavren_accept", "kavren_budget", "kavren_hire", "kavren_job_status", "kavren_quote", "kavren_refund"]
    assert ann["kavren_budget"].read_only_hint is True and ann["kavren_hire"].read_only_hint is False
    import json
    assert not res.is_error and json.loads(res.content[0].text)["price_near"] == "0.24"
    assert json.loads(bud.content[0].text)["funded"] is True
