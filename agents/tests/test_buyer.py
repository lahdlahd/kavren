import json
import random
import sys
from pathlib import Path

import base58
import pytest
from fastapi.testclient import TestClient
from nacl.signing import SigningKey

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(ROOT / "kavren_core" / "tests"))

from fake_contract import BUYER, CONTRACT, EVALUATOR, NEAR, OWNER, PROVIDER, AgentChainFake, FakeContract  # noqa: E402
from reference_cleaner import clean  # noqa: E402

from kavren_agents.buyer import BuyerAgent  # noqa: E402
from kavren_agents.common import ApiClient  # noqa: E402
from kavren_api.config import Settings  # noqa: E402
from kavren_api.db import make_engine  # noqa: E402
from kavren_api.main import create_app  # noqa: E402
from kavren_api.service import Service  # noqa: E402
from kavren_api.storage import LocalStorage  # noqa: E402
from kavren_core.canonical import sha256_hex  # noqa: E402
from kavren_core.spec import load_spec  # noqa: E402

INPUT = (ROOT / "fixtures" / "contacts" / "input.csv").read_bytes()


class Clock:
    t = 1_800_000_000

    def __call__(self):
        return self.t


def fc_key(contract, account):
    sk = SigningKey.generate()
    pub = "ed25519:" + base58.b58encode(bytes(sk.verify_key)).decode()
    contract.keys[(account, pub)] = {"FunctionCall": {"receiver_id": CONTRACT, "method_names": [], "allowance": None}}
    return "ed25519:" + base58.b58encode(bytes(sk) + bytes(sk.verify_key)).decode()


def policy(**over):
    p = {"max_per_job": str(2 * NEAR), "max_open_jobs": 2, "allowed_templates": ["contacts-v1"],
         "allowed_providers": [PROVIDER], "max_deadline_secs": 7 * 86400, "max_attempts": 3,
         "expires_at": Clock.t + 30 * 86400}
    p.update(over)
    return p


@pytest.fixture
def world(tmp_path):
    clock = Clock()
    clock.t = Clock.t
    contract = FakeContract(clock)
    s = Settings(database_url=f"sqlite:///{tmp_path}/db.sqlite", storage_dir=str(tmp_path / "files"),
                 contract_id=CONTRACT, rpc_url="unused", evaluator_account_id=EVALUATOR,
                 evaluator_fc_private_key="", auth_recipient="kavren-api", file_retention_days=7)
    svc = Service(s, make_engine(s.database_url), LocalStorage(s.storage_dir), contract, clock, random.Random(11))
    http = TestClient(create_app(svc))
    buyer_api = ApiClient("unused", BUYER, fc_key(contract, BUYER), http=http)
    prov_api = ApiClient("unused", PROVIDER, fc_key(contract, PROVIDER), http=http)
    buyer_api.sleep = prov_api.sleep = lambda s: None
    contract.fund_budget(OWNER, BUYER, 5 * NEAR, policy())
    agent = BuyerAgent(BUYER, OWNER, AgentChainFake(contract, BUYER), buyer_api,
                       tmp_path / "inbox", tmp_path / "outbox", clock)
    return dict(clock=clock, contract=contract, svc=svc, agent=agent, prov_api=prov_api, tmp=tmp_path)


def provider_does_work(w, job_id, good=True):
    """Stand-in for the provider agent: fetch work, clean (or not), submit on-chain, upload."""
    work = w["prov_api"].request("GET", f"/jobs/{job_id}/work").content
    j = w["contract"].view("get_job", {"job_id": job_id})
    out = clean(load_spec(j["spec_json"].encode()), work)
    if not good:
        out = b"\n".join(out.split(b"\n")[:4]) + b"\n"
    w["contract"].call_as(PROVIDER, "submit_work", {"job_id": job_id, "output_hash": sha256_hex(out)})
    r = w["prov_api"].request("POST", f"/jobs/{job_id}/submissions", files={"file": ("s.csv", out)})
    assert r.status_code == 200, r.text


def test_agent_hires_without_any_human_step_and_verifies_before_accepting(world):
    w = world
    (w["agent"].inbox / "leads-for-outreach.csv").write_bytes(INPUT)
    ev = w["agent"].tick()
    hired = ev[0]
    assert hired["action"] == "hired" and hired["provider_id"] == PROVIDER
    assert int(hired["price_yocto"]) == 240 * 10**21  # 0.2 + 20 rows x 0.002 NEAR
    j = w["contract"].jobs[hired["job_id"]]
    assert j["buyer_agent"] == BUYER and j["status"] == "Open"
    assert (w["agent"].inbox / "processed" / "leads-for-outreach.csv").exists()

    provider_does_work(w, hired["job_id"], good=False)
    w["svc"].process_pending()
    assert w["contract"].jobs[0]["status"] == "Open"  # FAIL keeps escrow

    provider_does_work(w, hired["job_id"], good=True)
    w["svc"].process_pending()
    assert w["contract"].jobs[0]["status"] == "Passed"
    # the canary set is served once the job is final; the buyer checks it against the on-chain commitment
    ev = w["agent"].tick()
    assert ev[0]["action"] == "accepted" and all(ev[0]["checks"].values())
    receipt = json.loads((w["agent"].outbox / "leads-for-outreach.receipt.json").read_text())
    assert receipt["result"] == "passed"
    cleaned = (w["agent"].outbox / "leads-for-outreach.cleaned.csv").read_text()
    assert cleaned.startswith("full_name,email,signup_date\n") and "amaka.obi@example.com" in cleaned
    assert w["agent"].tick() == []  # done; nothing repeats
    assert w["svc"].reveal_finished()[0]["job_id"] == hired["job_id"]  # evaluator also opens it on-chain
    assert w["contract"].jobs[0]["canaries_revealed"] is True


def test_agent_catches_a_lying_evaluator(world):
    """If the evaluator (or its key) PASSes bad work, the buyer's own re-run says no."""
    w = world
    (w["agent"].inbox / "x.csv").write_bytes(INPUT)
    jid = w["agent"].tick()[0]["job_id"]
    provider_does_work(w, jid, good=False)
    j = w["contract"].jobs[jid]
    w["contract"].submit_verdict(jid, j["submission_seq"], j["current_output_hash"], "PASS", "e" * 64)
    w["svc"].reveal_finished()
    w["svc"]._ensure_registered(jid, j["draft_id"])
    w["svc"].reveal_finished()
    ev = w["agent"].tick()
    assert ev[0]["action"] == "verification_failed"
    assert ev[0]["checks"]["independent_verdict_is_pass"] is False
    assert not (w["agent"].outbox / "x.cleaned.csv").exists()


def test_agent_refunds_after_deadline(world):
    w = world
    (w["agent"].inbox / "x.csv").write_bytes(INPUT)
    jid = w["agent"].tick()[0]["job_id"]
    bal = int(w["contract"].budgets[(OWNER, BUYER)]["balance"])
    w["clock"].t += 3600
    ev = w["agent"].tick()
    assert ev[0]["action"] == "refunded" and ev[0]["reason"] == "deadline passed"
    assert int(w["contract"].budgets[(OWNER, BUYER)]["balance"]) == bal + 240 * 10**21


def test_agent_refunds_when_evaluator_is_silent(world):
    w = world
    (w["agent"].inbox / "x.csv").write_bytes(INPUT)
    jid = w["agent"].tick()[0]["job_id"]
    provider_does_work(w, jid, good=True)  # but the evaluator never runs
    w["clock"].t += 1800
    ev = w["agent"].tick()
    assert ev[0]["action"] == "refunded" and "evaluator" in ev[0]["reason"]


@pytest.mark.parametrize("over,reason", [
    ({"allowed_templates": ["leads-v1"]}, "no eligible template"),
    ({"max_per_job": str(NEAR // 10)}, "exceeds the owner's max_per_job"),
    ({"expires_at": Clock.t - 1}, "budget expired"),
])
def test_agent_respects_owner_policy(world, over, reason):
    w = world
    w["contract"].budgets[(OWNER, BUYER)]["policy"].update(over)
    (w["agent"].inbox / "x.csv").write_bytes(INPUT)
    ev = w["agent"].tick()
    assert ev[0]["action"] == "skip" and reason in ev[0]["reason"]
    assert w["contract"].jobs == {}
    assert reason in json.loads((w["agent"].outbox / "x.receipt.json").read_text())["reason"]


def test_agent_waits_when_open_job_limit_reached(world):
    w = world
    w["contract"].budgets[(OWNER, BUYER)]["policy"]["max_open_jobs"] = 1
    (w["agent"].inbox / "a.csv").write_bytes(INPUT)
    (w["agent"].inbox / "b.csv").write_bytes(INPUT)
    ev = w["agent"].tick()
    assert [e["action"] for e in ev[:2]] == ["hired", "wait"]
    assert (w["agent"].inbox / "b.csv").exists()


def test_agent_skips_unwinnable_file(world):
    w = world
    (w["agent"].inbox / "dirty.csv").write_bytes(b"full_name,email,signup_date\nA B,bad@x,\nC D,,\nE F,ok@example.com,\n")
    ev = w["agent"].tick()
    assert ev[0]["action"] == "skip" and "no provider could pass" in ev[0]["reason"]


def test_agent_cannot_exceed_budget_even_if_its_logic_were_wrong(world):
    """Defence in depth: the CONTRACT enforces the policy, not the agent."""
    w = world
    from fake_contract import Reject
    w["contract"].budgets[(OWNER, BUYER)]["policy"]["max_per_job"] = str(NEAR // 10)
    with pytest.raises(Reject):
        w["agent"].chain.call("create_job", {"args": {
            "owner_id": OWNER, "provider_id": PROVIDER, "amount": str(NEAR), "template_id": "contacts-v1",
            "spec_json": "{}", "draft_id": "d", "input_hash": "1" * 64, "work_hash": "2" * 64,
            "canary_commitment": "3" * 64, "deadline": w["clock"].t + 3600, "max_attempts": 3}})


# ---------------------------------------------------------------- read-after-write lag
class LaggingChain:
    """The API's view of the chain, `n` reads behind: models an RPC node that has not
    yet seen the transaction the agent just sent (the live bug of jobs 1 and 2)."""

    def __init__(self, contract):
        self.c, self.n, self.snap = contract, 0, {}

    def lag(self, n):
        import copy
        self.n, self.snap = n, copy.deepcopy(self.c.jobs)

    def get_job(self, job_id):
        if self.n > 0:
            self.n -= 1
            j = self.snap.get(job_id)
            return json.loads(json.dumps(j)) if j else None
        return self.c.get_job(job_id)

    def __getattr__(self, name):
        return getattr(self.c, name)


def test_register_lag_does_not_cause_a_second_hire(world):
    w = world
    lagging = LaggingChain(w["contract"])
    w["svc"].chain = lagging
    lagging.lag(100)  # the API will not see the new job for a long time
    (w["agent"].inbox / "leads.csv").write_bytes(INPUT)
    ev = w["agent"].tick()
    assert ev[0]["action"] == "hired"
    assert (w["agent"].inbox / "processed" / "leads.csv").exists()  # the file is gone from the inbox
    st = w["agent"].state["jobs"]["0"]
    assert st["registered"] is False
    lagging.n = 0  # the node catches up
    for _ in range(3):
        w["agent"].tick()
    assert len(w["contract"].jobs) == 1  # never hired twice
    assert w["agent"].state["jobs"]["0"]["registered"] is True


def test_register_succeeds_after_a_short_lag_in_the_same_tick(world):
    w = world
    lagging = LaggingChain(w["contract"])
    w["svc"].chain = lagging
    (w["agent"].inbox / "leads.csv").write_bytes(INPUT)
    real_call = w["agent"].chain.call

    def call_then_lag(method, args):
        lagging.lag(2)  # snapshot BEFORE the write: two stale reads, then fresh
        return real_call(method, args)
    w["agent"].chain.call = call_then_lag
    ev = w["agent"].tick()
    assert ev[0]["action"] == "hired"
    assert w["agent"].state["jobs"]["0"]["registered"] is True


def test_crash_after_create_job_is_recovered_not_rehired(world):
    w = world
    (w["agent"].inbox / "leads.csv").write_bytes(INPUT)
    real_call = w["agent"].chain.call

    def lands_then_raises(method, args):
        real_call(method, args)
        raise RuntimeError("RPC timed out waiting for the result")
    w["agent"].chain.call = lands_then_raises
    with pytest.raises(RuntimeError):
        w["agent"].tick()
    w["agent"].chain.call = real_call
    assert (w["agent"].inbox / "leads.csv").exists()  # the file is still there...
    ev = w["agent"].tick()
    assert ev[0] == {"file": "leads.csv", "action": "recovered", "job_id": 0}  # ...but it is not hired again
    assert len(w["contract"].jobs) == 1
    assert (w["agent"].inbox / "processed" / "leads.csv").exists()


def test_failed_transaction_is_retried_only_after_the_grace_period(world):
    w = world
    (w["agent"].inbox / "leads.csv").write_bytes(INPUT)
    real_call = w["agent"].chain.call

    def never_lands(method, args):
        raise RuntimeError("transaction rejected")
    w["agent"].chain.call = never_lands
    with pytest.raises(RuntimeError):
        w["agent"].tick()
    w["agent"].chain.call = real_call
    assert w["agent"].tick()[0]["action"] == "wait"  # might still land: do not hire yet
    w["clock"].t += 200
    assert w["agent"].tick()[0]["action"] == "hired"
    assert len(w["contract"].jobs) == 1


def test_restart_adopts_live_jobs_missing_from_state(world):
    w = world
    (w["agent"].inbox / "a.csv").write_bytes(INPUT)
    w["agent"].tick()
    w["agent"].state["jobs"].clear()  # simulate the lost state of the live run
    w["agent"]._save()
    restarted = BuyerAgent(BUYER, OWNER, w["agent"].chain, w["agent"].api,
                           w["agent"].inbox, w["agent"].outbox, w["clock"])
    ev = restarted.tick()
    assert ev[0] == {"job_id": 0, "action": "adopted", "status": "Open"}
    assert restarted.state["jobs"]["0"]["adopted"] is True
    w["clock"].t += 3700  # past the deadline with no work: the adopted job is refunded
    ev = restarted.tick()
    assert ev[0]["action"] == "refunded" and w["contract"].jobs[0]["status"] == "Refunded"


def test_adoption_ignores_other_agents_and_finished_jobs(world):
    w = world
    (w["agent"].inbox / "a.csv").write_bytes(INPUT)
    w["agent"].tick()
    w["contract"].jobs[0]["buyer_agent"] = "someone-else.testnet"
    restarted = BuyerAgent(BUYER, OWNER, w["agent"].chain, w["agent"].api,
                           w["agent"].inbox, w["agent"].outbox, w["clock"])
    restarted.state["jobs"].clear()
    assert restarted.reconcile() == []
