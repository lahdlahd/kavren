"""The evaluator's 30-minute window under realistic conditions.

The contract accepts a verdict only while now < submitted_at + eval_window_secs (1800 s).
The worker stops trying 300 s before that (verdict_safety_margin_secs) so a slow
transaction can't land after the window. These tests drive the real worker pass
(service.process_pending, as run every 15 s by kavren_api.worker) through a clock
and a fake chain that enforces the window, and inject the failures seen live:
RPC errors, a worker that is down for a while, a slow upload, a backlog, and a
transaction that lands although the call errored."""
import time

import pytest

from test_api import FakeChain, clean, env, load_spec, login, make_job, sha256_hex  # noqa: F401
from kavren_api.worker import run_once

WINDOW, MARGIN, PASS_EVERY = 1800, 300, 15


class WindowChain(FakeChain):
    """Adds the contract's window check and controllable failures."""

    def __init__(self, clock):
        super().__init__()
        self.clock, self.fail_verdicts, self.land_then_raise, self.broken_jobs = clock, 0, False, set()

    def get_job(self, job_id):
        if job_id in self.broken_jobs:
            raise RuntimeError("RPC 503 for this query")
        return super().get_job(job_id)

    def submit_verdict(self, job_id, seq, output_hash, verdict, report_hash):
        j = self.jobs[job_id]
        if self.clock.t >= j["submitted_at"] + j["eval_window_secs"]:
            raise RuntimeError("Smart contract panicked: evaluation window closed")
        if self.fail_verdicts:
            self.fail_verdicts -= 1
            raise RuntimeError("all RPC providers failed: 429")
        tx = super().submit_verdict(job_id, seq, output_hash, verdict, report_hash)
        if self.land_then_raise:
            self.land_then_raise = False
            raise RuntimeError("timeout waiting for the result")  # but the verdict is on-chain
        return tx


@pytest.fixture
def w(env):
    svc, _, clock, c = env
    chain = WindowChain(clock)
    svc.chain = chain
    return svc, chain, clock, c


def submit(w, upload_delay=0):
    svc, chain, clock, c = w
    agent = login(c, chain, "buyer-agent.testnet")
    prov = login(c, chain, "provider-agent.testnet")
    jid, draft = make_job(svc, chain, c, agent)
    c.post(f"/jobs/{jid}/register", headers=agent)
    work = c.get(f"/jobs/{jid}/work", headers=prov).content
    good = clean(load_spec(draft["spec_json"].encode()), work)
    chain.submit_work(jid, sha256_hex(good), clock.t)
    clock.t += upload_delay
    r = c.post(f"/jobs/{jid}/submissions", files={"file": ("s.csv", good)}, headers=prov)
    return jid, r


def run_worker(svc, clock, seconds, down=None):
    """Advance time in 15 s worker passes; `down`=(from, to) offsets when the worker is not running."""
    start = clock.t
    while clock.t - start < seconds:
        off = clock.t - start
        if not (down and down[0] <= off < down[1]):
            run_once(svc)
        clock.t += PASS_EVERY


def verdict_at(chain, jid):
    j = chain.jobs[jid]
    return (j["status"], j["verdicts"][-1]["verdict"]) if j["verdicts"] else (j["status"], None)


def test_normal_conditions_verdict_on_the_first_pass(w):
    svc, chain, clock, _ = w
    jid, _ = submit(w)
    t0 = chain.jobs[jid]["submitted_at"]
    run_worker(svc, clock, PASS_EVERY)
    assert verdict_at(chain, jid) == ("Passed", "PASS") and clock.t - t0 <= 2 * PASS_EVERY


def test_rpc_failures_for_ten_minutes_still_meet_the_window(w):
    svc, chain, clock, _ = w
    jid, _ = submit(w)
    chain.fail_verdicts = 40  # 40 failed passes x 15 s = 10 minutes of RPC trouble
    run_worker(svc, clock, 1500)
    assert verdict_at(chain, jid) == ("Passed", "PASS")


def test_worker_down_20_minutes_then_restarted_still_meets_the_window(w):
    svc, chain, clock, _ = w
    jid, _ = submit(w)
    run_worker(svc, clock, 1500, down=(0, 1200))
    assert verdict_at(chain, jid) == ("Passed", "PASS")


def test_worker_down_past_the_safety_margin_gives_up_cleanly(w):
    """No verdict is attempted inside the last 5 minutes; the buyer can then refund
    after 30 minutes (contract rule, live-tested with job 1 on 3 Nov 2026)."""
    svc, chain, clock, _ = w
    jid, _ = submit(w)
    run_worker(svc, clock, 1800, down=(0, 1530))
    assert verdict_at(chain, jid) == ("Submitted", None)
    assert chain.verdict_calls == []
    from sqlalchemy import select
    from kavren_api.db import submissions
    with svc.engine.begin() as c:
        assert c.execute(select(submissions.c.state)).scalar() == "expired"


def test_late_upload_is_still_judged_in_time(w):
    svc, chain, clock, _ = w
    jid, r = submit(w, upload_delay=1200)  # provider uploads 20 minutes after submit_work
    assert r.status_code == 200
    run_worker(svc, clock, 60)
    assert verdict_at(chain, jid) == ("Passed", "PASS")


def test_one_failing_job_does_not_block_another(w):
    svc, chain, clock, _ = w
    a, _ = submit(w)
    b, _ = submit(w)
    chain.broken_jobs.add(a)
    run_worker(svc, clock, PASS_EVERY)
    assert verdict_at(chain, b) == ("Passed", "PASS") and verdict_at(chain, a) == ("Submitted", None)
    chain.broken_jobs.clear()
    run_worker(svc, clock, PASS_EVERY)
    assert verdict_at(chain, a) == ("Passed", "PASS")


def test_backlog_of_25_submissions_is_cleared_in_one_pass(w):
    svc, chain, clock, _ = w
    jobs = [submit(w)[0] for _ in range(25)]
    t = time.perf_counter()
    run_worker(svc, clock, PASS_EVERY)
    elapsed = time.perf_counter() - t
    assert all(verdict_at(chain, j) == ("Passed", "PASS") for j in jobs)
    assert elapsed < 60, f"one pass took {elapsed:.1f}s of real time for 25 jobs"


def test_verdict_that_landed_despite_an_error_is_recorded_as_sent(w):
    svc, chain, clock, _ = w
    jid, _ = submit(w)
    chain.land_then_raise = True
    run_worker(svc, clock, 2 * PASS_EVERY)
    from sqlalchemy import select
    from kavren_api.db import submissions
    with svc.engine.begin() as c:
        assert c.execute(select(submissions.c.state)).scalar() == "sent"
    assert len(chain.verdict_calls) == 1  # never sent twice
