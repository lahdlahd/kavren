"""Backup, verify and restore the API's state: the SQLite database and the private files.

  python -m kavren_api.backup create  [--out DIR]          # consistent snapshot while running
  python -m kavren_api.backup verify  ARCHIVE              # hashes + SQLite integrity check
  python -m kavren_api.backup restore ARCHIVE [--force]    # into DATABASE_URL / STORAGE_DIR

An archive (kavren-backup-YYYYmmddTHHMMSSZ.tar.gz) holds:
  db.sqlite      taken with SQLite's online backup API (consistent even mid-write)
  files/...      every private file under STORAGE_DIR
  MANIFEST.json  sha256 and size of every member, row counts, creation time

Backups contain private files, so they get the same treatment: they stay on the
service's private disk (or the owner's own machine after export), and `create`
deletes archives older than FILE_RETENTION_DAYS, so a backup never keeps a file
longer than the published retention policy.
"""
from __future__ import annotations

import argparse
import calendar
import hashlib
import io
import json
import os
import sqlite3
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from .config import Settings

PREFIX = "kavren-backup-"


def _db_path(settings: Settings) -> Path:
    url = settings.database_url
    if not url.startswith("sqlite:///"):
        raise SystemExit("backup supports the SQLite deployment only (DATABASE_URL=sqlite:///...)")
    return Path(url[len("sqlite:///"):])


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def default_out(settings: Settings) -> Path:
    return Path(os.environ.get("BACKUP_DIR") or Path(settings.storage_dir).parent / "backups")


def create(settings: Settings, out_dir: Path, now: float | None = None) -> Path:
    now = time.time() if now is None else now
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    target = out_dir / f"{PREFIX}{stamp}.tar.gz"
    manifest = {"created_at": int(now), "members": {}, "rows": {}}
    with tempfile.TemporaryDirectory() as tmp:
        snap = Path(tmp) / "db.sqlite"
        src = sqlite3.connect(_db_path(settings))
        dst = sqlite3.connect(snap)
        with dst:
            src.backup(dst)
        for (t,) in dst.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            manifest["rows"][t] = dst.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        src.close()
        dst.close()
        partial = target.with_suffix(".partial")
        with tarfile.open(partial, "w:gz") as tar:
            data = snap.read_bytes()
            manifest["members"]["db.sqlite"] = {"sha256": _sha(data), "size": len(data)}
            tar.add(snap, arcname="db.sqlite")
            root = Path(settings.storage_dir)
            if root.exists():
                for f in sorted(p for p in root.rglob("*") if p.is_file()):
                    name = "files/" + f.relative_to(root).as_posix()
                    data = f.read_bytes()
                    manifest["members"][name] = {"sha256": _sha(data), "size": len(data)}
                    tar.add(f, arcname=name)
            m = json.dumps(manifest, indent=2, sort_keys=True).encode()
            info = tarfile.TarInfo("MANIFEST.json")
            info.size, info.mtime = len(m), int(now)
            tar.addfile(info, io.BytesIO(m))
        partial.replace(target)  # only complete archives ever carry the final name
    os.chmod(target, 0o600)
    prune(out_dir, settings.file_retention_days, now)
    return target


def prune(out_dir: Path, retention_days: int, now: float | None = None) -> list[Path]:
    now = time.time() if now is None else now
    gone = []
    for f in out_dir.glob(f"{PREFIX}*.tar.gz"):
        try:
            t = calendar.timegm(time.strptime(f.name[len(PREFIX):-len(".tar.gz")], "%Y%m%dT%H%M%SZ"))
        except ValueError:
            continue
        if now - t > retention_days * 86400:
            f.unlink()
            gone.append(f)
    return gone


def verify(archive: Path) -> dict:
    with tarfile.open(archive, "r:gz") as tar:
        manifest = json.loads(tar.extractfile("MANIFEST.json").read())
        names = {m.name for m in tar.getmembers() if m.isfile()} - {"MANIFEST.json"}
        if names != set(manifest["members"]):
            raise ValueError("archive members do not match the manifest")
        for name, meta in manifest["members"].items():
            data = tar.extractfile(name).read()
            if _sha(data) != meta["sha256"]:
                raise ValueError(f"{name}: hash mismatch")
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "db.sqlite"
            p.write_bytes(tar.extractfile("db.sqlite").read())
            con = sqlite3.connect(p)
            ok = con.execute("PRAGMA integrity_check").fetchone()[0]
            con.close()
            if ok != "ok":
                raise ValueError(f"SQLite integrity check: {ok}")
    return manifest


def restore(archive: Path, settings: Settings, force: bool = False) -> None:
    manifest = verify(archive)
    db, root = _db_path(settings), Path(settings.storage_dir)
    if not force and (db.exists() or (root.exists() and any(root.iterdir()))):
        raise SystemExit("target database or storage is not empty; stop the service and use --force")
    db.parent.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        for name in manifest["members"]:
            data = tar.extractfile(name).read()
            dest = db if name == "db.sqlite" else root / name[len("files/"):]
            if root.resolve() not in dest.resolve().parents and dest != db:
                raise ValueError(f"unsafe path in archive: {name}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m kavren_api.backup")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("--out")
    v = sub.add_parser("verify")
    v.add_argument("archive")
    r = sub.add_parser("restore")
    r.add_argument("archive")
    r.add_argument("--force", action="store_true")
    a = ap.parse_args()
    s = Settings.from_env()
    if a.cmd == "create":
        p = create(s, Path(a.out) if a.out else default_out(s))
        print(p)
    elif a.cmd == "verify":
        m = verify(Path(a.archive))
        print(f"ok: {len(m['members'])} members, rows {m['rows']}")
    else:
        restore(Path(a.archive), s, a.force)
        print("restored")


if __name__ == "__main__":
    sys.exit(main())
