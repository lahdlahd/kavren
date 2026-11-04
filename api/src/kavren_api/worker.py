"""Evaluator worker loop: evaluate + post verdicts, reveal canaries, apply retention.
Run: python -m kavren_api.worker   (one process; the reconciler pass repeats every 15 s)."""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from .chain import NearChain
from .config import Settings
from .db import make_engine
from .service import Service
from .storage import LocalStorage

log = logging.getLogger("kavren.worker")


def run_once(svc: Service) -> dict:
    out = {"verdicts": [], "reveals": [], "deleted": []}
    try:
        out["verdicts"] = svc.process_pending()
        out["reveals"] = svc.reveal_finished()
        out["deleted"] = svc.apply_retention()
        svc.purge_auth()
    except Exception:  # keep the loop alive; each item records its own error
        log.exception("worker pass failed")
    return out


def heartbeat_path(settings: Settings) -> Path:
    """Where the worker records that it is alive (read by /health and the supervisor)."""
    return Path(os.environ.get("WORKER_HEARTBEAT_FILE") or Path(settings.storage_dir).parent / "worker-heartbeat")


def beat(path: Path, now: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(str(int(now if now is not None else time.time())))
    tmp.replace(path)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    from .chain import quiet_rpc_race_noise
    quiet_rpc_race_noise()
    s = Settings.from_env()
    svc = Service(s, make_engine(s.database_url), LocalStorage(s.storage_dir),
                  NearChain(s.rpc_url, s.contract_id, s.evaluator_account_id, s.evaluator_fc_private_key))
    hb = heartbeat_path(s)
    while True:
        r = run_once(svc)
        beat(hb)
        if any(r.values()):
            log.info("%s", r)
        time.sleep(15)


if __name__ == "__main__":
    main()
