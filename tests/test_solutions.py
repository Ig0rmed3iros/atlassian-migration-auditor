import pytest

from auditor.solutions import build_query, finding_signature


def test_query_includes_metadata_not_body():
    # Pack sensitive content into every field build_query might be tempted to
    # read — detail.head AND a top-level summary — and prove none of it leaks.
    finding = {"area": "macros", "name": "pagetree", "kind": "missing_in_tgt",
               "summary": "SUMMARY customer issue title here",
               "detail": {"head": "SECRET customer body text here", "count": 12},
               "product": "confluence", "deployment_from": "dc"}
    q = build_query(finding)
    assert "pagetree" in q and "macro" in q.lower()
    assert "Confluence" in q
    # PRIVACY: neither the body head nor the summary may leak into the query.
    assert "SECRET customer body text" not in q
    assert "SUMMARY customer issue title" not in q


def test_query_for_issue_finding_uses_keys_not_content():
    finding = {"project": "ACME", "kind": "missing_in_tgt", "src_key": "ACME-7",
               "field": "description", "product": "jira"}
    q = build_query(finding)
    assert "ACME" in q and "Jira" in q


def test_signature_stable_and_distinct():
    a = {"area": "statuses", "name": "Triage", "kind": "missing_in_tgt"}
    b = {"area": "statuses", "name": "Triage", "kind": "missing_in_tgt"}
    c = {"area": "statuses", "name": "Done", "kind": "missing_in_tgt"}
    assert finding_signature(a) == finding_signature(b)
    assert finding_signature(a) != finding_signature(c)


def test_signature_tolerates_explicit_none_values():
    """compare.py presence findings carry explicit None for tgt_key and field.
    finding_signature must not raise TypeError when joining these values."""
    finding = {
        "src_key": "X-1", "tgt_key": None, "field": None,
        "kind": "missing_in_tgt", "project": "X", "area": "issues",
    }
    # Must not raise; result must be a 16-char hex string.
    sig = finding_signature(finding)
    assert len(sig) == 16
    assert sig.isalnum()


def test_query_area_noun_irregular_plurals():
    """'statuses' and 'priorities' must produce correct singular nouns in query."""
    status_finding = {"area": "statuses", "name": "Triage", "kind": "missing_in_tgt"}
    priority_finding = {"area": "priorities", "name": "Blocker", "kind": "missing_in_tgt"}
    q_status = build_query(status_finding)
    q_priority = build_query(priority_finding)
    assert "status" in q_status and "statuse" not in q_status
    assert "priority" in q_priority and "prioritie" not in q_priority


def test_query_article_an_for_unknown_product():
    """Fallback product 'Atlassian' starts with a vowel — query must use 'an'."""
    # No 'product' key injected — triggers Atlassian fallback
    finding = {"area": "issues", "name": "X-1", "kind": "missing_in_tgt"}
    q = build_query(finding)
    assert "In an Atlassian" in q


def test_build_query_raises_on_unknown_deployment():
    """deployment_from='server' is not a valid value; must raise ValueError."""
    finding = {"area": "issues", "name": "X-1", "kind": "missing_in_tgt",
               "product": "jira", "deployment_from": "server"}
    with pytest.raises(ValueError, match="deployment_from"):
        build_query(finding)


class _Block:
    def __init__(self, type, **kw):
        self.type = type
        for k, v in kw.items():
            setattr(self, k, v)


class _Resp:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason
        self.stop_details = None


class _FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        return self._responses.pop(0)


class _FakeClient:
    def __init__(self, responses):
        self.messages = _FakeMessages(responses)


def _json_answer(text):
    return _Resp([_Block("text", text=text)])


_FINDING = {"area": "macros", "name": "pagetree", "kind": "missing_in_tgt",
            "product": "confluence", "deployment_from": "dc"}
_GOOD = ('{"solutions": [{"title": "Install the Cloud app", "summary": "x", '
         '"steps": ["a", "b"], "applicability": "high", '
         '"sources": [{"title": "Atlassian", "url": "https://support.atlassian.com/x"}], '
         '"confidence": "high"}]}')


def test_find_solutions_parses_json_and_sources():
    from auditor.solutions import find_solutions
    client = _FakeClient([_json_answer(_GOOD)])
    out = find_solutions(_FINDING, client)
    assert out["error"] is None
    assert len(out["solutions"]) == 1
    assert out["solutions"][0]["title"] == "Install the Cloud app"
    assert out["solutions"][0]["sources"][0]["url"].startswith("https://")
    # web_search tool + scoped domains were sent
    body = client.messages.calls[0]
    assert any(t.get("type", "").startswith("web_search") for t in body["tools"])


def test_find_solutions_pause_turn_continues_once():
    from auditor.solutions import find_solutions
    paused = _Resp([_Block("server_tool_use", name="web_search", id="s1")],
                   stop_reason="pause_turn")
    client = _FakeClient([paused, _json_answer(_GOOD)])
    out = find_solutions(_FINDING, client)
    assert out["error"] is None and len(out["solutions"]) == 1
    assert len(client.messages.calls) == 2   # original + one continuation


def test_find_solutions_pause_turn_exhaustion_degrades():
    """All 5 iterations return pause_turn: the loop exhausts without ever
    seeing a final answer. resp holds the last pause_turn response (no text),
    so _final_text is '' and _parse degrades to the single advisory entry.
    The result must still be safe — one advisory solution, no crash."""
    from auditor.solutions import find_solutions
    paused = lambda: _Resp([_Block("server_tool_use", name="web_search", id="s1")],
                           stop_reason="pause_turn")
    client = _FakeClient([paused() for _ in range(5)])
    out = find_solutions(_FINDING, client)
    assert len(client.messages.calls) == 5   # cap hit: original + 4 continuations
    assert len(out["solutions"]) == 1        # degrade advisory entry, never a crash
    assert out["solutions"][0]["confidence"] == "low"
    assert out["solutions"][0]["title"] == "Search summary"


def test_find_solutions_search_error_content_does_not_raise():
    """When Anthropic returns a search-domain error, a web_search_tool_result
    block's .content is a WebSearchToolResultError object (truthy, NOT a list).
    _collect_source_urls must not iterate it (TypeError would propagate into
    the request thread, violating R3). The call must return cleanly."""
    from auditor.solutions import find_solutions
    err = _Block("web_search_tool_result_error", error_code="max_uses_exceeded")
    # truthy, non-list content — the 'or []' guard would NOT fire on this
    search_block = _Block("web_search_tool_result", content=err)
    resp = _Resp([search_block, _Block("text", text=_GOOD)])
    client = _FakeClient([resp])
    out = find_solutions(_FINDING, client)
    assert out["error"] is None
    assert len(out["solutions"]) == 1
    assert out["solutions"][0]["title"] == "Install the Cloud app"


def test_find_solutions_refusal():
    from auditor.solutions import find_solutions
    client = _FakeClient([_Resp([], stop_reason="refusal")])
    out = find_solutions(_FINDING, client)
    assert out["solutions"] == [] and "declin" in out["error"].lower()


def test_find_solutions_malformed_json_degrades():
    from auditor.solutions import find_solutions
    client = _FakeClient([_json_answer("Sorry, here is prose, not JSON.")])
    out = find_solutions(_FINDING, client)
    assert out["error"] is None
    assert len(out["solutions"]) == 1   # one advisory entry, never a crash
    assert out["solutions"][0]["summary"]


def test_find_solutions_invalid_deployment_returns_error_dict():
    """build_query raises ValueError on an unknown deployment_from, but
    find_solutions must never let that escape (it would surface as an
    unhandled 500 in the route). It returns the standard error-dict shape
    with no solutions and never touches the API client."""
    from auditor.solutions import find_solutions
    bad = {**_FINDING, "deployment_from": "server"}
    client = _FakeClient([])   # popping from an empty list would IndexError
    out = find_solutions(bad, client)
    assert out["solutions"] == []
    assert out["error"] and "deployment_from" in out["error"]
    assert "searched_at" in out and "model" in out
    assert client.messages.calls == []   # never reached the API


def test_find_solutions_auth_error_mapped():
    import anthropic
    import httpx
    from auditor.solutions import find_solutions

    class _DummyResp:
        def __init__(self):
            self.status_code = 401
            self.headers = {}
            self.request = httpx.Request("POST", "https://api.anthropic.com")

    class _Boom:
        class messages:
            @staticmethod
            def create(**kw):
                raise anthropic.AuthenticationError(
                    "bad", response=_DummyResp(), body=None)

    out = find_solutions(_FINDING, _Boom())
    assert out["solutions"] == [] and "key" in out["error"].lower()
