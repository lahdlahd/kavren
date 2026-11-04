"""Run the API and the evaluator worker together, and keep both alive.

One process for one hosting service (Render runs a single start command):
  * starts `uvicorn kavren_api.main:create_app` and `python -m kavren_api.worker`;
  * restarts whichever one exits, with exponential backoff (1 s ... 60 s), reset
    after it has stayed up for 5 minutes;
  * restarts the worker if its heartbeat file goes stale (alive but stuck, e.g.
    a hung network call), so a verdict is never silently missed;
  * runs a backup once a day (kavren_api.backup create);
  * on SIGTERM/SIGINT stops both children cleanly.
If the supervisor itself dies, the host restarts the whole service.

Run: python -m kavren_api.supervise   (HOST / PORT from the environment)
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("kavren.supervise")

STABLE_SECS = 300       # up this long -> backoff resets
MAX_BACKOFF = 60
HEARTBEAT_STALE = 300   # worker silent this long (after its start-up grace) -> restart it


@dataclass
class Child:
    name: str
    cmd: list[str]
    heartbeat: Optional[Path] = None
    proc: Optional[subprocess.Popen] = None
    started_at: float = 0.0
    backoff: float = 1.0
    next_start: float = 0.0
    restarts: int = 0
    reasons: list[str] = field(default_factory=list)


class Supervisor:
    def __init__(self, children: list[Child], clock: Callable[[], float] = time.time,
                 popen=subprocess.Popen, heartbeat_stale: float = HEARTBEAT_STALE,
                 periodic: Optional[list[tuple[str, list[str], float]]] = None):
        self.children, self.clock, self.popen, self.stale = children, clock, popen, heartbeat_stale
        self.stopping = False
        # (name, command, interval secs): one-shot jobs such as the daily backup; first run
        # one interval after start-up, never two at once
        self.periodic = [{"name": n, "cmd": c, "every": e, "next": clock() + e, "proc": None}
                         for n, c, e in (periodic or [])]

    def _start(self, c: Child) -> None:
        log.info("starting %s: %s", c.name, " ".join(c.cmd))
        c.proc = self.popen(c.cmd)
        c.started_at = self.clock()

    def _schedule_restart(self, c: Child, why: str) -> None:
        now = self.clock()
        if now - c.started_at >= STABLE_SECS:
            c.backoff = 1.0
        c.next_start = now + c.backoff
        c.backoff = min(MAX_BACKOFF, c.backoff * 2)
        c.restarts += 1
        c.reasons.append(why)
        c.proc = None
        log.warning("%s %s; restarting in %.0f s (restart #%d)", c.name, why, c.next_start - now, c.restarts)

    def _heartbeat_age(self, c: Child) -> Optional[float]:
        try:
            return self.clock() - int(c.heartbeat.read_text())
        except (OSError, ValueError, AttributeError):
            return None

    def step(self) -> None:
        """One supervision pass (called every second or two)."""
        now = self.clock()
        for c in self.children:
            if c.proc is None:
                if now >= c.next_start and not self.stopping:
                    self._start(c)
                continue
            code = c.proc.poll()
            if code is not None:
                self._schedule_restart(c, f"exited with code {code}")
                continue
            if c.heartbeat is not None and now - c.started_at > self.stale:
                age = self._heartbeat_age(c)
                if age is None or age > self.stale:
                    c.proc.kill()
                    c.proc.wait()
                    self._schedule_restart(c, f"heartbeat stale ({'none' if age is None else int(age)} s)")

        for p in self.periodic:
            if p["proc"] is not None and p["proc"].poll() is not None:
                code = p["proc"].poll()
                (log.info if code == 0 else log.error)("%s finished with code %s", p["name"], code)
                p["proc"] = None
            if p["proc"] is None and now >= p["next"] and not self.stopping:
                log.info("running %s", p["name"])
                p["proc"] = self.popen(p["cmd"])
                p["next"] = now + p["every"]

    def stop(self, timeout: float = 10.0) -> None:
        self.stopping = True
        for c in self.children:
            if c.proc and c.proc.poll() is None:
                c.proc.terminate()
        deadline = time.time() + timeout
        for c in self.children:
            if c.proc:
                try:
                    c.proc.wait(max(0.1, deadline - time.time()))
                except subprocess.TimeoutExpired:
                    c.proc.kill()


def default_children() -> list[Child]:
    from .config import Settings
    from .worker import heartbeat_path

    host, port = os.environ.get("HOST", "0.0.0.0"), os.environ.get("PORT", "8000")
    py = sys.executable
    return [
        Child("api", [py, "-m", "uvicorn", "kavren_api.main:create_app", "--factory", "--host", host, "--port", port]),
        Child("worker", [py, "-m", "kavren_api.worker"], heartbeat=heartbeat_path(Settings.from_env())),
    ]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    every = float(os.environ.get("BACKUP_INTERVAL_SECS", "86400"))
    sup = Supervisor(default_children(),
                     periodic=[("backup", [sys.executable, "-m", "kavren_api.backup", "create"], every)])

    def on_signal(signum, _frame):
        log.info("signal %s: stopping", signum)
        sup.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    while True:
        sup.step()
        time.sleep(1)


if __name__ == "__main__":
    main()
