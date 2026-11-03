"""Canary generation and work-file construction (brief section 10.1).

build_work_file(spec, input_bytes, rng) -> WorkBuild

- validates the buyer's input against the spec (dialect, header as a set, size);
- gives every row a random 12-hex record_id;
- generates synthetic control records from the input's column profile, with
  expected outcomes defined by the SAME value rules the evaluator uses;
- shuffles canaries into random positions;
- returns the work file bytes, the canonical canary set (salted) and hashes.

`rng` must be a cryptographically secure source in production
(random.SystemRandom()); tests pass a seeded random.Random for reproducibility.
"""

from __future__ import annotations

import csv
import io
import random
from collections import Counter
from dataclasses import dataclass

from . import formats as f
from .canonical import canonical_bytes, sha256_hex
from .evaluator import DialectError, input_key, parse_csv, reason_is_true
from .spec import Spec

FIRST = ["Adaeze", "Bolaji", "Chiamaka", "Dapo", "Efe", "Folake", "Gbenga", "Halima", "Ifeoma", "Jide",
         "Kemi", "Lanre", "Musa", "Nkechi", "Obinna", "Remi", "Sade", "Tobi", "Uche", "Yemi", "Zara",
         "Amara", "Bayo", "Damilola", "Emeka", "Funke", "Ibrahim", "Kunle", "Ngozi", "Seun"]
LAST = ["Adeyemi", "Balogun", "Chukwu", "Danjuma", "Eze", "Fashola", "Garba", "Ibe", "Johnson", "Kalu",
        "Lawal", "Mohammed", "Nwosu", "Okafor", "Okoro", "Salami", "Taiwo", "Usman", "Yusuf", "Bello"]
COMPANIES = ["Ajala Foods", "Bright Path Logistics", "Coral Bay Studios", "Delta Grid Energy", "Eko Prints",
             "Fadeyi Motors", "Greenleaf Farms", "Harbour Point Realty", "Ikeja Tech Hub", "Juno Health"]
COUNTRIES = ["Nigeria", "Ghana", "Kenya", "South Africa", "Rwanda", "Senegal"]

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


class InputError(ValueError):
    """The buyer's input file does not fit the spec."""


@dataclass
class WorkBuild:
    work_bytes: bytes
    work_hash: str
    input_hash: str
    canary_set: dict
    canary_bytes: bytes
    canary_commitment: str
    input_rows: int
    work_rows: int


# ------------------------------------------------------------------ input

def read_input(spec: Spec, input_bytes: bytes) -> list[dict[str, str]]:
    try:
        rows = parse_csv(input_bytes, spec)
    except DialectError as e:
        raise InputError(f"input file: {e}") from None
    header = [h.strip() for h in rows[0]]
    if len(set(header)) != len(header) or set(header) != set(spec.column_names):
        raise InputError("input header does not match the template columns")
    body = rows[1:]
    if not body:
        raise InputError("input file has no data rows")
    return [{h: r[i] for i, h in enumerate(header)} for r in body]


def unavoidable_rejections(spec: Spec, rows: list[dict[str, str]]) -> int:
    """Rows that even a perfect provider must reject: some rejection reason is true
    and there is no clean twin (same dedupe key) to merge them into."""
    reasons = spec.reason_codes
    keys = [input_key(spec, r) for r in rows]
    bad = [any(reason_is_true(spec, x, r) for x in reasons) for r in rows]
    clean_keys = {k for k, b in zip(keys, bad) if k is not None and not b}
    return sum(1 for k, b in zip(keys, bad) if b and (k is None or k not in clean_keys))


def check_winnable(spec: Spec, rows: list[dict[str, str]]) -> None:
    n = unavoidable_rejections(spec, rows)
    if n * 100 > spec.max_reject_pct * len(rows):
        raise InputError(
            f"{n} of {len(rows)} rows can only be rejected, which exceeds the template's "
            f"{spec.max_reject_pct}% rejection cap; no provider could pass this job")


# ------------------------------------------------------------------ value generators

def _render_date(d, fmt: str) -> str:
    if fmt == "YYYY-MM-DD":
        return f"{d.year:04d}-{d.month:02d}-{d.day:02d}"
    if fmt == "DD/MM/YYYY":
        return f"{d.day:02d}/{d.month:02d}/{d.year:04d}"
    if fmt == "D MMM YYYY":
        return f"{d.day} {MONTHS[d.month - 1]} {d.year:04d}"
    raise ValueError(fmt)


class _Gen:
    def __init__(self, spec: Spec, rows: list[dict[str, str]], rng: random.Random):
        self.spec, self.rng = spec, rng
        self.used_keys = {k for r in rows if (k := input_key(spec, r)) is not None}
        domains = Counter()
        dates = []
        for r in rows:
            for c in spec.columns:
                v = r[c.name]
                if c.type == "email" and "@" in v:
                    dom = f.norm_email(v).rsplit("@", 1)[1]
                    if f.is_valid_email("a@" + dom):
                        domains[dom] += 1
                if c.type == "date" and (d := f.parse_input_date(v, spec.date_rules.input_formats)):
                    dates.append(d)
        self.domains = [d for d, _ in domains.most_common(3)] or ["example.com"]
        self.dates = sorted(dates)

    def person(self) -> tuple[str, str]:
        return self.rng.choice(FIRST), self.rng.choice(LAST)

    def text(self, col: str) -> str:
        if "company" in col:
            return self.rng.choice(COMPANIES)
        if "country" in col:
            return self.rng.choice(COUNTRIES)
        a, b = self.person()
        return f"{a} {b}"

    def email(self, hint: str) -> str:
        local = "".join(ch for ch in hint.lower() if ch.isalnum() or ch == " ").replace(" ", ".").strip(".")
        return f"{local or 'user'}{self.rng.randint(10, 999)}@{self.rng.choice(self.domains)}"

    def date(self):
        import datetime as dt
        if self.dates:
            lo, hi = self.dates[0], self.dates[-1]
            span = max((hi - lo).days, 1)
            d = lo + dt.timedelta(days=self.rng.randint(0, span))
        else:
            d = dt.date(2026, 1, 1) + dt.timedelta(days=self.rng.randint(0, 300))
        return d

    def clean_record(self) -> dict[str, str]:
        """A fresh record whose dedupe key is unused (valid, normalised values)."""
        for _ in range(200):
            first, last = self.person()
            rec = {}
            for c in self.spec.columns:
                if c.type == "text":
                    rec[c.name] = f"{first} {last}" if not any(h in c.name for h in ("company", "country")) else self.text(c.name)
                elif c.type == "email":
                    rec[c.name] = self.email(f"{first} {last}")
                else:
                    rec[c.name] = _render_date(self.date(), "YYYY-MM-DD")
            k = input_key(self.spec, rec)
            if k is not None and k not in self.used_keys:
                self.used_keys.add(k)
                return rec
        raise RuntimeError("could not generate a unique canary key")

    def messy(self, rec: dict[str, str]) -> dict[str, str]:
        """Same meaning, dirty form: extra spaces, mixed-case email, other date format."""
        out = {}
        alt = [fm for fm in self.spec.date_rules.input_formats if fm != "YYYY-MM-DD"] or ["YYYY-MM-DD"]
        for c in self.spec.columns:
            v = rec[c.name]
            if c.type == "text":
                out[c.name] = "  " + v.replace(" ", "   ") + " "
            elif c.type == "email":
                out[c.name] = " " + v.upper() + " "
            else:
                d = f.parse_date(v, "YYYY-MM-DD")
                out[c.name] = _render_date(d, self.rng.choice(alt))
        return out


# ------------------------------------------------------------------ canaries

def generate_canaries(spec: Spec, rows: list[dict[str, str]], rng: random.Random) -> tuple[list[dict], list[dict]]:
    """Returns (canary rows without IDs, expectations with placeholder indexes)."""
    g = _Gen(spec, rows, rng)
    email_cols = [c.name for c in spec.columns if c.type == "email"]
    date_cols = [c.name for c in spec.columns if c.type == "date"]
    req_email = next((c.name for c in spec.columns if c.type == "email" and c.required), None)
    canaries, exps = [], []

    def add(row: dict, exp: dict | None):
        canaries.append(row)
        if exp is not None:
            exps.append(exp)

    # 1-2: fixable mess -> kept with exact normalised values
    for _ in range(2):
        clean = g.clean_record()
        add(g.messy(clean), {"type": "kept_with_values", "idx": len(canaries), "values": clean})
    # 3: one clean record kept as-is
    clean = g.clean_record()
    add(dict(clean), {"type": "kept_with_values", "idx": len(canaries), "values": clean})
    # 4-5: duplicate pair (same key, different surface form)
    clean = g.clean_record()
    i = len(canaries)
    add(dict(clean), None)
    add(g.messy(clean), None)
    exps.append({"type": "duplicate_group", "idxs": [i, i + 1]})
    # 6: unfixable email -> rejected INVALID_EMAIL
    if email_cols:
        bad = g.clean_record()
        local, dom = bad[email_cols[0]].split("@")
        bad[email_cols[0]] = f"{local}@{dom.split('.')[0]}"  # drop the TLD
        add(bad, {"type": "rejected_with_reason", "idx": len(canaries), "reason": "INVALID_EMAIL"})
    # 7: missing required -> rejected MISSING_REQUIRED
    if req_email:
        miss = g.clean_record()
        miss[req_email] = ""
        add(miss, {"type": "rejected_with_reason", "idx": len(canaries), "reason": "MISSING_REQUIRED"})
    # 8: impossible date -> rejected INVALID_DATE
    if date_cols:
        bad = g.clean_record()
        bad[date_cols[0]] = f"31/02/{g.date().year:04d}"
        add(bad, {"type": "rejected_with_reason", "idx": len(canaries), "reason": "INVALID_DATE"})

    _self_check(spec, canaries, exps)
    return canaries, exps


def _require(cond: bool, what: str) -> None:
    if not cond:
        raise RuntimeError(f"canary self-check failed: {what}")


def _self_check(spec: Spec, canaries: list[dict], exps: list[dict]) -> None:
    """Every expectation must follow from the evaluator's own rules."""
    for e in exps:
        if e["type"] == "kept_with_values":
            src, want = canaries[e["idx"]], e["values"]
            for c in spec.columns:
                v = src[c.name]
                if c.type == "text":
                    _require(f.text_key(v) == f.text_key(want[c.name]) and f.is_clean_text(want[c.name]), "text")
                elif c.type == "email":
                    _require(f.norm_email(v) == want[c.name] and f.is_valid_email(want[c.name]), "email")
                elif v.strip():
                    d = f.parse_input_date(v, spec.date_rules.input_formats)
                    _require(d is not None and f.format_date(d, "YYYY-MM-DD") == want[c.name], "date")
            _require(not any(reason_is_true(spec, r, src) for r in spec.reason_codes), "kept canary has a true rejection reason")
        elif e["type"] == "rejected_with_reason":
            _require(reason_is_true(spec, e["reason"], canaries[e["idx"]]), "rejection reason")
        elif e["type"] == "duplicate_group":
            keys = {input_key(spec, canaries[i]) for i in e["idxs"]}
            _require(len(keys) == 1 and None not in keys, "duplicate group key")


# ------------------------------------------------------------------ work file

def build_work_file(spec: Spec, input_bytes: bytes, rng: random.Random) -> WorkBuild:
    rows = read_input(spec, input_bytes)
    check_winnable(spec, rows)
    canaries, exps = generate_canaries(spec, rows, rng)

    ids: set[str] = set()

    def new_id() -> str:
        while True:
            x = "%012x" % rng.getrandbits(48)
            if x not in ids:
                ids.add(x)
                return x

    entries = [(new_id(), r, False) for r in rows]
    canary_ids = [new_id() for _ in canaries]
    for cid, row in zip(canary_ids, canaries):
        entries.insert(rng.randint(0, len(entries)), (cid, row, True))

    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(spec.work_header())
    for rid, row, _ in entries:
        w.writerow([rid, *(row[c] for c in spec.column_names)])
    work_bytes = buf.getvalue().encode("utf-8")
    if len(entries) > spec.dialect.max_rows or len(work_bytes) > spec.dialect.max_bytes:
        raise InputError("input too large once control records are added")

    def map_exp(e: dict) -> dict:
        e = dict(e)
        if "idx" in e:
            e["record_id"] = canary_ids[e.pop("idx")]
        if "idxs" in e:
            e["record_ids"] = [canary_ids[i] for i in e.pop("idxs")]
        return e

    canary_set = {
        "salt": "%064x" % rng.getrandbits(256),
        "template_id": spec.template_id,
        "canaries": [{"record_id": cid, "row": {c: row[c] for c in spec.column_names}} for cid, row in zip(canary_ids, canaries)],
        "expectations": [map_exp(e) for e in exps],
    }
    cb = canonical_bytes(canary_set)
    return WorkBuild(work_bytes, sha256_hex(work_bytes), sha256_hex(input_bytes), canary_set, cb, sha256_hex(cb),
                     len(rows), len(entries))
