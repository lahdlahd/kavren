import pytest
from pydantic import ValidationError
from kavren_core.spec import Column, DateRules, Spec, load_spec
from kavren_core.templates import TEMPLATES, YOCTO_PER_NEAR, get_template, select_template


def test_templates_build_valid_specs_and_round_trip():
    for t in TEMPLATES.values():
        spec = t.build_spec()
        data = spec.canonical()
        assert load_spec(data) == spec
        assert len(spec.hash()) == 64
        assert len(data) < 1500  # stored on-chain; keep it small


def test_contacts_spec_hash_is_pinned():
    # Changing the schema or template changes this hash: that must be deliberate.
    spec = get_template("contacts-v1").build_spec()
    assert spec.hash() == open("tests/contacts-v1.spec.sha256").read().strip()


def test_unknown_field_rejected():
    obj = get_template("contacts-v1").build_spec().to_json_obj()
    obj["extra"] = 1
    with pytest.raises(ValidationError):
        Spec.model_validate(obj)


def test_dedupe_key_must_be_required_column():
    with pytest.raises(ValidationError):
        Spec(template_id="x-v1", template_version=1,
             columns=(Column(name="a", type="text", required=True), Column(name="b", type="email", required=False)),
             dedupe_key=("b",), date_rules=DateRules(input_formats=("YYYY-MM-DD",), output_format="YYYY-MM-DD"),
             max_reject_pct=10)


def test_noncanonical_spec_bytes_rejected():
    data = get_template("contacts-v1").build_spec().canonical()
    with pytest.raises(Exception):
        load_spec(data.replace(b",", b", ", 1))


def test_select_template():
    assert select_template(["email", "full_name", "signup_date"]) == "contacts-v1"
    assert select_template([" full_name", "email ", "signup_date"]) == "contacts-v1"
    assert select_template(["full_name", "email"]) is None
    assert select_template(["full_name", "email", "signup_date"], allowed=["leads-v1"]) is None
    assert select_template(["company", "contact_email", "country", "created_at"]) == "leads-v1"


def test_price():
    t = get_template("contacts-v1")
    assert t.price_yocto(20) == 240 * 10**21  # 0.24 NEAR
    assert t.price_yocto(650) == 1500 * 10**21  # 1.5 NEAR
    assert YOCTO_PER_NEAR == 10**24
