"""Phase 0 live check: py-near signing with a FUNCTION-CALL key on testnet.

Reads (from the environment or .env.signer-check): SIGNER_CHECK_ACCOUNT_ID,
SIGNER_CHECK_FC_PRIVATE_KEY, SIGNER_CHECK_RECEIVER_ID, SIGNER_CHECK_METHODS,
optional SIGNER_CHECK_ARGS (JSON, default {}), NEAR_RPC_URL (default testnet RPC).

Checks, in order:
  1. the key on-chain is a FunctionCall key for the expected receiver and methods;
  2. an allowed method call with 0 deposit SUCCEEDS (positive test);
  3. the same call with a 1-yocto deposit is REJECTED (FC keys cannot attach deposits);
  4. a plain 1-yocto transfer is REJECTED;
  5. a method outside the allowed list is REJECTED (only if a method list was set).
Rejected transactions are refused by validation and do not move funds.
Prints PASS/FAIL per check; exit code 0 only if all pass.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

from py_near.account import Account

ENV_FILE = Path(__file__).resolve().parents[2] / ".env.signer-check"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

ACCOUNT = os.environ["SIGNER_CHECK_ACCOUNT_ID"]
KEY = os.environ["SIGNER_CHECK_FC_PRIVATE_KEY"]
RECEIVER = os.environ["SIGNER_CHECK_RECEIVER_ID"]
METHODS = [m for m in os.environ.get("SIGNER_CHECK_METHODS", "").split(",") if m]
ARGS = json.loads(os.environ.get("SIGNER_CHECK_ARGS", "{}"))
RPC = os.environ.get("NEAR_RPC_URL") or "https://rpc.testnet.near.org"
GAS = 30 * 10**12

results = []


def record(name, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


async def expect_rejected(name, coro):
    try:
        r = await coro
        status = getattr(r, "status", None)
        failed = isinstance(status, dict) and "Failure" in status
        record(name, failed, f"tx was NOT rejected at validation: {status}" if not failed else "failed on-chain")
    except Exception as e:  # rejected by RPC validation (expected)
        record(name, True, f"rejected: {type(e).__name__}: {str(e)[:160]}")


async def main():
    acc = Account(ACCOUNT, KEY, RPC)
    await acc.startup()

    ak = await acc.get_access_key(None)
    perm = ak.permission
    fc = perm.get("FunctionCall") if isinstance(perm, dict) else None
    record("key is a FunctionCall key", fc is not None, f"permission={perm}")
    if fc is None:
        sys.exit(1)
    record("receiver matches", fc.get("receiver_id") == RECEIVER, f"{fc.get('receiver_id')}")
    record("method list matches", sorted(fc.get("method_names", [])) == sorted(METHODS), f"{fc.get('method_names')}")

    method = METHODS[0]
    try:
        r = await acc.function_call(RECEIVER, method, ARGS, gas=GAS, amount=0)
        status = getattr(r, "status", {})
        ok = isinstance(status, dict) and "SuccessValue" in status
        tx = getattr(getattr(r, "transaction", None), "hash", "?")
        record(f"allowed call {method}() with 0 deposit succeeds", ok, f"tx={tx} status={status}")
    except Exception as e:
        record(f"allowed call {method}() with 0 deposit succeeds", False, f"{type(e).__name__}: {e}")

    await expect_rejected("same call with 1 yocto deposit is rejected",
                          acc.function_call(RECEIVER, method, ARGS, gas=GAS, amount=1))
    await expect_rejected("1 yocto transfer is rejected", acc.send_money(RECEIVER, 1))
    if METHODS:
        await expect_rejected("method outside allowed list is rejected",
                              acc.function_call(RECEIVER, "kavren_not_allowed_method", {}, gas=GAS, amount=0))

    await acc.shutdown()
    print("\nRESULT:", "ALL PASS" if all(results) else "SOME CHECKS FAILED")
    sys.exit(0 if all(results) else 1)


asyncio.run(main())
