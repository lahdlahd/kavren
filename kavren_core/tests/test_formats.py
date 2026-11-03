import datetime as dt
import pytest
from kavren_core import formats as f


@pytest.mark.parametrize("raw,clean", [("  Tunde   Bakare ", "Tunde Bakare"), ("a\tb\nc", "a b c"), ("", "")])
def test_norm_text(raw, clean):
    assert f.norm_text(raw) == clean


def test_text_key_casefold():
    assert f.text_key(" YUSUF  BELLO") == f.text_key("Yusuf Bello")


@pytest.mark.parametrize("email", ["amaka.obi@example.com", "femi+news@example.com", "peter.jr@example.co.uk", "a@b.io"])
def test_valid_emails(email):
    assert f.is_valid_email(email)


@pytest.mark.parametrize("email", [
    "ngozi.eze@example", "grace.udo@example.com.", "halima..sani@example.com", ".a@example.com",
    "a.@example.com", "ruth.nnamdi@examplecom", "A@example.com", "a@-example.com", "a@example.c",
    "a b@example.com", "", "@example.com", "a@", "x" * 65 + "@example.com",
])
def test_invalid_emails(email):
    assert not f.is_valid_email(email)


def test_norm_email():
    assert f.norm_email(" Tunde.Bakare@Example.com ") == "tunde.bakare@example.com"


FMTS = ("DD/MM/YYYY", "YYYY-MM-DD", "D MMM YYYY")


@pytest.mark.parametrize("raw,expected", [
    ("03/02/2026", dt.date(2026, 2, 3)),
    ("2026-01-15", dt.date(2026, 1, 15)),
    ("5 Mar 2026", dt.date(2026, 3, 5)),
    ("10 may 2026", dt.date(2026, 5, 10)),
    (" 02/06/2026 ", dt.date(2026, 6, 2)),
])
def test_parse_input_date(raw, expected):
    assert f.parse_input_date(raw, FMTS) == expected


@pytest.mark.parametrize("raw", ["31/02/2026", "13/13/2026", "3/02/2026", "2026/01/15", "5 March 2026", "", "05 Mrz 2026"])
def test_parse_input_date_invalid(raw):
    assert f.parse_input_date(raw, FMTS) is None


def test_format_date():
    assert f.format_date(dt.date(2026, 2, 3), "YYYY-MM-DD") == "2026-02-03"


def test_bad_format_rejected():
    with pytest.raises(f.DateFormatError):
        f.validate_format("YYYY-QQ-DD")
