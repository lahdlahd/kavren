"""Canonical JSON and hashing.

Rules (documented in docs/EVALUATION_RULES.md, section 1):
- UTF-8, object keys sorted by code point, separators "," and ":", no whitespace.
- Non-ASCII characters are written as-is (not \\u-escaped).
- Allowed value types: dict (str keys), list/tuple, str, int, bool, None.
- Floats are rejected everywhere: amounts and percentages are ints or strings.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


class CanonicalJSONError(ValueError):
    pass


def _check(value: Any, path: str = "$") -> None:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        raise CanonicalJSONError(f"float not allowed at {path}")
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise CanonicalJSONError(f"non-string key at {path}")
            _check(v, f"{path}.{k}")
        return
    if isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _check(v, f"{path}[{i}]")
        return
    raise CanonicalJSONError(f"unsupported type {type(value).__name__} at {path}")


def canonical_bytes(value: Any) -> bytes:
    _check(value)
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_hash(value: Any) -> str:
    return sha256_hex(canonical_bytes(value))


def load_canonical(data: bytes) -> Any:
    """Parse JSON and require that it is already in canonical form."""
    try:
        value = json.loads(data.decode("utf-8"), parse_float=_reject_float)
    except UnicodeDecodeError as e:
        raise CanonicalJSONError("not valid UTF-8") from e
    except json.JSONDecodeError as e:
        raise CanonicalJSONError(f"invalid JSON: {e}") from e
    if canonical_bytes(value) != data:
        raise CanonicalJSONError("JSON is valid but not in canonical form")
    return value


def _reject_float(s: str) -> Any:
    raise CanonicalJSONError(f"float not allowed: {s}")
