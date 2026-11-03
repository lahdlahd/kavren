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
    return ProviderAgent(PROVIDER, AgentChainFake(w["contract"], PROVIDER), w["prov_api"], planner, w["clock"])


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


def test_near_ai_planner_request_shape():
    seen = {}

    def handler(req: httpx.Request):
        seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers["authorization"]
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"rejects":[],"merges":[]}'}}],
                                         "usage": {"total_tokens": 5}})

    from kavren_core.templates import get_template
    pl = NearAIPlanner("k", http=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://x/v1",
                                                headers={"Authorization": "Bearer k"}))
    plan, usage = pl.plan(get_template("contacts-v1").build_spec(), "record_id,full_name\n")
    assert plan == {"rejects": [], "merges": []} and usage["total_tokens"] == 5
    assert seen["body"]["model"] == "deepseek-ai/DeepSeek-V4.1-Flash-1017"
    assert seen["body"]["max_tokens"] >= 4000 and seen["auth"] == "Bearer k"
