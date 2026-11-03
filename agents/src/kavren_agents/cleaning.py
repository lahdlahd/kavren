"""Deterministic executor for a cleaning PLAN.

The LLM never writes cell values. It only decides, per record:
  - reject (with a reason code), or
  - merge into another record (a duplicate), or
  - keep (the default).
This module turns that plan into a submission using ONLY the spec's allowed
transformations (trim/collapse text, trim+lowercase email, reformat dates from a
declared input format). It cannot invent values, by construction.
"""
from __future__ import annotations

import csv
import io

from kavren_core import formats as f
from kavren_core.evaluator import parse_work
from kavren_core.spec import Spec


class PlanError(ValueError):
    pass


def validate_plan(spec: Spec, work_ids: set[str], plan: object) -> dict:
    if not isinstance(plan, dict) or set(plan) - {"rejects", "merges", "notes"}:
        raise PlanError("plan must be an object with 'rejects' and 'merges'")
    rejects, merges = plan.get("rejects", []), plan.get("merges", [])
    if not isinstance(rejects, list) or not isinstance(merges, list):
        raise PlanError("'rejects' and 'merges' must be lists")
    seen: set[str] = set()
    out_r, out_m = [], []
    for r in rejects:
        if not isinstance(r, dict) or r.get("record_id") not in work_ids or r.get("reason") not in spec.reason_codes:
            raise PlanError(f"bad reject entry: {r!r}")
        if r["record_id"] in seen:
            raise PlanError(f"record decided twice: {r['record_id']}")
        seen.add(r["record_id"])
        out_r.append({"record_id": r["record_id"], "reason": r["reason"]})
    for m in merges:
        if not isinstance(m, dict) or m.get("record_id") not in work_ids or m.get("into") not in work_ids \
                or m["record_id"] == m["into"]:
            raise PlanError(f"bad merge entry: {m!r}")
        if m["record_id"] in seen:
            raise PlanError(f"record decided twice: {m['record_id']}")
        seen.add(m["record_id"])
        out_m.append({"record_id": m["record_id"], "into": m["into"]})
    targets = {m["into"] for m in out_m}
    if targets & seen:
        raise PlanError("a merge target must itself be kept")
    return {"rejects": out_r, "merges": out_m}


def execute(spec: Spec, work_bytes: bytes, plan: dict) -> bytes:
    order, work = parse_work(spec, work_bytes)
    plan = validate_plan(spec, set(order), plan)
    reject = {r["record_id"]: r["reason"] for r in plan["rejects"]}
    merge = {m["record_id"]: m["into"] for m in plan["merges"]}
    groups: dict[str, list[str]] = {}
    for rid, into in merge.items():
        groups.setdefault(into, []).append(rid)
    rows = []
    for rid in order:
        if rid in reject:
            rows.append([rid, "rejected", "", reject[rid]] + [""] * len(spec.columns))
        elif rid in merge:
            rows.append([rid, "merged", merge[rid], ""] + [""] * len(spec.columns))
        else:
            members = [rid] + groups.get(rid, [])
            rows.append([rid, "kept", "", ""] + [_value(spec, c, [work[m][c.name] for m in members]) for c in spec.columns])
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(spec.submission_header())
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _value(spec: Spec, col, candidates: list[str]) -> str:
    """First non-empty candidate (own value first), transformed as the spec allows."""
    for v in candidates:
        if not v.strip():
            continue
        if col.type == "text":
            return f.norm_text(v)
        if col.type == "email":
            return f.norm_email(v)
        d = f.parse_input_date(v, spec.date_rules.input_formats)
        if d is not None:
            return f.format_date(d, spec.date_rules.output_format)
    return ""
