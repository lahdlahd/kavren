"""The provider agent: an LLM (NEAR AI Cloud) plans; deterministic tools execute.

Loop:
  1. find OPEN jobs assigned to this provider on-chain;
  2. ask the API whether the on-chain job matches the protocol draft; download the
     work file and check its hash against the chain before doing anything;
  3. plan (LLM) -> execute (tools) -> run the PUBLIC checks C1-C8 locally; on a
     violation, give the codes back to the LLM and revise (max LOCAL_REVISIONS);
  4. submit_work(hash) on-chain with its function-call key, then upload the file;
  5. if the evaluator returns FAIL, read the report (canaries are never named),
     re-plan and resubmit while attempts and time remain;
  6. on PASS, withdraw its pay.

Run: kavren-provider  (reads ~/.kavren/provider-agent.env and ~/.kavren/near-ai.env)
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Callable

from kavren_core.canonical import sha256_hex
from kavren_core.evaluator import evaluate
from kavren_core.spec import load_spec

from .cleaning import PlanError, execute
from .planner import Planner

log = logging.getLogger("kavren.provider")
LOCAL_REVISIONS = 3
MIN_TIME_LEFT = 120  # don't start an attempt with less than 2 minutes to the deadline


class ProviderAgent:
    def __init__(self, account_id: str, chain, api, planner: Planner, clock: Callable[[], float] = time.time,
                 log_dir: Path | None = None, on_progress: Callable[[], None] | None = None,
                 state_dir: Path | None = None):
        self.on_progress = on_progress
        # Submissions are written here BEFORE submit_work, so a file whose upload fails
        # (API lag, crash, restart) can still be delivered while the job is Submitted.
        self.pending_dir = Path(state_dir or Path.home() / ".kavren-state" / "provider")
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.account_id, self.chain, self.api, self.planner, self.clock = account_id, chain, api, planner, clock
        self.usage: list[dict] = []
        self._plan_ids: list[str] = []
        self.log_dir = log_dir
        self.waiting: dict[int, int] = {}  # job_id -> seq we are waiting on
        self.gave_up: set[tuple[int, int]] = set()
        self.paid_seen: set[int] = set()  # passed jobs whose payment we already checked  # (job_id, verdict count): never re-spend on the same state

    def tick(self) -> list[dict]:
        events = []
        passed_now = False
        for j in self.chain.view("get_jobs_for_provider", {"provider_id": self.account_id}):
            if j["status"] == "Passed" and j["job_id"] not in self.paid_seen:
                passed_now = True
            ev = self.handle(j)
            if ev:
                events.append(ev)
        if not passed_now:
            return events
        claimable = int(self.chain.view("get_claimable", {"account_id": self.account_id}))
        for j in self.chain.view("get_jobs_for_provider", {"provider_id": self.account_id}):
            if j["status"] == "Passed":
                self.paid_seen.add(j["job_id"])
        if claimable > 0:
            res = self.chain.call("withdraw", {})
            events.append({"action": "withdrew", "amount_yocto": str(claimable), "tx": res["tx"]})
        return events

    def handle(self, j: dict) -> dict | None:
        jid = j["job_id"]
        if j["status"] == "Submitted":
            return self._deliver_pending(j)  # usually a no-op: the evaluator is working
        if j["status"] != "Open":
            self.waiting.pop(jid, None)
            return None
        now = int(self.clock())
        if j["attempts_used"] >= j["max_attempts"] or now + MIN_TIME_LEFT >= j["deadline"]:
            return None
        state = (jid, len(j["verdicts"]))
        if state in self.gave_up:
            return None
        check = self.api.public("GET", f"/jobs/{jid}/check").json()
        if not check.get("matches_draft"):
            self.gave_up.add(state)
            return {"job_id": jid, "action": "refused", "reason": check.get("reason", "job does not match draft")}
        work = self.api.request("GET", f"/jobs/{jid}/work").content
        if sha256_hex(work) != j["work_hash"]:
            return {"job_id": jid, "action": "refused", "reason": "work file hash differs from the chain"}
        spec = load_spec(j["spec_json"].encode())

        feedback = self._report_feedback(jid, j) if j["verdicts"] else None
        sub, local = self._plan_until_clean(spec, work, feedback)
        if sub is None:
            self.gave_up.add(state)
            return {"job_id": jid, "action": "gave_up", "reason": "no plan passed the public checks", "local": local}
        h = sha256_hex(sub)
        pending = self._pending_path(jid, h)
        pending.write_bytes(sub)
        prov = self._provenance()
        pending.with_suffix(".json").write_text(json.dumps(prov))
        res = self.chain.call("submit_work", {"job_id": jid, "output_hash": h})
        self.waiting[jid] = res["value"]
        up = self._upload(jid, sub, after_write=True, provenance=prov)
        return {"job_id": jid, "action": "submitted", "seq": res["value"], "output_hash": h, "tx": res["tx"],
                "uploaded": up, "local_revisions": len(local) - 1}

    def _pending_path(self, jid: int, h: str) -> Path:
        return self.pending_dir / f"job{jid}-{h}.csv"

    def _provenance(self) -> dict:
        """What produced this submission, as the provider reports it: the model and service the planner
        called, and the completion ids of the plan calls behind this file. Shown as 'reported by the
        provider' unless the evaluator can verify it independently."""
        return {"model": getattr(self.planner, "last_served_model", None) or getattr(self.planner, "model", None),
                "service": getattr(self.planner, "base_url", None),
                "completion_ids": list(self._plan_ids)}

    def _upload(self, jid: int, sub: bytes, after_write: bool = False, provenance: dict | None = None) -> bool:
        kw = {"files": {"file": ("submission.csv", sub, "text/csv")}}
        if provenance and provenance.get("model"):
            kw["data"] = {"provenance": json.dumps(provenance)}
        url = f"/jobs/{jid}/submissions"
        r = self.api.request_after_write("POST", url, **kw) if after_write else self.api.request("POST", url, **kw)
        if r.status_code == 200:
            p = self._pending_path(jid, sha256_hex(sub))
            p.unlink(missing_ok=True)
            p.with_suffix(".json").unlink(missing_ok=True)
            return True
        log.warning("upload for job %s not accepted yet (%s); will retry", jid, r.status_code)
        return False

    def _deliver_pending(self, j: dict) -> dict | None:
        p = self._pending_path(j["job_id"], j["current_output_hash"] or "")
        if not p.exists():
            return None
        meta = p.with_suffix(".json")
        prov = json.loads(meta.read_text()) if meta.exists() else None
        if self._upload(j["job_id"], p.read_bytes(), provenance=prov):
            return {"job_id": j["job_id"], "action": "uploaded_pending", "output_hash": j["current_output_hash"]}
        return None

    def _plan_until_clean(self, spec, work: bytes, feedback):
        attempts = []
        self._plan_ids: list[str] = []
        for _ in range(LOCAL_REVISIONS + 1):
            try:
                if self.on_progress is not None:
                    plan, usage = self.planner.plan(spec, work.decode("utf-8"), feedback, on_progress=self.on_progress)
                else:
                    plan, usage = self.planner.plan(spec, work.decode("utf-8"), feedback)
                self.usage.append(usage)
                cid = getattr(self.planner, "last_completion_id", None)
                if cid:
                    self._plan_ids.append(cid)
                sub = execute(spec, work, plan)
            except (PlanError, ValueError, KeyError, TimeoutError) as e:
                feedback = [{"check": "PLAN", "code": "INVALID_PLAN", "record_id": None, "detail": str(e)[:300]}]
                attempts.append({"ok": False, "error": str(e)[:200]})
                continue
            ev = evaluate(spec, work, sub, None)  # PUBLIC checks only: the provider never has the canaries
            violations = [{"check": c["check"], "code": v["code"], "record_id": v["record_id"]}
                          for c in ev.report["checks"] for v in c["violations"]]
            attempts.append({"ok": not violations, "violations": len(violations)})
            if not violations:
                return sub, attempts
            feedback = violations[:200]
        return None, attempts

    def _report_feedback(self, jid: int, j: dict) -> list[dict]:
        last = j["verdicts"][-1]
        r = self.api.request("GET", f"/jobs/{jid}/reports/{last['seq']}")
        if r.status_code != 200:
            return []
        rep = r.json()["report"]
        fb = [{"check": c["check"], "code": v["code"], "record_id": v["record_id"]}
              for c in rep["checks"] for v in c["violations"]]
        if rep.get("canary_failures"):
            fb.append({"check": "H1", "code": "CONTROL_RECORDS_WRONG", "record_id": None,
                       "detail": f"{rep['canary_failures']} hidden control records were handled incorrectly; "
                                 "re-check every rejection reason and every duplicate decision"})
        return fb


def single_instance(state_dir: Path, account_id: str):
    """Refuse to start a second provider for the same account: two copies share one key and race each
    other's submissions (seen live on job 10). The OS releases the lock when the process exits."""
    state_dir.mkdir(parents=True, exist_ok=True)
    f = open(state_dir / f"{account_id}.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    f.write(str(os.getpid()))
    f.flush()
    return f


def main() -> None:
    from .common import AgentChain, ApiClient, load_env_file
    from .planner import DEFAULT_MODEL, NearAIPlanner

    from kavren_api.chain import quiet_rpc_race_noise

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    quiet_rpc_race_noise()
    env = {**load_env_file(os.environ.get("PROVIDER_KEY_FILE", "~/.kavren/provider-agent.env")),
           **load_env_file(os.environ.get("NEAR_AI_KEY_FILE", "~/.kavren/near-ai.env")), **os.environ}
    acct, key = env["PROVIDER_ACCOUNT_ID"], env["PROVIDER_FC_PRIVATE_KEY"]
    lock = single_instance(Path.home() / ".kavren-state" / "provider", acct)
    if lock is None:
        sys.exit(f"another kavren-provider is already running for {acct}; stop it first (pgrep -af kavren-provider)")
    chain = AgentChain(env.get("NEAR_RPC_URL", ""), env["KAVREN_CONTRACT_ID"], acct, key)
    api = ApiClient(env.get("KAVREN_API_URL", "https://kavren.onrender.com"), acct, key)
    planner = NearAIPlanner(env["NEAR_AI_API_KEY"], env.get("NEAR_AI_BASE_URL", "https://cloud-api.near.ai/v1"),
                            env.get("NEAR_AI_MODEL") or DEFAULT_MODEL)
    agent = ProviderAgent(acct, chain, api, planner)
    log.info("provider agent %s using model %s", acct, planner.model)
    while True:
        try:
            events = agent.tick()
        except Exception as e:  # a transient RPC/API error must never kill the agent
            log.warning("tick failed, will retry: %s", str(e)[:300])
            time.sleep(30)
            continue
        for ev in events:
            log.info("%s", json.dumps(ev, default=str))
        time.sleep(15)


if __name__ == "__main__":
    main()
