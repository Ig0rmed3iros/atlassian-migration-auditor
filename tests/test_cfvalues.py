"""Type-aware custom-field VALUE normalization (auditor.cfvalues.normalize_cf).

The contract: two sides of a migration store the SAME value under different
instance-specific shapes (option ids, account ids, timestamp spellings, wiki vs
ADF). normalize_cf must fingerprint them to the SAME token when the value is
genuinely the same, and a DIFFERENT token when it differs — matched by NAME /
VALUE, never by instance id. Empty values return None (not compared). Each
result carries a `kind` so the comparator can badge identity/representation-
sensitive types as verify-rather-than-certain-loss.
"""
from auditor.cfvalues import normalize_cf, SENSITIVE_KINDS


def _fp(raw, schema, dialect="adf"):
    r = normalize_cf(raw, schema, dialect)
    return r["fp"] if r else None


def _kind(raw, schema, dialect="adf"):
    r = normalize_cf(raw, schema, dialect)
    return r["kind"] if r else None


# --- emptiness ------------------------------------------------------------

def test_empty_values_return_none():
    for empty in (None, "", [], {}):
        assert normalize_cf(empty, {"type": "string"}) is None


def test_zero_and_false_are_not_empty():
    # 0 is a real number value; it must NOT be treated as absent.
    assert normalize_cf(0, {"type": "number"}) is not None


def test_valueless_and_blank_shapes_are_absent():
    # Shapes that RENDER as empty must normalize to None, or they false-mismatch
    # against a faithfully-cleared (None) counterpart.
    assert normalize_cf({"value": None}, {"type": "option"}) is None
    assert normalize_cf([None], {"type": "array", "items": "option"}) is None
    assert normalize_cf("   ", {"type": "string"}) is None          # whitespace
    assert normalize_cf({"type": "doc", "content": []},             # empty ADF
                        {"type": "string",
                         "custom": "x:textarea"}, dialect="adf") is None


def test_date_with_time_suffix_is_normalized_like_datetime():
    s = {"type": "date"}
    # a date field that arrives with a tz/time suffix on one side must still
    # match the same instant spelled differently (defensive norm_ts).
    assert normalize_cf("2024-01-01T00:00:00.000+0000", s)["fp"] == \
           normalize_cf("2024-01-01T00:00:00Z", s)["fp"]


# --- number ---------------------------------------------------------------

def test_number_int_and_float_spellings_match():
    s = {"type": "number"}
    assert _fp(1, s) == _fp(1.0, s)


def test_number_distinct_values_differ_and_sign_matters():
    s = {"type": "number"}
    assert _fp(1, s) != _fp(2, s)
    assert _fp(-1, s) != _fp(1, s)   # canon must not collapse the sign


# --- date / datetime ------------------------------------------------------

def test_datetime_spellings_of_same_instant_match():
    s = {"type": "datetime"}
    assert _fp("2024-01-01T00:00:00.000+0000", s) == \
           _fp("2024-01-01T00:00:00Z", s)


def test_date_distinct_days_differ():
    s = {"type": "date"}
    assert _fp("2024-01-01", s) != _fp("2024-01-02", s)


# --- single select / option ----------------------------------------------

def test_option_matched_by_value_not_id():
    s = {"type": "option"}
    # Same option value, DIFFERENT instance option ids -> must match.
    src = {"value": "High", "id": "10001"}
    tgt = {"value": "High", "id": "29999"}
    assert _fp(src, s) == _fp(tgt, s)


def test_option_distinct_values_differ():
    s = {"type": "option"}
    assert _fp({"value": "High"}, s) != _fp({"value": "Low"}, s)


# --- multi-select / labels (order-independent) ----------------------------

def test_multiselect_is_order_independent():
    s = {"type": "array", "items": "option"}
    a = [{"value": "A", "id": "1"}, {"value": "B", "id": "2"}]
    b = [{"value": "B", "id": "9"}, {"value": "A", "id": "8"}]
    assert _fp(a, s) == _fp(b, s)


def test_labels_array_order_independent_and_distinct_sets_differ():
    s = {"type": "array", "items": "string"}
    assert _fp(["x", "y"], s) == _fp(["y", "x"], s)
    assert _fp(["x", "y"], s) != _fp(["x", "z"], s)


# --- user picker (identity-sensitive) -------------------------------------

def test_user_matched_by_displayname_not_accountid():
    s = {"type": "user"}
    src = {"displayName": "Jane Doe", "accountId": "abc:123"}
    tgt = {"displayName": "Jane Doe", "accountId": "557058:zzz"}
    assert _fp(src, s) == _fp(tgt, s)


def test_user_kind_is_sensitive():
    assert _kind({"displayName": "Jane"}, {"type": "user"}) in SENSITIVE_KINDS


# --- cascading select -----------------------------------------------------

def test_cascade_parent_and_child_compose():
    s = {"type": "option-with-child"}
    parent_only = {"value": "Parent"}
    with_child = {"value": "Parent", "child": {"value": "Child"}}
    assert _fp(parent_only, s) != _fp(with_child, s)
    assert _kind(with_child, s) in SENSITIVE_KINDS


# --- rich text across dialects --------------------------------------------

def test_rich_text_kind_and_dialect_routing():
    s = {"type": "string",
         "custom": "com.atlassian.jira.plugin.system.customfieldtypes:textarea"}
    # wiki (DC) side and a plain-string ADF side both normalize without error;
    # the kind marks it representation-sensitive (cross-dialect).
    k = _kind("h1. Title\n\nbody", s, dialect="wiki")
    assert k == "text_rich" and k in SENSITIVE_KINDS


def test_plain_text_is_exact_not_sensitive():
    s = {"type": "string",
         "custom": "com.atlassian.jira.plugin.system.customfieldtypes:textfield"}
    assert _kind("CODE-123", s) == "text"
    assert "text" not in SENSITIVE_KINDS


# --- app-provided / unknown -----------------------------------------------

def test_app_provided_type_is_compared_but_sensitive():
    s = {"type": "any", "custom": "com.thirdparty.app:weirdfield"}
    r = normalize_cf({"opaque": [1, 2]}, s)
    assert r is not None and r["kind"] == "app" and r["kind"] in SENSITIVE_KINDS


def test_app_provided_equal_payloads_match_regardless_of_key_order():
    s = {"type": "any", "custom": "com.thirdparty.app:weirdfield"}
    assert _fp({"a": 1, "b": 2}, s) == _fp({"b": 2, "a": 1}, s)
