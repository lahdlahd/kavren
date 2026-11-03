import json
import shutil
from pathlib import Path

from kavren_core.cli import main

FX = Path(__file__).resolve().parents[2] / "fixtures" / "contacts"


def test_verify_pass(capsys):
    assert main(["verify", str(FX), "--submission", "pass"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "PASS" and all(out["integrity"].values())


def test_expected_report_hash_mismatch_is_an_error(capsys):
    assert main(["verify", str(FX), "--submission", "pass", "--expect-report-hash", "0" * 64]) == 3


def test_tampered_canary_reveal_fails_integrity(tmp_path, capsys):
    d = tmp_path / "job"
    shutil.copytree(FX, d)
    p = d / "canary_reveal.json"
    p.write_bytes(p.read_bytes().replace(b"Esther", b"Esthar"))
    assert main(["verify", str(d), "--submission", "pass"]) == 2
    assert json.loads(capsys.readouterr().out)["result"] == "INTEGRITY_FAILURE"


def test_tampered_submission_fails_integrity(tmp_path, capsys):
    d = tmp_path / "job"
    shutil.copytree(FX, d)
    p = d / "submissions" / "fail_fabricated.csv"
    p.write_bytes((d / "submissions" / "pass.csv").read_bytes())  # swap in a passing file
    assert main(["verify", str(d), "--submission", "fail_fabricated"]) == 2
