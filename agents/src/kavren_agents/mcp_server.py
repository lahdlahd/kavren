"""KAVREN for any AI agent: a buyer-side MCP server.

An owner funds a budget for an agent account and sets its rules on-chain (owner
console). After that, any MCP-capable agent can use these tools to hire a provider,
wait for the evaluator's verdict, check the work itself and accept it, or take the
refund, with no human approving each step. The contract enforces the owner's rules;
these tools cannot fund, withdraw or change rules.

The server runs on the agent's own machine and signs with the agent's restricted
function-call key (create_job and refund_job only, no deposits), read from a local
key file. The key never leaves the machine.

    kavren-mcp        (stdio; configure it in Claude Desktop, Claude Code or any MCP client)

Settings (environment):
    KAVREN_AGENT_KEY_FILE  key file, default ~/.kavren/claude-buyer.env, containing
                           BUYER_AGENT_ACCOUNT_ID, BUYER_AGENT_FC_PRIVATE_KEY,
                           KAVREN_CONTRACT_ID, BUYER_OWNER_ACCOUNT_ID
    KAVREN_API_URL         default https://kavren.onrender.com
    KAVREN_WEB_URL         proof pages, default = KAVREN_API_URL
    KAVREN_AGENT_STATE_DIR job state and accepted files, default ~/.kavren-state/<account>
"""
from __future__ import annotations

import csv
import hashlib
import io
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

from .buyer import BuyerAgent, _out

log = logging.getLogger("kavren.mcp")

MAX_INPUT_BYTES = 5 * 1024 * 1024  # the API's upload limit
DEFAULT_API = "https://kavren.onrender.com"


def _near(yocto: str | int) -> str:
    v = int(yocto)
    whole, frac = divmod(v, 10**24)
    f = f"{frac:024d}"[:4].rstrip("0")
    return f"{whole}.{f}" if f else str(whole)


class BuyerTools:
    """The tool logic, independent of MCP so it can be tested directly."""

    def __init__(self, agent: BuyerAgent, web_url: str):
        self.a, self.web = agent, web_url.rstrip("/")

    # ------------------------------------------------------------ inputs
    def _input(self, file_path: Optional[str], csv_text: Optional[str]) -> tuple[str, bytes]:
        if bool(file_path) == bool(csv_text):
            raise ValueError("give exactly one of file_path or csv_text")
        if file_path:
            p = Path(file_path).expanduser()
            if not p.is_file():
                raise ValueError(f"no such file: {p}")
            if p.stat().st_size > MAX_INPUT_BYTES:
                raise ValueError("file is larger than the 5 MB limit")
            data = p.read_bytes()
        else:
            data = csv_text.encode("utf-8")
            if not data.endswith(b"\n"):
                data += b"\n"
            if len(data) > MAX_INPUT_BYTES:
                raise ValueError("text is larger than the 5 MB limit")
        # one stable name per content: a retried hire of the same data is recognised, not repeated
        name = f"mcp-{hashlib.sha256(data).hexdigest()[:12]}.csv"
        return name, data

    def _proof(self, job_id: int) -> str:
        return f"{self.web}/job/?id={job_id}"

    # ------------------------------------------------------------ tools
    def budget(self) -> dict:
        b = self.a.chain.view("get_budget", {"owner_id": self.a.owner_id, "agent_id": self.a.account_id})
        if not b:
            return {"agent": self.a.account_id, "owner": self.a.owner_id, "funded": False,
                    "next_step": "The owner has not funded a budget for this agent yet (owner console)."}
        p = b["policy"]
        return {
            "agent": self.a.account_id, "owner": self.a.owner_id, "funded": True,
            "balance_near": _near(b["balance"]), "jobs_running": b["open_jobs"], "revoked": b["revoked"],
            "rules": {
                "max_per_job_near": _near(p["max_per_job"]), "max_jobs_at_once": p["max_open_jobs"],
                "allowed_providers": p["allowed_providers"], "allowed_job_types": p["allowed_templates"],
                "max_attempts": p["max_attempts"],
                "expires_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(p["expires_at"])),
            },
        }

    def quote(self, file_path: Optional[str] = None, csv_text: Optional[str] = None) -> dict:
        name, data = self._input(file_path, csv_text)
        d = self.a.decide(name, data)
        out = {"can_hire_now": d["action"] == "hire"}
        if d["action"] == "hire":
            out.update(job_type=d["template_id"], price_near=_near(d["price_yocto"]),
                       provider=d["provider_id"], max_attempts=d["max_attempts"],
                       note="Nothing has been spent. Call kavren_hire with the same input to hire.")
        else:
            out.update(reason=d["reason"], retry_later=d["action"] == "wait")
        return out

    def hire(self, file_path: Optional[str] = None, csv_text: Optional[str] = None) -> dict:
        name, data = self._input(file_path, csv_text)
        recovered = self.a._recover_pending(name)
        if recovered:
            out = recovered
        else:
            out = self.a.hire_bytes(name, data)
        self.a._save()
        if out["action"] in ("hired", "recovered"):
            jid = out["job_id"]
            res = {"hired": True, "job_id": jid, "proof_page": self._proof(jid),
                   "next_step": "The provider now works on it and the evaluator judges each submission. "
                                "Check progress with kavren_job_status (a PASS usually comes within a few minutes)."}
            if "price_yocto" in out:
                res.update(price_near=_near(out["price_yocto"]), provider=out["provider_id"], create_job_tx=out["tx"])
            return res
        return {"hired": False, "reason": out.get("reason", out["action"]), "retry_later": out["action"] == "wait"}

    def job_status(self, job_id: int) -> dict:
        st = self.a.state["jobs"].get(str(job_id))
        if st and not st.get("registered", True):
            self.a._register(job_id)
            self.a._save()
        j = self.a.chain.view("get_job", {"job_id": job_id})
        if not j:
            return {"job_id": job_id, "exists": False}
        mine = j["buyer_agent"] == self.a.account_id
        now = int(self.a.clock())
        refundable = ((j["status"] == "Open" and (now >= j["deadline"] or j["attempts_used"] >= j["max_attempts"]))
                      or (j["status"] == "Submitted" and now >= j["submitted_at"] + j["eval_window_secs"]))
        if j["status"] == "Passed":
            nxt = ("Call kavren_accept: it re-checks the work independently before accepting it."
                   if not (st or {}).get("done") else "Already accepted.")
        elif refundable:
            nxt = "No PASS is possible any more. Call kavren_refund to return the escrow to the budget."
        elif j["status"] == "Submitted":
            nxt = (f"The evaluator is judging submission {j['submission_seq']}; it has "
                   f"{max(0, j['submitted_at'] + j['eval_window_secs'] - now) // 60} minutes. Check again shortly.")
        elif j["status"] == "Open":
            nxt = (f"Waiting for the provider ({j['attempts_used']} of {j['max_attempts']} attempts used). "
                   "Check again in a minute or two.")
        else:
            nxt = "Final: refunded to the owner's budget."
        return {
            "job_id": job_id, "exists": True, "created_by_this_agent": mine, "status": j["status"],
            "price_near": _near(j["amount"]), "provider": j["provider_id"],
            "attempts_used": j["attempts_used"], "max_attempts": j["max_attempts"],
            "verdicts": [{"submission": v["seq"], "verdict": v["verdict"]} for v in j["verdicts"]],
            "refundable_now": refundable, "next_step": nxt, "proof_page": self._proof(job_id),
        }

    def accept(self, job_id: int) -> dict:
        j = self.a.chain.view("get_job", {"job_id": job_id})
        if not j or j["buyer_agent"] != self.a.account_id:
            return {"accepted": False, "reason": "not a job created by this agent"}
        if j["status"] != "Passed":
            return {"accepted": False, "reason": f"job is {j['status']}, not Passed"}
        st = self._adopt(j)
        if st.get("done") and st.get("verified"):
            return self._accepted_result(job_id, st, already=True)
        ev = self.a._accept(job_id, st, j)
        self.a._save()
        if ev is None:
            return {"accepted": False, "retry_later": True,
                    "reason": "the evaluator has not published the canary set yet; try again in a minute"}
        if ev["action"] != "accepted":
            return {"accepted": False, "checks": ev["checks"],
                    "reason": "independent re-check FAILED: the PASS on-chain does not match the files. "
                              "Do not rely on this output; report it to the owner."}
        return self._accepted_result(job_id, st, checks=ev["checks"])

    def _accepted_result(self, job_id: int, st: dict, checks: Optional[dict] = None, already: bool = False) -> dict:
        path = self.a.outbox / f"{_out(job_id, st)}.cleaned.csv"
        rows = list(csv.reader(io.StringIO(path.read_text()))) if path.exists() else []
        return {"accepted": True, "already_accepted": already, "independent_checks": checks,
                "cleaned_file": str(path), "rows": max(0, len(rows) - 1), "preview": rows[:6],
                "proof_page": self._proof(job_id)}

    def refund(self, job_id: int) -> dict:
        s = self.job_status(job_id)
        if not s["exists"] or not s["created_by_this_agent"]:
            return {"refunded": False, "reason": "not a job created by this agent"}
        if not s["refundable_now"]:
            return {"refunded": False, "reason": f"no refund condition holds yet ({s['status']}). {s['next_step']}"}
        j = self.a.chain.view("get_job", {"job_id": job_id})
        st = self._adopt(j)
        ev = self.a._refund(job_id, st, "requested by the agent")
        self.a._save()
        return {"refunded": True, "amount_near": _near(j["amount"]), "refund_tx": ev["tx"],
                "proof_page": self._proof(job_id)}

    def _adopt(self, j: dict) -> dict:
        key = str(j["job_id"])
        if key not in self.a.state["jobs"]:
            self.a._track(j["job_id"], f"job{j['job_id']}.csv", j["draft_id"], j["amount"], adopted=True)
        return self.a.state["jobs"][key]


INSTRUCTIONS = """KAVREN lets you hire another AI agent and pay only for work that passes verification.
Your owner has funded a budget and set rules on-chain (NEAR testnet); you act within them.
Typical flow: kavren_budget -> kavren_quote -> kavren_hire -> kavren_job_status (repeat every
minute or two) -> kavren_accept when the job has Passed, or kavren_refund when it says refundable.
Supported job type today: cleaning contact lists (CSV with full_name,email,signup_date)."""


def build_server(tools: BuyerTools):
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations

    s = MCPServer(name="kavren", instructions=INSTRUCTIONS, version="0.1.0")
    ro = ToolAnnotations(read_only_hint=True, open_world_hint=True)
    spend = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True)

    @s.tool(name="kavren_budget", annotations=ro,
            description="Show this agent's budget on KAVREN: balance, jobs running, and the owner's rules "
                        "(max price per job, allowed providers and job types, expiry).")
    def kavren_budget() -> dict:
        return tools.budget()

    @s.tool(name="kavren_quote", annotations=ro,
            description="Check a CSV against the owner's rules and get its job type and price, without spending. "
                        "Give file_path (a file on this machine) or csv_text.")
    def kavren_quote(file_path: Optional[str] = None, csv_text: Optional[str] = None) -> dict:
        return tools.quote(file_path, csv_text)

    @s.tool(name="kavren_hire", annotations=spend,
            description="Hire the allowed provider agent to clean this CSV. Locks the price in on-chain escrow "
                        "from the budget; the provider is paid only if the evaluator passes its work. "
                        "Retrying with the same input never hires twice.")
    def kavren_hire(file_path: Optional[str] = None, csv_text: Optional[str] = None) -> dict:
        return tools.hire(file_path, csv_text)

    @s.tool(name="kavren_job_status", annotations=ro,
            description="Status of a KAVREN job read from the chain, with verdicts so far and what to do next.")
    def kavren_job_status(job_id: int) -> dict:
        return tools.job_status(job_id)

    @s.tool(name="kavren_accept", annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True),
            description="For a Passed job: re-run the evaluation independently (files must match the on-chain "
                        "hashes and the verdict must reproduce), then save and return the cleaned file.")
    def kavren_accept(job_id: int) -> dict:
        return tools.accept(job_id)

    @s.tool(name="kavren_refund", annotations=spend,
            description="Return a job's escrow to the owner's budget. Only works when the contract allows a "
                        "refund (deadline passed, attempts used up, or the evaluator missed its window).")
    def kavren_refund(job_id: int) -> dict:
        return tools.refund(job_id)

    return s


def make_tools() -> BuyerTools:
    from kavren_api.chain import quiet_rpc_race_noise

    from .common import AgentChain, ApiClient, load_env_file

    quiet_rpc_race_noise()
    env = {**load_env_file(os.environ.get("KAVREN_AGENT_KEY_FILE", "~/.kavren/claude-buyer.env")), **os.environ}
    acct, key = env["BUYER_AGENT_ACCOUNT_ID"], env["BUYER_AGENT_FC_PRIVATE_KEY"]
    api_url = env.get("KAVREN_API_URL") or DEFAULT_API
    state = Path(env.get("KAVREN_AGENT_STATE_DIR") or Path.home() / ".kavren-state" / acct).expanduser()
    agent = BuyerAgent(acct, env["BUYER_OWNER_ACCOUNT_ID"], AgentChain(env.get("NEAR_RPC_URL", ""),
                       env["KAVREN_CONTRACT_ID"], acct, key), ApiClient(api_url, acct, key),
                       state / "inbox-unused", state / "outbox")
    return BuyerTools(agent, env.get("KAVREN_WEB_URL") or api_url)


def main() -> None:
    # stdout carries the MCP protocol: all logging goes to stderr
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(asctime)s %(name)s %(message)s")
    build_server(make_tools()).run("stdio")


if __name__ == "__main__":
    main()
