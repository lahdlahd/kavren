"""Build the generated fixture files from the hand-written sources.

Hand-written (fixtures/contacts/src/): input.csv, canaries.csv, submission_pass.csv,
answer_key.json. This script only (1) assigns record IDs, (2) builds the work file,
(3) builds the committed canary set, (4) applies the documented one-line edits that
turn the pass submission into each fail submission, and (5) writes job.json.

Deterministic: running it twice produces identical bytes. Fixture record IDs are
derived from labels so they are stable; production IDs are random.

Usage: python3 fixtures/build_fixtures.py
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path

from kavren_core.canonical import canonical_bytes, sha256_hex
from kavren_core.templates import get_template

ROOT = Path(__file__).resolve().parent / "contacts"
SRC = ROOT / "src"
SALT = hashlib.sha256(b"kavren fixture salt v1 - NOT SECRET, NOT FOR PRODUCTION").hexdigest()


def rid(label: str) -> str:
    return hashlib.sha256(f"kavren-fixture-v1:{label}".encode()).hexdigest()[:12]


def read_csv(path: Path) -> list[list[str]]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


def write_csv(rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def main() -> None:
    spec = get_template("contacts-v1").build_spec()
    key = json.loads((SRC / "answer_key.json").read_text(encoding="utf-8"))
    cols = list(spec.column_names)

    # ---- input
    input_bytes = (SRC / "input.csv").read_bytes()
    inp = read_csv(SRC / "input.csv")
    header, body = inp[0], inp[1:]
    assert [h.strip() for h in header] == cols
    labelled = [(f"r{i:02d}", row) for i, row in enumerate(body, 1)]

    # ---- canaries
    can = read_csv(SRC / "canaries.csv")
    assert can[0] == ["label", *cols]
    canary_rows = {r[0]: r[1:] for r in can[1:]}

    # ---- work file: canaries inserted after the rows named in canary_positions
    after = {v: k for k, v in key["canary_positions"].items()}
    work_order: list[tuple[str, list[str]]] = []
    for label, row in labelled:
        work_order.append((label, row))
        if label in after:
            k = after[label]
            work_order.append((k, canary_rows[k]))
    work_bytes = write_csv([["record_id", *cols]] + [[rid(l), *r] for l, r in work_order])
    labels_map = {l: rid(l) for l, _ in work_order}
    assert len(set(labels_map.values())) == len(labels_map)

    # ---- committed canary set
    def map_exp(e: dict) -> dict:
        e = dict(e)
        if "label" in e:
            e["record_id"] = rid(e.pop("label"))
        if "labels" in e:
            e["record_ids"] = [rid(x) for x in e.pop("labels")]
        return e

    canary_set = {
        "salt": SALT,
        "template_id": spec.template_id,
        "canaries": [{"record_id": rid(k), "row": dict(zip(cols, v))} for k, v in canary_rows.items()],
        "expectations": [map_exp(e) for e in key["canary_expectations"]],
    }
    canary_bytes = canonical_bytes(canary_set)

    # ---- submissions
    sub = read_csv(SRC / "submission_pass.csv")
    sub_header, sub_rows = sub[0], sub[1:]
    assert sub_header == list(spec.submission_header())
    by_label = {r[0]: r for r in sub_rows}

    def emptied(label: str, outcome: str, merged_into: str = "", reason: str = "") -> list[str]:
        return [label, outcome, merged_into, reason] + [""] * len(cols)

    def variant(edits: dict[str, list[str] | None]) -> list[list[str]]:
        out = []
        for r in sub_rows:
            if r[0] in edits:
                if edits[r[0]] is not None:
                    out.append(edits[r[0]])
            else:
                out.append(r)
        return out

    variants: dict[str, list[list[str]]] = {
        "pass": sub_rows,
        "fail_empty": [],
        "fail_deletion": variant({l: None for l in ["r05", "r06", "r09", "r13", "k4", "k5"]}),
        "fail_fabricated": variant({"r05": ["r05", "kept", "", "", "Ngozi Eze", "ngozi.eze@example.com", "2026-04-01"]}),
        "fail_false_reject": variant({l: emptied(l, "rejected", reason="INVALID_EMAIL") for l in ["r01", "r11", "r12", "r18", "r20"]}),
        "fail_fake_merge": variant({"r12": emptied("r12", "merged", merged_into="r01")}),
        "fail_format": variant({
            "r02": [*by_label["r02"][:6], "03/02/2026"],
            "r03": [*by_label["r03"][:5], "CHIDI@EXAMPLE.COM", by_label["r03"][6]],
            "r12": [*by_label["r12"][:6], "10 May 2026"],
        }),
        "fail_canary_dup": variant({"k3": ["k3", "kept", "", "", "Daniel Okoro", "daniel.okoro@example.com", "2026-08-09"]}),
        "fail_canary_date": variant({"k1": [*by_label["k1"][:6], "2026-07-08"]}),
    }
    assert set(variants) == set(key["submissions"]), "answer key and variants must list the same submissions"

    out_dir = ROOT / "submissions"
    out_dir.mkdir(exist_ok=True)
    sub_hashes = {}
    for name, rows in variants.items():
        mapped = [[rid(r[0]), r[1], rid(r[2]) if r[2] else "", *r[3:]] for r in rows]
        data = write_csv([sub_header] + mapped)
        (out_dir / f"{name}.csv").write_bytes(data)
        sub_hashes[name] = sha256_hex(data)

    # ---- write generated files
    (ROOT / "input.csv").write_bytes(input_bytes)
    (ROOT / "work.csv").write_bytes(work_bytes)
    (ROOT / "canary_reveal.json").write_bytes(canary_bytes)
    (ROOT / "spec.json").write_bytes(spec.canonical())
    (ROOT / "labels.json").write_text(json.dumps(labels_map, indent=2) + "\n", encoding="utf-8")
    job = {
        "template_id": spec.template_id,
        "spec_hash": spec.hash(),
        "input_hash": sha256_hex(input_bytes),
        "work_hash": sha256_hex(work_bytes),
        "canary_commitment": sha256_hex(canary_bytes),
        "submission_hashes": sub_hashes,
        "rows": {"input": len(body), "canaries": len(canary_rows), "work": len(work_order)},
    }
    (ROOT / "job.json").write_text(json.dumps(job, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(job, indent=2))


if __name__ == "__main__":
    main()
