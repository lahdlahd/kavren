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

    # ------------------------------------------------------------------ main loop
    def tick(self) -> list[dict]:
        events = []
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
        data = path.read_bytes()
        decision = self.decide(path.name, data)
        if decision["action"] == "wait":
            return decision  # keep the file in the inbox; try again on a later tick
        if decision["action"] != "hire":
            self._move(path, "processed")
            self._receipt(path.stem, decision)
            return decision
        r = self.api.request("POST", "/drafts", files={"file": (path.name, data, "text/csv")},
                             data={"template_id": decision["template_id"]})
        if r.status_code != 200:
            out = {"file": path.name, "action": "skip", "reason": f"draft refused: {r.json().get('detail')}"}
            self._move(path, "processed")
            self._receipt(path.stem, out)
            return out
        draft = r.json()
        price = int(draft["price_yocto"])
        if price != decision["price_yocto"]:
            return {"file": path.name, "action": "skip", "reason": "price mismatch between agent and protocol"}
        args = {"args": {
            "owner_id": self.owner_id, "provider_id": decision["provider_id"], "amount": str(price),
            "template_id": draft["template_id"], "spec_json": draft["spec_json"], "draft_id": draft["draft_id"],
            "input_hash": draft["input_hash"], "work_hash": draft["work_hash"],
            "canary_commitment": draft["canary_commitment"],
            "deadline": int(self.clock()) + DEFAULT_DEADLINE_SECS, "max_attempts": decision["max_attempts"],
        }}
        res = self.chain.call("create_job", args)
        job_id = int(res["value"])
        self.api.request("POST", f"/jobs/{job_id}/register").raise_for_status()
        self.state["jobs"][str(job_id)] = {"file": path.name, "draft_id": draft["draft_id"], "create_tx": res["tx"],
                                           "status": "Open", "price_yocto": str(price)}
        self._move(path, "processed")
        out = {"file": path.name, "action": "hired", "job_id": job_id, "provider_id": decision["provider_id"],
               "price_yocto": str(price), "tx": res["tx"]}
        log.info("hired: %s", out)
        return out

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
        j = self.chain.view("get_job", {"job_id": job_id})
        st["status"] = j["status"]
        now = int(self.clock())
        if j["status"] == "Open" and (now >= j["deadline"] or j["attempts_used"] >= j["max_attempts"]):
            return self._refund(job_id, st, "deadline passed" if now >= j["deadline"] else "attempts exhausted")
        if j["status"] == "Submitted" and now >= j["submitted_at"] + j["eval_window_secs"]:
            return self._refund(job_id, st, "evaluator did not answer in time")
        if j["status"] == "Refunded":
            st["done"] = True
            self._receipt(Path(st["file"]).stem, {"job_id": job_id, "result": "refunded", "job": _summ(j)})
            return {"job_id": job_id, "action": "closed", "result": "refunded"}
        if j["status"] == "Passed":
            return self._accept(job_id, st, j)
        return None

    def _refund(self, job_id: int, st: dict, why: str) -> dict:
        res = self.chain.call("refund_job", {"job_id": job_id})
        st.update(status="Refunded", refund_tx=res["tx"], done=True)
        self._receipt(Path(st["file"]).stem, {"job_id": job_id, "result": "refunded", "reason": why, "tx": res["tx"]})
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
        stem = Path(st["file"]).stem
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
        self.state_path.write_text(json.dumps(self.state, indent=2) + "\n")


def _summ(j: dict) -> dict:
    return {k: j[k] for k in ("job_id", "status", "provider_id", "amount", "work_hash", "canary_commitment",
                              "attempts_used", "verdicts")}


def main() -> None:
    from .common import AgentChain, ApiClient, load_env_file

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    env = {**load_env_file(os.environ.get("BUYER_KEY_FILE", "~/.kavren/buyer-agent.env")), **os.environ}
    acct, key = env["BUYER_AGENT_ACCOUNT_ID"], env["BUYER_AGENT_FC_PRIVATE_KEY"]
    chain = AgentChain(env.get("NEAR_RPC_URL", ""), env["KAVREN_CONTRACT_ID"], acct, key)
    api = ApiClient(env.get("KAVREN_API_URL", "http://127.0.0.1:8000"), acct, key)
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
