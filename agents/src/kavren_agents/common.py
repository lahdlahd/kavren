"""Shared plumbing: key files, the KAVREN API client (NEP-413 login), chain access."""
from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
from typing import Optional

import httpx

from kavren_api import nep413
from kavren_api.chain import NearChain


def load_env_file(path: str | Path) -> dict[str, str]:
    """Read KEY=VALUE lines from a key file (e.g. ~/.kavren/buyer-agent.env).
    Refuses files readable by other users."""
    p = Path(path).expanduser()
    if os.name == "posix" and p.stat().st_mode & 0o077:
        raise PermissionError(f"{p} must not be readable by other users (chmod 600)")
    out = {}
    for line in p.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


class ApiClient:
    """Talks to the KAVREN API, logging in with a NEP-413 signature from the agent's FC key."""

    def __init__(self, base_url: str, account_id: str, private_key: str, http: Optional[httpx.Client] = None):
        self.account_id, self.private_key = account_id, private_key
        self.http = http or httpx.Client(base_url=base_url, timeout=60)
        self.token: Optional[str] = None
        import time as _time
        self.sleep = _time.sleep

    def login(self) -> None:
        ch = self.http.post("/auth/challenge", json={"account_id": self.account_id})
        ch.raise_for_status()
        c = ch.json()
        pk, sig = nep413.sign(self.private_key, c["message"], base64.b64decode(c["nonce"]), c["recipient"])
        r = self.http.post("/auth/verify", json={"account_id": self.account_id, "public_key": pk,
                                                 "signature": sig, "nonce": c["nonce"]})
        r.raise_for_status()
        self.token = r.json()["token"]

    def request(self, method: str, url: str, **kw) -> httpx.Response:
        for attempt in (1, 2):
            if self.token is None:
                self.login()
            r = self.http.request(method, url, headers={"Authorization": f"Bearer {self.token}"}, **kw)
            if r.status_code == 401 and attempt == 1:
                self.token = None
                continue
            return r
        return r

    def request_after_write(self, method: str, url: str, tries: int = 6, delay: float = 3.0, **kw) -> httpx.Response:
        """For calls made right after our own on-chain transaction: the API may not see
        it yet (a lagging RPC node), so 404/409 are retried a few times before giving up."""
        r = self.request(method, url, **kw)
        for _ in range(tries - 1):
            if r.status_code not in (404, 409):
                break
            self.sleep(delay)
            r = self.request(method, url, **kw)
        return r

    def public(self, method: str, url: str, **kw) -> httpx.Response:
        return self.http.request(method, url, **kw)


def _describe(e: Exception) -> str:
    """py-near's RPC exceptions have an empty str(); the useful part (e.g. the contract's
    panic message or MethodNameMismatch) is in the class name, error_json and attributes."""
    extra = getattr(e, "error_json", None) or {k: v for k, v in vars(e).items() if not k.startswith("_")}
    return f"{type(e).__name__}: {e} {json.dumps(extra, default=str)}"[:2000]


class ChainCallError(RuntimeError):
    """A contract call that did not succeed. `tx` is the transaction hash when the
    transaction reached the chain (a contract panic is recorded on-chain and costs gas)."""

    def __init__(self, method: str, detail: str, tx: Optional[str]):
        super().__init__(f"{method} failed: {detail[:1500]}")
        self.method, self.detail, self.tx = method, detail, tx


class AgentChain:
    """Views over RPC; writes signed with the agent's FUNCTION-CALL key (no deposit possible)."""

    GAS = 100 * 10**12

    def __init__(self, rpc_url, contract_id: str, account_id: str, private_key: str):
        self.views = NearChain(rpc_url, contract_id)
        self.rpc_url, self.contract_id = self.views.rpcs, contract_id
        self.account_id, self.private_key = account_id, private_key

    def view(self, method: str, args: dict):
        return self.views.view(method, args)

    def call(self, method: str, args: dict):
        from py_near.account import Account

        async def go():
            acc = Account(self.account_id, self.private_key, self.rpc_url)
            await acc.startup()
            try:
                r = await acc.function_call(self.contract_id, method, args, gas=self.GAS, amount=0)
            except Exception as e:  # py-near raises for most contract panics; keep the tx hash
                raise ChainCallError(method, _describe(e), getattr(e, "trx_hash", None)) from e
            finally:
                await acc.shutdown()
            status = getattr(r, "status", {})
            if not (isinstance(status, dict) and "SuccessValue" in status):
                raise ChainCallError(method, json.dumps(status), r.transaction.hash)
            raw = base64.b64decode(status["SuccessValue"] or "")
            return {"tx": r.transaction.hash, "value": json.loads(raw) if raw else None}

        return asyncio.run(go())
