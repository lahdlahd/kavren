"""Supervisor: crashed children come back with backoff; a stuck worker (stale
heartbeat) is killed and restarted; stop() ends everything. Uses real processes."""
import sys
import time

from kavren_api.supervise import Child, Supervisor
from kavren_api.worker import beat

PY = sys.executable


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def wait_exit(c, secs=5):
    end = time.time() + secs
    while c.proc and c.proc.poll() is None and time.time() < end:
        time.sleep(0.05)


def test_crashed_child_is_restarted_with_backoff():
    clock = Clock()
    c = Child("crashy", [PY, "-c", "import sys; sys.exit(3)"])
    sup = Supervisor([c], clock=clock)
    sup.step()  # start
    first = c.proc
    wait_exit(c)
    sup.step()  # notices the exit
    assert c.proc is None and c.restarts == 1 and "code 3" in c.reasons[0]
    sup.step()  # backoff not elapsed
    assert c.proc is None
    clock.t += 1.0
    sup.step()
    assert c.proc is not None and c.proc is not first
    wait_exit(c)
    sup.step()
    assert c.next_start - clock.t == 2.0  # 1 s, then 2 s, ...
    sup.stop()


def test_backoff_resets_after_a_stable_run():
    clock = Clock()
    c = Child("crashy", [PY, "-c", "import sys; sys.exit(1)"])
    sup = Supervisor([c], clock=clock)
    for _ in range(4):  # build up backoff
        sup.step()
        wait_exit(c)
        sup.step()
        clock.t = c.next_start
    assert c.backoff >= 8
    sup.step()
    clock.t += 400  # this run "lasted" 400 s
    wait_exit(c)
    sup.step()
    assert c.next_start - clock.t == 1.0
    sup.stop()


def test_stuck_worker_with_stale_heartbeat_is_killed_and_restarted(tmp_path):
    clock = Clock()
    hb = tmp_path / "hb"
    c = Child("worker", [PY, "-c", "import time; time.sleep(60)"], heartbeat=hb)
    sup = Supervisor([c], clock=clock, heartbeat_stale=300)
    sup.step()
    beat(hb, clock.t)
    clock.t += 200
    sup.step()
    assert c.restarts == 0  # within grace
    clock.t += 200  # 400 s since start, heartbeat 400 s old
    sup.step()
    assert c.restarts == 1 and "heartbeat stale" in c.reasons[0]
    clock.t = c.next_start
    sup.step()
    assert c.proc is not None and c.proc.poll() is None
    beat(hb, clock.t + 350)  # a fresh beat keeps it alive
    clock.t += 350
    sup.step()
    assert c.restarts == 1
    sup.stop()
    assert c.proc.poll() is not None


def test_healthy_children_are_left_alone():
    clock = Clock()
    a = Child("api", [PY, "-c", "import time; time.sleep(60)"])
    sup = Supervisor([a], clock=clock)
    sup.step()
    pid = a.proc.pid
    for _ in range(5):
        clock.t += 100
        sup.step()
    assert a.proc.pid == pid and a.restarts == 0
    sup.stop()
