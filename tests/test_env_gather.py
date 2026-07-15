import json
import httpx
from auditor.client import Connection, JiraClient
from auditor.envaudit.gather import gather_config


def mk(handler, deployment="cloud"):
    conn = Connection(auth_type="pat", site_url="https://t.atlassian.net",
                      deployment=deployment, email="a@b.c", api_token="x")
    return JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                      sleeper=lambda s: None)


def test_gather_cloud_collects_areas():
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/status"):
            return httpx.Response(200, json=[{"name": "Open"}, {"name": "Done"}])
        if p.endswith("/field"):
            return httpx.Response(200, json=[
                {"id": "customfield_1", "name": "Severity", "custom": True,
                 "schema": {"custom": "...:select"}}])
        if "/search" in p or p.endswith("/workflow/search"):
            return httpx.Response(200, json={"values": [], "isLast": True})
        return httpx.Response(200, json={"values": [], "isLast": True})
    snap = gather_config(mk(handler), ["ACME"], progress=lambda m: None)
    assert "Open" in snap["areas"]["statuses"]["names"]
    assert snap["areas"]["custom_fields"]["names"] == ["Severity"]
    assert snap["deployment"] == "cloud"


def test_gather_dc_marks_skipped():
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/status"):
            return httpx.Response(200, json=[{"name": "Open"}])
        return httpx.Response(200, json=[])
    snap = gather_config(mk(handler, "dc"), ["ACME"], progress=lambda m: None)
    # workflow_schemes has no DC list API -> recorded skipped, never a false []
    assert snap["areas"]["workflow_schemes"]["skipped"] is True


def test_gather_progress_defaults_to_none():
    """gather_config(client, keys) — omitting progress must not raise TypeError."""
    def handler(req):
        return httpx.Response(200, json={"values": [], "isLast": True})
    # Must not raise — the default None is handled internally.
    snap = gather_config(mk(handler), ["ACME"])
    assert snap["deployment"] == "cloud"


def test_gather_area_error_preserves_partial_rows():
    """On truncated error, names/count reflect partial rows, not hard-coded zero."""
    call_count = {"n": 0}

    def handler(req):
        p = str(req.url.path)
        if p.endswith("/status"):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # First page returns two statuses and signals more (isLast=False).
                return httpx.Response(200, json={
                    "values": [{"name": "Open"}, {"name": "In Progress"}],
                    "isLast": False, "total": 10})
            # Second page is a server error mid-pagination.
            return httpx.Response(500, json={})
        return httpx.Response(200, json={"values": [], "isLast": True})

    snap = gather_config(mk(handler), ["ACME"])
    st = snap["areas"]["statuses"]
    # Partial rows from page 1 must survive; count must not be zero.
    assert st["count"] == 2
    assert "Open" in st["names"]
    assert "In Progress" in st["names"]
    assert st["error"] is not None


def test_gather_cloud_workflow_detail_shape():
    """Cloud snapshot includes a detail dict keyed on workflow name with
    statuses and transitions lists."""
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/workflow/search"):
            return httpx.Response(200, json={
                "values": [{"name": "Software Simplified Workflow",
                            "statuses": [{"name": "To Do"}, {"name": "Done"}],
                            "transitions": [{"name": "Start Progress"},
                                            {"name": "Close Issue"}]}],
                "isLast": True})
        if p.endswith("/field"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"values": [], "isLast": True})

    snap = gather_config(mk(handler), ["ACME"])
    wf = snap["areas"]["workflows"]
    assert wf["structure_checked"] is True
    # count must be set so the near_workflow_limit guardrail can evaluate it
    # (the guardrail reads area["count"]; without it the HIGH 150-workflow
    # guardrail is dead code).
    assert wf["count"] == len(wf["names"]) == 1
    assert "Software Simplified Workflow" in wf["names"]
    d = wf["detail"]["Software Simplified Workflow"]
    assert "To Do" in d["statuses"]
    assert "Start Progress" in d["transitions"]


def test_gather_custom_fields_by_type():
    """custom_fields area must include a by_type dict mapping name -> type slug."""
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/field"):
            return httpx.Response(200, json=[
                {"id": "customfield_10100", "name": "Story Points", "custom": True,
                 "schema": {"custom": "com.atlassian.jira.plugin.system.customfieldtypes:float"}},
                {"id": "customfield_10101", "name": "Severity", "custom": True,
                 "schema": {"custom": "com.atlassian.jira.plugin.system.customfieldtypes:select"}},
            ])
        if p.endswith("/workflow/search"):
            return httpx.Response(200, json={"values": [], "isLast": True})
        return httpx.Response(200, json={"values": [], "isLast": True})

    snap = gather_config(mk(handler), ["ACME"])
    cf = snap["areas"]["custom_fields"]
    assert cf["by_type"]["Story Points"] == "float"
    assert cf["by_type"]["Severity"] == "select"


def test_gather_custom_fields_app_provided_count():
    """custom_fields must classify app-provided types (any namespace OTHER than
    the Atlassian built-in customfieldtypes namespace) into app_provided_count,
    computed from the FULL type key. by_type keeps only the lossy suffix, so the
    discrimination must NOT be re-derived from it downstream (review Bug 1)."""
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/field"):
            return httpx.Response(200, json=[
                {"id": "customfield_10100", "name": "Story Points", "custom": True,
                 "schema": {"custom": "com.atlassian.jira.plugin.system.customfieldtypes:float"}},
                {"id": "customfield_10101", "name": "Sprint", "custom": True,
                 "schema": {"custom": "com.pyxis.greenhopper.jira:gh-sprint"}},
                {"id": "customfield_10102", "name": "Account", "custom": True,
                 "schema": {"custom": "com.tempoplugin.tempo-accounts:accounts.customfield"}},
            ])
        return httpx.Response(200, json={"values": [], "isLast": True})

    snap = gather_config(mk(handler), ["ACME"])
    cf = snap["areas"]["custom_fields"]
    # Two of three are app-provided (greenhopper sprint, tempo account); the
    # built-in float type is NOT counted.
    assert cf["app_provided_count"] == 2
    # by_type still carries the readable suffix, never the full namespace.
    assert cf["by_type"]["Sprint"] == "gh-sprint"


def test_gather_screen_fields_populated_on_cloud():
    """Cloud gather must populate screens['fields'] = {screen_name: [field, ...]}
    using the tabs+fields sub-endpoints.  This is required for empty_screen and
    unused_custom_field checks to fire on real snapshots."""
    def handler(req):
        p = str(req.url.path)
        # /screens list
        if p.endswith("/screens"):
            return httpx.Response(200, json={"values": [
                {"id": 10, "name": "Default Screen"}], "isLast": True})
        # /screens/10/tabs
        if "/screens/10/tabs" in p and "fields" not in p:
            return httpx.Response(200, json={"values": [
                {"id": 1, "name": "Field Tab"}], "isLast": True})
        # /screens/10/tabs/1/fields
        if "/screens/10/tabs/1/fields" in p:
            return httpx.Response(200, json=[{"name": "Severity"},
                                             {"name": "Priority"}])
        if p.endswith("/field"):
            return httpx.Response(200, json=[])
        if p.endswith("/workflow/search"):
            return httpx.Response(200, json={"values": [], "isLast": True})
        return httpx.Response(200, json={"values": [], "isLast": True})

    snap = gather_config(mk(handler), ["ACME"])
    scr = snap["areas"]["screens"]
    assert "fields" in scr, "screens area must have a 'fields' key on Cloud"
    assert "Default Screen" in scr["fields"]
    assert "Severity" in scr["fields"]["Default Screen"]
    assert "Priority" in scr["fields"]["Default Screen"]


def test_gather_screen_fields_absent_on_dc():
    """DC gather must NOT attempt to populate screens['fields'] — the tabs/fields
    endpoint exists on DC but the area is gathered via _dc_list_sliced; the check
    must simply be skipped (no 'fields' key), not errored."""
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/status"):
            return httpx.Response(200, json=[{"name": "Open"}])
        # DC screens returns a plain array
        if p.endswith("/screens"):
            return httpx.Response(200, json=[{"id": 10, "name": "Default Screen"}])
        return httpx.Response(200, json=[])

    snap = gather_config(mk(handler, "dc"), ["ACME"])
    scr = snap["areas"]["screens"]
    assert "fields" not in scr, "screens.fields must not be populated on DC"


def test_gather_workflow_schemes_projects_using_populated_on_cloud():
    """Cloud gather must populate workflow_schemes['projects_using'] from the
    projectIds field returned inline by the /workflowscheme list endpoint.
    This is required for scheme_unused to fire."""
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/workflowscheme"):
            return httpx.Response(200, json={"values": [
                {"id": 1, "name": "Default WF Scheme",
                 "projectIds": ["10000"]},
                {"id": 2, "name": "Legacy Scheme",
                 "projectIds": []},
            ], "isLast": True})
        if p.endswith("/field"):
            return httpx.Response(200, json=[])
        if p.endswith("/workflow/search"):
            return httpx.Response(200, json={"values": [], "isLast": True})
        return httpx.Response(200, json={"values": [], "isLast": True})

    snap = gather_config(mk(handler), ["ACME"])
    wfs = snap["areas"]["workflow_schemes"]
    assert "projects_using" in wfs, "workflow_schemes must have projects_using on Cloud"
    assert wfs["projects_using"]["Default WF Scheme"] == ["10000"]
    assert wfs["projects_using"]["Legacy Scheme"] == []


def test_scheme_second_fetch_error_leaves_projects_using_absent():
    """workflow_schemes lists fine on the first pass but the second fetch (for
    projectIds) fails transiently. projects_using must be ABSENT so the usage
    checks skip — never {}, which would flag every scheme as unused."""
    state = {"wfs": 0}

    def handler(req):
        p = str(req.url.path)
        if p.endswith("/workflowscheme"):
            state["wfs"] += 1
            if state["wfs"] == 1:                      # SIMPLE-loop pass: OK
                return httpx.Response(200, json={"values": [
                    {"id": "1", "name": "Default WF Scheme"}], "isLast": True})
            return httpx.Response(500, json={"_error": "boom"})  # detail pass: fail
        if p.endswith("/status"):
            return httpx.Response(200, json=[{"name": "Open"}])
        if "/search" in p:
            return httpx.Response(200, json={"values": [], "isLast": True})
        return httpx.Response(200, json={"values": [], "isLast": True})

    snap = gather_config(mk(handler), ["ACME"], progress=lambda m: None)
    wfs = snap["areas"]["workflow_schemes"]
    assert "Default WF Scheme" in wfs["names"]      # listing succeeded
    assert "projects_using" not in wfs              # detail failed -> absent, not {}


# ---------------------------------------------------------------------------
# Phase A: 8 new snapshot areas + extended projects_using
# ---------------------------------------------------------------------------

def _base_handler(req):
    """Minimal baseline handler: returns empty envelopes for all standard paths
    so new-area tests can focus on their own paths without routing noise."""
    p = str(req.url.path)
    if p.endswith("/field"):
        return httpx.Response(200, json=[])
    if p.endswith("/workflow/search"):
        return httpx.Response(200, json={"values": [], "isLast": True})
    if p.endswith("/workflow"):
        return httpx.Response(200, json={"values": [], "isLast": True})
    # All SIMPLE paths (statuses, issuetype, priority, …)
    return httpx.Response(200, json={"values": [], "isLast": True})


def _mk_handler(*overrides):
    """Build a handler that checks each override tuple (predicate, response_fn)
    in order, falling through to _base_handler if nothing matches."""
    def handler(req):
        for pred, resp_fn in overrides:
            if pred(req):
                return resp_fn(req)
        return _base_handler(req)
    return handler


# ---- 1. permission_scheme_grants cloud shape ----------------------------

def test_permission_scheme_grants_cloud_shape():
    def handler(req):
        p = str(req.url.path)
        url = str(req.url)
        if p.endswith("/permissionscheme") and "expand=permissions" in url:
            return httpx.Response(200, json={"permissionSchemes": [
                {"name": "Default Permission Scheme", "permissions": [
                    {"permission": "BROWSE_PROJECTS",
                     "holder": {"type": "anyone", "parameter": ""}},
                    {"permission": "CREATE_ISSUES",
                     "holder": {"type": "projectRole", "parameter": "Developers"}},
                ]},
                {"name": "Admin Scheme", "permissions": [
                    {"permission": "ADMINISTER_PROJECTS",
                     "holder": {"type": "group", "parameter": "jira-admins"}},
                ]},
            ], "isLast": True})
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    areas = snap["areas"]
    assert "permission_scheme_grants" in areas
    pg = areas["permission_scheme_grants"]
    assert "Default Permission Scheme" in pg["by_scheme"]
    assert "Admin Scheme" in pg["by_scheme"]
    grants = pg["by_scheme"]["Default Permission Scheme"]
    assert len(grants) == 2
    for g in grants:
        assert "permission" in g
        assert "holder_type" in g
    assert pg["count"] > 0
    assert pg["error"] is None


# ---- 2. permission_scheme_grants dc skipped ----------------------------

def test_permission_scheme_grants_dc_skipped():
    def handler(req):
        return _base_handler(req)

    snap = gather_config(mk(handler, "dc"), ["ACME"])
    assert snap["areas"]["permission_scheme_grants"]["skipped"] is True


# ---- 3. permission grants privacy: no holder value ---------------------

def test_permission_grants_no_holder_value():
    def handler(req):
        p = str(req.url.path)
        url = str(req.url)
        if p.endswith("/permissionscheme") and "expand=permissions" in url:
            return httpx.Response(200, json={"permissionSchemes": [
                {"name": "Scheme A", "permissions": [
                    {"permission": "BROWSE_PROJECTS",
                     "holder": {"type": "group", "parameter": "jira-admins",
                                "value": "jira-admins", "accountId": "acc999"}},
                ]},
            ], "isLast": True})
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    snap_str = json.dumps(snap)
    assert "jira-admins" not in snap_str
    assert "acc999" not in snap_str


# ---- 4. groups cloud shape ---------------------------------------------

def test_groups_cloud_shape():
    groups = [
        {"name": "jira-users", "groupId": "g1"},
        {"name": "jira-admins", "groupId": "g2"},
        {"name": "developers", "groupId": "g3"},
    ]
    member_totals = {"g1": 10, "g2": 3, "g3": 7}

    def handler(req):
        p = str(req.url.path)
        if p.endswith("/group/bulk"):
            return httpx.Response(200, json={
                "values": groups, "isLast": True})
        if p.endswith("/group/member"):
            gid = dict(req.url.params).get("groupId", "")
            total = member_totals.get(gid, 0)
            return httpx.Response(200, json={"values": [], "total": total})
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    areas = snap["areas"]
    assert "groups" in areas
    g = areas["groups"]
    assert len(g["names"]) == 3
    assert "jira-users" in g["names"]
    assert len(g["member_counts"]) == 3
    assert g["member_counts"]["jira-users"] == 10
    assert g["capped"] is False
    assert g["error"] is None


# ---- 5. groups dc skipped ---------------------------------------------

def test_groups_dc_skipped():
    def handler(req):
        return _base_handler(req)

    snap = gather_config(mk(handler, "dc"), ["ACME"])
    assert snap["areas"]["groups"]["skipped"] is True


# ---- 6. groups privacy: no member identities --------------------------

def test_groups_no_member_identities():
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/group/bulk"):
            return httpx.Response(200, json={
                "values": [{"name": "jira-users", "groupId": "g1"}],
                "isLast": True})
        if p.endswith("/group/member"):
            return httpx.Response(200, json={
                "values": [{"accountId": "acc123", "displayName": "Alice"}],
                "total": 1})
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    snap_str = json.dumps(snap)
    assert "acc123" not in snap_str
    assert "Alice" not in snap_str


# ---- 7. groups capped when over 60 ------------------------------------

def test_groups_capped_when_over_60():
    # 65 groups returned in one page
    many_groups = [{"name": f"group-{i}", "groupId": f"gid-{i}"} for i in range(65)]

    def handler(req):
        p = str(req.url.path)
        if p.endswith("/group/bulk"):
            return httpx.Response(200, json={
                "values": many_groups, "isLast": True})
        if p.endswith("/group/member"):
            return httpx.Response(200, json={"values": [], "total": 5})
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    g = snap["areas"]["groups"]
    assert g["capped"] is True
    assert len(g["member_counts"]) <= 60


# ---- 8. components cloud shape ----------------------------------------

def test_components_cloud_shape():
    def handler(req):
        p = str(req.url.path)
        if "/project/ACME/components" in p:
            return httpx.Response(200, json=[
                {"name": "Backend", "lead": {"displayName": "Bob", "accountId": "acc1"},
                 "assigneeType": "PROJECT_LEAD"},
                {"name": "Frontend", "assigneeType": "UNASSIGNED"},
            ])
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    areas = snap["areas"]
    assert "components" in areas
    c = areas["components"]
    assert "ACME" in c["by_project"]
    assert len(c["by_project"]["ACME"]) == 2
    for entry in c["by_project"]["ACME"]:
        assert "name" in entry
        assert "has_lead" in entry
        assert "assignee_type" in entry
    # Backend has lead, Frontend does not
    backend = next(e for e in c["by_project"]["ACME"] if e["name"] == "Backend")
    frontend = next(e for e in c["by_project"]["ACME"] if e["name"] == "Frontend")
    assert backend["has_lead"] is True
    assert frontend["has_lead"] is False
    assert c["count"] == 2
    assert c["error"] is None


# ---- 9. components privacy: no lead name/accountId --------------------

def test_components_no_lead_name():
    def handler(req):
        p = str(req.url.path)
        if "/project/ACME/components" in p:
            return httpx.Response(200, json=[
                {"name": "Engine", "lead": {"displayName": "Bob", "accountId": "acc456"},
                 "assigneeType": "PROJECT_LEAD"},
            ])
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    snap_str = json.dumps(snap)
    assert "Bob" not in snap_str
    assert "acc456" not in snap_str


# ---- 10. versions cloud shape -----------------------------------------

def test_versions_cloud_shape():
    def handler(req):
        p = str(req.url.path)
        if "/project/ACME/versions" in p:
            return httpx.Response(200, json=[
                {"name": "1.0", "released": True, "archived": False, "overdue": False},
                {"name": "2.0", "released": False, "archived": False, "overdue": True},
            ])
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    areas = snap["areas"]
    assert "versions" in areas
    v = areas["versions"]
    assert "ACME" in v["by_project"]
    assert len(v["by_project"]["ACME"]) == 2
    for entry in v["by_project"]["ACME"]:
        assert "name" in entry
        assert "released" in entry
        assert "archived" in entry
        assert "overdue" in entry
    assert v["count"] == 2
    assert v["error"] is None


# ---- 11. versions privacy: no PII ------------------------------------

def test_versions_no_pii():
    def handler(req):
        p = str(req.url.path)
        if "/project/ACME/versions" in p:
            return httpx.Response(200, json=[
                {"name": "1.0", "released": True, "archived": False, "overdue": False,
                 "releaseDate": "2025-01-01",
                 "description": "Initial release",
                 "creator": {"displayName": "Carol", "accountId": "acc789"}},
            ])
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    snap_str = json.dumps(snap)
    assert "Carol" not in snap_str
    assert "2025-01-01" not in snap_str


# ---- 12. custom_field_options cloud shape ----------------------------

def test_custom_field_options_cloud_shape():
    field_id = "customfield_10200"
    context_id = "ctx1"

    def handler(req):
        p = str(req.url.path)
        if p.endswith("/field"):
            return httpx.Response(200, json=[
                {"id": field_id, "name": "Priority Level", "custom": True,
                 "schema": {"custom": "com.atlassian.jira:select"}},
            ])
        if p.endswith(f"/field/{field_id}/context"):
            return httpx.Response(200, json={
                "values": [{"id": context_id, "name": "Default Context"}],
                "isLast": True})
        if p.endswith(f"/field/{field_id}/context/{context_id}/option"):
            return httpx.Response(200, json={
                "values": [{"value": "Low"}, {"value": "Medium"}, {"value": "High"}],
                "isLast": True})
        if p.endswith("/workflow/search"):
            return httpx.Response(200, json={"values": [], "isLast": True})
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    areas = snap["areas"]
    assert "custom_field_options" in areas
    cfo = areas["custom_field_options"]
    assert "Priority Level" in cfo["by_field"]
    assert cfo["by_field"]["Priority Level"] == {"contexts": 1, "options": 3}
    assert cfo["capped"] is False
    assert cfo["error"] is None


# ---- 13. custom_field_options dc skipped ----------------------------

def test_custom_field_options_dc_skipped():
    def handler(req):
        return _base_handler(req)

    snap = gather_config(mk(handler, "dc"), ["ACME"])
    assert snap["areas"]["custom_field_options"]["skipped"] is True


# ---- 14. custom_field_options capped --------------------------------

def test_custom_field_options_capped():
    # 85 select-type custom fields
    fields = [
        {"id": f"customfield_{i}", "name": f"Field {i}", "custom": True,
         "schema": {"custom": "com.atlassian.jira:select"}}
        for i in range(85)
    ]

    def handler(req):
        p = str(req.url.path)
        if p.endswith("/field"):
            return httpx.Response(200, json=fields)
        # Context and option endpoints return empty
        if "/context" in p and "/option" not in p:
            return httpx.Response(200, json={"values": [], "isLast": True})
        if "/option" in p:
            return httpx.Response(200, json={"values": [], "isLast": True})
        if p.endswith("/workflow/search"):
            return httpx.Response(200, json={"values": [], "isLast": True})
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    cfo = snap["areas"]["custom_field_options"]
    assert cfo["capped"] is True
    assert len(cfo["by_field"]) <= 80


# ---- 15. boards cloud shape -----------------------------------------

def test_boards_cloud_shape():
    def handler(req):
        p = str(req.url.path)
        if p == "/rest/agile/1.0/board":
            return httpx.Response(200, json={
                "values": [
                    {"id": 1, "name": "ACME Board"},
                    {"id": 2, "name": "Dev Board"},
                ], "isLast": True})
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    areas = snap["areas"]
    assert "boards" in areas
    b = areas["boards"]
    assert "ACME Board" in b["names"]
    assert "Dev Board" in b["names"]
    assert b["count"] == 2
    assert b["capped"] is False
    assert b["error"] is None


# ---- 16. boards capped ----------------------------------------------

def test_boards_capped():
    # Exactly 500 boards in one page → capped=True
    boards_500 = [{"id": i, "name": f"Board {i}"} for i in range(500)]

    def handler(req):
        p = str(req.url.path)
        if p == "/rest/agile/1.0/board":
            return httpx.Response(200, json={
                "values": boards_500, "isLast": True})
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    b = snap["areas"]["boards"]
    assert b["capped"] is True


# ---- 17. filters cloud shape ----------------------------------------

def test_filters_cloud_shape():
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/filter/search"):
            return httpx.Response(200, json={
                "values": [{"id": "1"}, {"id": "2"}, {"id": "3"}],
                "isLast": True})
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    areas = snap["areas"]
    assert "filters" in areas
    f = areas["filters"]
    assert f["count"] == 3
    assert f["capped"] is False
    assert f["error"] is None


# ---- 18. dashboards cloud shape -------------------------------------

def test_dashboards_cloud_shape():
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/dashboard"):
            return httpx.Response(200, json={
                "dashboards": [{"id": "1"}, {"id": "2"}],
                "isLast": True})
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    areas = snap["areas"]
    assert "dashboards" in areas
    d = areas["dashboards"]
    assert d["count"] == 2
    assert d["capped"] is False
    assert d["error"] is None


# ---- 19. issuetype_schemes projects_using ---------------------------

def test_issuetype_schemes_projects_using():
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/issuetypescheme/project"):
            return httpx.Response(200, json={"values": [
                {"issueTypeScheme": {"id": "1", "name": "Default IT Scheme"},
                 "projectIds": ["10001", "10002"]},
            ], "isLast": True})
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    its = snap["areas"]["issuetype_schemes"]
    assert "projects_using" in its
    assert its["projects_using"]["Default IT Scheme"] == ["10001", "10002"]


# ---- 20. issuetype_screen_schemes projects_using --------------------

def test_issuetype_screen_schemes_projects_using():
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/issuetypescreenscheme/project"):
            return httpx.Response(200, json={"values": [
                {"issueTypeScreenScheme": {"id": "2", "name": "Screen Scheme A"},
                 "projectIds": ["10003"]},
            ], "isLast": True})
        # Cloud-only area, must not be called on DC
        if p.endswith("/issuetypescreenscheme"):
            return httpx.Response(200, json={"values": [], "isLast": True})
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    itss = snap["areas"]["issuetype_screen_schemes"]
    assert "projects_using" in itss
    assert "Screen Scheme A" in itss["projects_using"]
    assert itss["projects_using"]["Screen Scheme A"] == ["10003"]


# ---- 21. components dc shape (not skipped) --------------------------

def test_components_dc_shape():
    def handler(req):
        p = str(req.url.path)
        if "/project/ACME/components" in p:
            return httpx.Response(200, json=[
                {"name": "Core", "assigneeType": "PROJECT_LEAD"},
            ])
        return _base_handler(req)

    snap = gather_config(mk(handler, "dc"), ["ACME"])
    areas = snap["areas"]
    assert "components" in areas
    assert not areas["components"].get("skipped")
    assert areas["components"]["count"] == 1


# ---- 22. boards dc shape (not skipped) ------------------------------

def test_boards_dc_shape():
    def handler(req):
        p = str(req.url.path)
        if p == "/rest/agile/1.0/board":
            return httpx.Response(200, json={
                "values": [{"id": 5, "name": "DC Board"}], "isLast": True})
        return _base_handler(req)

    snap = gather_config(mk(handler, "dc"), ["ACME"])
    areas = snap["areas"]
    assert "boards" in areas
    assert "DC Board" in areas["boards"]["names"]


# ============================================================
# Error-preservation tests (I2 false-clean guard)
# ============================================================

# ---- 23. permission_scheme_grants error -----------------------------

def test_permission_scheme_grants_error():
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/permissionscheme"):
            return httpx.Response(500, json={"error": "internal"})
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    pg = snap["areas"]["permission_scheme_grants"]
    assert pg["error"] is not None
    assert "by_scheme" in pg


# ---- 24. groups error -----------------------------------------------

def test_groups_error():
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/group/bulk"):
            return httpx.Response(500, json={"error": "internal"})
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    g = snap["areas"]["groups"]
    assert g["error"] is not None
    assert "names" in g
    assert "count" in g
    assert "member_counts" in g
    assert "capped" in g


# ---- 25. components error -------------------------------------------

def test_components_error():
    def handler(req):
        p = str(req.url.path)
        if "/project/ACME/components" in p:
            return httpx.Response(500, json={"error": "internal"})
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    c = snap["areas"]["components"]
    assert c["error"] is not None
    assert "by_project" in c


# ---- 26. versions error ---------------------------------------------

def test_versions_error():
    def handler(req):
        p = str(req.url.path)
        if "/project/ACME/versions" in p:
            return httpx.Response(500, json={"error": "internal"})
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    v = snap["areas"]["versions"]
    assert v["error"] is not None
    assert "by_project" in v


# ---- 27. custom_field_options error ---------------------------------

def test_custom_field_options_error():
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/field"):
            # Return one select-type custom field so the options loop runs
            return httpx.Response(200, json=[
                {"id": "customfield_99", "name": "Priority Level", "custom": True,
                 "schema": {"custom": "com.atlassian.jira.plugin.system.customfieldtypes:select"}},
            ])
        if "/field/customfield_99/context" in p:
            return httpx.Response(500, json={"error": "internal"})
        if p.endswith("/workflow/search"):
            return httpx.Response(200, json={"values": [], "isLast": True})
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    cfo = snap["areas"]["custom_field_options"]
    assert cfo["error"] is not None
    assert "by_field" in cfo


# ---- 28. boards error -----------------------------------------------

def test_boards_error():
    def handler(req):
        p = str(req.url.path)
        if p == "/rest/agile/1.0/board":
            return httpx.Response(500, json={"error": "internal"})
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    b = snap["areas"]["boards"]
    assert b["error"] is not None
    assert "names" in b
    assert "count" in b
    assert "capped" in b


# ---- 29. filters error ----------------------------------------------

def test_filters_error():
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/filter/search"):
            return httpx.Response(500, json={"error": "internal"})
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    f = snap["areas"]["filters"]
    assert f["error"] is not None


# ---- 30. dashboards error -------------------------------------------

def test_dashboards_error():
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/dashboard"):
            return httpx.Response(500, json={"error": "internal"})
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    d = snap["areas"]["dashboards"]
    assert d["error"] is not None


# ============================================================
# Cap tests (missing for filters and dashboards)
# ============================================================

# ---- 31. filters cap ------------------------------------------------

def test_filters_capped():
    filters_500 = [{"id": str(i)} for i in range(500)]

    def handler(req):
        p = str(req.url.path)
        if p.endswith("/filter/search"):
            return httpx.Response(200, json={"values": filters_500, "isLast": True})
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    f = snap["areas"]["filters"]
    assert f["capped"] is True
    assert f["count"] == 500


# ---- 32. dashboards cap ---------------------------------------------

def test_dashboards_capped():
    dashboards_500 = [{"id": str(i)} for i in range(500)]

    def handler(req):
        p = str(req.url.path)
        if p.endswith("/dashboard"):
            return httpx.Response(200, json={"dashboards": dashboards_500, "isLast": True})
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    d = snap["areas"]["dashboards"]
    assert d["capped"] is True
    assert d["count"] == 500


# ============================================================
# DC behavior tests (not-skipped) for versions, filters, dashboards
# ============================================================

# ---- 33. versions DC not skipped ------------------------------------

def test_versions_dc_not_skipped():
    def handler(req):
        p = str(req.url.path)
        if "/project/ACME/versions" in p:
            return httpx.Response(200, json=[
                {"name": "1.0", "released": True, "archived": False}
            ])
        return _base_handler(req)

    snap = gather_config(mk(handler, "dc"), ["ACME"])
    v = snap["areas"]["versions"]
    assert "versions" in snap["areas"]
    assert not v.get("skipped")
    assert v["count"] == 1


# ---- 34. filters DC not skipped -------------------------------------

def test_filters_dc_not_skipped():
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/filter/search"):
            return httpx.Response(200, json={"values": [{"id": "10"}], "isLast": True})
        return _base_handler(req)

    snap = gather_config(mk(handler, "dc"), ["ACME"])
    f = snap["areas"]["filters"]
    assert "filters" in snap["areas"]
    assert not f.get("skipped")
    assert f["count"] == 1


# ---- 35. dashboards DC not skipped ----------------------------------

def test_dashboards_dc_not_skipped():
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/dashboard"):
            return httpx.Response(200, json={"dashboards": [{"id": "10"}], "isLast": True})
        return _base_handler(req)

    snap = gather_config(mk(handler, "dc"), ["ACME"])
    d = snap["areas"]["dashboards"]
    assert "dashboards" in snap["areas"]
    assert not d.get("skipped")
    assert d["count"] == 1


# ===========================================================================
# SECTION 2 (NEW GATHER) — project activity + shared-object ownership
# Privacy invariant I1: ONLY booleans, counts, project KEYS, dates-reduced-
# to-booleans. NEVER an owner/lead name, accountId, email, or displayName.
# ===========================================================================

import datetime as _dt


def _iso_days_ago(days):
    """ISO-8601 timestamp `days` days before now, in Jira's insight format."""
    d = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)
    return d.strftime("%Y-%m-%dT%H:%M:%S.000+0000")


# ---- 36. projects activity cloud shape ------------------------------------

def test_projects_activity_cloud_shape():
    def handler(req):
        p = str(req.url.path)
        url = str(req.url)
        if p.endswith("/project/search") and "expand=insight" in url:
            return httpx.Response(200, json={"values": [
                {"key": "ACME", "insight": {"totalIssueCount": 42,
                 "lastIssueUpdateTime": _iso_days_ago(10)}},
                {"key": "OLD", "insight": {"totalIssueCount": 7,
                 "lastIssueUpdateTime": _iso_days_ago(500)}},
                {"key": "EMPTY", "insight": {"totalIssueCount": 0,
                 "lastIssueUpdateTime": None}},
            ], "isLast": True})
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    areas = snap["areas"]
    assert "projects" in areas
    pj = areas["projects"]
    assert pj["error"] is None
    assert pj["count"] == 3
    bp = pj["by_project"]
    # Per-project store ONLY {issue_count, stale}
    assert set(bp["ACME"].keys()) == {"issue_count", "stale"}
    assert bp["ACME"]["issue_count"] == 42
    assert bp["ACME"]["stale"] is False
    # OLD: has issues, last update > 365 days -> stale
    assert bp["OLD"]["issue_count"] == 7
    assert bp["OLD"]["stale"] is True
    # EMPTY: zero issues -> not stale (no issues to be stale about)
    assert bp["EMPTY"]["issue_count"] == 0
    assert bp["EMPTY"]["stale"] is False


# ---- 37. projects activity DC: issue_count None, stale False --------------

def test_projects_activity_dc_no_insight():
    def handler(req):
        p = str(req.url.path)
        # DC uses GET /project (no insight expand) -> plain array, no insight
        if p.endswith("/project"):
            return httpx.Response(200, json=[
                {"key": "ACME"}, {"key": "BETA"}])
        return _base_handler(req)

    snap = gather_config(mk(handler, "dc"), ["ACME"])
    pj = snap["areas"]["projects"]
    assert not pj.get("skipped")
    assert pj["count"] == 2
    bp = pj["by_project"]
    # DC lacks insight -> issue_count None, stale False
    assert bp["ACME"]["issue_count"] is None
    assert bp["ACME"]["stale"] is False
    assert bp["BETA"]["issue_count"] is None


# ---- 38. projects activity error preserves shape --------------------------

def test_projects_activity_error():
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/project/search"):
            return httpx.Response(500, json={"error": "internal"})
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    pj = snap["areas"]["projects"]
    assert pj["error"] is not None
    assert "by_project" in pj
    assert "count" in pj


# ---- 39. projects activity privacy: no lead identity ----------------------

def test_projects_activity_no_lead_identity():
    def handler(req):
        p = str(req.url.path)
        url = str(req.url)
        if p.endswith("/project/search") and "expand=insight" in url:
            return httpx.Response(200, json={"values": [
                {"key": "ACME", "name": "Acme Project",
                 "lead": {"displayName": "Lana Lead", "accountId": "acc-lead-1"},
                 "insight": {"totalIssueCount": 5,
                             "lastIssueUpdateTime": _iso_days_ago(3)}},
            ], "isLast": True})
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    snap_str = json.dumps(snap)
    assert "Lana Lead" not in snap_str
    assert "acc-lead-1" not in snap_str
    # The full ISO timestamp must NOT be stored — only the stale boolean.
    assert "lastIssueUpdateTime" not in snap_str
    # But the project key MUST be present (it is config metadata, not PII).
    assert "ACME" in snap_str


# ---- 40. filters UPGRADE cloud shape (items with owner_active/public) ------

def test_filters_upgraded_cloud_shape():
    def handler(req):
        p = str(req.url.path)
        url = str(req.url)
        if p.endswith("/filter/search") and "expand=owner" in url:
            return httpx.Response(200, json={"values": [
                {"id": "1", "owner": {"active": True},
                 "sharePermissions": [{"type": "project"}]},
                {"id": "2", "owner": {"active": False},
                 "sharePermissions": [{"type": "global"}]},
                {"id": "3", "owner": {"active": True},
                 "sharePermissions": []},
            ], "isLast": True})
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    f = snap["areas"]["filters"]
    assert f["count"] == 3
    assert f["capped"] is False
    assert f["error"] is None
    assert "items" in f
    items = f["items"]
    assert len(items) == 3
    # Each item stores ONLY booleans
    for it in items:
        assert set(it.keys()) == {"owner_active", "public"}
    assert items[0] == {"owner_active": True, "public": False}    # project (non-public type)
    assert items[1] == {"owner_active": False, "public": True}    # global -> public
    assert items[2] == {"owner_active": True, "public": False}    # no shares


# ---- 41. filters public detection across share types ----------------------

def test_filters_public_share_types():
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/filter/search"):
            return httpx.Response(200, json={"values": [
                {"id": "1", "owner": {"active": True},
                 "sharePermissions": [{"type": "loggedin"}]},
                {"id": "2", "owner": {"active": True},
                 "sharePermissions": [{"type": "authenticated"}]},
                {"id": "3", "owner": {"active": True},
                 "sharePermissions": [{"type": "group", "group": {"name": "x"}}]},
            ], "isLast": True})
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    items = snap["areas"]["filters"]["items"]
    assert items[0]["public"] is True   # loggedin
    assert items[1]["public"] is True   # authenticated
    assert items[2]["public"] is False  # group share is not public


# ---- 42. filters DC stays count-only (no items) ---------------------------

def test_filters_dc_count_only():
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/filter/search"):
            return httpx.Response(200, json={"values": [{"id": "10"}], "isLast": True})
        return _base_handler(req)

    snap = gather_config(mk(handler, "dc"), ["ACME"])
    f = snap["areas"]["filters"]
    assert f["count"] == 1
    assert "items" not in f, "DC filters must stay count-only"


# ---- 43. filters privacy: no owner identity -------------------------------

def test_filters_no_owner_identity():
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/filter/search"):
            return httpx.Response(200, json={"values": [
                {"id": "1", "name": "My Filter",
                 "owner": {"active": False, "displayName": "Fred Filter",
                           "accountId": "acc-filt-9", "emailAddress": "fred@x.example"},
                 "sharePermissions": [{"type": "global"}]},
            ], "isLast": True})
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    snap_str = json.dumps(snap)
    assert "Fred Filter" not in snap_str
    assert "acc-filt-9" not in snap_str
    assert "fred@x.example" not in snap_str
    # the boolean we keep must survive
    assert snap["areas"]["filters"]["items"][0]["owner_active"] is False


# ---- 44. filters capped still upgraded ------------------------------------

def test_filters_upgraded_capped():
    filters_500 = [{"id": str(i), "owner": {"active": True},
                    "sharePermissions": []} for i in range(500)]

    def handler(req):
        p = str(req.url.path)
        if p.endswith("/filter/search"):
            return httpx.Response(200, json={"values": filters_500, "isLast": True})
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    f = snap["areas"]["filters"]
    assert f["capped"] is True
    assert f["count"] == 500


# ---- 45. dashboards UPGRADE cloud shape -----------------------------------

def test_dashboards_upgraded_cloud_shape():
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/dashboard"):
            return httpx.Response(200, json={"dashboards": [
                {"id": "1", "owner": {"active": True},
                 "sharePermissions": [{"type": "project"}]},
                {"id": "2", "owner": {"active": False},
                 "sharePermissions": [{"type": "loggedin"}]},
            ], "isLast": True})
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    d = snap["areas"]["dashboards"]
    assert d["count"] == 2
    assert d["capped"] is False
    assert d["error"] is None
    assert "items" in d
    items = d["items"]
    for it in items:
        assert set(it.keys()) == {"owner_active", "public"}
    assert items[0] == {"owner_active": True, "public": False}
    assert items[1] == {"owner_active": False, "public": True}


# ---- 46. dashboards DC count-only -----------------------------------------

def test_dashboards_dc_count_only():
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/dashboard"):
            return httpx.Response(200, json={"dashboards": [{"id": "10"}], "isLast": True})
        return _base_handler(req)

    snap = gather_config(mk(handler, "dc"), ["ACME"])
    d = snap["areas"]["dashboards"]
    assert d["count"] == 1
    assert "items" not in d, "DC dashboards must stay count-only"


# ---- 47. dashboards privacy: no owner identity ----------------------------

def test_dashboards_no_owner_identity():
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/dashboard"):
            return httpx.Response(200, json={"dashboards": [
                {"id": "1", "name": "Exec Dashboard",
                 "owner": {"active": False, "displayName": "Dana Dash",
                           "accountId": "acc-dash-7"},
                 "sharePermissions": [{"type": "global"}]},
            ], "isLast": True})
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    snap_str = json.dumps(snap)
    assert "Dana Dash" not in snap_str
    assert "acc-dash-7" not in snap_str
    assert snap["areas"]["dashboards"]["items"][0]["public"] is True


# ===========================================================================
# WORKFLOW-STRUCTURE enrichment — transition GRAPH (edges) on Cloud
# Cloud GET /workflow/search?expand=transitions,statuses returns each
# transition with from/to status references and a type. gather must reduce
# this to a privacy-safe edge list: {to: <name>, from: [<name>...], global}.
# DC has no transition expand -> edges ABSENT, structure_checked False.
# ===========================================================================


def test_gather_cloud_workflow_edges_shape():
    """Cloud workflow detail must carry an `edges` list mapping status IDs to
    names. A directed transition has a concrete from/to; a global transition
    is flagged global=True with an empty/all-status from-set."""
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/workflow/search"):
            return httpx.Response(200, json={"values": [{
                "name": "Dev Workflow",
                "statuses": [
                    {"id": "1", "name": "To Do"},
                    {"id": "2", "name": "In Progress"},
                    {"id": "3", "name": "Done"},
                ],
                "transitions": [
                    # create/initial transition into To Do
                    {"id": "1", "name": "Create", "type": "initial",
                     "from": [], "to": {"id": "1"}},
                    # directed To Do -> In Progress
                    {"id": "11", "name": "Start", "type": "directed",
                     "from": [{"id": "1"}], "to": {"id": "2"}},
                    # directed In Progress -> Done
                    {"id": "21", "name": "Finish", "type": "directed",
                     "from": [{"id": "2"}], "to": {"id": "3"}},
                    # global transition (from ANY status) -> Done
                    {"id": "31", "name": "Close", "type": "global",
                     "from": [], "to": {"id": "3"}},
                ]}], "isLast": True})
        if p.endswith("/field"):
            return httpx.Response(200, json=[])
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    wf = snap["areas"]["workflows"]
    assert wf["structure_checked"] is True
    d = wf["detail"]["Dev Workflow"]
    # names preserved as before
    assert "To Do" in d["statuses"]
    assert "Start" in d["transitions"]
    # edges added: each entry is {to, from, global} with NAMES not ids
    assert "edges" in d
    edges = d["edges"]
    # Find each edge by its `to` name.
    by_to = {}
    for e in edges:
        by_to.setdefault(e["to"], []).append(e)
        assert set(e.keys()) == {"to", "from", "global"}
        # IDs must be mapped to names — no bare numeric id strings leak through.
        assert e["to"] in ("To Do", "In Progress", "Done")
        for src in e["from"]:
            assert src in ("To Do", "In Progress", "Done")
    # The initial transition lands on To Do (create destination).
    assert "To Do" in by_to
    # Start: To Do -> In Progress, not global
    start = next(e for e in edges if e["to"] == "In Progress")
    assert start["from"] == ["To Do"]
    assert start["global"] is False
    # Close: global -> Done
    global_edges = [e for e in edges if e["global"] is True]
    assert any(e["to"] == "Done" for e in global_edges)


def test_gather_cloud_workflow_edges_global_empty_from():
    """A transition with type != global but an EMPTY from-set (applies from any
    status) is also treated as global."""
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/workflow/search"):
            return httpx.Response(200, json={"values": [{
                "name": "WF",
                "statuses": [{"id": "1", "name": "A"}, {"id": "2", "name": "B"}],
                "transitions": [
                    {"id": "1", "name": "Create", "type": "initial",
                     "from": [], "to": {"id": "1"}},
                    {"id": "9", "name": "Reopen", "type": "directed",
                     "from": [], "to": {"id": "2"}},
                ]}], "isLast": True})
        if p.endswith("/field"):
            return httpx.Response(200, json=[])
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    d = snap["areas"]["workflows"]["detail"]["WF"]
    reopen = next(e for e in d["edges"] if e["to"] == "B")
    # empty from-set (and not the create transition) -> global
    assert reopen["global"] is True
    assert reopen["from"] == []


def test_gather_cloud_workflow_edges_no_pii():
    """The edge list stores only status NAMES and a boolean — no transition id,
    no rules, no conditions/validators."""
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/workflow/search"):
            return httpx.Response(200, json={"values": [{
                "name": "WF",
                "statuses": [{"id": "1", "name": "Open"}],
                "transitions": [
                    {"id": "1", "name": "Create", "type": "initial",
                     "from": [], "to": {"id": "1"},
                     "rules": {"conditions": [{"type": "SecretCondition"}]}},
                ]}], "isLast": True})
        if p.endswith("/field"):
            return httpx.Response(200, json=[])
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    snap_str = json.dumps(snap)
    assert "SecretCondition" not in snap_str
    edges = snap["areas"]["workflows"]["detail"]["WF"]["edges"]
    for e in edges:
        assert set(e.keys()) == {"to", "from", "global"}


def test_gather_dc_workflow_edges_absent():
    """DC /workflow has no transition expand -> edges must be ABSENT and
    structure_checked False, so the new structure checks treat it as
    unevaluable (never a false finding)."""
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/workflow"):
            return httpx.Response(200, json={"values": [
                {"id": {"name": "Legacy WF"}}], "isLast": True})
        return _base_handler(req)

    snap = gather_config(mk(handler, "dc"), ["ACME"])
    wf = snap["areas"]["workflows"]
    assert wf["structure_checked"] is False
    # No detail/edges on DC.
    assert "detail" not in wf or all(
        "edges" not in v for v in (wf.get("detail") or {}).values())


def test_gather_workflow_schemes_workflows_used_populated_on_cloud():
    """workflow_schemes must carry `workflows_used`: the SET (as a sorted list)
    of workflow names referenced by any scheme's defaultWorkflow or
    issueTypeMappings. Needed for the workflow_unreferenced check."""
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/workflowscheme"):
            return httpx.Response(200, json={"values": [
                {"id": 1, "name": "Scheme A",
                 "defaultWorkflow": "Default WF",
                 "issueTypeMappings": {"10001": "Bug WF", "10002": "Default WF"}},
                {"id": 2, "name": "Scheme B",
                 "defaultWorkflow": "Story WF",
                 "issueTypeMappings": {}},
            ], "isLast": True})
        if p.endswith("/field"):
            return httpx.Response(200, json=[])
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    wfs = snap["areas"]["workflow_schemes"]
    assert "workflows_used" in wfs
    used = set(wfs["workflows_used"])
    assert used == {"Default WF", "Bug WF", "Story WF"}


def test_gather_workflow_schemes_workflows_used_absent_on_dc():
    """DC has no /workflowscheme list API (scheme area is skipped), so
    workflows_used must be absent -> workflow_unreferenced is unevaluable."""
    def handler(req):
        return _base_handler(req)

    snap = gather_config(mk(handler, "dc"), ["ACME"])
    wfs = snap["areas"]["workflow_schemes"]
    assert wfs.get("skipped") is True
    assert "workflows_used" not in wfs


def test_gather_screen_schemes_screens_used_populated_on_cloud():
    """screen_schemes must carry `screens_used`: the SET (sorted list) of screen
    names referenced by any screen scheme's `screens` map values. Needed for the
    screen_not_in_scheme check. Cloud /screenscheme returns screen IDs, so the
    gather resolves them to names via the screens list."""
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/screens"):
            return httpx.Response(200, json={"values": [
                {"id": 10, "name": "Default Screen"},
                {"id": 11, "name": "Resolve Screen"},
                {"id": 12, "name": "Orphan Screen"},
            ], "isLast": True})
        if p.endswith("/screenscheme"):
            return httpx.Response(200, json={"values": [
                {"id": 1, "name": "Default SS",
                 "screens": {"default": 10, "edit": 10, "create": 11}},
            ], "isLast": True})
        if p.endswith("/field"):
            return httpx.Response(200, json=[])
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    ss = snap["areas"]["screen_schemes"]
    assert "screens_used" in ss
    used = set(ss["screens_used"])
    assert used == {"Default Screen", "Resolve Screen"}
    assert "Orphan Screen" not in used


def test_gather_screen_schemes_screens_used_absent_on_dc():
    """DC has no /screenscheme list API (area skipped) -> screens_used absent ->
    screen_not_in_scheme is unevaluable (never a false finding)."""
    def handler(req):
        return _base_handler(req)

    snap = gather_config(mk(handler, "dc"), ["ACME"])
    ss = snap["areas"]["screen_schemes"]
    assert ss.get("skipped") is True
    assert "screens_used" not in ss


# ===========================================================================
# SECTION 3 (ISSUE-LEVEL / DATA QUALITY) — issue_quality area
# Privacy invariant I1: issue queries return COUNTS ONLY. The gather stores
# ONLY integers (or None on a per-metric failure). NEVER an issue key, summary,
# description, comment, field value, reporter/assignee identity, or any issue
# content. approx_count returns an int on success or an "ERR.." string on
# failure -> a non-int is recorded as None (unevaluable), never an area abort.
# ===========================================================================


def _cloud_count_handler(counts, extra_keys=False):
    """Build a Cloud handler that answers approximate-count POSTs by matching
    the JQL substring in the request body. `counts` maps a JQL fragment to the
    count to return; an unmatched query returns 0.

    extra_keys=True attaches issue keys/content to the count response to prove
    the gather never reads or stores anything beyond the integer `count`."""
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/search/approximate-count"):
            body = json.loads(req.content.decode() or "{}")
            jql = body.get("jql", "")
            n = 0
            for frag, val in counts.items():
                if frag in jql:
                    n = val
                    break
            payload = {"count": n}
            if extra_keys:
                # Hostile server: stuff issue content into the response. The
                # gather must IGNORE all of this and keep only the integer.
                payload["issues"] = [
                    {"key": "SECRET-123",
                     "fields": {"summary": "LEAKED ISSUE SUMMARY",
                                "assignee": {"displayName": "Eve Leak",
                                             "accountId": "acc-leak-9"}}}]
                payload["issueKeys"] = ["SECRET-123", "SECRET-124"]
            return httpx.Response(200, json=payload)
        return _base_handler(req)
    return handler


def test_issue_quality_cloud_shape():
    """The issue_quality area runs a fixed set of approx-count queries and
    stores ONLY integers keyed by metric, plus error=None on success."""
    counts = {
        "statusCategory = Done AND resolution = EMPTY": 4,
        "statusCategory != Done AND updated": 17,
        "resolution = EMPTY AND assignee is EMPTY": 9,
        "resolution != EMPTY AND statusCategory != Done": 2,
        "resolution = EMPTY": 50,   # broadest match -> total_unresolved
    }

    def handler(req):
        p = str(req.url.path)
        if p.endswith("/search/approximate-count"):
            body = json.loads(req.content.decode() or "{}")
            jql = body.get("jql", "")
            # Order matters: check the most specific fragments first so the
            # bare "resolution = EMPTY" only matches total_unresolved.
            for frag in ("statusCategory = Done AND resolution = EMPTY",
                         "statusCategory != Done AND updated",
                         "resolution = EMPTY AND assignee is EMPTY",
                         "resolution != EMPTY AND statusCategory != Done"):
                if frag in jql:
                    return httpx.Response(200, json={"count": counts[frag]})
            if jql.strip() == "resolution = EMPTY":
                return httpx.Response(200, json={"count": counts["resolution = EMPTY"]})
            return httpx.Response(200, json={"count": 0})
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    iq = snap["areas"]["issue_quality"]
    assert iq["error"] is None
    assert iq["done_unresolved"] == 4
    assert iq["stale_open"] == 17
    assert iq["unassigned_unresolved"] == 9
    assert iq["resolved_but_open"] == 2
    assert iq["total_unresolved"] == 50
    # Every stored metric is an int (or None) — never a string/dict/list.
    for k, v in iq.items():
        if k == "error":
            continue
        assert v is None or isinstance(v, int), f"{k} must be int|None, got {type(v)}"


def test_issue_quality_per_metric_failure_yields_none_not_area_error():
    """One failing approx-count query must set ONLY that metric to None — the
    other metrics still resolve and the AREA error stays None (no whole-area
    abort)."""
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/search/approximate-count"):
            body = json.loads(req.content.decode() or "{}")
            jql = body.get("jql", "")
            # Fail ONLY the done_unresolved query.
            if "statusCategory = Done AND resolution = EMPTY" in jql:
                return httpx.Response(500, json={"_error": "boom"})
            return httpx.Response(200, json={"count": 7})
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    iq = snap["areas"]["issue_quality"]
    # The failed query -> None; the area itself did NOT abort.
    assert iq["done_unresolved"] is None
    assert iq["error"] is None
    # The other metrics resolved normally.
    assert iq["total_unresolved"] == 7
    assert iq["stale_open"] == 7


def test_issue_quality_dc_path():
    """On DC, approx_count GETs {api_prefix}/search?maxResults=0 and reads
    `total`. The issue_quality area must populate from that path too."""
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/search"):
            # DC search with maxResults=0 returns a `total`.
            params = dict(req.url.params)
            if params.get("maxResults") == "0":
                return httpx.Response(200, json={"total": 13, "issues": []})
        return _base_handler(req)

    snap = gather_config(mk(handler, "dc"), ["ACME"])
    iq = snap["areas"]["issue_quality"]
    assert not iq.get("skipped"), "issue_quality must run on DC (approx_count is DC-aware)"
    assert iq["error"] is None
    assert iq["done_unresolved"] == 13
    assert iq["total_unresolved"] == 13


def test_issue_quality_privacy_counts_only_no_issue_content():
    """PRIVACY I1: even when the server returns issue keys/summaries/identities
    alongside the count, the snapshot must contain NONE of them — only ints."""
    counts = {
        "statusCategory = Done AND resolution = EMPTY": 4,
        "resolution = EMPTY": 20,
    }
    snap = gather_config(mk(_cloud_count_handler(counts, extra_keys=True)),
                         ["ACME"])
    snap_str = json.dumps(snap)
    # No issue key, summary, or identity may appear anywhere in the snapshot.
    assert "SECRET-123" not in snap_str
    assert "SECRET-124" not in snap_str
    assert "LEAKED ISSUE SUMMARY" not in snap_str
    assert "Eve Leak" not in snap_str
    assert "acc-leak-9" not in snap_str
    # The integer counts must still be present.
    iq = snap["areas"]["issue_quality"]
    assert iq["done_unresolved"] == 4
    # issue_quality must store ONLY the documented integer-or-None metrics + error.
    allowed = {"done_unresolved", "stale_open", "unassigned_unresolved",
               "resolved_but_open", "total_unresolved", "error"}
    assert set(iq.keys()) <= allowed, f"unexpected keys: {set(iq.keys()) - allowed}"


def test_issue_quality_whole_area_failure_records_error():
    """If the approx-count surface is entirely unavailable, every metric is
    None and the area error is set (never silently clean)."""
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/search/approximate-count") or p.endswith("/search"):
            return httpx.Response(500, json={"_error": "down"})
        return _base_handler(req)

    snap = gather_config(mk(handler), ["ACME"])
    iq = snap["areas"]["issue_quality"]
    # Every metric None; area error set so consumers treat it as unevaluable.
    assert iq["done_unresolved"] is None
    assert iq["total_unresolved"] is None
    assert iq["error"] is not None


# ===========================================================================
# PARALLELISM — bounded thread pool (perf) with byte-identical output.
# The gather runs its independent per-object/per-project/per-area reads
# concurrently. These tests PIN the hard invariant: the snapshot is identical
# regardless of worker count, a per-object error is still captured (no false
# clean), and the pool width is bounded/configurable via MA_GATHER_WORKERS.
# ===========================================================================

import threading
from auditor.envaudit import _pool


def _rich_cloud_handler():
    """A Cloud handler that populates MANY areas with several per-object reads
    (multi-project components/versions, multi-group member probes, multi-field
    options, multi-screen field deep-fetches) so the equivalence test actually
    exercises the parallel merge paths, not a degenerate single-object case."""
    projects = ["ALPHA", "BRAVO", "CHARLIE", "DELTA", "ECHO"]
    fields = [
        {"id": "customfield_1", "name": "Severity", "custom": True,
         "schema": {"custom": "com.atlassian.jira:select"}},
        {"id": "customfield_2", "name": "Team", "custom": True,
         "schema": {"custom": "com.atlassian.jira:select"}},
        {"id": "customfield_3", "name": "Points", "custom": True,
         "schema": {"custom": "com.atlassian.jira:float"}},
    ]
    groups = [{"name": f"group-{i}", "groupId": f"gid-{i}"} for i in range(8)]

    def handler(req):
        p = str(req.url.path)
        # screens list + deep tabs/fields
        if p.endswith("/screens"):
            return httpx.Response(200, json={"values": [
                {"id": 10, "name": "Default Screen"},
                {"id": 11, "name": "Bug Screen"}], "isLast": True})
        if "/screens/10/tabs" in p and "fields" not in p:
            return httpx.Response(200, json={"values": [
                {"id": 1, "name": "Tab A"}], "isLast": True})
        if "/screens/11/tabs" in p and "fields" not in p:
            return httpx.Response(200, json={"values": [
                {"id": 2, "name": "Tab B"}], "isLast": True})
        if "/screens/10/tabs/1/fields" in p:
            return httpx.Response(200, json=[{"name": "Summary"},
                                             {"name": "Severity"}])
        if "/screens/11/tabs/2/fields" in p:
            return httpx.Response(200, json=[{"name": "Priority"}])
        if p.endswith("/field") and "/context" not in p:
            return httpx.Response(200, json=fields)
        if p.endswith("/context") and "/option" not in p:
            return httpx.Response(200, json={
                "values": [{"id": "ctx1"}], "isLast": True})
        if p.endswith("/option"):
            return httpx.Response(200, json={
                "values": [{"value": "Low"}, {"value": "High"}], "isLast": True})
        if p.endswith("/group/bulk"):
            return httpx.Response(200, json={"values": groups, "isLast": True})
        if p.endswith("/group/member"):
            gid = dict(req.url.params).get("groupId", "g")
            return httpx.Response(200, json={
                "values": [], "total": int(gid.split("-")[-1]) + 1})
        if p.endswith("/permissionscheme"):
            return httpx.Response(200, json={"permissionSchemes": [
                {"name": "Default", "permissions": [
                    {"permission": "BROWSE_PROJECTS",
                     "holder": {"type": "anyone"}}]}], "isLast": True})
        if "/components" in p:
            key = p.split("/project/")[1].split("/")[0]
            return httpx.Response(200, json=[
                {"name": f"{key}-comp-1", "assigneeType": "PROJECT_LEAD"},
                {"name": f"{key}-comp-2", "assigneeType": "UNASSIGNED"}])
        if "/versions" in p:
            key = p.split("/project/")[1].split("/")[0]
            return httpx.Response(200, json=[
                {"name": f"{key}-1.0", "released": True, "archived": False}])
        if p.endswith("/project/search"):
            return httpx.Response(200, json={"values": [
                {"key": k, "insight": {"totalIssueCount": i,
                 "lastIssueUpdateTime": None}}
                for i, k in enumerate(projects)], "isLast": True})
        if p.endswith("/filter/search"):
            return httpx.Response(200, json={"values": [
                {"id": "1", "owner": {"active": True},
                 "sharePermissions": [{"type": "global"}]}], "isLast": True})
        if p.endswith("/dashboard"):
            return httpx.Response(200, json={"dashboards": [
                {"id": "1", "owner": {"active": True},
                 "sharePermissions": []}], "isLast": True})
        if p == "/rest/agile/1.0/board":
            return httpx.Response(200, json={"values": [
                {"id": 1, "name": "Board One"}], "isLast": True})
        if p.endswith("/search/approximate-count"):
            return httpx.Response(200, json={"count": 3})
        if p.endswith("/status"):
            return httpx.Response(200, json=[{"name": "Open"}, {"name": "Done"}])
        return httpx.Response(200, json={"values": [], "isLast": True})

    return handler, projects


def test_gather_equivalence_seq_vs_parallel(monkeypatch):
    """HARD INVARIANT: the snapshot is byte-for-byte identical with 1 worker
    (forced sequential) and 10 workers, against the SAME handler — proving
    concurrency never changes area keys, per-object data, caps, errors, or
    sort order."""
    handler, projects = _rich_cloud_handler()

    monkeypatch.setenv("MA_GATHER_WORKERS", "1")
    snap_seq = gather_config(mk(handler), projects)

    monkeypatch.setenv("MA_GATHER_WORKERS", "10")
    snap_par = gather_config(mk(handler), projects)

    assert json.dumps(snap_seq, sort_keys=True) == \
        json.dumps(snap_par, sort_keys=True)
    # Sanity: the snapshot is non-trivial (the parallel paths actually ran).
    assert len(snap_par["areas"]["components"]["by_project"]) == len(projects)
    assert snap_par["areas"]["components"]["count"] == len(projects) * 2


def test_gather_per_object_error_captured_under_concurrency(monkeypatch):
    """A single project's components read failing under concurrency must be
    captured into the area error (no false clean, no lost error, no crash),
    while every OTHER project still merges in normally."""
    handler, projects = _rich_cloud_handler()

    def failing(req):
        p = str(req.url.path)
        # Fail exactly one project's components read.
        if "/project/CHARLIE/components" in p:
            return httpx.Response(500, json={"_error": "boom"})
        return handler(req)

    monkeypatch.setenv("MA_GATHER_WORKERS", "10")
    snap = gather_config(mk(failing), projects)
    comp = snap["areas"]["components"]
    # Error recorded (never swallowed into a false clean)...
    assert comp["error"] is not None
    assert "CHARLIE" in comp["error"]
    # ...and the sibling projects still merged.
    assert "ALPHA" in comp["by_project"]
    assert "CHARLIE" not in comp["by_project"]
    assert comp["count"] == (len(projects) - 1) * 2


def test_gather_determinism_by_project_and_names_sorted(monkeypatch):
    """by_project dicts carry the same entries and names lists stay sorted
    regardless of worker count."""
    handler, projects = _rich_cloud_handler()

    monkeypatch.setenv("MA_GATHER_WORKERS", "8")
    snap = gather_config(mk(handler), projects)
    # by_project covers every project.
    assert set(snap["areas"]["components"]["by_project"].keys()) == set(projects)
    assert set(snap["areas"]["versions"]["by_project"].keys()) == set(projects)
    # SIMPLE-area names sorted.
    st = snap["areas"]["statuses"]["names"]
    assert st == sorted(st)
    # screen fields keyed by name, each field list sorted.
    flds = snap["areas"]["screens"]["fields"]
    for name, lst in flds.items():
        assert lst == sorted(lst)


def test_gather_worker_count_env_override(monkeypatch):
    """MA_GATHER_WORKERS is honored and clamped to >= 1; absent -> default."""
    monkeypatch.delenv("MA_GATHER_WORKERS", raising=False)
    assert _pool.worker_count() == _pool.MAX_WORKERS
    monkeypatch.setenv("MA_GATHER_WORKERS", "4")
    assert _pool.worker_count() == 4
    monkeypatch.setenv("MA_GATHER_WORKERS", "0")
    assert _pool.worker_count() == 1     # clamped to a sequential floor
    monkeypatch.setenv("MA_GATHER_WORKERS", "-5")
    assert _pool.worker_count() == 1
    monkeypatch.setenv("MA_GATHER_WORKERS", "garbage")
    assert _pool.worker_count() == _pool.MAX_WORKERS   # invalid -> default


def test_gather_pool_is_bounded(monkeypatch):
    """The pool never exceeds the configured width: with N projects and a cap
    of 3 workers, no more than 3 component reads are ever in flight at once."""
    handler, projects = _rich_cloud_handler()
    in_flight = {"now": 0, "max": 0}
    lock = threading.Lock()

    def counting(req):
        p = str(req.url.path)
        if "/components" in p:
            with lock:
                in_flight["now"] += 1
                in_flight["max"] = max(in_flight["max"], in_flight["now"])
            try:
                # Hold the slot briefly so concurrent reads actually overlap.
                import time as _t
                _t.sleep(0.02)
                return handler(req)
            finally:
                with lock:
                    in_flight["now"] -= 1
        return handler(req)

    monkeypatch.setenv("MA_GATHER_WORKERS", "3")
    gather_config(mk(counting), projects)
    assert in_flight["max"] <= 3, f"peak in-flight {in_flight['max']} exceeded cap 3"
    # And it actually parallelized (more than one in flight at some point).
    assert in_flight["max"] >= 2



def test_plugins_area_dc_reduces_to_counts_no_keys():
    """DC: the plugins area reduces UPM apps to counts + a script-app boolean;
    app KEYS are detected but never stored in the snapshot."""
    def handler(req):
        if str(req.url.path) == "/rest/plugins/1.0/":
            return httpx.Response(200, json={"plugins": [
                {"key": "com.atlassian.jira.core", "userInstalled": False,
                 "enabled": True},
                {"key": "com.onresolve.jira.groovy.groovyrunner",
                 "userInstalled": True, "enabled": True},   # ScriptRunner
                {"key": "com.acme.reporting", "userInstalled": True,
                 "enabled": False}]})
        return httpx.Response(200, json={"values": [], "isLast": True})

    snap = gather_config(mk(handler, deployment="dc"), ["ACME"],
                         progress=lambda m: None)
    p = snap["areas"]["plugins"]
    assert p["user_installed_count"] == 2 and p["enabled_count"] == 1
    assert p["script_apps_present"] is True
    import json as _json
    blob = _json.dumps(snap)
    assert "groovyrunner" not in blob and "com.acme.reporting" not in blob


def test_plugins_area_cloud_is_skipped():
    def handler(req):
        return httpx.Response(200, json={"values": [], "isLast": True})
    snap = gather_config(mk(handler, deployment="cloud"), ["ACME"],
                         progress=lambda m: None)
    assert snap["areas"]["plugins"]["skipped"] is True
