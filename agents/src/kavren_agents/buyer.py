"""The buyer agent: rule-based and autonomous within the owner's on-chain policy.

Loop (no human per job):
  1. a CSV appears in INBOX_DIR (the owner drops it there and does nothing else);
  2. pick the single approved template whose columns match (owner policy decides
     which templates are allowed);
  3. check the on-chain budget policy: not revoked/expired, open-job room, price
     within max_per_job and the available balance;
  4. pick an allowed provider; get a protocol draft from the API;
  5. create_job on-chain with the agent's function-call key; register it;
  6. monitor: refund when a refund condition holds; on PASS download the
     deliverable AND re-run the evaluation itself (kavren_core) against the
     on-chain hashes before accepting it; write a receipt to OUTBOX_DIR.

Run: kavren-buyer  (reads ~/.kavren/buyer-agent.env plus the environment)
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Callable, Optional

from kavren_core.canaries import InputError, read_input
from kavren_core.canonical import load_canonical, sha256_hex
from kavren_core.evaluator import evaluate
from kavren_core.spec import load_spec
from kavren_core.templates import get_template, select_template

log = logging.getLogger("kavren.buyer")

DEFAULT_DEADLINE_SECS = 3600
DEFAULT_ATTEMPTS = 3


class BuyerAgent:
    def __init__(self, account_id: str, owner_id: str, chain, api, inbox: Path, outbox: Path,
                 clock: Callable[[], float] = time.time):
        self.account_id, self.owner_id = account_id, owner_id
        self.chain, self.api, self.clock = chain, api, clock
        self.inbox, self.outbox = Path(inbox), Path(outbox)
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.outbox.mkdir(parents=True, exist_ok=True)
        (self.inbox / "processed").mkdir(exist_ok=True)
        self.state_path = self.outbox / "state.json"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {"jobs": {}}
        self.state.setdefault("pending", {})
        self._reconciled = False

    # ------------------------------------------------------------------ main loop
    def tick(self) -> list[dict]:
        events = []
        if not self._reconciled:
            events.extend(self.reconcile())
            self._reconciled = True
            self._save()
        for f in sorted(self.inbox.glob("*.csv")):
            events.append(self.take(f))
        for job_id in list(self.state["jobs"]):
            ev = self.monitor(int(job_id))
            if ev:
                events.append(ev)
        self._save()
        return events

    # ------------------------------------------------------------------ hiring
    def take(self, path: Path) -> dict:
        recovered = self._recover_pending(path.name, on_found=lambda: self._move(path, "processed"))
        if recovered:
            return recovered
        out = self.hire_bytes(path.name, path.read_bytes(), after_hire=lambda: self._move(path, "processed"))
        if out["action"] == "wait":
            return out  # keep the file in the inbox; try again on a later tick
        if out["action"] != "hired":
            self._move(path, "processed")
            self._receipt(path.stem, out)
        return out

    def hire_bytes(self, name: str, data: bytes, after_hire: Optional[Callable[[], None]] = None) -> dict:
        """Decide under the owner's policy and, if allowed, hire: draft -> create_job -> register.
        Shared by the inbox loop and the MCP tools. `after_hire` runs once the job is tracked
        (the inbox loop moves the file there)."""
        decision = self.decide(name, data)
        if decision["action"] != "hire":
            return decision
        r = self.api.request("POST", "/drafts", files={"file": (name, data, "text/csv")},
                             data={"template_id": decision["template_id"]})
        if r.status_code != 200:
            return {"file": name, "action": "skip", "reason": f"draft refused: {r.json().get('detail')}"}
        draft = r.json()
        price = int(draft["price_yocto"])
        if price != decision["price_yocto"]:
            return {"file": name, "action": "skip", "reason": "price mismatch between agent and protocol"}
        args = {"args": {
            "owner_id": self.owner_id, "provider_id": decision["provider_id"], "amount": str(price),
            "template_id": draft["template_id"], "spec_json": draft["spec_json"], "draft_id": draft["draft_id"],
            "input_hash": draft["input_hash"], "work_hash": draft["work_hash"],
            "canary_commitment": draft["canary_commitment"],
            "deadline": int(self.clock()) + DEFAULT_DEADLINE_SECS, "max_attempts": decision["max_attempts"],
        }}
        # Idempotent hiring: remember the draft BEFORE the transaction, so that if the call
        # errors after the job was actually created, the next attempt finds and adopts it
        # instead of hiring a second time.
        self.state.setdefault("pending", {})[name] = {
            "draft_id": draft["draft_id"], "provider_id": decision["provider_id"], "at": int(self.clock())}
        self._save()
        res = self.chain.call("create_job", args)
        job_id = int(res["value"])
        self._track(job_id, name, draft["draft_id"], str(price), create_tx=res["tx"])
        self.state["pending"].pop(name, None)
        if after_hire:
            after_hire()
        self._save()
        self._register(job_id, after_write=True)
        out = {"file": name, "action": "hired", "job_id": job_id, "provider_id": decision["provider_id"],
               "price_yocto": str(price), "tx": res["tx"]}
        log.info("hired: %s", out)
        return out

    def _recover_pending(self, name: str, on_found: Optional[Callable[[], None]] = None) -> Optional[dict]:
        """A previous hire for this file may have reached the chain even though the call
        raised. Look for it before hiring again."""
        p = self.state.get("pending", {}).get(name)
        if not p:
            return None
        for j in self._provider_jobs(p["provider_id"]):
            if j["draft_id"] == p["draft_id"] and j["buyer_agent"] == self.account_id:
                self._track(j["job_id"], name, p["draft_id"], j["amount"])
                self.state["pending"].pop(name)
                if on_found:
                    on_found()
                self._register(j["job_id"])
                return {"file": name, "action": "recovered", "job_id": j["job_id"]}
        if int(self.clock()) - p["at"] < 180:
            return {"file": name, "action": "wait", "reason": "checking whether the last hire reached the chain"}
        self.state["pending"].pop(name)  # it really did not happen; hiring again is safe
        return None

    def reconcile(self) -> list[dict]:
        """Adopt any live job this agent created that its state file does not know about
        (e.g. after a crash between create_job and saving state)."""
        budget = self.chain.view("get_budget", {"owner_id": self.owner_id, "agent_id": self.account_id})
        if not budget:
            return []
        out = []
        for prov in budget["policy"]["allowed_providers"]:
            for j in self._provider_jobs(prov):
                if (j["buyer_agent"] == self.account_id and j["owner_id"] == self.owner_id
                        and str(j["job_id"]) not in self.state["jobs"] and j["status"] in ("Open", "Submitted")):
                    self._track(j["job_id"], "adopted.csv", j["draft_id"], j["amount"], adopted=True)
                    self._register(j["job_id"])
                    out.append({"job_id": j["job_id"], "action": "adopted", "status": j["status"]})
        return out

    def _provider_jobs(self, provider_id: str) -> list[dict]:
        jobs, start = [], 0
        while True:
            page = self.chain.view("get_jobs_for_provider",
                                   {"provider_id": provider_id, "from_index": start, "limit": 100})
            jobs.extend(page)
            if len(page) < 100:
                return jobs
            start += 100

    def _track(self, job_id: int, file: str, draft_id: str, price: str, **extra) -> None:
        self.state["jobs"][str(job_id)] = {"file": file, "draft_id": draft_id, "status": "Open",
                                           "price_yocto": price, "registered": False, **extra}

    def _register(self, job_id: int, after_write: bool = False) -> None:
        st = self.state["jobs"][str(job_id)]
        url = f"/jobs/{job_id}/register"
        r = self.api.request_after_write("POST", url) if after_write else self.api.request("POST", url)
        if r.status_code == 200:
            st["registered"] = True
        else:
            log.warning("register job %s not accepted yet (%s); will retry", job_id, r.status_code)

    def decide(self, name: str, data: bytes) -> dict:
        """Pure policy decision from the on-chain budget; no side effects."""
        budget = self.chain.view("get_budget", {"owner_id": self.owner_id, "agent_id": self.account_id})
        if not budget:
            return {"file": name, "action": "skip", "reason": "no budget from the owner"}
        p = budget["policy"]
        now = int(self.clock())
        header = data.split(b"\n", 1)[0].decode("utf-8", "replace").split(",")
        template_id = select_template(header, allowed=p["allowed_templates"])
        if template_id is None:
            return {"file": name, "action": "skip", "reason": "no eligible template allowed by the owner"}
        if budget["revoked"]:
            return {"file": name, "action": "skip", "reason": "agent revoked by the owner"}
        if now >= p["expires_at"]:
            return {"file": name, "action": "skip", "reason": "budget expired"}
        if budget["open_jobs"] >= p["max_open_jobs"]:
            return {"file": name, "action": "wait", "reason": "max open jobs reached"}
        try:
            rows = len(read_input(get_template(template_id).build_spec(), data))
        except InputError as e:
            return {"file": name, "action": "skip", "reason": f"input not usable: {e}"}
        price = get_template(template_id).price_yocto(rows)
        if price > int(p["max_per_job"]):
            return {"file": name, "action": "skip", "reason": "price exceeds the owner's max_per_job"}
        # leave a margin for the job's storage cost, which the contract also charges
        if price + 10**23 > int(budget["balance"]):
            return {"file": name, "action": "skip", "reason": "insufficient budget"}
        if not p["allowed_providers"]:
            return {"file": name, "action": "skip", "reason": "no provider allowed"}
        return {"file": name, "action": "hire", "template_id": template_id, "price_yocto": price,
                "provider_id": p["allowed_providers"][0],
                "max_attempts": min(DEFAULT_ATTEMPTS, p["max_attempts"])}

    # ------------------------------------------------------------------ monitoring
    def monitor(self, job_id: int) -> Optional[dict]:
        st = self.state["jobs"][str(job_id)]
        if st.get("done"):
            return None
        if not st.get("registered", True):
            self._register(job_id)
        j = self.chain.view("get_job", {"job_id": job_id})
        st["status"] = j["status"]
        now = int(self.clock())
        if j["status"] == "Open" and (now >= j["deadline"] or j["attempts_used"] >= j["max_attempts"]):
            return self._refund(job_id, st, "deadline passed" if now >= j["deadline"] else "attempts exhausted")
        if j["status"] == "Submitted" and now >= j["submitted_at"] + j["eval_window_secs"]:
            return self._refund(job_id, st, "evaluator did not answer in time")
        if j["status"] == "Refunded":
            st["done"] = True
            self._receipt(_out(job_id, st), {"job_id": job_id, "result": "refunded", "job": _summ(j)})
            return {"job_id": job_id, "action": "closed", "result": "refunded"}
        if j["status"] == "Passed":
            return self._accept(job_id, st, j)
        return None

    def _refund(self, job_id: int, st: dict, why: str) -> dict:
        res = self.chain.call("refund_job", {"job_id": job_id})
        st.update(status="Refunded", refund_tx=res["tx"], done=True)
        self._receipt(_out(job_id, st), {"job_id": job_id, "result": "refunded", "reason": why, "tx": res["tx"]})
        return {"job_id": job_id, "action": "refunded", "reason": why, "tx": res["tx"]}

    def _accept(self, job_id: int, st: dict, j: dict) -> Optional[dict]:
        """Re-run the evaluation locally before trusting the PASS."""
        reveal = self.api.public("GET", f"/jobs/{job_id}/reveal")
        if reveal.status_code != 200 or sha256_hex(reveal.content) != j["canary_commitment"]:
            return None  # the evaluator reveals shortly after the job is final; try again next tick
        passed = next(v for v in j["verdicts"] if v["verdict"] == "PASS")
        work = self.api.request("GET", f"/jobs/{job_id}/audit/work").content
        sub = self.api.request("GET", f"/jobs/{job_id}/audit/submissions/{passed['seq']}").content
        spec = load_spec(j["spec_json"].encode())
        checks = {
            "work_hash_matches_chain": sha256_hex(work) == j["work_hash"],
            "submission_hash_matches_chain": sha256_hex(sub) == passed["output_hash"],
        }
        ev = evaluate(spec, work, sub, load_canonical(reveal.content)) if all(checks.values()) else None
        checks["independent_verdict_is_pass"] = bool(ev) and ev.verdict == "PASS"
        checks["report_hash_matches_chain"] = bool(ev) and ev.report_hash == passed["report_hash"]
        deliverable = self.api.request("GET", f"/jobs/{job_id}/deliverable")
        ok = all(checks.values()) and deliverable.status_code == 200
        stem = _out(job_id, st)
        if ok:
            (self.outbox / f"{stem}.cleaned.csv").write_bytes(deliverable.content)
        st.update(done=True, verified=ok)
        self._receipt(stem, {"job_id": job_id, "result": "passed" if ok else "passed_but_verification_failed",
                             "checks": checks, "job": _summ(j)})
        return {"job_id": job_id, "action": "accepted" if ok else "verification_failed", "checks": checks}

    # ------------------------------------------------------------------ helpers
    def _move(self, path: Path, sub: str) -> None:
        dest = self.inbox / sub / path.name
        if dest.exists():
            dest = dest.with_name(f"{path.stem}.{int(self.clock())}{path.suffix}")
        shutil.move(str(path), dest)

    def _receipt(self, stem: str, body: dict) -> None:
        (self.outbox / f"{stem}.receipt.json").write_text(json.dumps(body, indent=2, default=str) + "\n")

    def _save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2) + "\n")
        tmp.replace(self.state_path)  # atomic: a crash never leaves a half-written state file


def _out(job_id: int, st: dict) -> str:
    """Outputs are named by job, so two jobs from files with the same name never overwrite each other."""
    return f"job{job_id}-{Path(st['file']).stem}"


def _summ(j: dict) -> dict:
    return {k: j[k] for k in ("job_id", "status", "provider_id", "amount", "work_hash", "canary_commitment",
                              "attempts_used", "verdicts")}


def main() -> None:
    from .common import AgentChain, ApiClient, load_env_file

    from kavren_api.chain import quiet_rpc_race_noise

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    quiet_rpc_race_noise()
    env = {**load_env_file(os.environ.get("BUYER_KEY_FILE", "~/.kavren/buyer-agent.env")), **os.environ}
    acct, key = env["BUYER_AGENT_ACCOUNT_ID"], env["BUYER_AGENT_FC_PRIVATE_KEY"]
    chain = AgentChain(env.get("NEAR_RPC_URL", ""), env["KAVREN_CONTRACT_ID"], acct, key)
    api = ApiClient(env.get("KAVREN_API_URL", "https://kavren.onrender.com"), acct, key)
    agent = BuyerAgent(acct, env["BUYER_OWNER_ACCOUNT_ID"], chain, api,
                       Path(env.get("INBOX_DIR", "./inbox")).expanduser(), Path(env.get("OUTBOX_DIR", "./outbox")).expanduser())
    log.info("buyer agent %s for owner %s watching %s", acct, agent.owner_id, agent.inbox)
    while True:
        try:
            events = agent.tick()
        except Exception as e:  # a transient RPC/API error must never kill the agent
            log.warning("tick failed, will retry: %s", str(e)[:300])
            time.sleep(30)
            continue
        for ev in events:
            log.info("%s", ev)
        time.sleep(15)


if __name__ == "__main__":
    main()
