"""Generate a RESTRICTED (function-call) key for one KAVREN role.

    python3 scripts/new_agent_key.py evaluator
    python3 scripts/new_agent_key.py buyer-agent
    python3 scripts/new_agent_key.py provider-agent
    python3 scripts/new_agent_key.py claude-buyer     (an outside agent using kavren-mcp)

- The private key is written ONLY to ~/.kavren/<role>.env (outside the repo;
  folder mode 0700, file mode 0600). It is never printed.
- The PUBLIC key is printed together with the exact PowerShell command that the
  account holder runs to attach it as a function-call key, limited to the KAVREN
  contract and to the role's methods, with a 0.25 NEAR gas allowance.
- This script never touches any account and never sees a full-access key.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import base58
from nacl.signing import SigningKey

CONTRACT = os.environ.get("KAVREN_CONTRACT_ID", "kavren.lahd-kavren.testnet")
PARENT = os.environ.get("KAVREN_PARENT_ACCOUNT", "lahd-kavren.testnet")

ROLES = {
    "evaluator": ("EVALUATOR_ACCOUNT_ID", "EVALUATOR_FC_PRIVATE_KEY", ["submit_verdict", "reveal_canaries"]),
    "buyer-agent": ("BUYER_AGENT_ACCOUNT_ID", "BUYER_AGENT_FC_PRIVATE_KEY", ["create_job", "refund_job"]),
    # an outside AI agent using the MCP tools: exactly the buyer agent's permissions
    "claude-buyer": ("BUYER_AGENT_ACCOUNT_ID", "BUYER_AGENT_FC_PRIVATE_KEY", ["create_job", "refund_job"]),
    "provider-agent": ("PROVIDER_ACCOUNT_ID", "PROVIDER_FC_PRIVATE_KEY", ["submit_work", "withdraw"]),
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("role", choices=sorted(ROLES))
    a = ap.parse_args()
    id_var, key_var, methods = ROLES[a.role]
    account = f"{a.role}.{PARENT}"

    d = Path.home() / ".kavren"
    d.mkdir(mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    out = d / f"{a.role}.env"
    if out.exists():
        sys.exit(f"{out} already exists; refusing to overwrite an existing key file")

    sk = SigningKey.generate()
    pk = bytes(sk.verify_key)
    secret = "ed25519:" + base58.b58encode(bytes(sk) + pk).decode()
    public = "ed25519:" + base58.b58encode(pk).decode()

    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"{id_var}={account}\n{key_var}={secret}\nKAVREN_CONTRACT_ID={CONTRACT}\n")
        if a.role.endswith("buyer") or a.role == "buyer-agent":
            f.write(f"BUYER_OWNER_ACCOUNT_ID={PARENT}\n")

    print(f"Private key saved to {out} (mode 0600). It was NOT printed.\n")
    print(f"Account : {account}")
    print(f"Methods : {', '.join(methods)}  (on {CONTRACT} only)")
    print(f"Public  : {public}\n")
    print("Run this in Windows PowerShell (your full-access key for the account signs it):\n")
    print(f"near account add-key {account} grant-function-call-access --allowance '0.25 NEAR' "
          f"--contract-account-id {CONTRACT} --function-names '{','.join(methods)}' "
          f"use-manually-provided-public-key {public} network-config testnet sign-with-keychain send")


if __name__ == "__main__":
    main()
