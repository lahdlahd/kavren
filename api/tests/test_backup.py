"""Backup round trip on a live-shaped deployment: a real job's database rows and
private files are archived, verified, restored elsewhere and read back identically.
Tampering is detected; old archives are pruned to the file-retention period."""
import sys
import tarfile
import time
from dataclasses import replace
from pathlib import Path

import pytest

from test_ops import full_flow  # noqa: F401
from test_api import env  # noqa: F401

from kavren_api import backup
from kavren_api.supervise import Supervisor


def test_round_trip_restores_identical_state(env, tmp_path):
    svc, chain, clock, c = env
    jid, draft, work, good = full_flow(env)
    svc.process_pending()
    arc = backup.create(svc.settings, tmp_path / "backups")
    assert oct(arc.stat().st_mode & 0o777) == "0o600"
    m = backup.verify(arc)
    assert m["rows"]["submissions"] == 1 and any(k.startswith("files/drafts/") for k in m["members"])

    target = replace(svc.settings, database_url=f"sqlite:///{tmp_path}/restored/db.sqlite",
                     storage_dir=str(tmp_path / "restored" / "files"))
    backup.restore(arc, target)
    src_files = sorted(p.relative_to(svc.settings.storage_dir) for p in Path(svc.settings.storage_dir).rglob("*") if p.is_file())
    dst_files = sorted(p.relative_to(target.storage_dir) for p in Path(target.storage_dir).rglob("*") if p.is_file())
    assert src_files == dst_files and src_files
    for rel in src_files:
        assert (Path(target.storage_dir) / rel).read_bytes() == (Path(svc.settings.storage_dir) / rel).read_bytes()

    # the restored state serves the same report through a fresh API
    from fastapi.testclient import TestClient
    from kavren_api.db import make_engine
    from kavren_api.main import create_app
    from kavren_api.service import Service
    from kavren_api.storage import LocalStorage
    import random
    svc2 = Service(target, make_engine(target.database_url), LocalStorage(target.storage_dir), chain, clock, random.Random(1))
    c2 = TestClient(create_app(svc2))
    from test_api import login
    prov = login(c2, chain, "provider-agent.testnet")
    assert c2.get(f"/jobs/{jid}/reports/1", headers=prov).json()["verdict"] == "PASS"


def test_restore_refuses_to_overwrite_without_force(env, tmp_path):
    svc, *_ = env
    full_flow(env)
    arc = backup.create(svc.settings, tmp_path / "b")
    with pytest.raises(SystemExit):
        backup.restore(arc, svc.settings)


def test_tampered_archive_is_rejected(env, tmp_path):
    svc, *_ = env
    full_flow(env)
    arc = backup.create(svc.settings, tmp_path / "b")
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(arc) as src, tarfile.open(bad, "w:gz") as dst:
        for m in src.getmembers():
            data = src.extractfile(m).read()
            if m.name.endswith("work.csv"):
                data = data.replace(b"@", b"#", 1)
                m.size = len(data)
            import io
            dst.addfile(m, io.BytesIO(data))
    with pytest.raises(ValueError, match="hash mismatch"):
        backup.verify(bad)


def test_old_archives_are_pruned_to_the_retention_period(env, tmp_path):
    svc, *_ = env
    out = tmp_path / "b"
    now = time.time()
    old = backup.create(svc.settings, out, now=now - 9 * 86400)
    recent = backup.create(svc.settings, out, now=now - 2 * 86400)
    backup.create(svc.settings, out, now=now)  # prunes beyond 7 days
    assert not old.exists() and recent.exists()


def test_supervisor_runs_the_backup_daily_without_overlap():
    class Clock:
        t = 0.0

        def __call__(self):
            return self.t
    clock, started = Clock(), []

    class P:
        def __init__(self, cmd):
            started.append(cmd)
            self.code = None

        def poll(self):
            return self.code
    sup = Supervisor([], clock=clock, popen=P, periodic=[("backup", [sys.executable, "-c", "pass"], 86400)])
    sup.step()
    assert started == []
    clock.t = 86400
    sup.step()
    assert len(started) == 1
    clock.t = 2 * 86400
    sup.step()
    assert len(started) == 1  # previous run still going: no overlap
    sup.periodic[0]["proc"].code = 0
    sup.step()
    sup.step()
    assert len(started) == 2
