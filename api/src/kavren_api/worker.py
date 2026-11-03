"""Evaluator worker loop: evaluate + post verdicts, reveal canaries, apply retention.
Run: python -m kavren_api.worker   (one process; the reconciler pass repeats every 15 s)."""
from __future__ import annotations

import logging
import time

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


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    s = Settings.from_env()
    svc = Service(s, make_engine(s.database_url), LocalStorage(s.storage_dir),
                  NearChain(s.rpc_url, s.contract_id, s.evaluator_account_id, s.evaluator_fc_private_key))
    while True:
        r = run_once(svc)
        if any(r.values()):
            log.info("%s", r)
        time.sleep(15)


if __name__ == "__main__":
    main()
