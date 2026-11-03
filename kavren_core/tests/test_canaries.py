import random
from pathlib import Path

import pytest

from kavren_core.canaries import InputError, build_work_file
from kavren_core.canonical import load_canonical, sha256_hex
from kavren_core.evaluator import evaluate, parse_work
from kavren_core.templates import get_template

from reference_cleaner import clean

FX = Path(__file__).resolve().parents[2] / "fixtures" / "contacts"
CONTACTS = get_template("contacts-v1").build_spec()
LEADS = get_template("leads-v1").build_spec()
LEADS_INPUT = (
    "company,contact_email,country,created_at\n"
    "Ajala Foods,ops@ajalafoods.ng,Nigeria,2026-02-01\n"
    "  ajala   foods , OPS@AjalaFoods.ng ,nigeria,01/02/2026\n"
    "Tema Freight,sales@temafreight.com.gh,Ghana,3 Mar 2026\n"
    "Nairobi Paints,info@nairobipaints,Kenya,2026-03-04\n"
    "Kigali Beans,,Rwanda,2026-03-05\n"
    "Lagos Looms,hello@lagosloom.ng,Nigeria,2026-03-06\n"
    "Accra Solar,team@accrasolar.com.gh,Ghana,7 Mar 2026\n"
    "Kano Leather,info@kanoleather.ng,,08/03/2026\n"
    "Dakar Docks,ops@dakardocks.sn,Senegal,2026-03-09\n"
).encode()  # 2 of 9 rows unfixable (22%): under the 25% cap


def test_unwinnable_job_is_refused_at_draft_time():
    """2 unfixable rows out of 5 (40%) would make even a perfect provider exceed the
    25% cap, so the draft is refused instead of creating a job nobody can pass."""
    small = b"\n".join(LEADS_INPUT.split(b"\n")[:6]) + b"\n"
    with pytest.raises(InputError, match="no provider could pass"):
        build_work_file(LEADS, small, random.Random(0))


def test_duplicate_with_gap_is_not_counted_as_unavoidable():
    from kavren_core.canaries import read_input, unavoidable_rejections
    rows = read_input(CONTACTS, (FX / "input.csv").read_bytes())
    assert unavoidable_rejections(CONTACTS, rows) == 4  # r05 r06 r09 r13; r07 can merge into r08


@pytest.mark.parametrize("seed", range(25))
@pytest.mark.parametrize("spec,inp", [(CONTACTS, (FX / "input.csv").read_bytes()), (LEADS, LEADS_INPUT)], ids=["contacts", "leads"])
def test_correct_cleaning_passes_including_canaries(seed, spec, inp):
    wb = build_work_file(spec, inp, random.Random(seed))
    ev = evaluate(spec, wb.work_bytes, clean(spec, wb.work_bytes), wb.canary_set)
    assert ev.verdict == "PASS", ev.failing_checks
    assert ev.report["canary_failures"] == 0


@pytest.mark.parametrize("seed", range(10))
def test_deleting_canaries_is_caught(seed):
    wb = build_work_file(CONTACTS, (FX / "input.csv").read_bytes(), random.Random(seed))
    sub = clean(CONTACTS, wb.work_bytes).decode().split("\n")
    cids = {c["record_id"] for c in wb.canary_set["canaries"]}
    sub = [l for l in sub if l.split(",")[0] not in cids]
    ev = evaluate(CONTACTS, wb.work_bytes, "\n".join(sub).encode(), wb.canary_set)
    assert ev.verdict == "FAIL" and "H1" in ev.failing_checks and "C2" in ev.failing_checks


def test_work_file_contains_input_and_canaries_with_unique_random_ids():
    inp = (FX / "input.csv").read_bytes()
    wb = build_work_file(CONTACTS, inp, random.Random(1))
    order, recs = parse_work(CONTACTS, wb.work_bytes)
    assert len(order) == len(set(order)) == wb.work_rows == wb.input_rows + len(wb.canary_set["canaries"])
    assert all(len(r) == 12 and int(r, 16) >= 0 for r in order)
    assert wb.input_hash == sha256_hex(inp)
    assert sha256_hex(wb.canary_bytes) == wb.canary_commitment
    assert load_canonical(wb.canary_bytes) == wb.canary_set
    assert len(wb.canary_set["salt"]) == 64
    # different randomness -> different ids, positions and salt
    wb2 = build_work_file(CONTACTS, inp, random.Random(2))
    assert wb2.canary_commitment != wb.canary_commitment and wb2.work_hash != wb.work_hash


def test_canaries_do_not_collide_with_real_keys():
    inp = (FX / "input.csv").read_bytes()
    real_emails = {l.split(",")[-2].strip().lower() for l in inp.decode().splitlines()[1:] if "@" in l}
    for seed in range(20):
        wb = build_work_file(CONTACTS, inp, random.Random(seed))
        for c in wb.canary_set["canaries"]:
            assert c["row"]["email"].strip().lower() not in real_emails


@pytest.mark.parametrize("bad,msg", [
    (b"name,email\nA,a@b.co\n", "header"),
    (b"full_name,email,signup_date\n", "no data rows"),
    (b"\xef\xbb\xbffull_name,email,signup_date\nA,a@b.co,\n", "BOM"),
    (b"full_name,email,signup_date\r\nA,a@b.co,\r\n", "CR"),
])
def test_bad_input_rejected(bad, msg):
    with pytest.raises(InputError, match=msg):
        build_work_file(CONTACTS, bad, random.Random(0))


def test_input_header_order_does_not_matter():
    inp = b"email,signup_date,full_name\na@example.com,2026-01-01,Ada Obi\n"
    wb = build_work_file(CONTACTS, inp, random.Random(3))
    order, recs = parse_work(CONTACTS, wb.work_bytes)
    assert any(r["full_name"] == "Ada Obi" and r["email"] == "a@example.com" for r in recs.values())
