import pytest
from kavren_core.canonical import CanonicalJSONError, canonical_bytes, canonical_hash, load_canonical


def test_sorted_compact_utf8():
    assert canonical_bytes({"b": 1, "a": ["ọ", True, None]}) == '{"a":["ọ",true,null],"b":1}'.encode()


def test_floats_rejected():
    with pytest.raises(CanonicalJSONError):
        canonical_bytes({"x": 1.5})
    with pytest.raises(CanonicalJSONError):
        load_canonical(b'{"x":1.0}')


def test_non_canonical_rejected():
    with pytest.raises(CanonicalJSONError):
        load_canonical(b'{"b":1, "a":2}')
    assert load_canonical(b'{"a":2,"b":1}') == {"a": 2, "b": 1}


def test_hash_stable():
    assert canonical_hash({"a": 1}) == canonical_hash({"a": 1})
    assert canonical_hash({"a": 1}) == "015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862"
