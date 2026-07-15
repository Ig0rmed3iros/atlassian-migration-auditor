import json
from urllib.parse import parse_qs, urlparse
import httpx
from webapp.oauth import (accessible_resources, build_authorize_url,
                          exchange_code, refresh_tokens)


def test_authorize_url_shape():
    url = build_authorize_url("cid", "http://localhost:8484/oauth/callback", "st8")
    p = urlparse(url)
    q = parse_qs(p.query)
    assert p.netloc == "auth.atlassian.com" and p.path == "/authorize"
    assert q["audience"] == ["api.atlassian.com"]
    assert q["client_id"] == ["cid"] and q["state"] == ["st8"]
    assert q["response_type"] == ["code"] and q["prompt"] == ["consent"]
    assert set(q["scope"][0].split()) == {"read:jira-work", "read:jira-user",
                                          "offline_access"}
    assert q["redirect_uri"] == ["http://localhost:8484/oauth/callback"]


def _http(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_exchange_code_posts_grant():
    seen = {}
    def handler(req):
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"access_token": "at",
                                         "refresh_token": "rt",
                                         "expires_in": 3600})
    tok = exchange_code("cid", "sec", "the-code",
                        "http://localhost:8484/oauth/callback",
                        http=_http(handler))
    assert tok["access_token"] == "at"
    assert seen["url"] == "https://auth.atlassian.com/oauth/token"
    assert seen["body"]["grant_type"] == "authorization_code"
    assert seen["body"]["code"] == "the-code"


def test_refresh_tokens_posts_refresh_grant():
    def handler(req):
        body = json.loads(req.content)
        assert body["grant_type"] == "refresh_token"
        assert body["refresh_token"] == "rt-old"
        return httpx.Response(200, json={"access_token": "at2",
                                         "refresh_token": "rt-new",
                                         "expires_in": 3600})
    tok = refresh_tokens("cid", "sec", "rt-old", http=_http(handler))
    assert tok["refresh_token"] == "rt-new"


def test_exchange_raises_on_error():
    import pytest
    def handler(req):
        return httpx.Response(403, text="denied")
    with pytest.raises(RuntimeError):
        exchange_code("cid", "sec", "c", "r", http=_http(handler))


def test_accessible_resources_bearer():
    def handler(req):
        assert req.headers["authorization"] == "Bearer at"
        return httpx.Response(200, json=[
            {"id": "cloud-1", "url": "https://acme.atlassian.net",
             "name": "acme"}])
    sites = accessible_resources("at", http=_http(handler))
    assert sites[0]["id"] == "cloud-1"
