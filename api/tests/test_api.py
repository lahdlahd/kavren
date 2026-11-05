"""API tests against an in-memory fake chain that mirrors the contract's rules
for the fields the API relies on. The real contract is tested in contract/."""
import base64
import hashlib
import random
import sys
from pathlib import Path

import base58
import pytest
from fastapi.testclient import TestClient
from nacl.signing import SigningKey

from kavren_api import nep413
from kavren_api.config import Settings
from kavren_api.db import make_engine
from kavren_api.main import create_app
from kavren_api.service import Service
from kavren_api.storage import LocalStorage

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "kavren_core" / "tests"))
from reference_cleaner import clean  # noqa: E402

from kavren_core.canonical import load_canonical, sha256_hex  # noqa: E402
from kavren_core.spec import load_spec  # noqa: E402

CONTRACT = "kavren.lahd-kavren.testnet"
EVALUATOR = "evaluator.lahd-kavren.testnet"
INPUT = (ROOT / "fixtures" / "contacts" / "input.csv").read_bytes()


class FakeChain:
    def __init__(self):
        self.jobs, self.keys, self.verdict_calls, self.reveals = {}, {}, [], []
        self.fail_next_verdict = False

    # --- test helpers mirroring contract behaviour
    def add_key(self, account, pk, perm):
        self.keys[(account, pk)] = perm

    def create_job(self, agent, owner, provider, draft, evaluator=EVALUATOR, **over):
        jid = len(self.jobs)
        j = dict(job_id=jid, owner_id=owner, buyer_agent=agent, provider_id=provider, evaluator_id=evaluator,
                 amount="1500000000000000000000000", template_id=draft["template_id"], spec_json=draft["spec_json"],
                 draft_id=draft["draft_id"], input_hash=draft["input_hash"], work_hash=draft["work_hash"],
                 canary_commitment=draft["canary_commitment"], created_at=0, deadline=10**12, max_attempts=3,
                 attempts_used=0, eval_window_secs=1800, submission_seq=0, current_output_hash=None,
                 submitted_at=None, status="Open", verdicts=[], canaries_revealed=False)
        j.update(over)
        self.jobs[jid] = j
        return jid

    def submit_work(self, jid, h, t):
        j = self.jobs[jid]
        assert j["status"] == "Open"
        j.update(submission_seq=j["submission_seq"] + 1, current_output_hash=h, submitted_at=t, status="Submitted")

    # --- Chain protocol
    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def get_access_key_permission(self, account_id, public_key):
        return self.keys.get((account_id, public_key))

    def submit_verdict(self, job_id, seq, output_hash, verdict, report_hash):
        if self.fail_next_verdict:
            self.fail_next_verdict = False
            raise RuntimeError("rpc timeout")
        j = self.jobs[job_id]
        assert j["status"] == "Submitted" and seq == j["submission_seq"] and output_hash == j["current_output_hash"]
        self.verdict_calls.append((job_id, seq, verdict, report_hash))
        j["verdicts"].append({"seq": seq, "verdict": verdict, "output_hash": output_hash, "report_hash": report_hash})
        if verdict == "PASS":
            j["status"] = "Passed"
        else:
            j.update(status="Open", attempts_used=j["attempts_used"] + 1)
        return f"tx-{len(self.verdict_calls)}"

    def reveal_canaries(self, job_id, blob):
        j = self.jobs[job_id]
        assert j["status"] in ("Passed", "Refunded") and sha256_hex(blob) == j["canary_commitment"]
        j["canaries_revealed"] = True
        self.reveals.append(job_id)
        return f"reveal-{job_id}"


class Clock:
    def __init__(self):
        self.t = 1_800_000_000

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    chain, clock = FakeChain(), Clock()
    s = Settings(database_url=f"sqlite:///{tmp_path}/db.sqlite", storage_dir=str(tmp_path / "files"),
                 contract_id=CONTRACT, rpc_url="unused", evaluator_account_id=EVALUATOR,
                 evaluator_fc_private_key="", auth_recipient="kavren-api", file_retention_days=7)
    svc = Service(s, make_engine(s.database_url), LocalStorage(s.storage_dir), chain, clock, random.Random(7))
    return svc, chain, clock, TestClient(create_app(svc))


def new_key():
    sk = SigningKey.generate()
    secret = "ed25519:" + base58.b58encode(bytes(sk) + bytes(sk.verify_key)).decode()
    public = "ed25519:" + base58.b58encode(bytes(sk.verify_key)).decode()
    return secret, public


def login(client, chain, account, perm=None, key=None):
    secret, public = key or new_key()
    chain.add_key(account, public, perm or {"FunctionCall": {"receiver_id": CONTRACT, "method_names": [], "allowance": None}})
    ch = client.post("/auth/challenge", json={"account_id": account}).json()
    pk, sig = nep413.sign(secret, ch["message"], base64.b64decode(ch["nonce"]), ch["recipient"])
    r = client.post("/auth/verify", json={"account_id": account, "public_key": pk, "signature": sig, "nonce": ch["nonce"]})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def make_job(svc, chain, client, agent_h, **over):
    d = client.post("/drafts", files={"file": ("input.csv", INPUT, "text/csv")}, headers=agent_h)
    assert d.status_code == 200, d.text
    draft = d.json()
    jid = chain.create_job("buyer-agent.testnet", "owner.testnet", "provider-agent.testnet", draft, **over)
    return jid, draft


# =========================================================================== end to end

def test_full_agent_to_agent_flow(env):
    svc, chain, clock, c = env
    agent = login(c, chain, "buyer-agent.testnet")
    prov = login(c, chain, "provider-agent.testnet")
    owner = login(c, chain, "owner.testnet", perm="FullAccess")

    jid, draft = make_job(svc, chain, c, agent)
    assert draft["template_id"] == "contacts-v1" and draft["evaluator_id"] == EVALUATOR
    assert "canary_bytes" not in draft and "salt" not in str(draft)
    assert c.post(f"/jobs/{jid}/register", headers=agent).json()["matches_draft"] is True
    assert c.get(f"/jobs/{jid}/check").json()["matches_draft"] is True

    work = c.get(f"/jobs/{jid}/work", headers=prov)
    assert work.status_code == 200 and sha256_hex(work.content) == draft["work_hash"]

    # attempt 1: a bad submission (rows silently deleted) -> FAIL, escrow stays
    spec = load_spec(draft["spec_json"].encode())
    good = clean(spec, work.content)
    bad = b"\n".join(good.split(b"\n")[:5]) + b"\n"
    chain.submit_work(jid, sha256_hex(bad), clock.t)
    assert c.post(f"/jobs/{jid}/submissions", files={"file": ("s.csv", bad)}, headers=prov).json()["state"] == "pending"
    r = svc.process_pending()
    assert r[0]["verdict"] == "FAIL" and chain.jobs[jid]["status"] == "Open"
    rep = c.get(f"/jobs/{jid}/reports/1", headers=prov).json()
    assert rep["verdict"] == "FAIL" and "C2" in [x["check"] for x in rep["report"]["checks"] if x["status"] == "fail"]
    assert chain.verdict_calls[0][3] == rep["report_hash"]

    # attempt 2: correct work -> PASS
    clock.t += 60
    chain.submit_work(jid, sha256_hex(good), clock.t)
    c.post(f"/jobs/{jid}/submissions", files={"file": ("s.csv", good)}, headers=prov)
    assert svc.process_pending()[0]["verdict"] == "PASS"
    assert chain.jobs[jid]["status"] == "Passed"

    # buyer gets the deliverable without canaries; provider cannot
    dl = c.get(f"/jobs/{jid}/deliverable", headers=agent)
    assert dl.status_code == 200
    canary = load_canonical(svc._draft(draft["draft_id"]).canary_bytes)
    for cn in canary["canaries"]:
        assert cn["row"]["email"].strip().lower() not in dl.text.lower() or not cn["row"]["email"].strip()
    assert c.get(f"/jobs/{jid}/deliverable", headers=prov).status_code == 403
    assert c.get(f"/jobs/{jid}/deliverable", headers=owner).status_code == 200

    # audit files: buyer side only, only once final
    assert c.get(f"/jobs/{jid}/audit/work", headers=agent).content == work.content
    assert sha256_hex(c.get(f"/jobs/{jid}/audit/submissions/2", headers=agent).content) == chain.jobs[jid]["current_output_hash"]
    assert c.get(f"/jobs/{jid}/audit/work", headers=prov).status_code == 403

    # reveal after final; public opening matches the commitment
    assert svc.reveal_finished() == [{"job_id": jid, "revealed_tx": f"reveal-{jid}"}]
    blob = c.get(f"/jobs/{jid}/reveal").content
    assert sha256_hex(blob) == draft["canary_commitment"]

    # retention: files gone 7 days after final; hashes/reports remain
    clock.t += 8 * 86400
    assert f"job {jid}" in svc.apply_retention()
    assert c.get(f"/jobs/{jid}/input", headers=owner).status_code == 401  # old session expired
    owner = login(c, chain, "owner.testnet", perm="FullAccess")
    assert c.get(f"/jobs/{jid}/input", headers=owner).status_code == 410
    assert c.get(f"/jobs/{jid}/reports/2", headers=owner).json()["verdict"] == "PASS"


# =========================================================================== auth

def test_signature_for_other_account_or_message_rejected(env):
    svc, chain, clock, c = env
    secret, public = new_key()
    chain.add_key("buyer-agent.testnet", public, "FullAccess")
    ch = c.post("/auth/challenge", json={"account_id": "buyer-agent.testnet"}).json()
    pk, sig = nep413.sign(secret, "Sign in to KAVREN as someone-else.testnet", base64.b64decode(ch["nonce"]), ch["recipient"])
    r = c.post("/auth/verify", json={"account_id": "buyer-agent.testnet", "public_key": pk, "signature": sig, "nonce": ch["nonce"]})
    assert r.status_code == 401


def test_challenge_is_single_use_and_expires(env):
    svc, chain, clock, c = env
    secret, public = new_key()
    chain.add_key("a.testnet", public, "FullAccess")
    ch = c.post("/auth/challenge", json={"account_id": "a.testnet"}).json()
    pk, sig = nep413.sign(secret, ch["message"], base64.b64decode(ch["nonce"]), ch["recipient"])
    body = {"account_id": "a.testnet", "public_key": pk, "signature": sig, "nonce": ch["nonce"]}
    assert c.post("/auth/verify", json=body).status_code == 200
    assert c.post("/auth/verify", json=body).status_code == 401  # replay
    ch2 = c.post("/auth/challenge", json={"account_id": "a.testnet"}).json()
    pk, sig = nep413.sign(secret, ch2["message"], base64.b64decode(ch2["nonce"]), ch2["recipient"])
    clock.t += 301
    assert c.post("/auth/verify", json={**body, "signature": sig, "nonce": ch2["nonce"]}).status_code == 401


def test_key_not_on_account_rejected(env):
    svc, chain, clock, c = env
    secret, public = new_key()  # never added to chain
    ch = c.post("/auth/challenge", json={"account_id": "a.testnet"}).json()
    pk, sig = nep413.sign(secret, ch["message"], base64.b64decode(ch["nonce"]), ch["recipient"])
    r = c.post("/auth/verify", json={"account_id": "a.testnet", "public_key": pk, "signature": sig, "nonce": ch["nonce"]})
    assert r.status_code == 401 and "not an access key" in r.json()["detail"]


def test_function_call_key_for_other_contract_rejected(env):
    svc, chain, clock, c = env
    secret, public = new_key()
    chain.add_key("a.testnet", public, {"FunctionCall": {"receiver_id": "other.testnet", "method_names": []}})
    ch = c.post("/auth/challenge", json={"account_id": "a.testnet"}).json()
    pk, sig = nep413.sign(secret, ch["message"], base64.b64decode(ch["nonce"]), ch["recipient"])
    r = c.post("/auth/verify", json={"account_id": "a.testnet", "public_key": pk, "signature": sig, "nonce": ch["nonce"]})
    assert r.status_code == 401


def test_session_expires(env):
    svc, chain, clock, c = env
    h = login(c, chain, "buyer-agent.testnet")
    clock.t += 1801
    assert c.post("/drafts", files={"file": ("i.csv", INPUT)}, headers=h).status_code == 401


def test_nep413_matches_reference_layout():
    # independent recomputation of the NEP-413 hash layout
    nonce = bytes(range(32))
    tag = (2**31 + 413).to_bytes(4, "little")
    body = (len(b"hi")).to_bytes(4, "little") + b"hi" + nonce + (len(b"app")).to_bytes(4, "little") + b"app" + b"\x00"
    assert nep413.payload_hash("hi", nonce, "app") == hashlib.sha256(tag + body).digest()


# =========================================================================== access control

def test_only_the_right_party_gets_each_file(env):
    svc, chain, clock, c = env
    agent = login(c, chain, "buyer-agent.testnet")
    prov = login(c, chain, "provider-agent.testnet")
    stranger = login(c, chain, "stranger.testnet")
    jid, _ = make_job(svc, chain, c, agent)
    assert c.get(f"/jobs/{jid}/work", headers=stranger).status_code == 403
    assert c.get(f"/jobs/{jid}/work", headers=agent).status_code == 403
    assert c.get(f"/jobs/{jid}/input", headers=prov).status_code == 403
    assert c.get(f"/jobs/{jid}/input", headers=stranger).status_code == 403
    assert c.get(f"/jobs/{jid}/input", headers=agent).status_code == 200
    assert c.get(f"/jobs/{jid}/work").status_code == 401
    assert c.get(f"/jobs/{jid}/reveal").status_code == 409  # secret until final
    assert c.get(f"/jobs/{jid}/audit/work", headers=agent).status_code == 409  # not final yet


def test_upload_must_match_on_chain_hash(env):
    svc, chain, clock, c = env
    agent, prov = login(c, chain, "buyer-agent.testnet"), login(c, chain, "provider-agent.testnet")
    jid, draft = make_job(svc, chain, c, agent)
    assert c.post(f"/jobs/{jid}/submissions", files={"file": ("s.csv", b"x\n")}, headers=prov).status_code == 409
    chain.submit_work(jid, sha256_hex(b"real\n"), clock.t)
    r = c.post(f"/jobs/{jid}/submissions", files={"file": ("s.csv", b"swapped\n")}, headers=prov)
    assert r.status_code == 409 and "does not match" in r.json()["detail"]


def test_job_not_matching_draft_is_refused_everywhere(env):
    svc, chain, clock, c = env
    agent, prov = login(c, chain, "buyer-agent.testnet"), login(c, chain, "provider-agent.testnet")
    jid, draft = make_job(svc, chain, c, agent, canary_commitment="0" * 64)
    assert c.get(f"/jobs/{jid}/check").json()["matches_draft"] is False
    assert c.get(f"/jobs/{jid}/work", headers=prov).status_code == 409


def test_draft_used_by_a_different_agent_is_refused(env):
    svc, chain, clock, c = env
    agent = login(c, chain, "buyer-agent.testnet")
    draft = c.post("/drafts", files={"file": ("i.csv", INPUT)}, headers=agent).json()
    jid = chain.create_job("thief-agent.testnet", "owner.testnet", "provider-agent.testnet", draft)
    assert c.get(f"/jobs/{jid}/check").json()["matches_draft"] is False


def test_unwinnable_input_refused_at_draft(env):
    svc, chain, clock, c = env
    agent = login(c, chain, "buyer-agent.testnet")
    dirty = b"full_name,email,signup_date\nA B,bad@x,\nC D,,\nE F,ok@example.com,\n"
    r = c.post("/drafts", files={"file": ("i.csv", dirty)}, headers=agent)
    assert r.status_code == 422 and "no provider could pass" in r.json()["detail"]


# =========================================================================== worker robustness

def test_verdict_retry_after_rpc_failure(env):
    svc, chain, clock, c = env
    agent, prov = login(c, chain, "buyer-agent.testnet"), login(c, chain, "provider-agent.testnet")
    jid, draft = make_job(svc, chain, c, agent)
    work = c.get(f"/jobs/{jid}/work", headers=prov).content
    good = clean(load_spec(draft["spec_json"].encode()), work)
    chain.submit_work(jid, sha256_hex(good), clock.t)
    c.post(f"/jobs/{jid}/submissions", files={"file": ("s.csv", good)}, headers=prov)
    chain.fail_next_verdict = True
    assert "error" in svc.process_pending()[0]
    clock.t += 15
    assert svc.process_pending()[0]["verdict"] == "PASS"
    assert len(chain.verdict_calls) == 1


def test_no_verdict_when_window_nearly_closed(env):
    svc, chain, clock, c = env
    agent, prov = login(c, chain, "buyer-agent.testnet"), login(c, chain, "provider-agent.testnet")
    jid, draft = make_job(svc, chain, c, agent)
    work = c.get(f"/jobs/{jid}/work", headers=prov).content
    good = clean(load_spec(draft["spec_json"].encode()), work)
    chain.submit_work(jid, sha256_hex(good), clock.t)
    c.post(f"/jobs/{jid}/submissions", files={"file": ("s.csv", good)}, headers=prov)
    clock.t += 1800 - 299
    assert svc.process_pending()[0]["state"] == "expired" and chain.verdict_calls == []


def test_stale_submission_is_not_judged(env):
    svc, chain, clock, c = env
    agent, prov = login(c, chain, "buyer-agent.testnet"), login(c, chain, "provider-agent.testnet")
    jid, _ = make_job(svc, chain, c, agent)
    chain.submit_work(jid, sha256_hex(b"a\n"), clock.t)
    c.post(f"/jobs/{jid}/submissions", files={"file": ("s.csv", b"a\n")}, headers=prov)
    chain.jobs[jid]["status"] = "Refunded"  # e.g. refunded meanwhile
    assert svc.process_pending()[0]["state"] == "stale" and chain.verdict_calls == []


def test_storage_rejects_path_tricks(tmp_path):
    st = LocalStorage(str(tmp_path / "s"))
    for bad in ["../x.csv", "drafts/../../x.csv", "/etc/passwd", "a/b.txt", "A/b.csv"]:
        with pytest.raises(ValueError):
            st.put(bad, b"x")


def test_submission_records_the_model_the_provider_reports(env):
    import json as _json
    svc, chain, clock, c = env
    agent, prov = login(c, chain, "buyer-agent.testnet"), login(c, chain, "provider-agent.testnet")
    jid, draft = make_job(svc, chain, c, agent)
    good = clean(load_spec(draft["spec_json"].encode()), c.get(f"/jobs/{jid}/work", headers=prov).content)
    chain.submit_work(jid, sha256_hex(good), clock.t)
    meta = {"model": "deepseek-ai/DeepSeek-V4.1-Flash-1017", "service": "https://cloud-api.near.ai/v1",
            "completion_ids": ["chatcmpl-abc123"]}
    r = c.post(f"/jobs/{jid}/submissions", files={"file": ("s.csv", good)},
               data={"provenance": _json.dumps(meta)}, headers=prov)
    assert r.status_code == 200
    # a second upload of the same file can't overwrite what was recorded first
    c.post(f"/jobs/{jid}/submissions", files={"file": ("s.csv", good)},
           data={"provenance": _json.dumps({**meta, "model": "other/model"})}, headers=prov)
    p = c.get(f"/jobs/{jid}/provenance").json()  # public, no login
    assert p["submissions"] == [{"seq": 1, "model": meta["model"], "service": "cloud-api.near.ai",
                                 "plan_calls": 1, "verification": "reported_by_provider"}]
    assert "chatcmpl" not in c.get(f"/jobs/{jid}/provenance").text


def test_bad_or_missing_provenance_never_blocks_the_file(env):
    svc, chain, clock, c = env
    agent, prov = login(c, chain, "buyer-agent.testnet"), login(c, chain, "provider-agent.testnet")
    jid, draft = make_job(svc, chain, c, agent)
    good = clean(load_spec(draft["spec_json"].encode()), c.get(f"/jobs/{jid}/work", headers=prov).content)
    chain.submit_work(jid, sha256_hex(good), clock.t)
    r = c.post(f"/jobs/{jid}/submissions", files={"file": ("s.csv", good)},
               data={"provenance": '{"model": "<script>", "service": "http://x"}'}, headers=prov)
    assert r.status_code == 200 and r.json()["state"] == "pending"
    assert c.get(f"/jobs/{jid}/provenance").json()["submissions"] == []
    assert svc.process_pending()[0]["verdict"] == "PASS"


def test_parse_provenance_keeps_only_safe_fields():
    from kavren_api.service import parse_provenance
    assert parse_provenance(None) is None and parse_provenance("x" * 5000) is None
    assert parse_provenance('{"model": "m", "service": "ftp://h"}') is None
    assert parse_provenance('{"model": "m", "completion_ids": ["a b"]}') is None
    ok = parse_provenance('{"model": "a/b-1", "service": "https://cloud-api.near.ai/v1?k=secret", "extra": 1}')
    assert ok == {"model": "a/b-1", "service": "cloud-api.near.ai", "completion_ids": []}
