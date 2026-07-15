import httpx
from fastapi.testclient import TestClient
from webapp.main import create_app
from webapp.config import Config


def _client(tmp_path, handler):
    cfg = Config(data_dir=str(tmp_path), bind_host="127.0.0.1", bind_port=8484,
                 public_base_url="http://localhost:8484", secret_key=None)
    app = create_app(cfg)
    # inject a mock transport so any Jira call the preview makes is faked
    app.state.http = httpx.Client(transport=httpx.MockTransport(handler))
    return TestClient(app), app


def _ok_handler(req):
    p = str(req.url.path)
    if p.endswith("/user/groups"):
        aid = req.url.params.get("accountId")
        return httpx.Response(200, json=[{"name": "g1", "groupId": "gid1"}]
                              if aid == "main-id" else [])
    if p.endswith("/user/search"):
        q = req.url.params.get("query")
        return httpx.Response(200, json=[{"accountId": q.split("@")[0] + "-id",
            "accountType": "atlassian", "active": True, "emailAddress": q}])
    return httpx.Response(200, json={})


def test_clone_page_renders(tmp_path):
    client, app = _client(tmp_path, _ok_handler)
    r = client.get("/clone")
    assert r.status_code == 200 and "Clone access" in r.text


def test_clone_preview_renders_plan(tmp_path):
    client, app = _client(tmp_path, _ok_handler)
    cid = app.state.store.create_saved_connection(
        "acme", "jira", "cloud", "https://acme.atlassian.net",
        "e@x.y", "tok")
    r = client.post("/clone/preview", data={"conn_id": str(cid),
                    "main": "main@x.y", "clone": "clone@x.y"})
    assert r.status_code == 200
    assert "main@x.y" in r.text and "g1" in r.text     # the group to add
