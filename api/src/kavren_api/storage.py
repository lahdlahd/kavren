"""Private file storage. Local filesystem now; Supabase Storage at deploy time.
Keys are internal paths built from validated IDs only, never from user input."""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Protocol

_KEY = re.compile(r"^[a-z0-9_\-/]+\.(csv|json)$")


class Storage(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def delete_prefix(self, prefix: str) -> None: ...


class LocalStorage:
    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    def _path(self, key: str) -> Path:
        if not _KEY.fullmatch(key) or ".." in key:
            raise ValueError(f"bad storage key {key!r}")
        p = (self.root / key).resolve()
        if self.root not in p.parents:
            raise ValueError("storage key escapes root")
        return p

    def put(self, key: str, data: bytes) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete_prefix(self, prefix: str) -> None:
        if not re.fullmatch(r"^[a-z0-9_\-/]+/$", prefix):
            raise ValueError("bad prefix")
        d = (self.root / prefix).resolve()
        if self.root in d.parents and d.exists():
            shutil.rmtree(d)
