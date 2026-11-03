"""Sanity checks on the hand-written fixtures and answer key.

This is NOT the evaluator. It cross-checks the hand-written answer key two ways:
(1) against the value rules in kavren_core.formats, record by record, and
(2) against the hand-written pass submission. A disagreement means the answer
key, the fixture or a value rule is wrong, and must be resolved by hand.
"""

import csv
import io
import json
from pathlib import Path

import pytest

from kavren_core import formats as f
from kavren_core.canonical import canonical_bytes, load_canonical, sha256_hex
from kavren_core.templates import get_template

FX = Path(__file__).resolve().parents[2] / "fixtures" / "contacts"
KEY = json.loads((FX / "src" / "answer_key.json").read_text(encoding="utf-8"))
SPEC = get_template("contacts-v1").build_spec()
COLS = list(SPEC.column_names)
DATES = SPEC.date_rules.input_formats


def rows(path):
    return list(csv.reader(io.StringIO(path.read_text(encoding="utf-8"))))


LABELS = json.loads((FX / "labels.json").read_text())
ID2LABEL = {v: k for k, v in LABELS.items()}
WORK = {ID2LABEL[r[0]]: dict(zip(COLS, r[1:])) for r in rows(FX / "work.csv")[1:]}


def test_every_record_covered_exactly_once():
    covered = list(KEY["records"]) + [m for g in KEY["groups"] for m in g["members"]]
    assert sorted(covered) == sorted(WORK)


@pytest.mark.parametrize("label", sorted(KEY["records"]))
def test_single_records_match_value_rules(label):
    exp, src = KEY["records"][label], WORK[label]
    if exp["outcome"] == "kept":
        v = exp["values"]
        assert f.text_key(v["full_name"]) == f.text_key(src["full_name"]) and f.is_clean_text(v["full_name"])
        assert v["email"] == f.norm_email(src["email"]) and f.is_valid_email(v["email"])
        if src["signup_date"].strip():
            assert f.format_date(f.parse_input_date(src["signup_date"], DATES), "YYYY-MM-DD") == v["signup_date"]
        else:
            assert v["signup_date"] == ""
    else:
        reason = exp["reason"]
        if reason == "MISSING_REQUIRED":
            assert any(not src[c.name].strip() for c in SPEC.columns if c.required)
        elif reason == "INVALID_EMAIL":
            assert src["email"].strip() and not f.is_valid_email(f.norm_email(src["email"]))
        elif reason == "INVALID_DATE":
            assert src["signup_date"].strip() and f.parse_input_date(src["signup_date"], DATES) is None
        else:
            pytest.fail(reason)


@pytest.mark.parametrize("group", KEY["groups"], ids=lambda g: "+".join(g["members"]))
def test_groups_share_key_and_values_derive(group):
    srcs = [WORK[m] for m in group["members"]]
    keys = {f.norm_email(s["email"]) for s in srcs}
    assert keys == {group["values"]["email"]}
    names = {f.text_key(s["full_name"]) for s in srcs if s["full_name"].strip()}
    assert f.text_key(group["values"]["full_name"]) in names
    dates = {f.format_date(d, "YYYY-MM-DD") for s in srcs if (d := f.parse_input_date(s["signup_date"], DATES))}
    assert set(group["values"]["signup_date"]) == dates


def test_pass_submission_agrees_with_answer_key():
    sub = rows(FX / "submissions" / "pass.csv")
    assert tuple(sub[0]) == SPEC.submission_header()
    got = {ID2LABEL[r[0]]: r for r in sub[1:]}
    assert sorted(got) == sorted(WORK)
    for label, exp in KEY["records"].items():
        r = got[label]
        assert r[1] == exp["outcome"]
        if exp["outcome"] == "kept":
            assert dict(zip(COLS, r[4:])) == exp["values"]
        else:
            assert r[3] == exp["reason"] and r[4:] == ["", "", ""]
    for g in KEY["groups"]:
        kept = [m for m in g["members"] if got[m][1] == "kept"]
        assert len(kept) == 1
        for m in g["members"]:
            if m != kept[0]:
                assert got[m][1] == "merged" and ID2LABEL[got[m][2]] == kept[0]
        v = dict(zip(COLS, got[kept[0]][4:]))
        assert v["full_name"] == g["values"]["full_name"] and v["email"] == g["values"]["email"]
        assert v["signup_date"] in g["values"]["signup_date"]
    rejected = sum(1 for r in sub[1:] if r[1] == "rejected")
    assert rejected * 100 <= SPEC.max_reject_pct * len(WORK)


def test_canary_commitment_and_contents():
    data = (FX / "canary_reveal.json").read_bytes()
    obj = load_canonical(data)
    assert sha256_hex(data) == json.loads((FX / "job.json").read_text())["canary_commitment"]
    assert canonical_bytes(obj) == data and len(obj["salt"]) == 64
    for c in obj["canaries"]:
        assert WORK[ID2LABEL[c["record_id"]]] == c["row"]


def test_job_hashes():
    job = json.loads((FX / "job.json").read_text())
    assert job["spec_hash"] == SPEC.hash()
    assert job["work_hash"] == sha256_hex((FX / "work.csv").read_bytes())
    assert job["input_hash"] == sha256_hex((FX / "input.csv").read_bytes())
    for name, h in job["submission_hashes"].items():
        assert sha256_hex((FX / "submissions" / f"{name}.csv").read_bytes()) == h


def test_fail_variants_differ_only_where_documented():
    base = {r[0]: r for r in rows(FX / "submissions" / "pass.csv")[1:]}
    for name in KEY["submissions"]:
        if name in ("pass", "fail_empty"):
            continue
        var = {r[0]: r for r in rows(FX / "submissions" / f"{name}.csv")[1:]}
        changed = {ID2LABEL[k] for k in base if var.get(k) != base[k]}
        edit = KEY["submissions"][name]["edit"]
        assert changed and all(lbl in edit for lbl in changed), (name, changed)
