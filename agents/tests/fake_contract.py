"""In-memory stand-in for the KAVREN contract, mirroring its rules for the
fields agents and the API use. The real contract is tested in contract/ and on
a real sandbox node in CI; this fake only lets agents be tested end to end
without a network."""
from __future__ import annotations

import base64
import json

from kavren_core.canonical import sha256_hex

CONTRACT = "kavren.lahd-kavren.testnet"
EVALUATOR = "evaluator.lahd-kavren.testnet"
OWNER = "lahd-kavren.testnet"
BUYER = "buyer-agent.lahd-kavren.testnet"
PROVIDER = "provider-agent.lahd-kavren.testnet"
NEAR = 10**24


class Reject(Exception):
    pass


class FakeContract:
    def __init__(self, clock):
        self.clock = clock
        self.budgets, self.jobs, self.claimable, self.keys = {}, {}, {}, {}
        self.tx = 0

    def _tx(self):
        self.tx += 1
        return f"tx{self.tx}"

    def now(self):
        return int(self.clock())

    # ---------------- owner (full key)
    def fund_budget(self, owner, agent, amount, policy):
        b = self.budgets.setdefault((owner, agent), {"owner_id": owner, "agent_id": agent, "balance": "0",
                                                     "open_jobs": 0, "revoked": False, "policy": policy})
        b["policy"] = policy
        b["balance"] = str(int(b["balance"]) + amount)

    # ---------------- contract methods by caller
    def call_as(self, caller, method, args):
        return getattr(self, "_m_" + method)(caller, **args)

    def _m_create_job(self, caller, args):
        b = self.budgets.get((args["owner_id"], caller))
        if not b:
            raise Reject("no budget for this owner and agent")
        p, amt = b["policy"], int(args["amount"])
        if b["revoked"] or self.now() >= p["expires_at"]:
            raise Reject("budget unusable")
        if b["open_jobs"] >= p["max_open_jobs"]:
            raise Reject("max_open_jobs reached")
        if args["template_id"] not in p["allowed_templates"] or args["provider_id"] not in p["allowed_providers"]:
            raise Reject("not allowed by policy")
        if amt > int(p["max_per_job"]) or amt + 10**22 > int(b["balance"]):
            raise Reject("amount")
        if not (self.now() + 600 <= args["deadline"] <= self.now() + p["max_deadline_secs"]):
            raise Reject("deadline")
        jid = len(self.jobs)
        self.jobs[jid] = dict(job_id=jid, owner_id=args["owner_id"], buyer_agent=caller, provider_id=args["provider_id"],
                              evaluator_id=EVALUATOR, amount=args["amount"], template_id=args["template_id"],
                              spec_json=args["spec_json"], draft_id=args["draft_id"], input_hash=args["input_hash"],
                              work_hash=args["work_hash"], canary_commitment=args["canary_commitment"],
                              created_at=self.now(), deadline=args["deadline"], max_attempts=args["max_attempts"],
                              attempts_used=0, eval_window_secs=1800, submission_seq=0, current_output_hash=None,
                              submitted_at=None, status="Open", verdicts=[], canaries_revealed=False)
        b["balance"] = str(int(b["balance"]) - amt - 10**22)
        b["open_jobs"] += 1
        return jid

    def _m_refund_job(self, caller, job_id):
        j = self.jobs[job_id]
        t = self.now()
        ok = (j["status"] == "Open" and (t >= j["deadline"] or j["attempts_used"] >= j["max_attempts"])) or \
             (j["status"] == "Submitted" and t >= j["submitted_at"] + j["eval_window_secs"])
        if not ok:
            raise Reject("no refund condition holds")
        b = self.budgets[(j["owner_id"], j["buyer_agent"])]
        b["balance"] = str(int(b["balance"]) + int(j["amount"]))
        b["open_jobs"] -= 1
        j["status"] = "Refunded"

    def _m_submit_work(self, caller, job_id, output_hash):
        j = self.jobs[job_id]
        if caller != j["provider_id"] or j["status"] != "Open" or self.now() >= j["deadline"] \
                or j["attempts_used"] >= j["max_attempts"]:
            raise Reject("submit not allowed")
        j.update(submission_seq=j["submission_seq"] + 1, current_output_hash=output_hash,
                 submitted_at=self.now(), status="Submitted")
        return j["submission_seq"]

    def _m_withdraw(self, caller):
        amt = self.claimable.pop(caller, 0)
        if not amt:
            raise Reject("nothing to withdraw")
        return str(amt)

    def _m_submit_verdict(self, caller, job_id, seq, output_hash, verdict, report_hash):
        j = self.jobs[job_id]
        if caller != j["evaluator_id"] or j["status"] != "Submitted" or seq != j["submission_seq"] \
                or output_hash != j["current_output_hash"] or self.now() >= j["submitted_at"] + j["eval_window_secs"]:
            raise Reject("verdict not allowed")
        j["verdicts"].append({"seq": seq, "verdict": verdict, "output_hash": output_hash, "report_hash": report_hash, "at": self.now()})
        if verdict == "PASS":
            j["status"] = "Passed"
            self.claimable[j["provider_id"]] = self.claimable.get(j["provider_id"], 0) + int(j["amount"])
            self.budgets[(j["owner_id"], j["buyer_agent"])]["open_jobs"] -= 1
        else:
            j.update(status="Open", attempts_used=j["attempts_used"] + 1)

    def _m_reveal_canaries(self, caller, job_id, blob):
        j = self.jobs[job_id]
        if caller != j["evaluator_id"] or j["status"] not in ("Passed", "Refunded") or j["canaries_revealed"]:
            raise Reject("reveal not allowed")
        if sha256_hex(base64.b64decode(blob)) != j["canary_commitment"]:
            raise Reject("reveal does not match commitment")
        j["canaries_revealed"] = True

    # ---------------- views
    def view(self, method, args):
        if method == "get_job":
            return json.loads(json.dumps(self.jobs.get(args["job_id"])))
        if method == "get_budget":
            return json.loads(json.dumps(self.budgets.get((args["owner_id"], args["agent_id"]))))
        if method == "get_jobs_for_provider":
            return [json.loads(json.dumps(j)) for j in self.jobs.values() if j["provider_id"] == args["provider_id"]]
        if method == "get_claimable":
            return str(self.claimable.get(args["account_id"], 0))
        raise KeyError(method)

    # ---------------- the API's Chain protocol (the evaluator side)
    def get_job(self, job_id):
        return self.view("get_job", {"job_id": job_id})

    def get_access_key_permission(self, account_id, public_key):
        return self.keys.get((account_id, public_key))

    def submit_verdict(self, job_id, seq, output_hash, verdict, report_hash):
        self._m_submit_verdict(EVALUATOR, job_id, seq, output_hash, verdict, report_hash)
        return self._tx()

    def reveal_canaries(self, job_id, blob):
        self._m_reveal_canaries(EVALUATOR, job_id, base64.b64encode(blob).decode())
        return self._tx()


class AgentChainFake:
    """Same interface as kavren_agents.common.AgentChain, bound to one caller."""

    def __init__(self, contract: FakeContract, account_id: str):
        self.c, self.account_id = contract, account_id

    def view(self, method, args):
        return self.c.view(method, args)

    def call(self, method, args):
        return {"tx": self.c._tx(), "value": self.c.call_as(self.account_id, method, args)}
