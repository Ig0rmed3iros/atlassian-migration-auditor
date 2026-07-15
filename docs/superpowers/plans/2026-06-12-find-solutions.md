# Find Solutions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** For any audit finding, search the web (Atlassian docs/community/KB) via the Claude API web-search tool and surface all credible solutions with cited sources, on demand, sending only finding metadata.

**Architecture:** A pure `auditor/solutions.py` builds a metadata-only query and calls an injected `anthropic` client with the `web_search_20260209` tool, returning a shaped solutions dict. The Anthropic key lives Fernet-encrypted in Settings. Results cache per finding in a new SQLite table. A `POST /runs/{id}/solutions` route + a Find-solutions button/panel on the analysis page wire it together.

**Tech Stack:** Python 3.11, FastAPI, the official `anthropic` SDK (injected for tests — no live calls), SQLite, pytest, vanilla JS. Spec: `docs/superpowers/specs/2026-06-12-find-solutions.md`.

**Conventions:** run the suite with `python3 -m pytest -q` from the repo root (`/mnt/d/Atlassian-Products/MA-solutions`). Tests inject a fake Anthropic client (a small object exposing `.messages.create(...)`); never call the real API. Default model `claude-opus-4-8` (the exact, complete ID — no date suffix), overridable via `MA_SOLUTIONS_MODEL`.

---

## File structure

**Create:** `auditor/solutions.py`, `webapp/anthropic_key.py`, `webapp/static/solutions.js`, `tests/test_solutions.py`, `tests/test_anthropic_key.py`, `tests/test_solutions_routes.py`.
**Modify:** `pyproject.toml` (add `anthropic`), `webapp/store.py` (table + methods + `_migrate`), `webapp/main.py` (route + Settings key field), `webapp/templates/settings.html` (key input), `webapp/templates/analysis.html` (button), `webapp/static/app.js` (button wiring — or load solutions.js), `README.md`.

---

### Task 1: Anthropic key in Settings (store + helper)

**Files:** Create `webapp/anthropic_key.py`; Test `tests/test_anthropic_key.py`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_anthropic_key.py
from webapp.store import Store
from webapp.anthropic_key import save_key, load_key, anthropic_client


def test_key_roundtrip_encrypted(tmp_path):
    s = Store(str(tmp_path / "a.db"), str(tmp_path / "a.key"))
    assert load_key(s) is None
    save_key(s, "sk-ant-test-123")
    assert load_key(s) == "sk-ant-test-123"
    # stored value is encrypted, not plaintext
    raw = s.settings_get("anthropic_api_key_enc")
    assert raw and "sk-ant-test-123" not in raw


def test_anthropic_client_none_without_key(tmp_path):
    s = Store(str(tmp_path / "b.db"), str(tmp_path / "b.key"))
    assert anthropic_client(s) is None


def test_anthropic_client_built_with_key(tmp_path):
    s = Store(str(tmp_path / "c.db"), str(tmp_path / "c.key"))
    save_key(s, "sk-ant-test-xyz")
    client = anthropic_client(s)
    assert client is not None and hasattr(client, "messages")
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_anthropic_key.py -q` — FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# webapp/anthropic_key.py
"""Anthropic API key storage (Fernet-encrypted in settings) + client factory.

Mirrors the oauth_client_secret_enc pattern: the key never sits in the DB in
plaintext, and anthropic_client() returns None when unset so callers can show
an actionable 'add a key in Settings' prompt instead of crashing."""
from __future__ import annotations

_SETTING = "anthropic_api_key_enc"


def save_key(store, key: str) -> None:
    if key and key.strip():
        store.settings_set(_SETTING, store.encrypt({"key": key.strip()}).decode())


def load_key(store) -> str | None:
    enc = store.settings_get(_SETTING)
    if not enc:
        return None
    try:
        return store.decrypt(enc.encode()).get("key")
    except Exception:
        return None


def anthropic_client(store):
    """Build an anthropic.Anthropic from the stored key, or None when unset.
    Imported lazily so the dependency is only needed when the feature is used."""
    key = load_key(store)
    if not key:
        return None
    import anthropic
    return anthropic.Anthropic(api_key=key)
```

- [ ] **Step 4: Add the dependency**

In `pyproject.toml`, add `"anthropic>=0.40"` to `dependencies`. Run `pip install -e .` so the import resolves.

- [ ] **Step 5: Run to verify pass + commit**

Run: `python3 -m pytest tests/test_anthropic_key.py -q` — PASS.
```bash
git add webapp/anthropic_key.py tests/test_anthropic_key.py pyproject.toml
git commit -m "feat: Anthropic API key storage + client factory"
```

---

### Task 2: Privacy-safe query builder + finding signature

**Files:** Create `auditor/solutions.py` (query + signature only this task); Test `tests/test_solutions.py`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_solutions.py
from auditor.solutions import build_query, finding_signature


def test_query_includes_metadata_not_body():
    finding = {"area": "macros", "name": "pagetree", "kind": "missing_in_tgt",
               "detail": {"head": "SECRET customer body text here", "count": 12},
               "product": "confluence", "deployment_from": "dc"}
    q = build_query(finding)
    assert "pagetree" in q and "macro" in q.lower()
    assert "Confluence" in q
    # PRIVACY: the body head must never leak into the query
    assert "SECRET customer body text" not in q


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
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest tests/test_solutions.py -q` — FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# auditor/solutions.py
"""Web-sourced solution discovery for a single finding (spec R1-R4).

Privacy boundary (R4): only finding METADATA — defect kind, object names,
keys, counts, product/deployment — is ever assembled into the query. Body
text, fingerprint heads/shas, and captured values are NEVER read here."""
from __future__ import annotations

import hashlib
import json

_PRODUCT = {"jira": "Jira", "confluence": "Confluence"}
_KIND_PHRASE = {
    "missing_in_tgt": "is missing on the target after migration",
    "missing_in_src": "exists on the target but not the source",
    "type_mismatch": "has a different type on the target",
    "option_mismatch": "is missing select options on the target",
    "structure_mismatch": "has a different structure on the target",
    "field_mismatch": "has fields missing on the target",
    "content_mismatch": "has differing content on the target",
    "count_mismatch": "has a lower count on the target",
    "key_collision": "has a key collision (same key, different item)",
    "user_gap": "references a user not resolvable on the target",
    "area_error": "could not be read on one side",
}


def build_query(finding: dict) -> str:
    product = _PRODUCT.get(finding.get("product"), "Atlassian")
    dep = finding.get("deployment_from")
    direction = f"{product} {'Data Center' if dep == 'dc' else 'Cloud'} to Cloud" \
        if dep else f"{product} Cloud to Cloud"
    obj = finding.get("name") or finding.get("src_key") or finding.get("tgt_key") \
        or finding.get("project") or "object"
    area = finding.get("area") or ("issue" if finding.get("src_key") else "object")
    noun = "macro" if area == "macros" else area.rstrip("s").replace("_", " ")
    phrase = _KIND_PHRASE.get(finding.get("kind"), "differs after migration")
    field = finding.get("field")
    field_bit = f" (field '{field}')" if field else ""
    return (f"In a {direction} migration, the {noun} '{obj}'{field_bit} {phrase}. "
            f"What are the known solutions, workarounds, and root causes? "
            f"Search Atlassian's documentation, community, support knowledge base, "
            f"and marketplace.")


def finding_signature(finding: dict) -> str:
    parts = [finding.get("kind", ""), finding.get("area", ""),
             finding.get("project", ""), finding.get("name", ""),
             finding.get("src_key", ""), finding.get("tgt_key", ""),
             finding.get("field", "")]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]
```

- [ ] **Step 4: Run to verify pass + commit**

Run: `python3 -m pytest tests/test_solutions.py -q` — PASS.
```bash
git add auditor/solutions.py tests/test_solutions.py
git commit -m "feat(solutions): privacy-safe query builder + finding signature"
```

---

### Task 3: `find_solutions` — Claude web-search call

**Files:** Modify `auditor/solutions.py`; Test `tests/test_solutions.py` (append).

- [ ] **Step 1: Write the failing tests** (fake client mimics the SDK shape)

```python
# tests/test_solutions.py  (append)
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


def test_find_solutions_auth_error_mapped():
    import anthropic
    from auditor.solutions import find_solutions

    class _Boom:
        class messages:
            @staticmethod
            def create(**kw):
                raise anthropic.AuthenticationError(
                    "bad", response=_DummyResp(), body=None)
    # AuthenticationError needs a response object; build a minimal one
    import httpx
    global _DummyResp
    class _DummyResp:
        def __init__(self):
            self.status_code = 401
            self.headers = {}
            self.request = httpx.Request("POST", "https://api.anthropic.com")
    out = find_solutions(_FINDING, _Boom())
    assert out["solutions"] == [] and "key" in out["error"].lower()
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest tests/test_solutions.py -k find_solutions -q` — FAIL.

- [ ] **Step 3: Implement** (append to `auditor/solutions.py`)

```python
import os

_DOMAINS = ["support.atlassian.com", "community.atlassian.com",
            "confluence.atlassian.com", "developer.atlassian.com",
            "marketplace.atlassian.com", "jira.atlassian.com", "atlassian.com"]
_SYSTEM = (
    "You are an Atlassian migration expert. Use the web_search tool against "
    "Atlassian's documentation, community, support KB and marketplace to find "
    "EVERY credible solution, workaround, and root cause for the described "
    "migration defect. Then reply with ONLY a JSON object of this exact shape, "
    "no prose around it: {\"solutions\": [{\"title\": str, \"summary\": str, "
    "\"steps\": [str], \"applicability\": str, \"sources\": [{\"title\": str, "
    "\"url\": str}], \"confidence\": \"high\"|\"medium\"|\"low\"}]}. Include the "
    "real source URLs you found in each solution's sources. If you find nothing "
    "credible, return {\"solutions\": []}.")


def _final_text(resp) -> str:
    return "".join(b.text for b in resp.content
                   if getattr(b, "type", None) == "text" and getattr(b, "text", None))


def _collect_source_urls(resp) -> list:
    """Best-effort harvest of web_search result URLs from the response blocks,
    used as a fallback if a solution omitted its sources."""
    urls = []
    for b in resp.content:
        if getattr(b, "type", None) == "web_search_tool_result":
            for r in (getattr(b, "content", None) or []):
                u = getattr(r, "url", None)
                if u:
                    urls.append({"title": getattr(r, "title", u), "url": u})
    return urls


def _parse(text, fallback_sources):
    try:
        start, end = text.index("{"), text.rindex("}") + 1
        data = json.loads(text[start:end])
        sols = data.get("solutions", [])
        if isinstance(sols, list):
            for s in sols:
                if not s.get("sources") and fallback_sources:
                    s["sources"] = fallback_sources[:5]
            return sols
    except (ValueError, json.JSONDecodeError, AttributeError):
        pass
    # Degrade: one advisory entry carrying the model's prose + any found sources.
    return [{"title": "Search summary", "summary": text[:1500].strip() or
             "No structured solutions were returned.", "steps": [],
             "applicability": "review", "sources": fallback_sources[:5],
             "confidence": "low"}]


def find_solutions(finding: dict, client, *, model: str | None = None,
                   effort: str = "medium", max_solutions: int = 8) -> dict:
    import anthropic
    model = model or os.environ.get("MA_SOLUTIONS_MODEL", "claude-opus-4-8")
    query = build_query(finding)
    messages = [{"role": "user", "content": query}]
    tools = [{"type": "web_search_20260209", "name": "web_search",
              "allowed_domains": _DOMAINS, "max_uses": 6}]
    sources = []
    try:
        for _ in range(5):   # original + up to 4 pause_turn continuations
            resp = client.messages.create(
                model=model, max_tokens=6000, system=_SYSTEM,
                thinking={"type": "adaptive"},
                output_config={"effort": effort},
                tools=tools, messages=messages)
            if getattr(resp, "stop_reason", None) == "refusal":
                return {"query": query, "solutions": [],
                        "error": "the model declined this request",
                        "searched_at": None, "model": model}
            sources = _collect_source_urls(resp) or sources
            if getattr(resp, "stop_reason", None) == "pause_turn":
                messages = messages + [{"role": "assistant", "content": resp.content}]
                continue
            break
        sols = _parse(_final_text(resp), sources)[:max_solutions]
        return {"query": query, "solutions": sols, "error": None,
                "searched_at": None, "model": model}
    except anthropic.AuthenticationError:
        return {"query": query, "solutions": [],
                "error": "invalid or missing Anthropic API key", "model": model}
    except (anthropic.RateLimitError, anthropic.APIConnectionError,
            anthropic.APIStatusError) as exc:
        return {"query": query, "solutions": [],
                "error": f"solution search failed: {exc}", "model": model}
```

Note: `searched_at` is stamped by the caller (the store/route) — `solutions.py` stays clock-free for testability.

- [ ] **Step 4: Run to verify pass + commit**

Run: `python3 -m pytest tests/test_solutions.py -q` — PASS.
```bash
git add auditor/solutions.py tests/test_solutions.py
git commit -m "feat(solutions): Claude web-search call with pause_turn + refusal + parse fallback"
```

---

### Task 4: Solutions cache table + store methods

**Files:** Modify `webapp/store.py`; Test `tests/test_store.py` (append).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_store.py  (append)
def test_finding_solutions_roundtrip(tmp_path):
    from webapp.store import Store
    s = Store(str(tmp_path / "sol.db"), str(tmp_path / "sol.key"))
    mid = s.create_migration("m"); rid = s.create_run(mid, {})
    assert s.get_solutions(rid, "sig1") is None
    s.save_solutions(rid, "sig1", {"solutions": [{"title": "x"}], "model": "m"})
    got = s.get_solutions(rid, "sig1")
    assert got["payload"]["solutions"][0]["title"] == "x"
    assert isinstance(got["created_at"], float)
    # overwrite (refresh)
    s.save_solutions(rid, "sig1", {"solutions": [], "model": "m"})
    assert s.get_solutions(rid, "sig1")["payload"]["solutions"] == []
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest tests/test_store.py -k finding_solutions -q` — FAIL.

- [ ] **Step 3: Implement** — add to `_SCHEMA`:

```sql
CREATE TABLE IF NOT EXISTS finding_solutions (
  run_id INTEGER NOT NULL REFERENCES runs(id),
  finding_sig TEXT NOT NULL, payload_json TEXT NOT NULL,
  created_at REAL NOT NULL, PRIMARY KEY (run_id, finding_sig));
```

`_migrate()` needs no change (CREATE TABLE IF NOT EXISTS in `_SCHEMA` runs on every open). Add methods:

```python
def save_solutions(self, run_id: int, sig: str, payload: dict) -> None:
    self._exec(
        "INSERT INTO finding_solutions(run_id,finding_sig,payload_json,created_at)"
        " VALUES(?,?,?,?) ON CONFLICT(run_id,finding_sig) DO UPDATE SET "
        "payload_json=excluded.payload_json,created_at=excluded.created_at",
        (run_id, sig, json.dumps(payload, default=str), time.time()))

def get_solutions(self, run_id: int, sig: str) -> dict | None:
    r = self._row("SELECT payload_json,created_at FROM finding_solutions "
                  "WHERE run_id=? AND finding_sig=?", (run_id, sig))
    if not r:
        return None
    return {"payload": json.loads(r["payload_json"]),
            "created_at": r["created_at"]}
```

- [ ] **Step 4: Run to verify pass + commit**

Run: `python3 -m pytest tests/test_store.py -q` — PASS.
```bash
git add webapp/store.py tests/test_store.py
git commit -m "feat(store): finding_solutions cache table"
```

---

### Task 5: Settings key field + solutions route

**Files:** Modify `webapp/main.py`, `webapp/templates/settings.html`; Test `tests/test_solutions_routes.py`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_solutions_routes.py
import httpx
from fastapi.testclient import TestClient
from webapp.main import create_app
from webapp.config import Config


def _app(tmp_path):
    cfg = Config(data_dir=str(tmp_path), oauth_redirect_uri="http://x/cb",
                 bind_host="127.0.0.1", bind_port=8484, secret_key=None)
    return create_app(cfg, http=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(404))))


def _seed_run(store):
    mid = store.create_migration("m", product="jira")
    rid = store.create_run(mid, {})
    store.update_run(rid, status="done", verdict="GAPS_FOUND")
    return rid


def test_solutions_requires_key(tmp_path):
    app = _app(tmp_path); rid = _seed_run(app.state.store)
    r = TestClient(app).post(f"/runs/{rid}/solutions",
                             data={"kind": "missing_in_tgt", "area": "statuses",
                                   "name": "Triage", "product": "jira"})
    assert r.status_code == 400 and "Settings" in r.json()["error"]


def test_solutions_returns_cached_then_fresh(tmp_path, monkeypatch):
    import webapp.main as m
    app = _app(tmp_path); store = app.state.store
    from webapp.anthropic_key import save_key
    save_key(store, "sk-ant-x")
    rid = _seed_run(store)
    calls = {"n": 0}

    def fake_find(finding, client, **kw):
        calls["n"] += 1
        return {"query": "q", "solutions": [{"title": "Sol"}], "error": None,
                "model": "claude-opus-4-8"}
    monkeypatch.setattr(m, "find_solutions", fake_find)
    monkeypatch.setattr(m, "anthropic_client", lambda store: object())

    data = {"kind": "missing_in_tgt", "area": "statuses", "name": "Triage",
            "product": "jira"}
    c = TestClient(app)
    r1 = c.post(f"/runs/{rid}/solutions", data=data)
    assert r1.status_code == 200 and r1.json()["solutions"][0]["title"] == "Sol"
    assert r1.json()["cached"] is False
    r2 = c.post(f"/runs/{rid}/solutions", data=data)
    assert r2.json()["cached"] is True and calls["n"] == 1  # served from cache
    r3 = c.post(f"/runs/{rid}/solutions", data={**data, "refresh": "1"})
    assert r3.json()["cached"] is False and calls["n"] == 2  # refresh re-queried
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest tests/test_solutions_routes.py -q` — FAIL (route missing).

- [ ] **Step 3: Implement** — in `webapp/main.py`, add imports near the top:

```python
from auditor.solutions import build_query, find_solutions, finding_signature
from .anthropic_key import save_key as save_anthropic_key, load_key as load_anthropic_key, anthropic_client
import time
```

Extend `settings_save` to persist the key (add a form param + call), and surface presence in `settings_page` context (`has_anthropic_key=bool(load_anthropic_key(store))`). Add the route inside `create_app` (after the analysis routes):

```python
    @app.post("/runs/{run_id}/solutions")
    def run_solutions(run_id: int, kind: str = Form(...), area: str = Form(""),
                      name: str = Form(""), project: str = Form(""),
                      src_key: str = Form(""), tgt_key: str = Form(""),
                      field: str = Form(""), product: str = Form(""),
                      deployment_from: str = Form(""), refresh: str = Form("")):
        run = store.get_run(run_id)
        if run is None:
            return JSONResponse({"error": "run not found"}, status_code=404)
        # Only safe metadata identifiers are accepted — never body/detail content.
        finding = {"kind": kind, "area": area or None, "name": name or None,
                   "project": project or None, "src_key": src_key or None,
                   "tgt_key": tgt_key or None, "field": field or None,
                   "product": product or None,
                   "deployment_from": deployment_from or None}
        sig = finding_signature(finding)
        if not refresh:
            cached = store.get_solutions(run_id, sig)
            if cached:
                p = cached["payload"]
                p["cached"] = True
                p["searched_at"] = cached["created_at"]
                return JSONResponse(p)
        client = anthropic_client(store)
        if client is None:
            return JSONResponse(
                {"error": "Add an Anthropic API key in Settings to search for "
                          "solutions."}, status_code=400)
        result = find_solutions(finding, client)
        result["searched_at"] = time.time()
        result["cached"] = False
        store.save_solutions(run_id, sig, result)
        return JSONResponse(result)
```

In `settings.html`, add an Anthropic key input to the existing form (mirror the OAuth secret field; show "key set" when `has_anthropic_key`). In `settings_save`, accept `anthropic_api_key: str = Form("")` and `if anthropic_api_key.strip(): save_anthropic_key(store, anthropic_api_key)`.

- [ ] **Step 4: Run to verify pass + commit**

Run: `python3 -m pytest tests/test_solutions_routes.py -q` — PASS.
```bash
git add webapp/main.py webapp/templates/settings.html tests/test_solutions_routes.py
git commit -m "feat(web): solutions route + Anthropic key in Settings"
```

---

### Task 6: UI — Find solutions button + results panel

**Files:** Create `webapp/static/solutions.js`; Modify `webapp/templates/analysis.html`, `webapp/static/app.js` (load + hook).
**Test:** server-render assertion in `tests/test_solutions_routes.py`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_solutions_routes.py  (append)
def test_analysis_page_loads_solutions_js(tmp_path):
    app = _app(tmp_path); rid = _seed_run(app.state.store)
    from fastapi.testclient import TestClient
    html = TestClient(app).get(f"/runs/{rid}/analysis").text
    assert "/static/solutions.js" in html
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest tests/test_solutions_routes.py -k solutions_js -q` — FAIL.

- [ ] **Step 3: Implement**

In `analysis.html`, add before `</body>` (or in the scripts block): `<script src="/static/solutions.js"></script>`.

`webapp/static/solutions.js` — delegated click handler: any element with `class="find-solutions"` carrying `data-*` finding metadata POSTs to `/runs/{runId}/solutions`, renders a results panel (solution cards: title, summary, steps list, applicability, source links target=_blank), with loading/error/no-key/empty states, a "searched <rel> · Refresh" line, and the privacy note "Only finding metadata is sent to Anthropic — never issue or page content." Use existing classes (`.card`, `.pad`, `.btn`, `.kbadge`) so it inherits the theme.

```javascript
(function () {
  function relTime(ts) {
    if (!ts) return '';
    const s = Math.floor(Date.now() / 1000 - ts);
    if (s < 60) return 'just now';
    if (s < 3600) return Math.floor(s / 60) + 'm ago';
    return Math.floor(s / 3600) + 'h ago';
  }
  function render(panel, data) {
    if (data.error) { panel.innerHTML =
      '<div class="card pad">' + escapeHtml(data.error) + '</div>'; return; }
    const sols = data.solutions || [];
    if (!sols.length) { panel.innerHTML =
      '<div class="card pad sub">No external solutions found — see the built-in '
      + 'guidance above.</div>'; return; }
    const cards = sols.map(s =>
      '<div class="card pad" style="margin-top:8px">'
      + '<b>' + escapeHtml(s.title || 'Solution') + '</b>'
      + (s.confidence ? ' <span class="kbadge">' + escapeHtml(s.confidence) + '</span>' : '')
      + '<p class="sub">' + escapeHtml(s.summary || '') + '</p>'
      + (Array.isArray(s.steps) && s.steps.length
          ? '<ol>' + s.steps.map(x => '<li>' + escapeHtml(x) + '</li>').join('') + '</ol>' : '')
      + ((s.sources || []).length
          ? '<div class="sub">Sources: ' + s.sources.map(src =>
              '<a href="' + encodeURI(src.url) + '" target="_blank" rel="noopener">'
              + escapeHtml(src.title || src.url) + '</a>').join(' · ') + '</div>' : '')
      + '</div>').join('');
    panel.innerHTML = cards
      + '<div class="sub" style="margin-top:6px">searched ' + relTime(data.searched_at)
      + ' · <a href="#" class="sol-refresh">Refresh</a> · Only finding metadata is '
      + 'sent to Anthropic — never issue or page content.</div>';
  }
  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g,
      c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  async function run(btn, refresh) {
    const runId = btn.getAttribute('data-run');
    let panel = btn.nextElementSibling;
    if (!panel || !panel.classList.contains('sol-panel')) {
      panel = document.createElement('div'); panel.className = 'sol-panel';
      btn.parentNode.insertBefore(panel, btn.nextSibling);
    }
    panel.innerHTML = '<div class="sub">Searching the web…</div>';
    const body = new URLSearchParams();
    ['kind', 'area', 'name', 'project', 'srcKey', 'tgtKey', 'field', 'product',
     'deploymentFrom'].forEach(k => {
      const v = btn.getAttribute('data-' + k.toLowerCase());
      if (v) body.set(k.replace('srcKey', 'src_key').replace('tgtKey', 'tgt_key')
        .replace('deploymentFrom', 'deployment_from'), v);
    });
    if (refresh) body.set('refresh', '1');
    try {
      const res = await fetch('/runs/' + runId + '/solutions',
        { method: 'POST', body });
      const data = await res.json();
      render(panel, data);
    } catch (e) { panel.innerHTML = '<div class="card pad">Request failed.</div>'; }
  }
  document.addEventListener('click', function (e) {
    const btn = e.target.closest('.find-solutions');
    if (btn) { e.preventDefault(); run(btn, false); return; }
    if (e.target.classList && e.target.classList.contains('sol-refresh')) {
      e.preventDefault();
      const b = e.target.closest('.sol-panel').previousElementSibling;
      if (b) run(b, true);
    }
  });
})();
```

The finding tables/cards rendered by `app.js` must emit a `<button class="find-solutions btn sm" data-run="{runId}" data-kind=... data-area=... data-name=... data-product=... data-deployment-from=...>Find solutions</button>` per finding. Add that button to the row/card templates app.js builds for issue findings and config findings (the metadata is already in scope there). Confluence/DC findings set `data-product`/`data-deployment-from` from the page's `data-product` / connection deployment.

- [ ] **Step 4: Run to verify pass + commit**

Run: `python3 -m pytest tests/test_solutions_routes.py -q` — PASS.
```bash
git add webapp/static/solutions.js webapp/static/app.js webapp/templates/analysis.html tests/test_solutions_routes.py
git commit -m "feat(ui): Find solutions button + results panel"
```

---

### Task 7: Docs + full-suite green

**Files:** Modify `README.md`; run the whole suite.

- [ ] **Step 1: README** — add a "Find solutions" section: what it does (web search per finding via the Claude API), the **metadata-only privacy boundary** (only kind/names/counts leave the machine; never issue or page content), the Anthropic key in Settings, the default pinnable model, on-demand + cached, and that it never auto-applies anything.

- [ ] **Step 2: Run the full suite**

Run: `python3 -m pytest -q` — PASS (existing + new).

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: Find solutions feature + privacy boundary"
```

---

## Self-review notes (resolved)

- **Spec coverage:** R1→T3, R2→T3, R3→T3, R4→T2 (+privacy test), R5→T1, R6→T4, R7→T5, R8→T6, R9→T1–T6 tests + back-compat (CREATE TABLE IF NOT EXISTS).
- **Privacy:** `build_query` reads only kind/name/keys/counts/product — the leak test (T2) pins that a body `head` never appears; the route accepts only metadata form fields, never detail/body.
- **No live API:** every test injects a fake client or monkeypatches `find_solutions`/`anthropic_client`. `searched_at` is stamped by the route (clock-free core) so `find_solutions` is deterministic.
- **Type consistency:** `find_solutions` returns `{query, solutions, error, searched_at, model, cached?}`; the route adds `cached`/`searched_at`; the store wraps `{payload, created_at}`; `finding_signature` is the cache key everywhere.
- **Model ID:** `claude-opus-4-8` used verbatim (complete ID, no date suffix), overridable via `MA_SOLUTIONS_MODEL`.
