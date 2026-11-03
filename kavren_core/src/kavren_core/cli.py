"""kavren CLI.

  kavren verify fixtures/contacts --submission pass
  kavren verify --spec S --work W --submission-file F --canaries C --job J

What `verify` proves, given the files:
  1. spec, work file and canary set hash to the values recorded for the job;
  2. the canary reveal opens the committed canary_commitment;
  3. re-running the deterministic checks gives this verdict and this report_hash.
It does NOT prove the evaluator service ran honestly; it lets you check its claim.
Job values come from job.json for now; reading them from the chain is added with
the API milestone.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .canonical import load_canonical, sha256_hex
from .evaluator import build_deliverable, evaluate
from .spec import load_spec


def _verify(a) -> int:
    if a.dir:
        d = Path(a.dir)
        spec_p, work_p, can_p, job_p = d / "spec.json", d / "work.csv", d / "canary_reveal.json", d / "job.json"
        if not a.submission:
            sys.exit("--submission NAME is required with a directory")
        sub_p = d / "submissions" / f"{a.submission}.csv"
    else:
        spec_p, work_p, can_p, job_p, sub_p = map(Path, (a.spec, a.work, a.canaries, a.job, a.submission_file))

    spec_b, work_b, can_b, sub_b = (p.read_bytes() for p in (spec_p, work_p, can_p, sub_p))
    job = json.loads(job_p.read_text())
    spec = load_spec(spec_b)

    checks = {
        "spec_hash_matches_job": spec.hash() == job["spec_hash"],
        "work_hash_matches_job": sha256_hex(work_b) == job["work_hash"],
        "canary_reveal_opens_commitment": sha256_hex(can_b) == job["canary_commitment"],
    }
    if "submission_hashes" in job and a.submission:
        checks["submission_hash_matches_job"] = sha256_hex(sub_b) == job["submission_hashes"].get(a.submission)
    if not all(checks.values()):
        print(json.dumps({"integrity": checks, "result": "INTEGRITY_FAILURE"}, indent=2))
        return 2

    ev = evaluate(spec, work_b, sub_b, load_canonical(can_b))
    result = {
        "integrity": checks,
        "verdict": ev.verdict,
        "failing_checks": ev.failing_checks,
        "canary_failures": ev.report["canary_failures"],
        "report_hash": ev.report_hash,
        "output_hash": ev.report["output_hash"],
    }
    if a.show_violations:
        result["violations"] = {c["check"]: c["violations"] for c in ev.report["checks"] if c["violations"]}
        result["canary_detail"] = [{"type": x["expectation"]["type"], "holds": x["holds"]} for x in ev.canary_detail]
    if a.expect_report_hash:
        result["report_hash_matches_expected"] = ev.report_hash == a.expect_report_hash
    if a.report_out:
        Path(a.report_out).write_bytes(ev.report_bytes)
    if a.deliverable_out and ev.verdict == "PASS":
        Path(a.deliverable_out).write_bytes(build_deliverable(spec, sub_b, load_canonical(can_b)))
    print(json.dumps(result, indent=2))
    return 0 if result.get("report_hash_matches_expected", True) else 3


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="kavren")
    sp = ap.add_subparsers(dest="cmd", required=True)
    v = sp.add_parser("verify", help="re-run the evaluation and check every hash")
    v.add_argument("dir", nargs="?", help="job directory (spec.json, work.csv, canary_reveal.json, job.json, submissions/)")
    v.add_argument("--submission", help="submission name inside DIR/submissions/")
    v.add_argument("--spec"); v.add_argument("--work"); v.add_argument("--canaries"); v.add_argument("--job")
    v.add_argument("--submission-file")
    v.add_argument("--expect-report-hash")
    v.add_argument("--show-violations", action="store_true")
    v.add_argument("--report-out"); v.add_argument("--deliverable-out")
    a = ap.parse_args(argv)
    return _verify(a)


if __name__ == "__main__":
    sys.exit(main())
