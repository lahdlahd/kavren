"""Chain access. Views over JSON-RPC; the evaluator's two write calls are signed
with its FUNCTION-CALL key (py-near). A Fake implementation lives in the tests."""
from __future__ import annotations

import base64
import json
from typing import Optional, Protocol

import httpx


class Chain(Protocol):
    def get_job(self, job_id: int) -> Optional[dict]: ...
    def get_access_key_permission(self, account_id: str, public_key: str) -> Optional[object]: ...
    def submit_verdict(self, job_id: int, seq: int, output_hash: str, verdict: str, report_hash: str) -> str: ...
    def reveal_canaries(self, job_id: int, blob: bytes) -> str: ...


class ChainError(RuntimeError):
    pass


# Public testnet RPC providers listed in NEAR's docs (docs.near.org/api/rpc/providers).
# rpc.testnet.near.org is "severely rate limited", so it is the last resort.
DEFAULT_TESTNET_RPCS = [
    "https://rpc.testnet.fastnear.com",
    "https://test.rpc.fastnear.com",
    "https://near-testnet.drpc.org",
    "https://rpc.testnet.near.org",
]


def rpc_list(value: str | None) -> list[str]:
    """NEAR_RPC_URL may hold one URL or a comma-separated list; empty -> defaults."""
    urls = [u.strip() for u in (value or "").split(",") if u.strip()]
    return urls or list(DEFAULT_TESTNET_RPCS)


class NearChain:
    GAS = 50 * 10**12

    def __init__(self, rpc_url, contract_id: str, evaluator_id: str = "", evaluator_key: str = "",
                 sleep=None, http: httpx.Client | None = None):
        import time as _time
        self.rpcs = rpc_url if isinstance(rpc_url, list) else rpc_list(rpc_url)
        self.rpc_url, self.contract_id = self.rpcs[0], contract_id
        self.evaluator_id, self.evaluator_key = evaluator_id, evaluator_key
        self.http = http or httpx.Client(timeout=20)
        self.sleep = sleep or _time.sleep
        self._i = 0

    def _post(self, payload: dict) -> dict:
        """Try each provider in turn with exponential backoff on 429 / 5xx / network errors."""
        last = None
        for attempt in range(2 * len(self.rpcs)):
            url = self.rpcs[self._i % len(self.rpcs)]
            try:
                r = self.http.post(url, json=payload)
                if r.status_code == 429 or r.status_code >= 500:
                    raise httpx.HTTPStatusError(f"{r.status_code} from {url}", request=r.request, response=r)
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPStatusError, httpx.TransportError) as e:
                last = e
                self._i += 1  # rotate provider
                self.sleep(min(8.0, 0.5 * 2 ** attempt))
        raise ChainError(f"all RPC providers failed: {last}")

    def _query(self, params: dict) -> dict:
        body = self._post({"jsonrpc": "2.0", "id": "kavren", "method": "query", "params": params})
        if "error" in body:
            raise ChainError(json.dumps(body["error"])[:300])
        if "error" in body.get("result", {}):
            raise ChainError(str(body["result"]["error"])[:300])
        return body["result"]

    def view(self, method: str, args: dict) -> object:
        res = self._query({
            "request_type": "call_function", "finality": "final", "account_id": self.contract_id,
            "method_name": method, "args_base64": base64.b64encode(json.dumps(args).encode()).decode(),
        })
        return json.loads(bytes(res["result"]).decode())

    def get_job(self, job_id: int) -> Optional[dict]:
        return self.view("get_job", {"job_id": job_id})

    def get_access_key_permission(self, account_id: str, public_key: str) -> Optional[object]:
        try:
            res = self._query({"request_type": "view_access_key", "finality": "final",
                               "account_id": account_id, "public_key": public_key})
        except ChainError:
            return None
        return res.get("permission")

    # -- evaluator writes (function-call key; no deposit)
    def _call(self, method: str, args: dict) -> str:
        import asyncio

        from py_near.account import Account

        async def go():
            acc = Account(self.evaluator_id, self.evaluator_key, self.rpcs)
            await acc.startup()
            try:
                r = await acc.function_call(self.contract_id, method, args, gas=self.GAS, amount=0)
            finally:
                await acc.shutdown()
            status = getattr(r, "status", {})
            if not (isinstance(status, dict) and "SuccessValue" in status):
                raise ChainError(f"{method} failed: {str(status)[:300]}")
            return r.transaction.hash

        return asyncio.run(go())

    def submit_verdict(self, job_id, seq, output_hash, verdict, report_hash) -> str:
        return self._call("submit_verdict", {"job_id": job_id, "seq": seq, "output_hash": output_hash,
                                             "verdict": verdict, "report_hash": report_hash})

    def reveal_canaries(self, job_id: int, blob: bytes) -> str:
        return self._call("reveal_canaries", {"job_id": job_id, "blob": base64.b64encode(blob).decode()})
