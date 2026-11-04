"""KAVREN attack gallery: a dishonest provider against the REAL deployed protocol.

What it does (testnet only):
  * opens its own isolated jobs with the buyer agent's function-call key (same API
    draft + create_job path the buyer agent uses), so earlier jobs are untouched;
  * plays a cheating provider with the provider agent's function-call key: six kinds
    of dishonest work, each submitted on-chain and uploaded, judged by the running
    evaluator worker;
  * tries protocol attacks: wrong uploads, wrong-account contract calls, a stale
    verdict replay (with the evaluator's own key), a submission after the attempts
    are used up;
  * checks every outcome automatically against what the protocol promises, and
    reads each rejected transaction back from the chain;
  * refunds its jobs at the end and writes docs/ATTACKS.md from what actually happened.

Before running: the API and the evaluator worker must be running; STOP the honest
provider agent (it signs as the same account and would work on these jobs) and the
buyer agent. Nothing here can move mainnet funds: every key is a testnet
function-call key that cannot attach a deposit.

Run (from ~/kavren):
  .venv/bin/python scripts/attack_gallery.py
"""
from __future__ import annotations

import csv
import io
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "kavren_core" / "tests"))  # the reference (honest) cleaner

from kavren_core.canonical import canonical_bytes, sha256_hex  # noqa: E402
from kavren_core.evaluator import evaluate  # noqa: E402
from kavren_core.spec import load_spec  # noqa: E402
from reference_cleaner import clean  # noqa: E402

INPUT = ROOT / "fixtures" / "contacts" / "input.csv"
TEMPLATE = "contacts-v1"
EXPLORER = "https://testnet.nearblocks.io/txns/"
# the runtime's answer when a function-call key calls a method outside its allowlist
PRE_EXECUTION = ("MethodNameMismatch", "InvalidAccessKeyError")
KEY_SCOPE = ("MethodNameMismatch", "InvalidAccessKeyError")
ACCESS_KEY = "Access key (NEAR runtime)"


# ---------------------------------------------------------------- dishonest submissions
def _rows(sub: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(sub.decode())))


def _csv(rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue().encode()


def _free_kept(rows: list[list[str]]) -> list[list[str]]:
    """Kept rows that no other row is merged into (safe to tamper with in isolation)."""
    targets = {r[2] for r in rows[1:] if r[1] == "merged"}
    return [r for r in rows[1:] if r[1] == "kept" and r[0] not in targets]


def attack_empty(spec, work: bytes) -> bytes:
    return b""


def attack_deleted_rows(spec, work: bytes) -> bytes:
    rows = _rows(clean(spec, work))
    return _csv(rows[:-3])  # silently drop three records


def attack_made_up_value(spec, work: bytes) -> bytes:
    rows = _rows(clean(spec, work))
    i = rows[0].index("email")
    victim = _free_kept(rows)[0]
    victim[i] = "verified.lead@premium-contacts.com"  # valid, plausible, and not in the input
    return _csv(rows)


def attack_false_rejection(spec, work: bytes) -> bytes:
    rows = _rows(clean(spec, work))
    for r in _free_kept(rows)[:2]:  # two good records thrown away as "invalid email"
        r[1:] = ["rejected", "", "INVALID_EMAIL"] + [""] * len(spec.columns)
    return _csv(rows)


def attack_fake_merge(spec, work: bytes) -> bytes:
    rows = _rows(clean(spec, work))
    a, b = _free_kept(rows)[:2]  # two different people
    a[1:] = ["merged", b[0], ""] + [""] * len(spec.columns)
    return _csv(rows)


def attack_canary_blind(spec, work: bytes) -> bytes:
    """Upper-cases every text value of kept rows. Whitespace-normalised and case-insensitively
    equal to the input, so every PUBLIC check (C1-C8) passes; only the hidden control
    records, which require the exact normalised value, can catch it."""
    rows = _rows(clean(spec, work))
    text_cols = [rows[0].index(c.name) for c in spec.columns if c.type == "text"]
    for r in rows[1:]:
        if r[1] == "kept":
            for i in text_cols:
                r[i] = r[i].upper()
    return _csv(rows)


@dataclass
class FileAttack:
    key: str
    title: str
    build: Callable
    expect_check: str
    expect_code: Optional[str]  # None: only H1 may fail (public checks must all pass)
    layer: str = "Evaluator"


FILE_ATTACKS = [
    FileAttack("empty", "Empty file", attack_empty, "C1", "BAD_DIALECT"),
    FileAttack("deleted", "Silently deleted rows", attack_deleted_rows, "C2", "MISSING_RECORD"),
    FileAttack("made_up", "Made-up value (invented email)", attack_made_up_value, "C7", "NOT_DERIVED_FROM_INPUT"),
    FileAttack("false_reject", "False rejection reason (good rows rejected as INVALID_EMAIL)",
               attack_false_rejection, "C2", "REASON_NOT_TRUE"),
    FileAttack("fake_merge", "Fake merge (two different people merged)", attack_fake_merge, "C2", "MERGE_KEY_MISMATCH"),
    FileAttack("canary", "Passes every public check, mishandles hidden control records",
               attack_canary_blind, "H1", None, "Evaluator (hidden canaries)"),
]


# ---------------------------------------------------------------- results
@dataclass
class Result:
    n: int
    title: str
    layer: str
    expected: str
    actual: str
    ok: bool
    job_id: Optional[int] = None
    txs: list[tuple[str, str]] = field(default_factory=list)  # (label, hash)


class Gallery:
    def __init__(self, *, owner_id: str, buyer_id: str, buyer_chain, buyer_api, provider_id: str,
                 provider_chain, provider_api, evaluator_chain, wait: Callable[[], None],
                 tx_failure: Callable[[str, str], Optional[str]] = lambda h, sender: None,
                 clock: Callable[[], float] = time.time, verdict_timeout: float = 900,
                 log: Callable[[str], None] = print):
        self.owner_id, self.buyer_id, self.provider_id = owner_id, buyer_id, provider_id
        self.bc, self.bapi, self.pc, self.papi, self.ec = buyer_chain, buyer_api, provider_chain, provider_api, evaluator_chain
        self.wait, self.tx_failure, self.clock, self.timeout, self.log = wait, tx_failure, clock, verdict_timeout, log
        self.results: list[Result] = []
        self.jobs: list[int] = []
        self.money: dict = {}

    # ------------------------------------------------------------ helpers
    def _record(self, title, layer, expected, actual, ok, job_id=None, txs=()):
        r = Result(len(self.results) + 1, title, layer, expected, actual, ok, job_id, list(txs))
        self.results.append(r)
        self.log(f"[{'OK  ' if ok else 'MISS'}] {r.n}. {title}: {actual}")
        return r

    def _expect_rejected(self, title, layer, chain, sender, method, args, must_contain, job_id=None):
        """`must_contain`: a phrase (or tuple of alternatives) the rejection must contain.
        Layer "Access key" means the NEAR runtime refuses the transaction before execution
        because the signing function-call key is not allowed to call that method."""
        wanted = must_contain if isinstance(must_contain, tuple) else (must_contain,)
        expected = (f"refused before execution: key not allowed to call `{method}`" if layer.startswith("Access key")
                    else f"contract panics: “{wanted[0]}”")
        try:
            res = chain.call(method, args)
        except Exception as e:
            text, tx = str(e), getattr(e, "tx", None)
            pre_exec = any(m in text for m in PRE_EXECUTION)
            onchain = None if pre_exec or not tx else self.tx_failure(tx, sender)
            if onchain is not None:
                text = onchain  # the failure exactly as the chain recorded it
            hit = next((w for w in wanted if w in text), None)
            where = ("refused before execution, no transaction on-chain" if pre_exec
                     else "rejected on-chain" if onchain is not None else "rejected")
            return self._record(title, layer, expected, f"{where}: “{hit or text[:160]}”", hit is not None, job_id,
                                [("rejected tx", tx)] if onchain is not None else [])
        return self._record(title, layer, expected, f"UNEXPECTEDLY SUCCEEDED (tx {res['tx']})", False, job_id,
                            [("tx", res["tx"])])

    def _job(self, job_id):
        return self.bc.view("get_job", {"job_id": job_id})

    def _budget(self):
        return self.bc.view("get_budget", {"owner_id": self.owner_id, "agent_id": self.buyer_id})

    def _open_job(self) -> tuple[int, str, bytes, dict]:
        data = INPUT.read_bytes()
        r = self.bapi.request("POST", "/drafts", files={"file": ("attack-gallery.csv", data, "text/csv")},
                              data={"template_id": TEMPLATE})
        r.raise_for_status()
        d = r.json()
        policy = self._budget()["policy"]
        args = {"args": {
            "owner_id": self.owner_id, "provider_id": self.provider_id, "amount": d["price_yocto"],
            "template_id": d["template_id"], "spec_json": d["spec_json"], "draft_id": d["draft_id"],
            "input_hash": d["input_hash"], "work_hash": d["work_hash"], "canary_commitment": d["canary_commitment"],
            "deadline": int(self.clock()) + 3600, "max_attempts": min(3, policy["max_attempts"]),
        }}
        res = self.bc.call("create_job", args)
        job_id = int(res["value"])
        self.bapi.request_after_write("POST", f"/jobs/{job_id}/register").raise_for_status()
        self.jobs.append(job_id)
        work = self.papi.request_after_write("GET", f"/jobs/{job_id}/work").content
        j = self._job(job_id)
        if sha256_hex(work) != j["work_hash"]:
            raise RuntimeError("work file does not match the on-chain hash")
        self.log(f"opened isolated job {job_id} (tx {res['tx']}), price {int(d['price_yocto']) / 1e24:.3f} NEAR")
        return job_id, res["tx"], work, args["args"]

    def _upload(self, job_id: int, data: bytes):
        return self.papi.request_after_write("POST", f"/jobs/{job_id}/submissions",
                                             files={"file": ("submission.csv", data, "text/csv")})

    def _await_verdict(self, job_id: int, n_before: int) -> dict:
        start = self.clock()
        while True:
            self.wait()
            j = self._job(job_id)
            if len(j["verdicts"]) > n_before:
                return j
            if self.clock() - start > self.timeout:
                raise TimeoutError(f"no verdict for job {job_id}; is the evaluator worker running?")

    # ------------------------------------------------------------ the attacks
    def run_file_attack(self, job_id: int, work: bytes, spec, fa: FileAttack, *, wrong_upload: bool = False) -> dict:
        sub = fa.build(spec, work)
        h = sha256_hex(sub)
        if fa.expect_code is None:  # pre-flight: make sure the canary attack really is publicly clean
            local = evaluate(spec, work, sub, None)
            if local.verdict != "PASS":
                raise RuntimeError("canary attack file does not pass the public checks; fix the attack")
        before = self._job(job_id)
        res = self.pc.call("submit_work", {"job_id": job_id, "output_hash": h})
        if wrong_upload:
            self._wrong_upload(job_id, sub, h)
        up = self._upload(job_id, sub)
        if up.status_code != 200:
            raise RuntimeError(f"upload refused: {up.status_code} {up.text[:200]}")
        j = self._await_verdict(job_id, len(before["verdicts"]))
        v = j["verdicts"][-1]
        rep = self.papi.request("GET", f"/jobs/{job_id}/reports/{v['seq']}").json()["report"]
        hash_ok = sha256_hex(canonical_bytes(rep)) == v["report_hash"] and v["output_hash"] == h
        failing = {c["check"]: c for c in rep["checks"] if c["status"] == "fail"}
        skipped = [c["check"] for c in rep["checks"] if c["status"] == "skipped"]
        if fa.expect_code is None:
            ok = (v["verdict"] == "FAIL" and set(failing) == {"H1"} and rep["canary_failures"] > 0)
            actual = (f"{v['verdict']}; public checks C1–C8 all pass; H1 fails "
                      f"({rep['canary_failures']} hidden control records wrong, not named in the report)")
            expected = "FAIL on H1 only (C1–C8 pass)"
        else:
            codes = {x["code"] for x in failing.get(fa.expect_check, {}).get("violations", [])}
            ok = v["verdict"] == "FAIL" and fa.expect_code in codes
            actual = f"{v['verdict']}; failing: " + ", ".join(
                f"{c} ({', '.join(sorted({x['code'] for x in failing[c]['violations']})) or 'count only'})"
                for c in failing) + (f"; {skipped[0]}–{skipped[-1]} skipped" if skipped else "")
            expected = f"FAIL with {fa.expect_check} {fa.expect_code}"
        if not hash_ok:
            ok, actual = False, actual + " — REPORT HASH DOES NOT MATCH THE CHAIN"
        self._record(fa.title, fa.layer, expected, actual + "; report hash matches the chain" * hash_ok, ok, job_id,
                     [("submit", res["tx"])])
        return {"seq": v["seq"], "output_hash": h, "report_hash": v["report_hash"]}

    def _wrong_upload(self, job_id: int, sub: bytes, h: str) -> None:
        other = sub + b"\n" if sub else b"x\n"  # any other bytes
        for _ in range(6):  # an RPC lag can also give 409 ("awaiting"); wait for the real reason
            r = self.papi.request("POST", f"/jobs/{job_id}/submissions",
                                  files={"file": ("swapped.csv", other, "text/csv")})
            detail = r.json().get("detail", "") if r.headers.get("content-type", "").startswith("application/json") else r.text
            if "awaiting" not in detail:
                break
            time.sleep(3)
        self._record("Upload a different file than the hash committed on-chain", "API",
                     "409: file hash does not match the on-chain hash",
                     f"{r.status_code}: {detail}", r.status_code == 409 and "does not match" in detail, job_id)

    def run(self) -> list[Result]:
        b0 = self._budget()
        spec = None
        queue = list(FILE_ATTACKS)
        first = True
        while queue:
            job_id, create_tx, work, args = self._open_job()
            spec = load_spec(args["spec_json"].encode())
            self._record(f"Open isolated job {job_id}", "Setup", "job created", "Open", True, job_id,
                         [("create_job", create_tx)])
            if first:
                self._protocol_attacks_on_open_job(job_id, args)
            attempts = args["max_attempts"]
            for k in range(attempts):
                if not queue:
                    break
                fa = queue.pop(0)
                info = self.run_file_attack(job_id, work, spec, fa, wrong_upload=(fa.key == "made_up"))
                if first and k == 0:
                    self._replay(job_id, info)
            first = False
            self._exhaust_and_refund(job_id)
        self.money = self._money(b0, self._budget())
        return self.results

    def _protocol_attacks_on_open_job(self, job_id: int, args: dict) -> None:
        r = self.papi.request("POST", f"/jobs/{job_id}/submissions", files={"file": ("early.csv", b"a\n", "text/csv")})
        detail = r.json().get("detail", "")
        self._record("Upload a file with no on-chain submission", "API",
                     "409: no on-chain submission is awaiting a file", f"{r.status_code}: {detail}",
                     r.status_code == 409 and "awaiting" in detail, job_id)
        self._expect_rejected("Provider gives itself a PASS verdict", ACCESS_KEY, self.pc, self.provider_id,
                              "submit_verdict", {"job_id": job_id, "seq": 1, "output_hash": "a" * 64,
                                                 "verdict": "PASS", "report_hash": "b" * 64}, KEY_SCOPE, job_id)
        self._expect_rejected("Buyer agent submits work on its own job", ACCESS_KEY, self.bc, self.buyer_id,
                              "submit_work", {"job_id": job_id, "output_hash": "c" * 64}, KEY_SCOPE, job_id)
        self._expect_rejected("Provider spends the owner's budget (create_job)", ACCESS_KEY, self.pc,
                              self.provider_id, "create_job", {"args": dict(args, deadline=int(self.clock()) + 3600)},
                              KEY_SCOPE, job_id)
        self._expect_rejected("Buyer agent's key tries to withdraw the owner's budget", ACCESS_KEY, self.bc,
                              self.buyer_id, "withdraw_budget", {"agent_id": self.buyer_id, "amount": "1"},
                              KEY_SCOPE, job_id)
        self._expect_rejected("Buyer refunds early (deadline not reached, attempts left)", "Contract", self.bc,
                              self.buyer_id, "refund_job", {"job_id": job_id}, "no refund condition holds", job_id)
        if int(self.pc.view("get_claimable", {"account_id": self.provider_id})) == 0:
            self._expect_rejected("Provider withdraws pay without any PASS", "Contract", self.pc, self.provider_id,
                                  "withdraw", {}, "nothing to withdraw", job_id)
        else:
            self._record("Provider withdraws pay without any PASS", "Contract", "contract panics: “nothing to withdraw”",
                         "not run: the provider has earlier, legitimately earned pay unclaimed", False, job_id)

    def _replay(self, job_id: int, info: dict) -> None:
        self._expect_rejected("Replay the old verdict as PASS (evaluator key)", "Contract", self.ec,
                              getattr(self.ec, "account_id", "evaluator"), "submit_verdict",
                              {"job_id": job_id, "seq": info["seq"], "output_hash": info["output_hash"],
                               "verdict": "PASS", "report_hash": info["report_hash"]},
                              "no submission awaiting a verdict", job_id)

    def _exhaust_and_refund(self, job_id: int) -> None:
        j = self._job(job_id)
        if j["status"] == "Open" and j["attempts_used"] >= j["max_attempts"]:
            self._expect_rejected("Submit again after all attempts are used", "Contract", self.pc, self.provider_id,
                                  "submit_work", {"job_id": job_id, "output_hash": "d" * 64},
                                  "no attempts left", job_id)
            res = self.bc.call("refund_job", {"job_id": job_id})
            j = self._job(job_id)
            self._record(f"Refund job {job_id} (attempts exhausted, no PASS)", "Contract",
                         "Refunded; escrow back to the owner's budget", j["status"], j["status"] == "Refunded",
                         job_id, [("refund_job", res["tx"])])
        else:
            self._record(f"Refund job {job_id}", "Contract", "Refunded", f"not refundable yet: {j['status']}",
                         False, job_id)

    def _money(self, b0: dict, b1: dict) -> dict:
        jobs = [self._job(i) for i in self.jobs]
        escrowed = sum(int(j["amount"]) for j in jobs)
        refunded = sum(int(j["amount"]) for j in jobs if j["status"] == "Refunded")
        return {"escrowed": escrowed, "refunded": refunded,
                "paid_to_provider": sum(int(j["amount"]) for j in jobs if j["status"] == "Passed"),
                "budget_before": int(b0["balance"]), "budget_after": int(b1["balance"]),
                "kept_by_contract": int(b0["balance"]) - int(b1["balance"])}


def _short(text: str, needle: str) -> str:
    i = text.find(needle)
    return needle if i >= 0 else text[:160]


# ---------------------------------------------------------------- report
def write_report(path: Path, g: Gallery, run_at: str, contract_id: str, gas: dict | None) -> None:
    n_ok = sum(r.ok for r in g.results)
    lines = [
        "# Attack gallery: live results",
        "",
        f"Generated by `scripts/attack_gallery.py` on {run_at} against `{contract_id}` (NEAR testnet).",
        "Every row is an action that was really sent. The **Actual** column is what the API, the evaluator and",
        "the contract returned. Rejected transactions are read back from the chain.",
        "",
        f"**{n_ok} of {len(g.results)} outcomes matched what the protocol promises.**",
        "",
        "| # | Attack | Job | Stopped by | Expected | Actual | ✓ | Transactions |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in g.results:
        txs = " · ".join(f"[{lbl} {h[:8]}…]({EXPLORER}{h})" for lbl, h in r.txs if h) or "—"
        job = "—" if r.job_id is None else str(r.job_id)
        lines.append(f"| {r.n} | {r.title} | {job} | {r.layer} | {r.expected} | {r.actual} | {'✅' if r.ok else '❌'} | {txs} |")
    m = g.money
    near = lambda y: f"{y / 1e24:.4f} NEAR"  # noqa: E731
    lines += [
        "",
        "## Money",
        "",
        f"- Escrowed in the attack jobs: {near(m['escrowed'])}",
        f"- Refunded to the owner's budget: {near(m['refunded'])}",
        f"- Paid to the cheating provider: **{near(m['paid_to_provider'])}**",
        f"- Budget before / after: {near(m['budget_before'])} → {near(m['budget_after'])} "
        f"(difference {near(m['kept_by_contract'])}: per-job storage, kept by the contract)",
    ]
    if gas:
        lines.append(f"- Gas paid by the three agent accounts during the run, including the evaluator's verdict "
                     f"transactions: {near(gas['total'])} "
                     f"({', '.join(f'{k} {near(v)}' for k, v in gas['by_account'].items())})")
    lines += ["", "What each attack demonstrates, and which layer stops it, is explained in the README "
              "(section *Attack gallery*).", ""]
    path.write_text("\n".join(lines))


# ---------------------------------------------------------------- live entry point
def main() -> None:
    from kavren_agents.common import AgentChain, ApiClient, load_env_file
    from kavren_api.chain import NearChain, quiet_rpc_race_noise
    import logging

    logging.basicConfig(level=logging.WARNING)
    quiet_rpc_race_noise()
    buyer = load_env_file(os.environ.get("BUYER_KEY_FILE", "~/.kavren/buyer-agent.env"))
    prov = load_env_file(os.environ.get("PROVIDER_KEY_FILE", "~/.kavren/provider-agent.env"))
    ev = load_env_file(os.environ.get("EVALUATOR_KEY_FILE", "~/.kavren/evaluator.env"))
    rpc = os.environ.get("NEAR_RPC_URL", "")
    contract = buyer["KAVREN_CONTRACT_ID"]
    api_url = os.environ.get("KAVREN_API_URL", "http://127.0.0.1:8000")
    owner = os.environ.get("BUYER_OWNER_ACCOUNT_ID") or buyer.get("BUYER_OWNER_ACCOUNT_ID", "")
    if not owner:
        sys.exit("set BUYER_OWNER_ACCOUNT_ID (the owner who funded the buyer agent's budget)")
    b_id, p_id, e_id = buyer["BUYER_AGENT_ACCOUNT_ID"], prov["PROVIDER_ACCOUNT_ID"], ev["EVALUATOR_ACCOUNT_ID"]
    bc = AgentChain(rpc, contract, b_id, buyer["BUYER_AGENT_FC_PRIVATE_KEY"])
    pc = AgentChain(rpc, contract, p_id, prov["PROVIDER_FC_PRIVATE_KEY"])
    ec = AgentChain(rpc, contract, e_id, ev["EVALUATOR_FC_PRIVATE_KEY"])
    reader = NearChain(rpc, contract)

    def tx_failure(tx_hash: str, sender: str) -> Optional[str]:
        """The failure message as recorded on-chain, or None if the tx is not on-chain."""
        for _ in range(5):
            try:
                body = reader._post({"jsonrpc": "2.0", "id": "kavren", "method": "tx",
                                     "params": {"tx_hash": tx_hash, "sender_account_id": sender,
                                                "wait_until": "EXECUTED"}})
            except Exception:
                time.sleep(3)
                continue
            if "error" in body:
                time.sleep(3)
                continue
            st = body["result"]["status"]
            return json.dumps(st["Failure"]) if "Failure" in st else None
        return None

    def balance(acct: str) -> int:
        r = reader._query({"request_type": "view_account", "finality": "optimistic", "account_id": acct})
        return int(r["amount"])

    print(f"Attack gallery against {contract}\n  buyer {b_id}\n  provider {p_id} (playing a CHEATING provider)\n"
          f"  evaluator {e_id}\nMake sure the API and the evaluator worker are running and the provider "
          "and buyer agents are STOPPED.")
    if input("Type 'go' to start: ").strip() != "go":
        sys.exit("cancelled")
    bal0 = {a: balance(a) for a in (b_id, p_id, e_id)}
    g = Gallery(owner_id=owner, buyer_id=b_id, buyer_chain=bc, buyer_api=ApiClient(api_url, b_id, buyer["BUYER_AGENT_FC_PRIVATE_KEY"]),
                provider_id=p_id, provider_chain=pc, provider_api=ApiClient(api_url, p_id, prov["PROVIDER_FC_PRIVATE_KEY"]),
                evaluator_chain=ec, wait=lambda: time.sleep(10), tx_failure=tx_failure)
    run_at = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    try:
        g.run()
    finally:
        bal1 = {a: balance(a) for a in (b_id, p_id, e_id)}
        by = {a.split(".")[0]: bal0[a] - bal1[a] for a in bal0}
        gas = {"total": sum(by.values()), "by_account": by}
        if g.money:
            out = ROOT / "docs" / "ATTACKS.md"
            write_report(out, g, run_at, contract, gas)
            print(f"\nwrote {out}")
    n_ok = sum(r.ok for r in g.results)
    print(f"{n_ok}/{len(g.results)} outcomes as the protocol promises")
    sys.exit(0 if n_ok == len(g.results) else 1)


if __name__ == "__main__":
    main()
