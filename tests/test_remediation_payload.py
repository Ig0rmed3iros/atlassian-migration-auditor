import httpx
from auditor.client import Connection, JiraClient
from auditor.remediation.payload import capture_config_payload


def mk(handler):
    conn = Connection(auth_type="pat", site_url="https://s.atlassian.net",
                      deployment="cloud", email="a@b.c", api_token="x")
    return JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                      sleeper=lambda s: None)


def test_custom_field_payload_gathers_type_contexts_and_options():
    def handler(req):
        p = str(req.url.path)
        if p == "/rest/api/3/field":
            return httpx.Response(200, json=[
                {"id": "customfield_1", "name": "Severity", "custom": True,
                 "schema": {"custom": "com.atlassian.jira.plugin.system."
                            "customfieldtypes:select", "type": "option"}}])
        if p.endswith("/context"):
            return httpx.Response(200, json={"values": [
                {"id": "10", "name": "Default Context"}], "isLast": True})
        if p.endswith("/context/10/option"):
            return httpx.Response(200, json={"values": [
                {"value": "High"}, {"value": "Low"}], "isLast": True})
        return httpx.Response(404, json={})
    finding = {"area": "custom_fields", "name": "Severity", "kind": "missing_in_tgt"}
    pl = capture_config_payload(mk(handler), finding)
    assert pl["type"] == "select"
    assert pl["contexts"][0]["options"] == ["High", "Low"]


def test_status_payload_is_name_and_category():
    def handler(req):
        if str(req.url.path) == "/rest/api/3/status":
            return httpx.Response(200, json=[
                {"name": "Triage", "statusCategory": {"key": "new"}}])
        return httpx.Response(404, json={})
    finding = {"area": "statuses", "name": "Triage", "kind": "missing_in_tgt"}
    pl = capture_config_payload(mk(handler), finding)
    assert pl == {"name": "Triage", "category": "TODO"}


def test_unfixable_area_returns_none():
    finding = {"area": "workflows", "name": "WF", "kind": "missing_in_tgt"}
    assert capture_config_payload(mk(lambda r: httpx.Response(404)), finding) is None


def test_non_missing_kind_returns_none_for_all_areas():
    # The early-exit guard covers every area uniformly — area_error /
    # type_mismatch findings have no recreatable payload. (option_mismatch on
    # custom_fields is the one exception and is captured; see its own tests.)
    for area in ("statuses", "priorities", "resolutions", "issue_types",
                 "link_types", "custom_fields"):
        finding = {"area": area, "name": "X", "kind": "type_mismatch"}
        assert capture_config_payload(mk(lambda r: httpx.Response(200, json=[])),
                                      finding) is None, f"expected None for {area}"


def test_link_types_non_200_returns_none():
    # Guard bug regression: a non-200 from /issueLinkType must return None,
    # not silently iterate an error dict and fall through to return None
    # without checking status first (both paths return None here, but the
    # guard ensures we never attempt .get() on a non-dict response body).
    def handler(req):
        if "issueLinkType" in str(req.url.path):
            return httpx.Response(500, json={"errorMessages": ["oops"]})
        return httpx.Response(404, json={})
    finding = {"area": "link_types", "name": "Blocks", "kind": "missing_in_tgt"}
    assert capture_config_payload(mk(handler), finding) is None


def test_link_types_name_not_found_returns_none():
    def handler(req):
        if "issueLinkType" in str(req.url.path):
            return httpx.Response(200, json={"issueLinkTypes": [
                {"name": "Cloners", "inward": "is cloned by",
                 "outward": "clones"}]})
        return httpx.Response(404, json={})
    finding = {"area": "link_types", "name": "Blocks", "kind": "missing_in_tgt"}
    assert capture_config_payload(mk(handler), finding) is None


def test_link_types_found_returns_payload():
    def handler(req):
        if "issueLinkType" in str(req.url.path):
            return httpx.Response(200, json={"issueLinkTypes": [
                {"name": "Blocks", "inward": "is blocked by",
                 "outward": "blocks"}]})
        return httpx.Response(404, json={})
    finding = {"area": "link_types", "name": "Blocks", "kind": "missing_in_tgt"}
    pl = capture_config_payload(mk(handler), finding)
    assert pl == {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"}


def test_simple_name_miss_returns_none():
    # Verify _simple returns None when the name is not in the list,
    # regardless of area (using priorities as representative).
    def handler(req):
        if str(req.url.path) == "/rest/api/3/priority":
            return httpx.Response(200, json=[
                {"name": "High", "description": ""},
                {"name": "Low", "description": ""}])
        return httpx.Response(404, json={})
    finding = {"area": "priorities", "name": "Critical", "kind": "missing_in_tgt"}
    assert capture_config_payload(mk(handler), finding) is None


def test_priorities_payload():
    def handler(req):
        if str(req.url.path) == "/rest/api/3/priority":
            return httpx.Response(200, json=[
                {"name": "High", "description": "High priority"}])
        return httpx.Response(404, json={})
    finding = {"area": "priorities", "name": "High", "kind": "missing_in_tgt"}
    pl = capture_config_payload(mk(handler), finding)
    assert pl == {"name": "High", "description": "High priority"}


def test_resolutions_payload():
    def handler(req):
        if str(req.url.path) == "/rest/api/3/resolution":
            return httpx.Response(200, json=[
                {"name": "Fixed", "description": "The issue is fixed"}])
        return httpx.Response(404, json={})
    finding = {"area": "resolutions", "name": "Fixed", "kind": "missing_in_tgt"}
    pl = capture_config_payload(mk(handler), finding)
    assert pl == {"name": "Fixed", "description": "The issue is fixed"}


def test_issue_types_payload():
    def handler(req):
        if str(req.url.path) == "/rest/api/3/issuetype":
            return httpx.Response(200, json=[
                {"name": "Epic", "description": "A big chunk of work",
                 "hierarchyLevel": 1}])
        return httpx.Response(404, json={})
    finding = {"area": "issue_types", "name": "Epic", "kind": "missing_in_tgt"}
    pl = capture_config_payload(mk(handler), finding)
    assert pl == {"name": "Epic", "description": "A big chunk of work",
                  "hierarchy_level": 1}


# --- C2/I5: option_mismatch on a custom field must produce an add_options
# payload (the missing-option delta), recomputed against live source options. -

def _opt_field_handler(source_options):
    def handler(req):
        p = str(req.url.path)
        if p == "/rest/api/3/field":
            return httpx.Response(200, json=[
                {"id": "customfield_1", "name": "Severity", "custom": True,
                 "schema": {"custom": "com.atlassian.jira.plugin.system."
                            "customfieldtypes:select", "type": "option"}}])
        if p.endswith("/context"):
            return httpx.Response(200, json={"values": [
                {"id": "10", "name": "Default"}], "isLast": True})
        if p.endswith("/context/10/option"):
            return httpx.Response(200, json={
                "values": [{"value": v} for v in source_options], "isLast": True})
        return httpx.Response(404, json={})
    return handler


def test_option_mismatch_captures_missing_delta():
    finding = {"area": "custom_fields", "name": "Severity",
               "kind": "option_mismatch",
               "detail": {"missing_options_in_tgt": ["Blocker", "Trivial"]}}
    # Source still carries both flagged-missing options -> both captured.
    pl = capture_config_payload(
        mk(_opt_field_handler(["Blocker", "Trivial", "High"])), finding)
    assert pl == {"field_name": "Severity",
                  "missing_options": ["Blocker", "Trivial"]}


def test_option_mismatch_recomputes_against_live_source():
    # "Trivial" was flagged missing at audit time but no longer exists on the
    # source -> recomputation drops it (fidelity), keeping only "Blocker".
    finding = {"area": "custom_fields", "name": "Severity",
               "kind": "option_mismatch",
               "detail": {"missing_options_in_tgt": ["Blocker", "Trivial"]}}
    pl = capture_config_payload(
        mk(_opt_field_handler(["Blocker", "High"])), finding)
    assert pl == {"field_name": "Severity", "missing_options": ["Blocker"]}


def test_option_mismatch_non_custom_field_area_returns_none():
    # option_mismatch is only meaningful for custom_fields; any other area
    # falls through the missing_in_tgt guard and returns None.
    finding = {"area": "statuses", "name": "X", "kind": "option_mismatch"}
    assert capture_config_payload(mk(lambda r: httpx.Response(404)), finding) is None
