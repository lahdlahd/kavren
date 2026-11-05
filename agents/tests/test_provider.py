"""Provider agent tests. The LLM is replaced by scripted planners so tests are
deterministic and free; the real NEAR AI planner is exercised by a live smoke
test run by the owner (scripts/provider_smoke.py)."""
import csv
import io
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from fake_contract import BUYER, NEAR, OWNER, PROVIDER, AgentChainFake  # noqa: E402
from reference_cleaner import clean  # noqa: E402
from test_buyer import INPUT, world  # noqa: E402,F401  (fixture)

from kavren_agents.cleaning import PlanError, execute, validate_plan  # noqa: E402
from kavren_agents.planner import NearAIPlanner, extract_json  # noqa: E402
from kavren_agents.provider import LOCAL_REVISIONS, ProviderAgent  # noqa: E402
from kavren_core.evaluator import evaluate  # noqa: E402
from kavren_core.spec import load_spec  # noqa: E402


def oracle_plan(spec, work_csv: str) -> dict:
    """The decisions a perfect cleaner makes (derived from the reference cleaner)."""
    sub = clean(spec, work_csv.encode()).decode()
    rows = list(csv.reader(io.StringIO(sub)))[1:]
    return {"rejects": [{"record_id": r[0], "reason": r[3]} for r in rows if r[1] == "rejected"],
            "merges": [{"record_id": r[0], "into": r[2]} for r in rows if r[1] == "merged"]}


class Scripted:
    """First returns `first` (default: a lazy 'keep everything' plan), then the oracle plan."""

    def __init__(self, first=None, always_bad=False):
        self.calls, self.first, self.always_bad = [], first, always_bad

    def plan(self, spec, work_csv, feedback=None):
        self.calls.append(feedback)
        if self.always_bad or (len(self.calls) == 1 and self.first is not None):
            return (self.first if self.first is not None else {"rejects": [], "merges": []}), {"total_tokens": 100}
        return oracle_plan(spec, work_csv), {"total_tokens": 100}


def hire(w):
    (w["agent"].inbox / "job.csv").write_bytes(INPUT)
    return w["agent"].tick()[0]["job_id"]


def provider(w, planner):
    return ProviderAgent(PROVIDER, AgentChainFake(w["contract"], PROVIDER), w["prov_api"], planner, w["clock"],
                         state_dir=w["tmp"] / "provider-state")


def test_lazy_first_plan_is_caught_locally_and_revised_before_spending_an_attempt(world):
    w = world
    jid = hire(w)
    pl = Scripted(first={"rejects": [], "merges": []})
    p = provider(w, pl)
    ev = p.tick()
    assert ev[0]["action"] == "submitted" and ev[0]["local_revisions"] == 1
    assert pl.calls[0] is None and any(f["check"] == "C2" or f["check"] == "C4" for f in pl.calls[1])
    assert w["contract"].jobs[jid]["attempts_used"] == 0 and w["contract"].jobs[jid]["submission_seq"] == 1
    w["svc"].process_pending()
    assert w["contract"].jobs[jid]["status"] == "Passed"  # canaries handled correctly too
    ev = p.tick()
    assert ev == [{"action": "withdrew", "amount_yocto": str(240 * 10**21), "tx": ev[0]["tx"]}]
    assert w["contract"].claimable.get(PROVIDER, 0) == 0
    # and the buyer accepts it after its own independent re-run
    assert w["agent"].tick()[0]["action"] == "accepted"


def test_invalid_plan_json_is_treated_as_a_failed_local_attempt(world):
    w = world
    hire(w)
    pl = Scripted(first={"rejects": [{"record_id": "nope", "reason": "INVALID_EMAIL"}]})
    ev = provider(w, pl).tick()
    assert ev[0]["action"] == "submitted"
    assert pl.calls[1][0]["code"] == "INVALID_PLAN"


def test_gives_up_without_burning_credits_in_a_loop(world):
    w = world
    jid = hire(w)
    pl = Scripted(always_bad=True)
    p = provider(w, pl)
    ev = p.tick()
    assert ev[0]["action"] == "gave_up" and len(pl.calls) == LOCAL_REVISIONS + 1
    assert w["contract"].jobs[jid]["submission_seq"] == 0  # no on-chain attempt spent
    assert p.tick() == [] and len(pl.calls) == LOCAL_REVISIONS + 1  # no new LLM calls


def test_refuses_job_that_does_not_match_the_protocol_draft(world):
    w = world
    jid = hire(w)
    w["contract"].jobs[jid]["canary_commitment"] = "0" * 64  # tampered on-chain terms
    pl = Scripted()
    ev = provider(w, pl).tick()
    assert ev[0]["action"] == "refused" and pl.calls == []


def test_fail_report_is_fed_back_and_job_is_retried(world):
    w = world
    jid = hire(w)
    # attempt 1 is made to fail on-chain by uploading a bad file directly (simulates a bad first submission)
    j = w["contract"].jobs[jid]
    bad = b"record_id,_outcome,_merged_into,_reason,full_name,email,signup_date\n"
    from kavren_core.canonical import sha256_hex
    w["contract"].call_as(PROVIDER, "submit_work", {"job_id": jid, "output_hash": sha256_hex(bad)})
    w["prov_api"].request("POST", f"/jobs/{jid}/submissions", files={"file": ("s.csv", bad)})
    w["svc"].process_pending()
    assert w["contract"].jobs[jid]["status"] == "Open" and w["contract"].jobs[jid]["attempts_used"] == 1
    pl = Scripted()
    ev = provider(w, pl).tick()
    assert ev[0]["action"] == "submitted" and ev[0]["seq"] == 2
    fb = pl.calls[0]
    assert any(f["code"] == "MISSING_RECORD" for f in fb)
    assert any(f["code"] == "CONTROL_RECORDS_WRONG" for f in fb)  # canaries reported as a count only
    from kavren_core.canonical import load_canonical
    canary_ids = {c["record_id"] for c in load_canonical(w["svc"]._draft(j["draft_id"]).canary_bytes)["canaries"]}
    assert len(canary_ids) >= 6
    assert not any(f.get("record_id") in canary_ids and f["code"] != "MISSING_RECORD" for f in fb)
    w["svc"].process_pending()
    assert w["contract"].jobs[jid]["status"] == "Passed"


def test_does_not_start_with_too_little_time_left(world):
    w = world
    jid = hire(w)
    w["clock"].t = w["contract"].jobs[jid]["deadline"] - 60
    pl = Scripted()
    assert provider(w, pl).tick() == [] and pl.calls == []


# ------------------------------------------------------------------ executor and planner units

def test_executor_cannot_invent_values():
    """Even with a lazy 'keep everything' plan, every kept value is an allowed transformation of the
    input; the only C7 hits are records whose impossible date had to be blanked (they should be rejected)."""
    from kavren_core import formats as f
    from kavren_core.evaluator import parse_work
    from kavren_core.templates import get_template
    spec = get_template("contacts-v1").build_spec()
    work = (Path(__file__).resolve().parents[2] / "fixtures" / "contacts" / "work.csv").read_bytes()
    sub = execute(spec, work, {"rejects": [], "merges": []})
    ev = evaluate(spec, work, sub, None)
    c7 = {v["record_id"] for c in ev.report["checks"] if c["check"] == "C7" for v in c["violations"]}
    _, recs = parse_work(spec, work)
    bad_dates = {rid for rid, r in recs.items()
                 if r["signup_date"].strip() and f.parse_input_date(r["signup_date"], spec.date_rules.input_formats) is None}
    assert bad_dates and c7 == bad_dates


@pytest.mark.parametrize("plan", [
    "not a dict", {"rejects": "x"}, {"rejects": [{"record_id": "a", "reason": "OTHER"}]},
    {"merges": [{"record_id": "a", "into": "a"}]}, {"extra": 1},
])
def test_plan_validation(plan):
    with pytest.raises(PlanError):
        validate_plan(load_spec(json.dumps({}).encode()) if False else
                      __import__("kavren_core.templates", fromlist=["x"]).get_template("contacts-v1").build_spec(),
                      {"a", "b"}, plan)


def test_merge_into_a_rejected_record_is_invalid():
    from kavren_core.templates import get_template
    spec = get_template("contacts-v1").build_spec()
    with pytest.raises(PlanError):
        validate_plan(spec, {"a", "b"}, {"rejects": [{"record_id": "b", "reason": "INVALID_EMAIL"}],
                                         "merges": [{"record_id": "a", "into": "b"}]})


@pytest.mark.parametrize("text", ['{"rejects":[],"merges":[]}', '```json\n{"rejects":[],"merges":[]}\n```',
                                  'Here you go: {"rejects":[],"merges":[]} done'])
def test_extract_json(text):
    assert extract_json(text) == {"rejects": [], "merges": []}


def test_empty_reply_from_reasoning_model_is_an_error():
    with pytest.raises(ValueError):
        extract_json(None)


def _sse(*chunks):
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


def test_near_ai_planner_streams_and_drops_unsupported_reasoning_effort():
    from kavren_core.templates import get_template
    seen = []

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        seen.append(body)
        if "reasoning_effort" in body:
            return httpx.Response(400, json={"error": "unknown field reasoning_effort"})
        return httpx.Response(200, text=_sse(
            {"choices": [{"delta": {"reasoning_content": "hmm"}}]},
            {"choices": [{"delta": {"content": '{"rejects":[],'}}]},
            {"choices": [{"delta": {"content": '"merges":[]}'}}]},
            {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 3}}),
            headers={"content-type": "text/event-stream"})

    pl = NearAIPlanner("k", http=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://x/v1"))
    ticks = []
    plan, usage = pl.plan(get_template("contacts-v1").build_spec(), "record_id,full_name,email,signup_date\nab,A,a@b.co,\n",
                          on_progress=lambda: ticks.append(1))
    assert plan == {"rejects": [], "merges": []} and usage["completion_tokens"] == 3
    assert seen[0]["reasoning_effort"] == "low" and "reasoning_effort" not in seen[1]
    assert seen[1]["model"] == "deepseek-ai/DeepSeek-V4.1-Flash-1017" and seen[1]["stream"] is True
    assert pl.reasoning_effort is None and ticks


def test_reasoning_only_reply_is_a_failed_attempt():
    from kavren_core.templates import get_template

    def handler(req):
        return httpx.Response(200, text=_sse({"choices": [{"delta": {"reasoning_content": "thinking..."}}]}),
                              headers={"content-type": "text/event-stream"})

    pl = NearAIPlanner("k", http=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://x/v1"))
    with pytest.raises(ValueError):
        pl.plan(get_template("contacts-v1").build_spec(), "record_id,full_name,email,signup_date\n")


def test_facts_table_reports_tool_computed_facts():
    from kavren_agents.planner import facts_table
    from kavren_core.templates import get_template
    spec = get_template("contacts-v1").build_spec()
    t = facts_table(spec, "record_id,full_name,email,signup_date\nr1,  Ada  ,ADA@X.CO ,31/02/2026\nr2,,bad@x,\n")
    lines = t.splitlines()
    assert lines[1].startswith("r1|ada@x.co|-|true|false|")
    assert lines[2].startswith("r2|bad@x|full_name|false|-|")


def test_upload_lag_is_retried_and_a_lost_upload_is_delivered_later(world):
    """The live bug of job 1: submit_work landed, the upload got 409 because the API's
    node had not seen it yet, and the file was lost."""
    from test_buyer import LaggingChain
    w = world
    jid = hire(w)
    lagging = LaggingChain(w["contract"])
    w["svc"].chain = lagging
    p = provider(w, Scripted())
    real_call = p.chain.call

    def call_then_lag(method, args):
        if method == "submit_work":
            lagging.lag(100)  # API keeps seeing the pre-submit state, longer than the in-tick retries
        return real_call(method, args)
    p.chain.call = call_then_lag
    ev = p.tick()
    assert ev[0]["action"] == "submitted" and ev[0]["uploaded"] is False
    pending = list((w["tmp"] / "provider-state").glob("*.csv"))
    assert len(pending) == 1  # the file survived
    lagging.n = 0
    restarted = provider(w, Scripted())  # even across a restart
    ev = restarted.tick()
    assert ev[0]["action"] == "uploaded_pending"
    assert not list((w["tmp"] / "provider-state").glob("*.csv"))
    w["svc"].process_pending()
    assert w["contract"].jobs[jid]["status"] == "Passed"


class Reporting(Scripted):
    """A scripted planner that also exposes what NearAIPlanner records about each call."""
    model, base_url = "deepseek-ai/DeepSeek-V4.1-Flash-1017", "https://cloud-api.near.ai/v1"

    def plan(self, spec, work_csv, feedback=None):
        self.last_completion_id, self.last_served_model = f"chatcmpl-{len(self.calls) + 1}", self.model
        return super().plan(spec, work_csv, feedback)


def test_each_submission_records_the_model_and_service_the_provider_reports(world):
    w = world
    jid = hire(w)
    ev = provider(w, Reporting(first={"rejects": [], "merges": []})).tick()  # 1 lazy plan + 1 revision
    assert ev[0]["action"] == "submitted" and ev[0]["uploaded"] is True
    p = w["svc"].get_provenance(jid)["submissions"]
    assert p == [{"seq": 1, "model": Reporting.model, "service": "cloud-api.near.ai", "plan_calls": 2,
                  "verification": "reported_by_provider"}]


def test_a_delayed_upload_keeps_its_provenance_across_a_restart(world):
    from test_buyer import LaggingChain
    w = world
    jid = hire(w)
    lagging = LaggingChain(w["contract"])
    w["svc"].chain = lagging
    p = provider(w, Reporting())
    real_call = p.chain.call

    def call_then_lag(method, args):
        if method == "submit_work":
            lagging.lag(100)
        return real_call(method, args)
    p.chain.call = call_then_lag
    assert p.tick()[0]["uploaded"] is False
    lagging.n = 0
    assert provider(w, Scripted()).tick()[0]["action"] == "uploaded_pending"  # restarted, plain planner
    assert w["svc"].get_provenance(jid)["submissions"][0]["model"] == Reporting.model
    assert not list((w["tmp"] / "provider-state").glob("job*"))  # file and its record both cleaned up


def test_planners_without_provenance_still_upload(world):
    w = world
    jid = hire(w)
    assert provider(w, Scripted()).tick()[0]["uploaded"] is True
    assert w["svc"].get_provenance(jid)["submissions"] == []


def test_only_one_provider_can_run_per_account(tmp_path):
    from kavren_agents.provider import single_instance
    first = single_instance(tmp_path, "provider-agent.testnet")
    assert first is not None
    assert single_instance(tmp_path, "provider-agent.testnet") is None  # the duplicate is refused
    assert single_instance(tmp_path, "other.testnet") is not None
    first.close()  # process exit releases it
    assert single_instance(tmp_path, "provider-agent.testnet") is not None


def test_near_ai_planner_records_completion_id_and_served_model():
    from kavren_core.templates import get_template

    def handler(req):
        return httpx.Response(200, text=_sse(
            {"id": "chatcmpl-xyz", "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
             "choices": [{"delta": {"content": '{"rejects":[],"merges":[]}'}}]},
            {"id": "chatcmpl-xyz", "choices": [], "usage": {"completion_tokens": 3}}),
            headers={"content-type": "text/event-stream"})

    pl = NearAIPlanner("k", base_url="https://cloud-api.near.ai/v1",
                       http=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://x/v1"))
    pl.plan(get_template("contacts-v1").build_spec(), "record_id,full_name,email,signup_date\nab,A,a@b.co,\n")
    assert (pl.last_completion_id, pl.last_served_model) == ("chatcmpl-xyz", "deepseek-ai/DeepSeek-V4.1-Flash-1017")
    assert pl.base_url == "https://cloud-api.near.ai/v1"


# ---------------------------------------------------------------- NEAR AI attestation evidence
import base64 as _b64  # noqa: E402


def _evidence(plan: dict, *, model=Reporting.model, stream=False, kind="provider_tee") -> dict:
    req = json.dumps({"model": model, "messages": [], **({"stream": True} if stream else {})}).encode()
    resp = json.dumps({"choices": [{"message": {"content": json.dumps(plan)}}]}).encode()
    return {"request_b64": _b64.b64encode(req).decode(), "response_b64": _b64.b64encode(resp).decode(),
            "signature": {"kind": kind, "signed_text": "t", "signature": "s", "signing_algo": "ed25519",
                          "signing_address": "ab" * 32}}


class Attested(Reporting):
    """Like NearAIPlanner(attested=True): each call leaves evidence for the plan it returned."""

    def __init__(self, evidence_for=None, **kw):
        super().__init__(**kw)
        self.evidence_for = evidence_for or (lambda plan: _evidence(plan))

    def plan(self, spec, work_csv, feedback=None):
        plan, usage = super().plan(spec, work_csv, feedback)
        self.last_evidence = self.evidence_for(plan)
        return plan, usage


async def _attested_ok(model, req, resp, sig):
    return {"tcb_status": "UpToDate", "gpu_evidence": "verified", "deployment_provenance": "not_checked",
            "signing_address": sig["signing_address"]}


def _run(w, planner, check=_attested_ok):
    jid = hire(w)
    w["svc"].attestation_check = check
    assert provider(w, planner).tick()[0]["uploaded"] is True
    w["svc"].process_pending()
    assert w["contract"].jobs[jid]["status"] == "Passed"  # attestation never changes the verdict
    w["svc"].verify_attestations()
    w["jid"] = jid
    return w["svc"].get_provenance(jid)["submissions"][0]


def test_evaluator_verifies_attested_evidence_that_reproduces_the_file(world):
    p = _run(world, Attested())
    assert p["verification"] == "attestation_verified"
    assert p["attestation"]["gpu_evidence"] == "verified" and p["attestation"]["tcb_status"] == "UpToDate"
    assert p["attestation"]["deployment_provenance"] == "not_checked"  # stated honestly on the page


def test_genuine_evidence_from_a_different_plan_is_not_accepted(world):
    lazy = {"rejects": [], "merges": []}  # a real signed call, but not the one that produced this file
    p = _run(world, Attested(evidence_for=lambda plan: _evidence(lazy)))
    assert p["verification"] == "reported_by_provider"
    assert p["attestation_note"] == "evidence.plan_does_not_reproduce_file"


def test_bad_signature_or_attestation_keeps_the_reported_label(world):
    from kavren_api.attest import EvidenceError

    async def bad(*a):
        raise EvidenceError("signature.invalid")
    p = _run(world, Attested(), check=bad)
    assert p["verification"] == "reported_by_provider" and p["attestation_note"] == "signature.invalid"


def test_streamed_or_other_model_evidence_is_refused(world):
    p = _run(world, Attested(evidence_for=lambda plan: _evidence(plan, stream=True)))
    assert p["attestation_note"] == "evidence.streamed_call"


def test_near_ai_unreachable_is_retried_then_given_up(world):
    from kavren_api.attest import TransientError
    calls = []

    async def down(*a):
        calls.append(1)
        raise TransientError("api.transport_failed")
    w = world
    p = _run(w, Attested(), check=down)
    assert "attestation_note" not in p and p["verification"] == "reported_by_provider"  # still retrying
    for _ in range(10):
        w["svc"].verify_attestations()
    assert len(calls) == w["svc"].MAX_ATTESTATION_TRIES
    p = w["svc"].get_provenance(w["jid"])["submissions"][0]
    assert p["verification"] == "reported_by_provider" and p["attestation_note"].startswith("gave_up_after_retries")


def test_without_an_evaluator_key_nothing_is_checked(world):
    w = world
    jid = hire(w)
    assert provider(w, Attested()).tick()[0]["uploaded"] is True
    assert w["svc"].verify_attestations() == []  # no key configured, no fake injected
    assert w["svc"].get_provenance(jid)["submissions"][0]["verification"] == "reported_by_provider"


def test_evidence_survives_a_delayed_upload(world):
    from test_buyer import LaggingChain
    w = world
    jid = hire(w)
    lagging = LaggingChain(w["contract"])
    w["svc"].chain = lagging
    p = provider(w, Attested())
    real_call = p.chain.call

    def call_then_lag(method, args):
        if method == "submit_work":
            lagging.lag(100)
        return real_call(method, args)
    p.chain.call = call_then_lag
    assert p.tick()[0]["uploaded"] is False
    lagging.n = 0
    assert provider(w, Scripted()).tick()[0]["action"] == "uploaded_pending"
    w["svc"].attestation_check = _attested_ok
    w["svc"].process_pending()
    w["svc"].verify_attestations()
    assert w["svc"].get_provenance(jid)["submissions"][0]["verification"] == "attestation_verified"


def test_parse_evidence_accepts_only_model_tee_signatures():
    from kavren_api.attest import parse_evidence
    good = _evidence({"rejects": [], "merges": []})
    assert parse_evidence(json.dumps(good).encode()) is not None
    assert parse_evidence(json.dumps(_evidence({}, kind="gateway")).encode()) is None
    assert parse_evidence(b"not json") is None and parse_evidence(b"") is None
    assert parse_evidence(json.dumps({**good, "request_b64": "%%%"}).encode()) is None


def test_attested_planner_keeps_evidence_and_falls_back_to_streaming():
    from kavren_core.templates import get_template
    spec, work = get_template("contacts-v1").build_spec(), "record_id,full_name,email,signup_date\nab,A,a@b.co,\n"
    sent = []

    async def ok(body):
        sent.append(body)
        return {"content": '{"rejects":[],"merges":[]}', "usage": {"total_tokens": 5}, "id": "chatcmpl-1",
                "model": body["model"], "evidence": {"x": 1}}
    pl = NearAIPlanner("k", attested=True, attested_call=ok,
                       http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)), base_url="https://x"))
    assert pl.plan(spec, work)[0] == {"rejects": [], "merges": []}
    assert "stream" not in sent[0] and pl.last_evidence == {"x": 1} and pl.last_completion_id == "chatcmpl-1"

    async def down(body):
        raise OSError("unreachable")

    def streamed(req):
        return httpx.Response(200, text=_sse({"id": "s-1", "choices": [{"delta": {"content": '{"rejects":[],"merges":[]}'}}]}),
                              headers={"content-type": "text/event-stream"})
    pl = NearAIPlanner("k", attested=True, attested_call=down,
                       http=httpx.Client(transport=httpx.MockTransport(streamed), base_url="https://x"))
    assert pl.plan(spec, work)[0] == {"rejects": [], "merges": []}
    assert pl.last_evidence is None and pl.last_completion_id == "s-1"  # answered, but unverifiable
