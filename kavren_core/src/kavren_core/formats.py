"""Value rules: text, email and date normalisation and validation.

These functions define the meaning of the spec's rule identifiers:
  text_rule  "kavren-text/1"
  email_rule "kavren-email/1"
  date tokens in date_rules.input_formats / output_format
See docs/EVALUATION_RULES.md, section 3.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Optional

# ---------------------------------------------------------------- text (kavren-text/1)

_WS = re.compile(r"\s+")


def norm_text(value: str) -> str:
    """Trim and collapse internal whitespace runs to one space."""
    return _WS.sub(" ", value).strip()


def text_key(value: str) -> str:
    """Comparison key for text: whitespace-normalised and case-folded."""
    return norm_text(value).casefold()


def is_clean_text(value: str) -> bool:
    """Output formatting rule for text cells (check C6)."""
    return value == norm_text(value)


# ---------------------------------------------------------------- email (kavren-email/1)

_ATOM = r"[a-z0-9!#$%&'*+/=?^_`{|}~-]+"
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_EMAIL_RE = re.compile(
    rf"^{_ATOM}(?:\.{_ATOM})*@{_LABEL}(?:\.{_LABEL})*\.[a-z]{{2,63}}$"
)


def norm_email(value: str) -> str:
    """Allowed email transformation: trim + lowercase."""
    return value.strip().lower()


def is_valid_email(value: str) -> bool:
    """Validity of an already-normalised email (check C4).

    Pragmatic, documented rule — not full RFC 5322:
    - lowercase only (normalisation is required by C6);
    - local part: dot-separated atoms, no leading/trailing/consecutive dots;
    - domain: at least two labels, last label alphabetic, 2-63 chars;
    - total length <= 254, local part <= 64.
    """
    if len(value) > 254 or "@" not in value:
        return False
    local = value.rsplit("@", 1)[0]
    if len(local) > 64:
        return False
    return _EMAIL_RE.fullmatch(value) is not None


def is_clean_email(value: str) -> bool:
    return value == norm_email(value)


# ---------------------------------------------------------------- dates

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

# longest token first so "MMM" wins over "MM" and "DD" over "D"
_TOKENS = [
    ("YYYY", r"(?P<y>\d{4})"),
    ("MMM", r"(?P<mon>[A-Za-z]{3})"),
    ("MM", r"(?P<m>\d{2})"),
    ("DD", r"(?P<d>\d{2})"),
    ("M", r"(?P<m>\d{1,2})"),
    ("D", r"(?P<d>\d{1,2})"),
]

ALLOWED_INPUT_FORMATS = ("DD/MM/YYYY", "YYYY-MM-DD", "D MMM YYYY")
OUTPUT_FORMATS = ("YYYY-MM-DD",)


class DateFormatError(ValueError):
    pass


def _compile(fmt: str) -> re.Pattern:
    out, i, seen = [], 0, set()
    while i < len(fmt):
        for tok, rx in _TOKENS:
            if fmt.startswith(tok, i):
                group = rx.split(">")[0][4:]
                if group in seen or (group in ("m", "mon") and ({"m", "mon"} & seen)):
                    raise DateFormatError(f"duplicate field in format {fmt!r}")
                seen.add(group)
                out.append(rx)
                i += len(tok)
                break
        else:
            if fmt[i].isalpha():
                raise DateFormatError(f"unknown token at {fmt[i:]!r} in {fmt!r}")
            out.append(re.escape(fmt[i]))
            i += 1
    if not ({"y", "d"} <= seen and ({"m", "mon"} & seen)):
        raise DateFormatError(f"format {fmt!r} must contain year, month and day")
    return re.compile("^" + "".join(out) + "$")


def validate_format(fmt: str) -> str:
    _compile(fmt)
    return fmt


def parse_date(value: str, fmt: str) -> Optional[_dt.date]:
    """Strictly parse `value` (no trimming) with one format. None if no match."""
    m = _compile(fmt).match(value)
    if not m:
        return None
    g = m.groupdict()
    if g.get("mon") is not None:
        month = _MONTHS.get(g["mon"].lower())
        if month is None:
            return None
    else:
        month = int(g["m"])
    try:
        return _dt.date(int(g["y"]), month, int(g["d"]))
    except ValueError:
        return None


def parse_input_date(value: str, formats: tuple[str, ...] | list[str]) -> Optional[_dt.date]:
    """Parse an input cell after trimming, trying each declared format.

    Returns None if no format matches, or if two formats yield different dates
    (ambiguity is never guessed).
    """
    v = value.strip()
    found = {d for f in formats if (d := parse_date(v, f)) is not None}
    return found.pop() if len(found) == 1 else None


def format_date(d: _dt.date, fmt: str) -> str:
    if fmt != "YYYY-MM-DD":
        raise DateFormatError(f"unsupported output format {fmt!r}")
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d}"
