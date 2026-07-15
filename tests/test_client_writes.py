import json
import httpx
from auditor.client import Connection, JiraClient


def mk(handler):
    conn = Connection(auth_type="pat", site_url="https://t.atlassian.net",
                      deployment="cloud", email="a@b.c", api_token="x")
    return JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                      sleeper=lambda s: None)


def test_create_field_posts_v3_and_returns_id():
    seen = {}
    def handler(req):
        seen["path"], seen["body"] = str(req.url.path), req.content.decode()
        return httpx.Response(201, json={"id": "customfield_10101"})
    st, d = mk(handler).create_field("Severity", "select")
    assert st == 201 and d["id"] == "customfield_10101"
    assert seen["path"] == "/rest/api/3/field"
    assert "Severity" in seen["body"]


def test_create_status_uses_statuses_endpoint_and_global_scope():
    seen = {}
    def handler(req):
        seen["path"], seen["body"] = str(req.url.path), req.content.decode()
        return httpx.Response(200, json=[{"id": "10010", "name": "Triage"}])
    st, d = mk(handler).create_status("Triage", "TODO")
    assert st == 200 and d[0]["id"] == "10010"
    assert seen["path"] == "/rest/api/3/statuses"
    assert "GLOBAL" in seen["body"] and "Triage" in seen["body"]


def test_add_field_to_screen_tab_targets_tab_fields():
    seen = {}
    def handler(req):
        seen["path"] = str(req.url.path)
        return httpx.Response(200, json={"id": "customfield_10101"})
    mk(handler).add_field_to_screen("99", "5", "customfield_10101")
    assert seen["path"] == "/rest/api/3/screens/99/tabs/5/fields"


# ---- set_issue_fields --------------------------------------------------

def test_set_issue_fields_uses_put_and_notifyUsers_false():
    seen = {}
    def handler(req):
        seen["method"] = req.method
        seen["path"] = str(req.url.path)
        seen["params"] = str(req.url.query)
        seen["body"] = json.loads(req.content)
        return httpx.Response(204, content=b"")
    mk(handler).set_issue_fields("PROJ-1", {"summary": "Migration copy"})
    assert seen["method"] == "PUT"
    assert seen["path"] == "/rest/api/3/issue/PROJ-1"
    assert "notifyUsers=false" in seen["params"]
    assert seen["body"]["fields"]["summary"] == "Migration copy"


def test_set_issue_fields_notify_true_encodes_correctly():
    seen = {}
    def handler(req):
        seen["params"] = str(req.url.query)
        return httpx.Response(204, content=b"")
    mk(handler).set_issue_fields("PROJ-2", {}, notify=True)
    assert "notifyUsers=true" in seen["params"]


# ---- create_issue_type --------------------------------------------------

def test_create_issue_type_standard_when_hierarchy_zero():
    seen = {}
    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(201, json={"id": "10002"})
    mk(handler).create_issue_type("Epic", hierarchy_level=0)
    assert seen["body"]["type"] == "standard"


def test_create_issue_type_subtask_when_hierarchy_negative():
    seen = {}
    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(201, json={"id": "10003"})
    mk(handler).create_issue_type("Sub-task", hierarchy_level=-1)
    assert seen["body"]["type"] == "subtask"


# ---- add_field_options --------------------------------------------------

def test_add_field_options_body_shape():
    seen = {}
    def handler(req):
        seen["path"] = str(req.url.path)
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"options": []})
    mk(handler).add_field_options("customfield_10200", "ctx-1", ["Low", "High"])
    assert seen["path"] == "/rest/api/3/field/customfield_10200/context/ctx-1/option"
    opts = seen["body"]["options"]
    assert len(opts) == 2
    assert opts[0] == {"value": "Low", "disabled": False}
    assert opts[1] == {"value": "High", "disabled": False}


# ---- create_field_context -----------------------------------------------

def test_create_field_context_omits_empty_project_and_issue_type_ids():
    seen = {}
    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(201, json={"id": "ctx-10"})
    mk(handler).create_field_context("customfield_10200", "Global Context")
    assert "projectIds" not in seen["body"]
    assert "issueTypeIds" not in seen["body"]
    assert seen["body"]["name"] == "Global Context"


def test_create_field_context_includes_project_and_issue_type_ids_when_given():
    seen = {}
    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(201, json={"id": "ctx-11"})
    mk(handler).create_field_context("customfield_10200", "Scoped",
                                     project_ids=["10001"], issue_type_ids=["10002"])
    assert seen["body"]["projectIds"] == ["10001"]
    assert seen["body"]["issueTypeIds"] == ["10002"]


# ---- get_workflow -------------------------------------------------------

def test_get_workflow_is_post_not_get():
    seen = {}
    def handler(req):
        seen["method"] = req.method
        seen["path"] = str(req.url.path)
        seen["body"] = json.loads(req.content)
        seen["params"] = str(req.url.query)
        return httpx.Response(200, json={"workflows": []})
    mk(handler).get_workflow("Software Simplified Workflow")
    assert seen["method"] == "POST"
    assert seen["path"] == "/rest/api/3/workflows"
    assert seen["body"]["workflowNames"] == ["Software Simplified Workflow"]
    assert "transitions" in seen["params"] and "statuses" in seen["params"]


# ---- update_workflow ----------------------------------------------------

def test_update_workflow_posts_to_update_endpoint():
    seen = {}
    def handler(req):
        seen["method"] = req.method
        seen["path"] = str(req.url.path)
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={})
    payload = {"statuses": [], "workflows": [{"id": "wf-1"}]}
    mk(handler).update_workflow(payload)
    assert seen["method"] == "POST"
    assert seen["path"] == "/rest/api/3/workflows/update"
    assert seen["body"] == payload


# ---- create_priority ----------------------------------------------------

def test_create_priority_posts_name_and_description():
    seen = {}
    def handler(req):
        seen["path"] = str(req.url.path)
        seen["body"] = json.loads(req.content)
        return httpx.Response(201, json={"id": "1"})
    mk(handler).create_priority("Highest", "Blocks everything")
    assert seen["path"] == "/rest/api/3/priority"
    assert seen["body"] == {"name": "Highest", "description": "Blocks everything"}


# ---- create_resolution --------------------------------------------------

def test_create_resolution_posts_name_and_description():
    seen = {}
    def handler(req):
        seen["path"] = str(req.url.path)
        seen["body"] = json.loads(req.content)
        return httpx.Response(201, json={"id": "2"})
    mk(handler).create_resolution("Won't Fix", "Out of scope")
    assert seen["path"] == "/rest/api/3/resolution"
    assert seen["body"] == {"name": "Won't Fix", "description": "Out of scope"}


# ---- create_link_type ---------------------------------------------------

def test_create_link_type_posts_name_inward_outward():
    seen = {}
    def handler(req):
        seen["path"] = str(req.url.path)
        seen["body"] = json.loads(req.content)
        return httpx.Response(201, json={"id": "10001"})
    mk(handler).create_link_type("blocks", "is blocked by", "blocks")
    assert seen["path"] == "/rest/api/3/issueLinkType"
    assert seen["body"] == {"name": "blocks", "inward": "is blocked by",
                            "outward": "blocks"}


# ---- create_screen ------------------------------------------------------

def test_create_screen_posts_to_screens_endpoint():
    seen = {}
    def handler(req):
        seen["path"] = str(req.url.path)
        seen["body"] = json.loads(req.content)
        return httpx.Response(201, json={"id": "3"})
    mk(handler).create_screen("Default Screen", "Main screen")
    assert seen["path"] == "/rest/api/3/screens"
    assert seen["body"] == {"name": "Default Screen", "description": "Main screen"}


# ---- add_screen_tab -----------------------------------------------------

def test_add_screen_tab_posts_to_tabs_endpoint():
    seen = {}
    def handler(req):
        seen["path"] = str(req.url.path)
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"id": "7", "name": "Field Tab"})
    mk(handler).add_screen_tab("42", "Field Tab")
    assert seen["path"] == "/rest/api/3/screens/42/tabs"
    assert seen["body"] == {"name": "Field Tab"}


# ---- app-tier env-fix deletes -------------------------------------------

def test_delete_screen_issues_delete_to_screens_path():
    seen = {}
    def handler(req):
        seen["method"], seen["path"] = req.method, str(req.url.path)
        return httpx.Response(204, content=b"")
    st, _ = mk(handler).delete_screen("10025")
    assert st == 204
    assert seen["method"] == "DELETE"
    assert seen["path"] == "/rest/api/3/screens/10025"


def test_delete_workflow_issues_delete_to_workflow_entity_path():
    seen = {}
    def handler(req):
        seen["method"], seen["path"] = req.method, str(req.url.path)
        return httpx.Response(204, content=b"")
    st, _ = mk(handler).delete_workflow("a1b2-uuid")
    assert st == 204
    assert seen["method"] == "DELETE"
    assert seen["path"] == "/rest/api/3/workflow/a1b2-uuid"


def test_delete_field_issues_delete_to_field_path():
    seen = {}
    def handler(req):
        seen["method"], seen["path"] = req.method, str(req.url.path)
        return httpx.Response(303, json={})  # Cloud field delete returns a task
    st, _ = mk(handler).delete_field("customfield_10500")
    assert st == 303
    assert seen["method"] == "DELETE"
    assert seen["path"] == "/rest/api/3/field/customfield_10500"


def test_delete_project_issues_delete_to_project_key_path():
    seen = {}
    def handler(req):
        seen["method"], seen["path"] = req.method, str(req.url.path)
        return httpx.Response(204, content=b"")
    st, _ = mk(handler).delete_project("ABANDONED")
    assert st == 204
    assert seen["method"] == "DELETE"
    assert seen["path"] == "/rest/api/3/project/ABANDONED"


def test_delete_status_issues_delete_with_id_query_param():
    """Cloud status delete is keyed by id via the ?id= query param on the bulk
    /statuses endpoint (not a path segment)."""
    seen = {}
    def handler(req):
        seen["method"], seen["path"] = req.method, str(req.url.path)
        seen["params"] = dict(req.url.params)
        return httpx.Response(204, content=b"")
    st, _ = mk(handler).delete_status("10042")
    assert st == 204
    assert seen["method"] == "DELETE"
    assert seen["path"] == "/rest/api/3/statuses"
    assert seen["params"].get("id") == "10042"


def test_deletes_do_not_raise_on_4xx():
    """Each delete returns the status (and error body) without raising on 4xx."""
    def handler(req):
        return httpx.Response(404, json={"errorMessages": ["gone"]})
    cl = mk(handler)
    for st, d in (cl.delete_screen("1"), cl.delete_workflow("w"),
                  cl.delete_field("customfield_1"), cl.delete_project("P"),
                  cl.delete_status("10042")):
        assert st == 404
        assert isinstance(d, dict)
