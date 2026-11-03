"""Evaluator vs the hand-checked answer key, plus edge cases.

The answer key (fixtures/contacts/src/answer_key.json) was written by hand before
the evaluator existed. These tests require an EXACT match: verdict, failing
checks, every (check, code, record) violation, and the canary failure count.
"""

import json
from pathlib import Path

import pytest

from kavren_core.canonical import load_canonical, sha256_hex
from kavren_core.evaluator import EvaluatorInputError, build_deliverable, evaluate, reason_is_true
from kavren_core.spec import load_spec

FX = Path(__file__).resolve().parents[2] / "fixtures" / "contacts"
KEY = json.loads((FX / "src" / "answer_key.json").read_text(encoding="utf-8"))
SPEC = load_spec((FX / "spec.json").read_bytes())
WORK = (FX / "work.csv").read_bytes()
CANARIES = load_canonical((FX / "canary_reveal.json").read_bytes())
LABELS = json.loads((FX / "labels.json").read_text())
ID2LABEL = {v: k for k, v in LABELS.items()}
PASS = (FX / "submissions" / "pass.csv").read_bytes()


def run(sub: bytes, canaries=CANARIES):
    return evaluate(SPEC, WORK, sub, canaries)


def violations(ev) -> set:
    return {(c["check"], v["code"], ID2LABEL.get(v["record_id"], v["record_id"]))
            for c in ev.report["checks"] for v in c["violations"]}


@pytest.mark.parametrize("name", sorted(KEY["submissions"]))
def test_matches_hand_checked_answer_key(name):
    exp = KEY["submissions"][name]
    ev = run((FX / "submissions" / f"{name}.csv").read_bytes())
    assert ev.verdict == exp["verdict"]
    assert sorted(ev.failing_checks) == sorted(exp.get("failing_checks", []))
    assert ev.report["canary_failures"] == exp.get("canary_failures", 0)
    want = set()
    for check, code, label in exp["violations"]:
        if label == "*all 26*":
            want |= {(check, code, l) for l in LABELS}
        else:
            want.add((check, code, label))
    assert violations(ev) == want


def test_report_is_deterministic_and_hash_is_of_report_bytes():
    a, b = run(PASS), run(PASS)
    assert a.report_bytes == b.report_bytes
    assert a.report_hash == sha256_hex(a.report_bytes)
    assert a.report["output_hash"] == sha256_hex(PASS)


def test_report_contains_no_cell_values():
    for name in KEY["submissions"]:
        rb = run((FX / "submissions" / f"{name}.csv").read_bytes()).report_bytes.decode()
        for rec in load_canonical((FX / "canary_reveal.json").read_bytes())["canaries"]:
            for v in rec["row"].values():
                if v.strip():
                    assert v.strip() not in rb
        for value in ["amaka.obi@example.com", "Okonkwo", "Adébáyọ̀", "2026-01-15"]:
            assert value not in rb, (name, value)


def test_report_never_names_canaries():
    ev = run((FX / "submissions" / "fail_canary_dup.csv").read_bytes())
    h1 = next(c for c in ev.report["checks"] if c["check"] == "H1")
    assert h1["violations"] == [] and ev.report["canary_failures"] == 1


# ----------------------------------------------------------------- rejection truth (C2f) and merge truth (C2e)

def test_reason_truth_reads_the_input_not_the_label():
    r05 = dict(zip(SPEC.column_names, ["Ngozi Eze", "ngozi.eze@example", "2026-04-01"]))
    assert reason_is_true(SPEC, "INVALID_EMAIL", r05)
    assert not reason_is_true(SPEC, "MISSING_REQUIRED", r05)
    assert not reason_is_true(SPEC, "INVALID_DATE", r05)
    good = dict(zip(SPEC.column_names, ["Amaka Obi", "  AMAKA@Example.com ", "2026-01-15"]))
    for r in ("INVALID_EMAIL", "MISSING_REQUIRED", "INVALID_DATE", "OTHER"):
        assert not reason_is_true(SPEC, r, good)


def _edit(label: str, new_fields: list[str]) -> bytes:
    rid = LABELS[label]
    lines = PASS.decode().split("\n")
    out = []
    for ln in lines:
        if ln.startswith(rid + ","):
            ln = ",".join([rid] + new_fields)
        out.append(ln)
    return "\n".join(out).encode()


def test_wrong_but_plausible_reason_is_caught():
    # r05 is a genuinely bad record, but the provider gives the wrong reason
    ev = run(_edit("r05", ["rejected", "", "MISSING_REQUIRED", "", "", ""]))
    assert violations(ev) == {("C2", "REASON_NOT_TRUE", "r05")}


def test_unknown_reason_code_is_a_helper_error():
    ev = run(_edit("r05", ["rejected", "", "LOOKS_WRONG", "", "", ""]))
    assert ("C2", "BAD_HELPER_FIELDS", "r05") in violations(ev)


def test_merge_into_rejected_or_self_is_bad_target():
    ev = run(_edit("r04", ["merged", LABELS["r05"], "", "", "", ""]))
    assert ("C2", "BAD_MERGE_TARGET", "r04") in violations(ev)
    ev = run(_edit("r04", ["merged", LABELS["r04"], "", "", "", ""]))
    assert ("C2", "BAD_MERGE_TARGET", "r04") in violations(ev)


def test_rejected_row_must_not_carry_data():
    ev = run(_edit("r06", ["rejected", "", "MISSING_REQUIRED", "Ibrahim Musa", "", ""]))
    assert violations(ev) == {("C2", "BAD_HELPER_FIELDS", "r06")}


def test_case_change_in_names_is_allowed():
    ev = run(_edit("r01", ["kept", "", "", "AMAKA OBI", "amaka.obi@example.com", "2026-01-15"]))
    assert ev.verdict == "PASS"


# ----------------------------------------------------------------- C1 dialect

@pytest.mark.parametrize("mutate,code", [
    (lambda b: b"\xef\xbb\xbf" + b, "BAD_DIALECT"),
    (lambda b: b.replace(b"\n", b"\r\n"), "BAD_DIALECT"),
    (lambda b: b.rstrip(b"\n"), "BAD_DIALECT"),
    (lambda b: b + b"x,y\n", "BAD_DIALECT"),
    (lambda b: b.replace(b"Amaka Obi", b"\"Amaka Obi"), "BAD_DIALECT"),
    (lambda b: b"\xff" + b, "BAD_DIALECT"),
    (lambda b: b.replace(b"record_id,_outcome", b"id,_outcome", 1), "BAD_HEADER"),
    (lambda b: b.replace(b",kept,", b",keep,", 1), "BAD_OUTCOME"),
])
def test_c1_failures_skip_the_rest(mutate, code):
    ev = run(mutate(PASS))
    c1 = next(c for c in ev.report["checks"] if c["check"] == "C1")
    assert c1["status"] == "fail" and code in {v["code"] for v in c1["violations"]}
    assert ev.verdict == "FAIL"
    assert all(c["status"] == "skipped" for c in ev.report["checks"] if c["check"] != "C1")


def test_duplicate_and_unknown_records():
    lines = PASS.decode().split("\n")
    lines.insert(2, lines[1])                       # duplicate r01
    lines.insert(3, "ffffffffffff,rejected,,INVALID_EMAIL,,,")  # unknown id
    ev = run("\n".join(lines).encode())
    v = violations(ev)
    assert ("C2", "DUPLICATE_RECORD", "r01") in v and ("C2", "UNKNOWN_RECORD", "ffffffffffff") in v


# ----------------------------------------------------------------- protocol inputs and deliverable

def test_tampered_canary_set_is_refused():
    bad = json.loads(json.dumps(CANARIES))
    bad["canaries"][0]["row"]["email"] = "changed@example.com"
    with pytest.raises(EvaluatorInputError):
        run(PASS, bad)


def test_deliverable_has_only_kept_real_records():
    d = build_deliverable(SPEC, PASS, CANARIES).decode().splitlines()
    assert d[0] == "full_name,email,signup_date"
    body = d[1:]
    kept_real = [l for l, r in KEY["records"].items() if r["outcome"] == "kept" and l.startswith("r")]
    assert len(body) == len(kept_real) + 3  # + one kept row per real duplicate group
    assert not any("bisi" in l or "esther" in l or "daniel" in l for l in body)


def test_without_canaries_h1_passes_vacuously():
    ev = run(PASS, None)
    assert ev.verdict == "PASS" and ev.report["canary_commitment"] is None
