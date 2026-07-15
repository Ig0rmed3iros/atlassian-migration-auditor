import httpx
import pytest
from auditor.client import Connection, JiraClient
from auditor.remediation.reaudit import compute_closure


def mk(handler):
    conn = Connection(auth_type="pat", site_url="https://t.atlassian.net",
                      deployment="cloud", email="a@b.c", api_token="x")
    return JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                      sleeper=lambda s: None)


def test_created_object_now_present_is_closed():
    def handler(req):
        if str(req.url.path) == "/rest/api/3/status":
            return httpx.Response(200, json=[{"name": "Triage"}])  # now exists
        return httpx.Response(404, json={})
    findings = [{"area": "statuses", "name": "Triage", "kind": "missing_in_tgt"}]
    res = compute_closure(mk(handler), findings,
                          touched_areas={"statuses"})
    assert res["closed"] == 1 and res["still_open"] == 0


def test_absent_object_stays_open():
    def handler(req):
        if str(req.url.path) == "/rest/api/3/status":
            return httpx.Response(200, json=[])    # still missing
        return httpx.Response(404, json={})
    findings = [{"area": "statuses", "name": "Triage", "kind": "missing_in_tgt"}]
    res = compute_closure(mk(handler), findings, touched_areas={"statuses"})
    assert res["closed"] == 0 and res["still_open"] == 1


def test_untouched_area_increments_unchanged():
    """A finding whose area was never touched must land in 'unchanged', not verified."""
    def handler(req):
        return httpx.Response(500, json={})  # should never be called
    findings = [{"area": "statuses", "name": "Triage", "kind": "missing_in_tgt"}]
    res = compute_closure(mk(handler), findings, touched_areas=set())
    assert res["unchanged"] == 1
    assert res["closed"] == 0 and res["still_open"] == 0
    assert res.get("not_verifiable", 0) == 0


def test_touched_area_without_precheck_is_not_verifiable():
    """A finding whose area was touched but absent from _PRECHECK must land in
    'not_verifiable', not 'unchanged', and must never increment closed/still_open."""
    def handler(req):
        return httpx.Response(500, json={})  # should never be called
    findings = [{"area": "no_precheck_area", "name": "X", "kind": "missing_in_tgt"}]
    res = compute_closure(mk(handler), findings, touched_areas={"no_precheck_area"})
    assert res["not_verifiable"] == 1
    assert res["unchanged"] == 0
    assert res["closed"] == 0 and res["still_open"] == 0


def test_mixed_batch_all_three_counters():
    """closed + still_open + unchanged must all be correct in a single call."""
    def handler(req):
        path = str(req.url.path)
        if path == "/rest/api/3/status":
            return httpx.Response(200, json=[{"name": "Done"}])   # closed
        if path == "/rest/api/3/priority":
            return httpx.Response(200, json=[])                   # still_open
        return httpx.Response(404, json={})
    findings = [
        {"area": "statuses",   "name": "Done",   "kind": "missing_in_tgt"},  # closed
        {"area": "priorities", "name": "Low",    "kind": "missing_in_tgt"},  # still_open
        {"area": "resolutions","name": "Fixed",  "kind": "missing_in_tgt"},  # unchanged
    ]
    res = compute_closure(mk(handler), findings,
                          touched_areas={"statuses", "priorities"})
    assert res["closed"] == 1
    assert res["still_open"] == 1
    assert res["unchanged"] == 1
    assert res.get("not_verifiable", 0) == 0


def test_link_type_closure_handles_issuelinktypes_wrapper():
    # /issueLinkType returns {"issueLinkTypes": [...]}, not a flat list nor a
    # {values:[...]} envelope — closure must unwrap it or every link-type fix
    # reads as still-open even after a successful create.
    def handler(req):
        if str(req.url.path) == "/rest/api/3/issueLinkType":
            return httpx.Response(200, json={"issueLinkTypes": [{"name": "Blocks"}]})
        return httpx.Response(404, json={})
    findings = [{"area": "link_types", "name": "Blocks", "kind": "missing_in_tgt"}]
    res = compute_closure(mk(handler), findings, touched_areas={"link_types"})
    assert res["closed"] == 1 and res["still_open"] == 0


# --- I8: closure must judge BY KIND. option_mismatch (the field already
# exists) must NOT be closed by name-presence; it is closed only when the
# previously-missing options now exist on the target field. ------------------

def _opt_finding(missing):
    return {"area": "custom_fields", "name": "Severity", "kind": "option_mismatch",
            "detail": {"missing_options_in_tgt": missing}}


def _opt_target_handler(target_options):
    def handler(req):
        p = str(req.url.path)
        if p == "/rest/api/3/field":
            return httpx.Response(200, json=[
                {"id": "customfield_T", "name": "Severity", "custom": True}])
        if p == "/rest/api/3/field/customfield_T/context":
            return httpx.Response(200, json={"values": [{"id": "100"}]})
        if p == "/rest/api/3/field/customfield_T/context/100/option":
            return httpx.Response(200, json={
                "values": [{"value": v} for v in target_options]})
        return httpx.Response(404, json={})
    return handler


def test_option_mismatch_not_falsely_closed_by_presence():
    # The field EXISTS on the target but the missing option is still absent —
    # the old name-presence logic reported FIXED_CLEAN; closure must call it open.
    res = compute_closure(mk(_opt_target_handler(["High", "Low"])),
                          [_opt_finding(["Blocker"])],
                          touched_areas={"custom_fields"})
    assert res["closed"] == 0 and res["still_open"] == 1
    assert res["not_verifiable"] == 0


def test_option_mismatch_closed_after_options_added():
    # Both previously-missing options now exist on the target field -> closed.
    res = compute_closure(mk(_opt_target_handler(["High", "Blocker", "Trivial"])),
                          [_opt_finding(["Blocker", "Trivial"])],
                          touched_areas={"custom_fields"})
    assert res["closed"] == 1 and res["still_open"] == 0


def test_option_mismatch_partially_added_stays_open():
    # Only one of the two missing options was added -> not fully closed.
    res = compute_closure(mk(_opt_target_handler(["High", "Blocker"])),
                          [_opt_finding(["Blocker", "Trivial"])],
                          touched_areas={"custom_fields"})
    assert res["closed"] == 0 and res["still_open"] == 1


def test_non_verifiable_kind_is_surfaced_not_closed():
    # A touched type_mismatch can't be cheaply re-verified -> not_verifiable,
    # never counted as closed even though the field name is present.
    def handler(req):
        if str(req.url.path) == "/rest/api/3/field":
            return httpx.Response(200, json=[
                {"id": "customfield_T", "name": "Severity", "custom": True}])
        return httpx.Response(404, json={})
    findings = [{"area": "custom_fields", "name": "Severity",
                 "kind": "type_mismatch"}]
    res = compute_closure(mk(handler), findings, touched_areas={"custom_fields"})
    assert res["not_verifiable"] == 1
    assert res["closed"] == 0 and res["still_open"] == 0
