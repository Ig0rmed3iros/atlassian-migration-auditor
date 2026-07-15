import httpx
from auditor.client import Connection, JiraClient
from auditor.permissions import (apply_elevation, detect_blind_spots,
                                 find_admin_role_id, undo_elevation)


def mk(handler):
    conn = Connection(auth_type="pat", site_url="https://t.atlassian.net",
                      email="a@b.c", api_token="x")
    return JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                      sleeper=lambda s: None)


def test_detect_blind_spot_when_search_zero_but_insight_populated():
    def handler(req):
        p = str(req.url.path)
        if p.endswith("approximate-count"):
            jql = req.content.decode()
            return httpx.Response(200, json={"count": 0 if "MS" in jql else 40000})
        if p.endswith("project/search"):
            return httpx.Response(200, json={"isLast": True, "values": [
                {"key": "MS", "insight": {"totalIssueCount": 16016}},
                {"key": "AC", "insight": {"totalIssueCount": 40092}}]})
        return httpx.Response(404)
    out = detect_blind_spots(mk(handler), ["MS", "AC"])
    ms = next(o for o in out if o["key"] == "MS")
    ac = next(o for o in out if o["key"] == "AC")
    assert ms["blind_spot"] is True and ms["search_count"] == 0
    assert ms["insight_count"] == 16016
    assert ac["blind_spot"] is False


def test_no_blind_spot_when_insight_missing_and_search_zero():
    def handler(req):
        p = str(req.url.path)
        if p.endswith("approximate-count"):
            return httpx.Response(200, json={"count": 0})
        if p.endswith("project/search"):
            return httpx.Response(200, json={"isLast": True, "values": [
                {"key": "EMPTY"}]})
        return httpx.Response(404)
    out = detect_blind_spots(mk(handler), ["EMPTY"])
    assert out[0]["blind_spot"] is False        # genuinely empty, not masked


def test_find_admin_role_id_prefers_administrators():
    def handler(req):
        return httpx.Response(200, json=[
            {"name": "Developers", "id": 1},
            {"name": "Administrators", "id": 9},
            {"name": "Admin-lite", "id": 3}])
    assert find_admin_role_id(mk(handler)) == 9


def test_apply_and_undo_elevation_logs():
    posts, deletes = [], []
    def handler(req):
        if req.method == "POST":
            posts.append(str(req.url.path))
            return httpx.Response(200, json={})
        if req.method == "DELETE":
            deletes.append(str(req.url))
            return httpx.Response(204)
        return httpx.Response(404)
    cl = mk(handler)
    grants = apply_elevation(cl, ["10001", "10002"], role_id=9, account_id="acc-1")
    assert [g["ok"] for g in grants] == [True, True]
    assert posts == ["/rest/api/3/project/10001/role/9",
                     "/rest/api/3/project/10002/role/9"]
    undone = undo_elevation(cl, grants, role_id=9, account_id="acc-1")
    assert all(u["ok"] for u in undone) and len(deletes) == 2
    assert "user=acc-1" in deletes[0]


def test_apply_elevation_records_failures():
    def handler(req):
        if "10002" in str(req.url.path):
            return httpx.Response(400, text="already a member")
        return httpx.Response(200, json={})
    grants = apply_elevation(mk(handler), ["10001", "10002"], 9, "acc-1")
    assert grants[0]["ok"] is True and grants[1]["ok"] is False


def test_already_member_is_never_undone():
    posts, deletes = [], []
    def handler(req):
        p = str(req.url.path)
        if req.method == "GET" and "/role/9" in p:
            return httpx.Response(200, json={"actors": [
                {"actorUser": {"accountId": "acc-1"}}]})
        if req.method == "POST":
            posts.append(p); return httpx.Response(200, json={})
        if req.method == "DELETE":
            deletes.append(p); return httpx.Response(204)
        return httpx.Response(404)
    cl = mk(handler)
    grants = apply_elevation(cl, ["10001"], role_id=9, account_id="acc-1")
    assert grants == [{"project_id": "10001", "status": 200, "ok": True,
                       "added": False}]
    assert posts == []                      # nothing granted
    undo_elevation(cl, grants, role_id=9, account_id="acc-1")
    assert deletes == []                    # NEVER deletes pre-existing membership


def test_undo_skips_unadded_grants_explicitly():
    deletes = []
    def handler(req):
        if req.method == "DELETE":
            deletes.append(str(req.url)); return httpx.Response(204)
        return httpx.Response(404)
    grants = [{"project_id": "1", "status": 400, "ok": False, "added": False},
              {"project_id": "2", "status": 200, "ok": True, "added": True}]
    out = undo_elevation(mk(handler), grants, role_id=9, account_id="acc-1")
    assert len(deletes) == 1 and "/project/2/" in deletes[0]
    assert out[0]["ok"] is True


def test_blind_spot_indeterminate_when_count_errors():
    def handler(req):
        p = str(req.url.path)
        if p.endswith("approximate-count"):
            return httpx.Response(503, text="down")
        if p.endswith("project/search"):
            return httpx.Response(200, json={"isLast": True, "values": [
                {"key": "MS", "insight": {"totalIssueCount": 16016}}]})
        return httpx.Response(404)
    out = detect_blind_spots(mk(handler), ["MS"])
    assert out[0]["blind_spot"] is False
    assert out[0]["indeterminate"] is True


def test_blind_spot_threshold_boundaries():
    def handler(req):
        p = str(req.url.path)
        if p.endswith("approximate-count"):
            import json as _j
            jql = req.content.decode()
            if "TINY" in jql:
                return httpx.Response(200, json={"count": 0})
            return httpx.Response(200, json={"count": 8008})
        if p.endswith("project/search"):
            return httpx.Response(200, json={"isLast": True, "values": [
                {"key": "TINY", "insight": {"totalIssueCount": 1}},
                {"key": "HALF", "insight": {"totalIssueCount": 16016}}]})
        return httpx.Response(404)
    out = {o["key"]: o for o in detect_blind_spots(mk(handler), ["TINY", "HALF"])}
    assert out["TINY"]["blind_spot"] is True      # 1 unreadable issue IS a blind spot
    assert out["HALF"]["blind_spot"] is False     # 8008 == 16016*0.5, strict <


def test_blind_spot_jql_escapes_quote_bearing_key():
    seen = []

    def handler(req):
        import json as _j
        p = str(req.url.path)
        if p.endswith("approximate-count"):
            seen.append(_j.loads(req.content.decode())["jql"])
            return httpx.Response(200, json={"count": 5})
        if p.endswith("project/search"):
            return httpx.Response(200, json={"isLast": True, "values": [
                {"key": 'EV"IL', "insight": {"totalIssueCount": 5}}]})
        return httpx.Response(404)
    out = detect_blind_spots(mk(handler), ['EV"IL'])
    assert out[0]["blind_spot"] is False
    # The quote must arrive backslash-escaped inside the JQL literal so the
    # key can never break out and rewrite the query.
    assert seen[0] == 'project = "EV\\"IL"'
