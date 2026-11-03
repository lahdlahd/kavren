"""The deterministic evaluator: checks C1-C8 and H1 (docs/EVALUATION_RULES.md).

evaluate(spec, work_bytes, submission_bytes, canary_set) -> Evaluation

Pure function of its inputs: no clock, no randomness, no network. The report is
canonical JSON without timestamps or cell values, so report_hash is reproducible
by anyone holding the same files.

Rejection reasons and merges are NEVER taken on trust: C2(e) and C2(f) re-derive
them from the work file's own values and the spec's rules.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Optional

from . import EVALUATOR_VERSION
from . import formats as f
from .canonical import canonical_bytes, sha256_hex
from .spec import HELPER_COLUMNS, OUTCOMES, RECORD_ID_COLUMN, Spec

CHECKS = ("C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "H1")


class DialectError(ValueError):
    pass


class EvaluatorInputError(ValueError):
    """The protocol's own inputs (spec, work file, canary set) are inconsistent."""


# ---------------------------------------------------------------------------- parsing

def parse_csv(data: bytes, spec: Spec) -> list[list[str]]:
    """Parse bytes under the spec's dialect. Raises DialectError with a reason."""
    d = spec.dialect
    if len(data) > d.max_bytes:
        raise DialectError("file too large")
    if data.startswith(b"\xef\xbb\xbf"):
        raise DialectError("BOM not allowed")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise DialectError("not UTF-8") from None
    if "\r" in text:
        raise DialectError("CR characters not allowed; use LF line endings")
    if not text.endswith("\n"):
        raise DialectError("file must end with a newline")
    try:
        rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=",", quotechar='"', strict=True))
    except csv.Error as e:
        raise DialectError(f"malformed CSV: {e}") from None
    if not rows:
        raise DialectError("missing header")
    width = len(rows[0])
    for i, r in enumerate(rows):
        if len(r) != width:
            raise DialectError(f"row {i} has {len(r)} fields, header has {width}")
    if len(rows) - 1 > d.max_rows:
        raise DialectError("too many rows")
    return rows


def parse_work(spec: Spec, work_bytes: bytes) -> tuple[list[str], dict[str, dict[str, str]]]:
    """Returns (record_ids in file order, {record_id: {column: raw value}})."""
    try:
        rows = parse_csv(work_bytes, spec)
    except DialectError as e:
        raise EvaluatorInputError(f"work file invalid: {e}") from None
    if tuple(rows[0]) != spec.work_header():
        raise EvaluatorInputError("work file header does not match spec")
    order, recs = [], {}
    for r in rows[1:]:
        rid = r[0]
        if rid in recs:
            raise EvaluatorInputError(f"duplicate record_id in work file: {rid}")
        order.append(rid)
        recs[rid] = dict(zip(spec.column_names, r[1:]))
    return order, recs


# ---------------------------------------------------------------------------- value helpers

def _empty(v: str) -> bool:
    return v.strip() == ""


def input_key(spec: Spec, values: dict[str, str]) -> Optional[tuple]:
    """Dedupe key from (raw) values; None if any key column is empty."""
    parts = []
    for col in spec.dedupe_key:
        v = values[col]
        if _empty(v):
            return None
        t = spec.column(col).type
        parts.append(f.norm_email(v) if t == "email" else f.text_key(v))
    return tuple(parts)


def reason_is_true(spec: Spec, reason: str, values: dict[str, str]) -> bool:
    """C2(f): is the stated rejection reason actually true of the work-file record?"""
    if reason == "MISSING_REQUIRED":
        return any(_empty(values[c.name]) for c in spec.columns if c.required)
    if reason == "INVALID_EMAIL":
        return any(
            c.type == "email" and not _empty(values[c.name]) and not f.is_valid_email(f.norm_email(values[c.name]))
            for c in spec.columns
        )
    if reason == "INVALID_DATE":
        return any(
            c.type == "date" and not _empty(values[c.name])
            and f.parse_input_date(values[c.name], spec.date_rules.input_formats) is None
            for c in spec.columns
        )
    return False


# ---------------------------------------------------------------------------- result types

@dataclass
class Evaluation:
    verdict: str
    report: dict
    report_bytes: bytes
    report_hash: str
    canary_detail: list[dict] = field(default_factory=list)  # only for after the job is final

    @property
    def failing_checks(self) -> list[str]:
        return [c["check"] for c in self.report["checks"] if c["status"] == "fail"]


class _Collector:
    def __init__(self, order_index: dict[str, int]):
        self.v: dict[str, set[tuple[str, Optional[str]]]] = {c: set() for c in CHECKS}
        self.order = order_index

    def add(self, check: str, code: str, record_id: Optional[str] = None) -> None:
        self.v[check].add((code, record_id))

    def sorted(self, check: str) -> list[dict]:
        def key(item):
            code, rid = item
            return (code, -1 if rid is None else self.order.get(rid, 10**9), rid or "")
        return [{"code": c, "record_id": r} for c, r in sorted(self.v[check], key=key)]


# ---------------------------------------------------------------------------- evaluate

def evaluate(spec: Spec, work_bytes: bytes, submission_bytes: bytes, canary_set: Optional[dict]) -> Evaluation:
    work_order, work = parse_work(spec, work_bytes)
    order_index = {rid: i for i, rid in enumerate(work_order)}
    cols = spec.column_names
    out = _Collector(order_index)
    skipped: set[str] = set()
    canary_failures = 0
    canary_detail: list[dict] = []

    if canary_set is not None:
        _check_canary_set(canary_set, spec, work)

    # ---------------- C1
    sub_rows: list[list[str]] = []
    try:
        rows = parse_csv(submission_bytes, spec)
        if tuple(rows[0]) != spec.submission_header():
            out.add("C1", "BAD_HEADER")
        else:
            sub_rows = rows[1:]
            for r in sub_rows:
                if r[1] not in OUTCOMES:
                    out.add("C1", "BAD_OUTCOME", r[0])
    except DialectError:
        out.add("C1", "BAD_DIALECT")

    if out.v["C1"]:
        skipped = set(CHECKS) - {"C1"}
    else:
        canary_failures, canary_detail = _run_checks(spec, work, work_order, sub_rows, cols, out, canary_set)

    checks = []
    for c in CHECKS:
        if c in skipped:
            status = "skipped"
        elif c == "H1":
            status = "pass" if canary_failures == 0 else "fail"
        else:
            status = "fail" if out.v[c] else "pass"
        # H1 never lists which records are canaries in the report (count only)
        checks.append({"check": c, "status": status, "violations": [] if c == "H1" else out.sorted(c)})
    verdict = "FAIL" if any(c["status"] != "pass" for c in checks) else "PASS"

    report = {
        "evaluator_version": EVALUATOR_VERSION,
        "spec_hash": spec.hash(),
        "work_hash": sha256_hex(work_bytes),
        "output_hash": sha256_hex(submission_bytes),
        "canary_commitment": sha256_hex(canonical_bytes(canary_set)) if canary_set is not None else None,
        "verdict": verdict,
        "checks": checks,
        "canary_failures": canary_failures,
    }
    rb = canonical_bytes(report)
    return Evaluation(verdict, report, rb, sha256_hex(rb), canary_detail)


def _check_canary_set(cs: dict, spec: Spec, work: dict) -> None:
    if set(cs) != {"salt", "template_id", "canaries", "expectations"}:
        raise EvaluatorInputError("canary set has unexpected fields")
    if cs["template_id"] != spec.template_id:
        raise EvaluatorInputError("canary set template does not match spec")
    for c in cs["canaries"]:
        if work.get(c["record_id"]) != c["row"]:
            raise EvaluatorInputError(f"canary {c['record_id']} not present unchanged in work file")


def _run_checks(spec, work, work_order, sub_rows, cols, out: _Collector, canary_set):
    reasons = set(spec.reason_codes)

    # ---------------- C2(a): identity of records
    first: dict[str, list[str]] = {}
    for r in sub_rows:
        rid = r[0]
        if rid not in work:
            out.add("C2", "UNKNOWN_RECORD", rid)
        elif rid in first:
            out.add("C2", "DUPLICATE_RECORD", rid)
        else:
            first[rid] = r
    for rid in work_order:
        if rid not in first:
            out.add("C2", "MISSING_RECORD", rid)

    sub = {rid: {"outcome": r[1], "merged_into": r[2], "reason": r[3], "values": dict(zip(cols, r[4:]))}
           for rid, r in first.items()}
    kept = [rid for rid in (r[0] for r in sub_rows) if rid in sub and sub[rid]["outcome"] == "kept"]
    kept = list(dict.fromkeys(kept))  # file order, unique
    kept_set = set(kept)

    # ---------------- C2(b)-(f): helper fields, merge truth, rejection truth
    for rid, s in sub.items():
        data_empty = all(v == "" for v in s["values"].values())
        if s["outcome"] == "kept":
            if s["merged_into"] or s["reason"]:
                out.add("C2", "BAD_HELPER_FIELDS", rid)
        elif s["outcome"] == "merged":
            if s["reason"] or not data_empty:
                out.add("C2", "BAD_HELPER_FIELDS", rid)
            target = s["merged_into"]
            if target == rid or target not in kept_set:
                out.add("C2", "BAD_MERGE_TARGET", rid)
            else:
                k1, k2 = input_key(spec, work[rid]), input_key(spec, work[target])
                if k1 is None or k1 != k2:
                    out.add("C2", "MERGE_KEY_MISMATCH", rid)
        else:  # rejected
            if s["merged_into"] or not data_empty or s["reason"] not in reasons:
                out.add("C2", "BAD_HELPER_FIELDS", rid)
            elif not reason_is_true(spec, s["reason"], work[rid]):
                out.add("C2", "REASON_NOT_TRUE", rid)

    # ---------------- C3: no duplicate keys among kept output rows
    seen: set[tuple] = set()
    for rid in kept:
        k = input_key(spec, sub[rid]["values"])
        if k is None:
            continue
        if k in seen:
            out.add("C3", "DUPLICATE_KEY", rid)
        seen.add(k)

    # ---------------- C4, C5, C6, C7 on kept rows
    groups: dict[str, list[str]] = {rid: [rid] for rid in kept}
    for rid, s in sub.items():
        if s["outcome"] == "merged" and s["merged_into"] in groups and s["merged_into"] != rid:
            groups[s["merged_into"]].append(rid)

    out_fmt = spec.date_rules.output_format
    in_fmts = spec.date_rules.input_formats
    for rid in kept:
        vals = sub[rid]["values"]
        members = groups[rid]
        for c in spec.columns:
            v = vals[c.name]
            if c.type == "email" and not _empty(v) and not f.is_valid_email(f.norm_email(v)):
                out.add("C4", "INVALID_EMAIL", rid)
            if c.required and _empty(v):
                out.add("C5", "MISSING_REQUIRED", rid)
            # C6 form
            date_form_ok = True
            if c.type == "text" and not f.is_clean_text(v):
                out.add("C6", "NOT_NORMALISED", rid)
            elif c.type == "email" and not f.is_clean_email(v):
                out.add("C6", "NOT_NORMALISED", rid)
            elif c.type == "date" and not _empty(v) and f.parse_date(v, out_fmt) is None:
                out.add("C6", "BAD_DATE_FORMAT", rid)
                date_form_ok = False
            # C7 provenance
            sources = [work[m][c.name] for m in members if not _empty(work[m][c.name])]
            if _empty(v):
                if sources:
                    out.add("C7", "NOT_DERIVED_FROM_INPUT", rid)
                continue
            if c.type == "text":
                ok = f.text_key(v) in {f.text_key(s) for s in sources}
            elif c.type == "email":
                ok = f.norm_email(v) in {f.norm_email(s) for s in sources}
            else:
                if not date_form_ok:
                    continue
                ok = f.parse_date(v, out_fmt) in {f.parse_input_date(s, in_fmts) for s in sources} - {None}
            if not ok:
                out.add("C7", "NOT_DERIVED_FROM_INPUT", rid)

    # ---------------- C8 rejection cap (integer arithmetic)
    rejected = sum(1 for s in sub.values() if s["outcome"] == "rejected")
    if rejected * 100 > spec.max_reject_pct * len(work_order):
        out.add("C8", "REJECT_CAP_EXCEEDED")

    # ---------------- H1 canaries
    failures, detail = 0, []
    if canary_set is not None:
        for e in canary_set["expectations"]:
            ok = _expectation_holds(e, sub, cols)
            failures += 0 if ok else 1
            detail.append({"expectation": e, "holds": ok})
    return failures, detail


def _expectation_holds(e: dict, sub: dict, cols) -> bool:
    t = e["type"]
    if t == "kept_with_values":
        s = sub.get(e["record_id"])
        return bool(s) and s["outcome"] == "kept" and all(s["values"].get(k) == v for k, v in e["values"].items())
    if t == "rejected_with_reason":
        s = sub.get(e["record_id"])
        return bool(s) and s["outcome"] == "rejected" and s["reason"] == e["reason"]
    if t == "duplicate_group":
        ids = e["record_ids"]
        if any(i not in sub for i in ids):
            return False
        kept = [i for i in ids if sub[i]["outcome"] == "kept"]
        if len(kept) != 1:
            return False
        return all(sub[i]["outcome"] == "merged" and sub[i]["merged_into"] == kept[0] for i in ids if i != kept[0])
    raise EvaluatorInputError(f"unknown expectation type {t!r}")


# ---------------------------------------------------------------------------- deliverable

def build_deliverable(spec: Spec, submission_bytes: bytes, canary_set: dict) -> bytes:
    """After PASS: kept rows only, canaries removed, template columns only."""
    rows = parse_csv(submission_bytes, spec)
    canary_ids = {c["record_id"] for c in canary_set["canaries"]}
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(spec.column_names)
    for r in rows[1:]:
        if r[1] == "kept" and r[0] not in canary_ids:
            w.writerow(r[4:])
    return buf.getvalue().encode("utf-8")
