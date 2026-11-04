"""Independently re-check docs/ATTACKS.md against the chain.

For every transaction linked in the report, ask NEAR RPC for its final status:
  * rows marked "rejected tx" must have FAILED on-chain, with the quoted contract
    message in the failure;
  * every other linked transaction (create_job, submit, refund_job) must have SUCCEEDED.

Uses only public RPC; no keys. Run from ~/kavren:
  .venv/bin/python scripts/verify_attacks.py [docs/ATTACKS.md]
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from kavren_api.chain import NearChain

LINK = re.compile(r"\[(?P<label>[a-z_ ]+) [^\]]*\]\(https://testnet\.nearblocks\.io/txns/(?P<hash>\w+)\)")
QUOTE = re.compile(r"rejected on-chain: “([^”]+)”")


def tx_status(rpc: NearChain, h: str, senders: list[str]):
    for s in senders:
        try:
            body = rpc._post({"jsonrpc": "2.0", "id": "kavren", "method": "tx",
                              "params": {"tx_hash": h, "sender_account_id": s, "wait_until": "FINAL"}})
        except Exception:
            continue
        if "result" in body:
            t = body["result"]["transaction"]
            return t["signer_id"], body["result"]["status"], t["receiver_id"]
    return None, None, None


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/ATTACKS.md")
    text = path.read_text()
    contract = re.search(r"against `([^`]+)`", text).group(1)
    parent = contract.split(".", 1)[1]
    senders = [f"buyer-agent.{parent}", f"provider-agent.{parent}", f"evaluator.{parent}", parent]
    rpc = NearChain("", contract)
    bad = 0
    for line in text.splitlines():
        if not line.startswith("| ") or "nearblocks" not in line:
            continue
        n = line.split("|")[1].strip()
        q = QUOTE.search(line)
        for m in LINK.finditer(line):
            label, h = m.group("label"), m.group("hash")
            sender, st, receiver = tx_status(rpc, h, senders)
            if st is None:
                ok, got = False, "NOT FOUND on-chain"
            elif label == "rejected tx":
                fail = json.dumps(st.get("Failure", ""))
                ok = "Failure" in st and q is not None and q.group(1) in fail
                got = f"failed: {q.group(1) if ok else fail[:120]}"
            else:
                ok = "SuccessValue" in st
                got = "succeeded" if ok else f"did not succeed: {json.dumps(st)[:120]}"
            role = {"submit": "provider-agent.", "create_job": "buyer-agent.", "refund_job": "buyer-agent."}.get(label)
            if role and sender and not sender.startswith(role):
                ok, got = False, got + f" — signed by {sender}, expected {role}{parent}"
            ok = ok and receiver == contract
            bad += not ok
            print(f"{'OK  ' if ok else 'BAD '} row {n:>2} {label:<11} {h[:10]}… signed by {sender}: {got}")
    print(f"\n{'all transactions match the report' if not bad else f'{bad} MISMATCH(ES)'}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
