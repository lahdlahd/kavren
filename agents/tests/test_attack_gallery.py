"""The attack gallery script, run end to end against the fake contract + the real API
service + the real evaluator. The live run (scripts/attack_gallery.py) does the same
against testnet; this test makes sure every attack is wired to the outcome it claims."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from fake_contract import BUYER, EVALUATOR, OWNER, PROVIDER, AgentChainFake  # noqa: E402
from test_buyer import world  # noqa: E402,F401  (fixture)

import attack_gallery as ag  # noqa: E402
from new_agent_key import ROLES  # noqa: E402  (the real key scopes used on testnet)
from kavren_core.evaluator import evaluate  # noqa: E402
from kavren_core.spec import load_spec  # noqa: E402


def gallery(w):
    scoped = lambda acct, role: AgentChainFake(w["contract"], acct, ROLES[role][2])  # noqa: E731
    return ag.Gallery(owner_id=OWNER, buyer_id=BUYER, buyer_chain=scoped(BUYER, "buyer-agent"), buyer_api=w["agent"].api,
                      provider_id=PROVIDER, provider_chain=scoped(PROVIDER, "provider-agent"),
                      provider_api=w["prov_api"], evaluator_chain=scoped(EVALUATOR, "evaluator"),
                      wait=w["svc"].process_pending, clock=w["clock"], log=lambda s: None)


def test_every_attack_is_stopped_and_the_cheater_is_never_paid(world):
    w = world
    g = gallery(w)
    results = g.run()
    bad = [(r.title, r.actual) for r in results if not r.ok]
    assert not bad, bad
    titles = " | ".join(r.title for r in results)
    for fa in ag.FILE_ATTACKS:
        assert fa.title in titles
    assert len(g.jobs) == 2
    assert all(w["contract"].jobs[i]["status"] == "Refunded" for i in g.jobs)
    assert g.money["paid_to_provider"] == 0
    assert g.money["refunded"] == g.money["escrowed"] == 2 * 240 * 10**21
    assert w["contract"].claimable.get(PROVIDER, 0) == 0
    layers = {r.layer for r in results}
    assert {"API", "Contract", "Evaluator", "Evaluator (hidden canaries)", ag.ACCESS_KEY} <= layers


def test_canary_attack_is_invisible_to_public_checks(world):
    w = world
    g = gallery(w)
    job_id, _, work, args = g._open_job()
    spec = load_spec(args["spec_json"].encode())
    sub = ag.attack_canary_blind(spec, work)
    assert evaluate(spec, work, sub, None).verdict == "PASS"  # what a provider can check
    assert sub != ag.clean(spec, work)


def test_a_wrongly_wired_attack_is_reported_as_a_miss(world):
    """The checker must not rubber-stamp: an 'attack' that is actually honest work gets a PASS,
    and the gallery must flag that as a miss rather than a success."""
    w = world
    g = gallery(w)
    job_id, _, work, args = g._open_job()
    spec = load_spec(args["spec_json"].encode())
    honest = ag.FileAttack("honest", "Honest work disguised as an attack", lambda s, wk: ag.clean(s, wk), "C2", "MISSING_RECORD")
    g.run_file_attack(job_id, work, spec, honest)
    assert g.results[-1].ok is False and g.results[-1].actual.startswith("PASS")


def test_report_is_written_from_actual_results(world, tmp_path):
    w = world
    g = gallery(w)
    g.run()
    out = tmp_path / "ATTACKS.md"
    ag.write_report(out, g, "2026-11-04 12:00 UTC", "kavren.test", None)
    text = out.read_text()
    assert f"{len(g.results)} of {len(g.results)} outcomes matched" in text
    assert "Paid to the cheating provider: **0.0000 NEAR**" in text
    assert "MERGE_KEY_MISMATCH" in text and "REASON_NOT_TRUE" in text


def test_access_key_refusals_are_not_reported_as_onchain_transactions(world):
    w = world
    g = gallery(w)
    g.run()
    for r in g.results:
        if r.layer == ag.ACCESS_KEY:
            assert r.ok and r.txs == [] and "no transaction on-chain" in r.actual


def test_unscoped_key_would_fall_through_to_the_contract_guard(world):
    """Defence in depth: if an agent key were NOT scoped, the contract still refuses."""
    w = world
    g = gallery(w)
    job_id, _, _, _ = g._open_job()
    loose = AgentChainFake(w["contract"], PROVIDER)
    r = g._expect_rejected("Provider PASS with an unscoped key", "Contract", loose, PROVIDER, "submit_verdict",
                           {"job_id": job_id, "seq": 1, "output_hash": "a" * 64, "verdict": "PASS",
                            "report_hash": "b" * 64}, "only the job's evaluator", job_id)
    assert r.ok
