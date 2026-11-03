"""A plain, rule-following cleaner used ONLY by tests: it produces the submission
a perfectly correct provider would produce, so generated work files can be
checked end to end against the evaluator."""
import csv
import io

from kavren_core import formats as f
from kavren_core.evaluator import input_key, parse_work, reason_is_true


def clean(spec, work_bytes: bytes) -> bytes:
    order, work = parse_work(spec, work_bytes)
    kept_by_key, out = {}, []
    for rid in order:
        rec = work[rid]
        reason = next((r for r in ("MISSING_REQUIRED", "INVALID_EMAIL", "INVALID_DATE") if reason_is_true(spec, r, rec)), None)
        k = input_key(spec, rec)
        if reason and k is not None and reason == "MISSING_REQUIRED":
            # a duplicate with a gap is better merged than rejected (if a kept twin exists or appears later)
            twins = [o for o in order if o != rid and input_key(spec, work[o]) == k
                     and not any(reason_is_true(spec, r, work[o]) for r in ("MISSING_REQUIRED", "INVALID_EMAIL", "INVALID_DATE"))]
            if twins:
                reason = None
        if reason:
            out.append([rid, "rejected", "", reason] + [""] * len(spec.columns))
            continue
        if k in kept_by_key:
            out.append([rid, "merged", kept_by_key[k], ""] + [""] * len(spec.columns))
            continue
        kept_by_key[k] = rid
        group = [rec] + [work[o] for o in order if o != rid and input_key(spec, work[o]) == k]
        vals = []
        for c in spec.columns:
            v = next((g[c.name] for g in group if g[c.name].strip()), "")
            if c.type == "text":
                vals.append(f.norm_text(v))
            elif c.type == "email":
                vals.append(f.norm_email(v))
            else:
                d = f.parse_input_date(v, spec.date_rules.input_formats) if v.strip() else None
                vals.append(f.format_date(d, "YYYY-MM-DD") if d else "")
        out.append([rid, "kept", "", ""] + vals)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(spec.submission_header())
    w.writerows(out)
    return buf.getvalue().encode()
