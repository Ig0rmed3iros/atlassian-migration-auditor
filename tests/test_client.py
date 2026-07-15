import json, re, httpx, pytest
from auditor.client import Connection, JiraClient, ClientError, adf_text, h16


def mk_pat(handler):
    conn = Connection(auth_type="pat", site_url="https://src.atlassian.net",
                      email="a@b.c", api_token="tok")
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return JiraClient(conn, http=http, sleeper=lambda s: None)


def test_client_refuses_link_local_and_metadata_targets():
    # No-bias review (SSRF): a user-supplied site_url sends the Bearer PAT to any
    # host. Block the cloud-metadata endpoint, metadata hostnames, link-local
    # IPs, and non-http(s) schemes before a client can be built around them.
    from auditor.confluence.client import ConfluenceClient
    for bad in ("http://169.254.169.254", "https://169.254.169.254/x",
                "https://metadata.google.internal", "ftp://example.test",
                "https://[fe80::1]"):
        conn = Connection(auth_type="pat", site_url=bad, email="e", api_token="t")
        with pytest.raises(ValueError):
            JiraClient(conn)
        with pytest.raises(ValueError):
            ConfluenceClient(conn)


def test_client_allows_cloud_and_private_dc_hosts():
    # A normal Cloud host and a self-hosted DC on a private RFC-1918 address must
    # both be allowed (DC legitimately runs on internal IPs, incl. loopback).
    JiraClient(Connection(auth_type="pat", site_url="https://acme.atlassian.net",
                          email="e", api_token="t"))
    JiraClient(Connection(auth_type="pat", site_url="https://10.0.0.5",
                          deployment="dc", email="e", api_token="t"))
    JiraClient(Connection(auth_type="pat", site_url="http://127.0.0.1:8080",
                          deployment="dc", email="e", api_token="t"))


def test_ssrf_guard_blocks_encoded_metadata_and_unspecified():
    # No-bias review: ipaddress.ip_address only accepts canonical dotted-quad, so
    # decimal/hex/octal/short-form encodings of 169.254.169.254 and IPv4-mapped
    # IPv6 SLIPPED THROUGH while the OS resolver still routed them to the metadata
    # endpoint -> proven PAT exfil. Normalize every encoding and block.
    from auditor.client import assert_safe_target
    for bad in (
        "http://2852039166",                    # decimal
        "http://0xA9FEA9FE",                    # hex
        "http://0251.0376.0251.0376",           # octal
        "http://0xA9.0xFE.0xA9.0xFE",           # mixed hex octets
        "http://169.254.43518",                 # 3-octet short form
        "https://[::ffff:169.254.169.254]",     # IPv4-mapped IPv6
        "http://metadata.google.internal.",     # trailing-dot FQDN
        "http://0.0.0.0",                        # unspecified
        "https://[::]",                          # IPv6 unspecified
    ):
        with pytest.raises(ValueError):
            assert_safe_target(bad)


def mk_dc(handler):
    conn = Connection(auth_type="pat", site_url="https://jira.acme.example",
                      deployment="dc", api_token="tok-123")
    return JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                      sleeper=lambda s: None)


def mk_oauth(handler, **kw):
    conn = Connection(auth_type="oauth", site_url="https://src.atlassian.net",
                      cloud_id="cid-1", access_token="at-1", refresh_token="rt-1",
                      expires_at=9e12, **kw)
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return JiraClient(conn, http=http, sleeper=lambda s: None), conn


def test_pat_base_and_basic_auth():
    seen = {}
    def handler(req):
        seen["url"] = str(req.url); seen["auth"] = req.headers.get("authorization", "")
        return httpx.Response(200, json={"ok": True})
    st, d = mk_pat(handler).req("/rest/api/3/myself")
    assert st == 200 and d == {"ok": True}
    assert seen["url"] == "https://src.atlassian.net/rest/api/3/myself"
    assert seen["auth"].startswith("Basic ")


def test_oauth_base_and_bearer():
    seen = {}
    def handler(req):
        seen["url"] = str(req.url); seen["auth"] = req.headers.get("authorization", "")
        return httpx.Response(200, json={})
    cl, _ = mk_oauth(handler)
    cl.req("/rest/api/3/myself")
    assert seen["url"] == "https://api.atlassian.com/ex/jira/cid-1/rest/api/3/myself"
    assert seen["auth"] == "Bearer at-1"


def test_429_honors_retry_after_then_succeeds():
    calls = {"n": 0}
    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1"}, json={})
        return httpx.Response(200, json={"done": 1})
    st, d = mk_pat(handler).req("/x")
    assert st == 200 and d == {"done": 1} and calls["n"] == 2


def test_5xx_retries_then_returns_error_dict():
    def handler(req):
        return httpx.Response(503, text="boom")
    st, d = mk_pat(handler).req("/x", tries=2)
    assert st == 503 and "_error" in d


def test_post_not_retried_on_5xx_to_avoid_double_action():
    # Review: a non-idempotent write (POST) must NOT be retried on 5xx — the
    # write may have landed despite the error, and a retry would double-create.
    calls = {"n": 0}
    def handler(req):
        calls["n"] += 1
        return httpx.Response(500, text="boom")
    st, d = mk_pat(handler).req("/x", method="POST", tries=4)
    assert calls["n"] == 1 and st == 500


def test_post_not_retried_on_transport_error():
    calls = {"n": 0}
    def handler(req):
        calls["n"] += 1
        raise httpx.ConnectError("boom")
    st, d = mk_pat(handler).req("/x", method="POST", tries=4)
    assert calls["n"] == 1 and st == -1 and "may have already applied" in d["_error"]


def test_delete_still_retried_on_5xx_idempotent():
    calls = {"n": 0}
    def handler(req):
        calls["n"] += 1
        return httpx.Response(503, text="x") if calls["n"] < 3 else httpx.Response(204, json={})
    st, d = mk_pat(handler).req("/x", method="DELETE", tries=5)
    assert st == 204 and calls["n"] == 3      # idempotent -> retried to success


def test_4xx_returns_immediately_no_retry():
    calls = {"n": 0}
    def handler(req):
        calls["n"] += 1
        return httpx.Response(404, text="nope")
    st, d = mk_pat(handler).req("/x")
    assert st == 404 and calls["n"] == 1


def test_oauth_refresh_on_401_persists_rotated_tokens():
    persisted = []
    calls = {"n": 0}
    def handler(req):
        calls["n"] += 1
        auth = req.headers.get("authorization")
        if auth == "Bearer at-1":
            return httpx.Response(401, json={})
        return httpx.Response(200, json={"fresh": True})
    def refresh(rt):
        assert rt == "rt-1"
        return {"access_token": "at-2", "refresh_token": "rt-2", "expires_in": 3600}
    cl, conn = mk_oauth(handler)
    conn.refresh_fn = refresh
    conn.on_tokens_refreshed = lambda c: persisted.append((c.access_token, c.refresh_token))
    st, d = cl.req("/rest/api/3/myself")
    assert st == 200 and d == {"fresh": True}
    assert conn.access_token == "at-2" and conn.refresh_token == "rt-2"   # ROTATED
    assert persisted == [("at-2", "rt-2")]


def test_search_jql_cursor_pagination():
    pages = [
        {"issues": [{"key": "A-1"}, {"key": "A-2"}], "nextPageToken": "t2"},
        {"issues": [{"key": "A-3"}], "isLast": True},
    ]
    def handler(req):
        body = json.loads(req.content)
        return httpx.Response(200, json=pages[1] if body.get("nextPageToken") else pages[0])
    keys = [i["key"] for i in mk_pat(handler).search_jql("project=A", ["summary"])]
    assert keys == ["A-1", "A-2", "A-3"]


def test_paginate_start_at_handles_values_envelope_and_plain_list():
    def handler(req):
        if "plain" in str(req.url):
            return httpx.Response(200, json=[{"name": "x"}])
        start = int(dict(req.url.params).get("startAt", 0))
        if start == 0:
            return httpx.Response(200, json={"values": [{"n": 1}], "isLast": False, "total": 2})
        return httpx.Response(200, json={"values": [{"n": 2}], "isLast": True, "total": 2})
    cl = mk_pat(handler)
    out, err = cl.paginate_start_at("/envelope")
    assert err is None and [r["n"] for r in out] == [1, 2]
    out2, err2 = cl.paginate_start_at("/plain")
    assert err2 is None and out2 == [{"name": "x"}]


def test_sd_list_start_limit_pagination():
    def handler(req):
        start = int(dict(req.url.params).get("start", 0))
        if start == 0:
            return httpx.Response(200, json={"values": [{"name": "q1"}], "isLastPage": False})
        return httpx.Response(200, json={"values": [{"name": "q2"}], "isLastPage": True})
    rows = mk_pat(handler).sd_list("/rest/servicedeskapi/servicedesk/1/queue")
    assert [r["name"] for r in rows] == ["q1", "q2"]


def test_approx_count_and_error_form():
    def handler(req):
        if b"bad" in req.content:
            return httpx.Response(400, text="bad jql")
        return httpx.Response(200, json={"count": 42})
    cl = mk_pat(handler)
    assert cl.approx_count("project=A") == 42
    assert cl.approx_count("bad") == "ERR400"


def test_myself_raises_client_error_on_failure():
    def handler(req):
        return httpx.Response(401, text="no")
    with pytest.raises(ClientError) as ei:
        mk_pat(handler).myself()
    assert ei.value.status == 401


def test_adf_text_and_h16():
    doc = {"type": "doc", "content": [
        {"type": "paragraph", "content": [
            {"type": "text", "text": "Hello "},
            {"type": "mention", "attrs": {"text": "Igor"}},
            {"type": "hardBreak"},
            {"type": "inlineCard", "attrs": {"url": "https://x"}},
        ]}]}
    assert adf_text(doc) == "Hello @Igor\nhttps://x"
    assert len(h16("abc")) == 16 and h16("abc") == h16("abc") and h16(None) == h16("")


def test_persistent_429_exhausts_and_fails_loud():
    def handler(req):
        return httpx.Response(429, headers={"Retry-After": "1"}, json={})
    st, d = mk_pat(handler).req("/x", tries=3)
    assert st == -1 and "_error" in d


def test_transport_error_retries_then_fails_loud():
    def handler(req):
        raise httpx.ConnectError("boom")
    st, d = mk_pat(handler).req("/x", tries=2)
    assert st == -1 and "boom" in d["_error"]


def test_401_refresh_is_single_shot_when_token_stays_bad():
    calls = {"n": 0, "refreshes": 0}
    def handler(req):
        calls["n"] += 1
        return httpx.Response(401, json={})
    def refresh(rt):
        calls["refreshes"] += 1
        return {"access_token": "still-bad", "refresh_token": "rt2",
                "expires_in": 3600}
    cl, conn = mk_oauth(handler)
    conn.refresh_fn = refresh
    st, d = cl.req("/x")
    assert st == 401 and calls["refreshes"] == 1


def test_retry_after_http_date_falls_back_to_default_wait():
    sleeps = []
    calls = {"n": 0}
    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"},
                json={})
        return httpx.Response(200, json={"ok": 1})
    conn = Connection(auth_type="pat", site_url="https://s.atlassian.net",
                      email="a@b.c", api_token="t")
    cl = JiraClient(conn, http=httpx.Client(
        transport=httpx.MockTransport(handler)), sleeper=sleeps.append)
    st, d = cl.req("/x")
    assert st == 200 and 6 in sleeps      # 5 (fallback) + 1


def test_failed_proactive_refresh_degrades_not_raises():
    def handler(req):
        return httpx.Response(200, json={"ok": 1})
    cl, conn = mk_oauth(handler)
    conn.expires_at = 1.0                  # long expired -> proactive path
    conn.refresh_fn = lambda rt: (_ for _ in ()).throw(RuntimeError("idp down"))
    st, d = cl.req("/x")                   # must not raise
    assert st == 200


def test_sd_list_raises_on_error():
    import pytest as _pytest
    def handler(req):
        return httpx.Response(503, text="down")
    with _pytest.raises(ClientError):
        mk_pat(handler).sd_list("/rest/servicedeskapi/servicedesk")


def test_paginate_reports_midloop_truncation():
    def handler(req):
        start = int(dict(req.url.params).get("startAt", 0))
        if start == 0:
            return httpx.Response(200, json={"values": [{"n": 1}], "isLast": False})
        return httpx.Response(503, text="down")
    out, err = mk_pat(handler).paginate_start_at("/x")
    assert out == [{"n": 1}] and err is not None and "ERR503" in err


def test_dc_auth_header_is_bearer():
    seen = {}
    def handler(req):
        seen["auth"] = req.headers.get("authorization", "")
        seen["path"] = req.url.path
        return httpx.Response(200, json={"name": "Igor Medeiros"})
    me = mk_dc(handler).myself()           # conn carries NO email at all
    assert me["name"] == "Igor Medeiros"
    assert seen["auth"] == "Bearer tok-123"
    assert seen["path"] == "/rest/api/2/myself"


def test_dc_search_keyset_paginates_by_id():
    reqs = []
    def handler(req):
        params = dict(req.url.params)
        reqs.append((req.url.path, params))
        m = re.search(r"id > (\d+)", params["jql"])
        after = int(m.group(1)) if m else 0
        ids = [i for i in range(1, 121) if i > after][:100]
        return httpx.Response(200, json={
            "startAt": 0, "maxResults": 100, "total": 120,
            "issues": [{"id": str(i), "key": f"ACME-{i}"} for i in ids]})
    issues = list(mk_dc(handler).search_jql(
        'project = "ACME" ORDER BY key ASC', ["summary"]))
    assert [i["key"] for i in issues] == [f"ACME-{n}" for n in range(1, 121)]
    assert len(reqs) == 3                  # 100 + 20 + empty terminator
    assert all(p == "/rest/api/2/search" for p, _ in reqs)
    assert all(prm["startAt"] == "0" for _, prm in reqs)
    assert "id >" not in reqs[0][1]["jql"]
    assert all("ORDER BY key" not in prm["jql"] for _, prm in reqs)
    assert all(prm["jql"].endswith("ORDER BY id ASC") for _, prm in reqs)


def test_dc_approx_count_uses_total():
    seen = {}
    def handler(req):
        seen["path"] = req.url.path
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json={"startAt": 0, "maxResults": 0,
                                         "total": 120, "issues": []})
    assert mk_dc(handler).approx_count('project = "ACME"') == 120
    assert seen["path"] == "/rest/api/2/search"
    assert seen["params"]["maxResults"] == "0"


def test_dc_all_projects_plain_array():
    def handler(req):
        assert req.url.path == "/rest/api/2/project"
        return httpx.Response(200, json=[{"key": "ACME", "name": "Acme"}])
    rows, err = mk_dc(handler).all_projects()
    assert err is None and rows == [{"key": "ACME", "name": "Acme"}]
    rows2, err2 = mk_dc(lambda r: httpx.Response(403, text="no")).all_projects()
    assert rows2 == [] and err2 is not None and "ERR403" in err2


def test_paginate_start_at_total_termination():
    calls = {"n": 0}
    def handler(req):
        calls["n"] += 1
        start = int(dict(req.url.params).get("startAt", 0))
        return httpx.Response(200, json={"startAt": start, "maxResults": 50,
                                         "total": 2, "values": [{"n": start + 1}]})
    out, err = mk_dc(handler).paginate_start_at("/rest/api/2/x")
    assert err is None and [r["n"] for r in out] == [1, 2]
    assert calls["n"] == 2                 # stops on total, no infinite loop


def test_paginate_wrapper_without_islast_is_single_request():
    calls = {"n": 0}
    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, json={"permissionSchemes": [{"id": 1}, {"id": 2}]})
    out, err = mk_pat(handler).paginate_start_at("/rest/api/3/permissionscheme")
    assert err is None and [r["id"] for r in out] == [1, 2]
    assert calls["n"] == 1                 # unpaginated wrapper: never re-request


def test_sd_list_sends_experimental_header():
    seen = []
    def handler(req):
        seen.append(req.headers.get("x-experimentalapi"))
        return httpx.Response(200, json={"values": [{"name": "q1"}],
                                         "isLastPage": True})
    rows = mk_pat(handler).sd_list("/rest/servicedeskapi/servicedesk/1/queue")
    assert [r["name"] for r in rows] == ["q1"] and seen == ["true"]


def test_cloud_paths_unchanged():
    seen = []
    def handler(req):
        seen.append(req.url.path)
        return httpx.Response(200, json={"issues": [], "isLast": True})
    cl = mk_pat(handler)
    assert cl.api_prefix == "/rest/api/3"
    list(cl.search_jql("project = ACME", ["summary"]))
    cl.myself()
    assert seen == ["/rest/api/3/search/jql", "/rest/api/3/myself"]


def test_dc_search_raises_on_non_advancing_page():
    """A DC backend that ignores the `id > X` keyset clause would re-serve
    the same page forever; the guard must abort loudly (ClientError) instead
    of spinning — the count-verification gate catches truncation, but a spin
    never returns to reach it."""
    calls = {"n": 0}
    def handler(req):
        calls["n"] += 1
        if calls["n"] > 3:
            raise AssertionError("keyset spin: same page re-served without abort")
        return httpx.Response(200, json={
            "issues": [{"id": "7", "key": "ACME-7"}]})
    with pytest.raises(ClientError):
        list(mk_dc(handler).search_jql('project = "ACME"', ["summary"]))
    assert calls["n"] == 2          # page 1 ok, page 2 trips the guard


def test_dc_search_order_by_strip_is_quote_aware():
    """A quoted JQL literal containing 'order by' must survive the trailing
    ORDER BY strip — the token only counts OUTSIDE double-quoted segments."""
    reqs = []
    def handler(req):
        reqs.append(dict(req.url.params)["jql"])
        return httpx.Response(200, json={"issues": []})
    list(mk_dc(handler).search_jql(
        'summary ~ "out of order by design" ORDER BY created DESC',
        ["summary"]))
    assert reqs == ['(summary ~ "out of order by design") ORDER BY id ASC']


def test_strip_order_by_quote_aware_unit():
    from auditor.client import _strip_order_by
    assert _strip_order_by('project = AC ORDER BY key ASC') == 'project = AC'
    assert _strip_order_by('summary ~ "order by"') == 'summary ~ "order by"'
    # Backslash-escaped quote inside the literal must not end the segment.
    assert _strip_order_by('summary ~ "a\\" order by b" ORDER BY id ASC') == \
        'summary ~ "a\\" order by b"'


def test_escape_query_key_backslash_escapes_quotes():
    from auditor.client import escape_query_key
    assert escape_query_key('ACME') == 'ACME'
    assert escape_query_key('AC"ME') == 'AC\\"ME'
    assert escape_query_key('AC\\"ME') == 'AC\\\\\\"ME'
    assert escape_query_key('') == ''


def test_user_groups_and_search():
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/user/groups"):
            assert req.url.params.get("accountId") == "acc-1"
            return httpx.Response(200, json=[{"name": "g1", "groupId": "gid-1"},
                                             {"name": "g2", "groupId": "gid-2"}])
        if p.endswith("/user/search"):
            assert req.url.params.get("query") == "x@y.z"
            return httpx.Response(200, json=[{"accountId": "acc-9",
                                              "accountType": "atlassian",
                                              "emailAddress": "x@y.z", "active": True}])
        return httpx.Response(404)
    c = mk_pat(handler)
    groups, err = c.user_groups("acc-1")
    assert err is None and [g["groupId"] for g in groups] == ["gid-1", "gid-2"]
    users, err = c.search_users("x@y.z")
    assert err is None and users[0]["accountId"] == "acc-9"


def test_add_user_to_group_posts_accountid():
    seen = {}
    def handler(req):
        seen["path"] = str(req.url.path)
        seen["gid"] = req.url.params.get("groupId")
        seen["body"] = json.loads(req.content)
        return httpx.Response(201, json={})
    c = mk_pat(handler)
    st, _ = c.add_user_to_group("gid-1", "acc-1")
    assert st == 201
    assert seen["path"].endswith("/group/user")
    assert seen["gid"] == "gid-1"
    assert seen["body"] == {"accountId": "acc-1"}


def test_project_role_map_and_actors():
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/project/AC/role"):
            return httpx.Response(200, json={
                "Administrators": "https://x.atlassian.net/rest/api/3/project/AC/role/10002",
                "Members": "https://x.atlassian.net/rest/api/3/project/AC/role/10001"})
        if p.endswith("/project/AC/role/10002"):
            return httpx.Response(200, json={"actors": [
                {"type": "atlassian-user-role-actor", "actorUser": {"accountId": "acc-1"}},
                {"type": "atlassian-group-role-actor", "actorGroup": {"name": "g1"}}]})
        return httpx.Response(404)
    c = mk_pat(handler)
    rmap, err = c.project_role_map("AC")
    assert err is None and rmap == {"Administrators": "10002", "Members": "10001"}
    actors, err = c.project_role_actors("AC", "10002")
    assert err is None and len(actors) == 2


def test_add_user_to_project_role_posts_user_array():
    seen = {}
    def handler(req):
        seen["path"] = str(req.url.path); seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={})
    c = mk_pat(handler)
    st, _ = c.add_user_to_project_role("AC", "10002", "acc-1")
    assert st == 200
    assert seen["path"].endswith("/project/AC/role/10002")
    assert seen["body"] == {"user": ["acc-1"]}
