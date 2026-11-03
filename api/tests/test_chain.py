import base64
import json

import httpx
import pytest

from kavren_api.chain import DEFAULT_TESTNET_RPCS, ChainError, NearChain, rpc_list


def ok_view(value):
    raw = list(json.dumps(value).encode())
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": "kavren", "result": {"result": raw}})


def test_rate_limited_provider_fails_over_to_the_next():
    hits = []

    def handler(req):
        hits.append(str(req.url))
        if "fastnear" in str(req.url):
            return httpx.Response(429, text="Too Many Requests")
        return ok_view({"job_id": 0})

    slept = []
    c = NearChain("", "kavren.testnet", http=httpx.Client(transport=httpx.MockTransport(handler)), sleep=slept.append)
    assert c.get_job(0) == {"job_id": 0}
    assert any("drpc" in h for h in hits) and slept  # rotated after backing off


def test_gives_a_clear_error_when_every_provider_fails():
    c = NearChain("https://a.example,https://b.example", "k",
                  http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))), sleep=lambda s: None)
    with pytest.raises(ChainError, match="all RPC providers failed"):
        c.get_job(0)


def test_network_errors_also_fail_over():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom")
        return ok_view(None)

    c = NearChain("https://a.example,https://b.example", "k", http=httpx.Client(transport=httpx.MockTransport(handler)),
                  sleep=lambda s: None)
    assert c.get_job(5) is None and calls["n"] == 2


def test_rpc_list_defaults_put_near_org_last():
    assert rpc_list("") == DEFAULT_TESTNET_RPCS and DEFAULT_TESTNET_RPCS[-1] == "https://rpc.testnet.near.org"
    assert rpc_list(" https://x.example , https://y.example ") == ["https://x.example", "https://y.example"]
