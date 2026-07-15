### Task 14: `webapp/main.py` + templates + static — the web app and analysis UI

**Files:**
- Create: `webapp/main.py`, `webapp/templates/base.html`, `webapp/templates/index.html`, `webapp/templates/settings.html`, `webapp/templates/migration.html`, `webapp/templates/run.html`, `webapp/templates/analysis.html`, `webapp/templates/elevate.html`, `webapp/static/app.css`, `webapp/static/app.js`
- Test: `tests/test_main.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_main.py`:
```python
import httpx
import pytest
from fastapi.testclient import TestClient
from webapp.config import Config
from webapp.main import create_app


def mk_app(tmp_path, handler=None):
    cfg = Config(data_dir=str(tmp_path / "data"), bind_host="127.0.0.1",
                 bind_port=8484, public_base_url="http://localhost:8484",
                 secret_key=None)
    http = httpx.Client(transport=httpx.MockTransport(handler)) if handler else None
    app = create_app(cfg, http=http)
    return app, TestClient(app, follow_redirects=False)


def ok_jira(req):
    p = str(req.url.path)
    if p.endswith("/myself"):
        return httpx.Response(200, json={"displayName": "Igor",
                                         "emailAddress": "i@x.y",
                                         "accountId": "acc-1"})
    if p.endswith("project/search"):
        return httpx.Response(200, json={"isLast": True, "values": [
            {"key": "AC", "name": "AC Support", "id": "1",
             "insight": {"totalIssueCount": 5}}]})
    if p.endswith("approximate-count"):
        return httpx.Response(200, json={"count": 5})
    if "/role" in p and req.method == "POST":
        return httpx.Response(200, json={})
    if "/role" in p and req.method == "DELETE":
        return httpx.Response(204)
    if p.endswith("/rest/api/3/role"):
        return httpx.Response(200, json=[{"name": "Administrators", "id": 9}])
    return httpx.Response(200, json={"values": [], "isLast": True})


def test_dashboard_renders(tmp_path):
    _, c = mk_app(tmp_path)
    r = c.get("/")
    assert r.status_code == 200 and "Migration Auditor" in r.text


def test_create_migration_and_page(tmp_path):
    app, c = mk_app(tmp_path)
    r = c.post("/migrations", data={"name": "acme->globex"})
    assert r.status_code == 303
    loc = r.headers["location"]
    page = c.get(loc)
    assert page.status_code == 200
    assert ("acme-&gt;globex" in page.text) or ("acme->globex" in page.text)


def test_pat_connection_save_and_verify(tmp_path):
    app, c = mk_app(tmp_path, handler=ok_jira)
    c.post("/migrations", data={"name": "m"})
    r = c.post("/migrations/1/connections",
               data={"role": "source", "site_url": "https://s.atlassian.net",
                     "email": "i@x.y", "api_token": "tok"})
    assert r.status_code == 303
    page = c.get("/migrations/1")
    assert "verified" in page.text
    row = app.state.store.get_connection(1, "source")
    assert row["status"] == "verified"
    assert b"tok" not in row["secret_enc"]


def test_pat_connection_bad_auth_shows_error(tmp_path):
    def deny(req):
        return httpx.Response(401, text="no")
    app, c = mk_app(tmp_path, handler=deny)
    c.post("/migrations", data={"name": "m"})
    r = c.post("/migrations/1/connections",
               data={"role": "source", "site_url": "https://s.atlassian.net",
                     "email": "i@x.y", "api_token": "bad"},
               follow_redirects=True)
    assert "could not authenticate" in r.text.lower()
    assert app.state.store.get_connection(1, "source") is None


def test_settings_roundtrip_encrypts_secret(tmp_path):
    app, c = mk_app(tmp_path)
    r = c.post("/settings", data={"oauth_client_id": "cid",
                                  "oauth_client_secret": "sek"})
    assert r.status_code == 303
    store = app.state.store
    assert store.settings_get("oauth_client_id") == "cid"
    enc = store.settings_get("oauth_client_secret_enc")
    assert "sek" not in enc
    assert store.decrypt(enc.encode())["secret"] == "sek"


def test_run_start_requires_both_connections(tmp_path):
    app, c = mk_app(tmp_path, handler=ok_jira)
    c.post("/migrations", data={"name": "m"})
    r = c.post("/migrations/1/runs", data={}, follow_redirects=True)
    assert "configure both connections" in r.text.lower()


def test_sse_stream_for_finished_run(tmp_path):
    app, c = mk_app(tmp_path)
    store = app.state.store
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})
    store.add_event(rid, "verify", "info", "hello-evt")
    store.update_run(rid, status="done")
    r = c.get(f"/runs/{rid}/stream")
    assert r.status_code == 200
    assert "hello-evt" in r.text and "event: done" in r.text


def test_run_and_analysis_pages_render(tmp_path):
    app, c = mk_app(tmp_path)
    store = app.state.store
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})
    store.update_run(rid, status="done", verdict="CLEAN",
                     stats={"headlines": [], "project_stats": {}, "areas": {}})
    assert c.get(f"/runs/{rid}").status_code == 200
    for view in ("", "/projects", "/config", "/issues", "/log"):
        resp = c.get(f"/runs/{rid}/analysis{view}")
        assert resp.status_code == 200, view
    assert c.get(f"/runs/{rid}/analysis/projects/AC").status_code == 200


def test_elevation_apply_and_undo(tmp_path):
    app, c = mk_app(tmp_path, handler=ok_jira)
    store = app.state.store
    c.post("/migrations", data={"name": "m"})
    c.post("/migrations/1/connections",
           data={"role": "source", "site_url": "https://s.atlassian.net",
                 "email": "i@x.y", "api_token": "t"})
    c.post("/migrations/1/connections",
           data={"role": "target", "site_url": "https://t.atlassian.net",
                 "email": "i@x.y", "api_token": "t"})
    rid = store.create_run(1, {})
    store.update_run(rid, status="done")
    store.set_run_projects(rid, [{"key": "AC", "name": "AC", "src_count": 5,
                                  "tgt_count": 0, "missing": None,
                                  "tail_count": None, "fidelity_pct": None,
                                  "blind_spot": 1, "status": "scoped"}])
    page = c.get(f"/runs/{rid}/elevate")
    assert page.status_code == 200 and "AC" in page.text
    r = c.post(f"/runs/{rid}/elevate", data={"side": "target"})
    assert r.status_code == 303
    log = store.settings_get(f"elevation:{rid}:target")
    assert log and '"ok": true' in log
    r2 = c.post(f"/runs/{rid}/elevate/undo", data={"side": "target"})
    assert r2.status_code == 303
    assert store.settings_get(f"elevation:{rid}:target") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_main.py -q`
Expected: `ModuleNotFoundError: No module named 'webapp.main'`.

- [ ] **Step 3: Write `webapp/main.py`**

```python
"""FastAPI app: wizard, runs, SSE, analysis pages, settings, elevation."""
from __future__ import annotations

import json
import os
import secrets
import time

import httpx
import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import (HTMLResponse, RedirectResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from auditor.client import ClientError, Connection, JiraClient
from auditor.permissions import (apply_elevation, find_admin_role_id,
                                 undo_elevation)
from .analysis import make_router
from .config import Config, load_config
from .oauth import accessible_resources, build_authorize_url, exchange_code
from .runs import RunEngine
from .stages import build_clients, build_stages
from .store import Store

_HERE = os.path.dirname(__file__)


def create_app(cfg: Config | None = None, http: httpx.Client | None = None) -> FastAPI:
    cfg = cfg or load_config()
    os.makedirs(cfg.data_dir, exist_ok=True)
    store = Store(db_path=cfg.db_path, key_path=cfg.key_path,
                  secret_key=cfg.secret_key)
    engine = RunEngine(store, os.path.join(cfg.data_dir, "migrations"),
                       stages=build_stages())
    engine.mark_stale_failed()

    app = FastAPI(title="Migration Auditor")
    app.state.store = store
    app.state.engine = engine
    app.state.config = cfg
    app.state.http = http            # injected mock in tests; None in prod
    app.state.oauth_pending = {}     # state -> {migration_id, role, tokens?}
    templates = Jinja2Templates(directory=os.path.join(_HERE, "templates"))
    app.mount("/static", StaticFiles(directory=os.path.join(_HERE, "static")),
              name="static")
    app.include_router(make_router())

    def page(request, name, **ctx):
        ctx.update({"request": request})
        return templates.TemplateResponse(name, ctx)

    def _mk_client(conn: Connection) -> JiraClient:
        return JiraClient(conn, http=app.state.http)

    # ------------------------------------------------------------ dashboard
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        migs = store.list_migrations()
        for m in migs:
            runs = store.list_runs(m["id"])
            m["last_run"] = runs[0] if runs else None
        return page(request, "index.html", migrations=migs)

    @app.post("/migrations")
    def create_migration(name: str = Form(...)):
        mid = store.create_migration(name.strip() or "untitled")
        return RedirectResponse(f"/migrations/{mid}", status_code=303)

    # ------------------------------------------------------------- settings
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, saved: int = 0):
        return page(request, "settings.html",
                    client_id=store.settings_get("oauth_client_id") or "",
                    has_secret=bool(store.settings_get("oauth_client_secret_enc")),
                    redirect_uri=cfg.oauth_redirect_uri, saved=saved)

    @app.post("/settings")
    def settings_save(oauth_client_id: str = Form(""),
                      oauth_client_secret: str = Form("")):
        store.settings_set("oauth_client_id", oauth_client_id.strip())
        if oauth_client_secret.strip():
            store.settings_set(
                "oauth_client_secret_enc",
                store.encrypt({"secret": oauth_client_secret.strip()}).decode())
        return RedirectResponse("/settings?saved=1", status_code=303)

    # ------------------------------------------------------- migration page
    @app.get("/migrations/{mid}", response_class=HTMLResponse)
    def migration_page(request: Request, mid: int, error: str = ""):
        mig = store.get_migration(mid)
        if mig is None:
            return RedirectResponse("/", status_code=303)
        conns = {role: store.get_connection(mid, role)
                 for role in ("source", "target")}
        return page(request, "migration.html", mig=mig, conns=conns,
                    runs=store.list_runs(mid), error=error,
                    oauth_ready=bool(store.settings_get("oauth_client_id")),
                    active=store.active_run(mid))

    @app.post("/migrations/{mid}/connections")
    def save_pat_connection(mid: int, role: str = Form(...),
                            site_url: str = Form(...), email: str = Form(...),
                            api_token: str = Form(...)):
        site = site_url.strip().rstrip("/")
        if not site.startswith("http"):
            site = "https://" + site
        conn = Connection(auth_type="pat", site_url=site,
                          email=email.strip(), api_token=api_token.strip())
        try:
            me = _mk_client(conn).myself()
        except ClientError as exc:
            return RedirectResponse(
                f"/migrations/{mid}?error=Could not authenticate {role}: "
                f"HTTP {exc.status}", status_code=303)
        store.save_connection(mid, role, "pat", site,
                              secret={"email": email.strip(),
                                      "token": api_token.strip()},
                              account_email=me.get("emailAddress"))
        row = store.get_connection(mid, role)
        store.mark_connection_verified(row["id"], me.get("emailAddress") or "")
        return RedirectResponse(f"/migrations/{mid}", status_code=303)

    # ----------------------------------------------------------- oauth flow
    @app.get("/oauth/start")
    def oauth_start(migration_id: int, role: str):
        client_id = store.settings_get("oauth_client_id")
        if not client_id:
            return RedirectResponse(
                f"/migrations/{migration_id}?error=Configure the OAuth client "
                f"in Settings first", status_code=303)
        state = secrets.token_urlsafe(24)
        app.state.oauth_pending[state] = {"migration_id": migration_id,
                                          "role": role}
        return RedirectResponse(build_authorize_url(
            client_id, cfg.oauth_redirect_uri, state), status_code=303)

    @app.get("/oauth/callback", response_class=HTMLResponse)
    def oauth_callback(request: Request, state: str = "", code: str = "",
                       error: str = ""):
        pend = app.state.oauth_pending.pop(state, None)
        if pend is None or error or not code:
            return page(request, "index.html", migrations=store.list_migrations(),
                        flash=f"OAuth failed: {error or 'invalid state'}")
        client_id = store.settings_get("oauth_client_id")
        enc = store.settings_get("oauth_client_secret_enc")
        secret = store.decrypt(enc.encode())["secret"] if enc else ""
        tokens = exchange_code(client_id, secret, code, cfg.oauth_redirect_uri,
                               http=app.state.http)
        sites = accessible_resources(tokens["access_token"], http=app.state.http)
        pend["tokens"] = tokens
        new_state = secrets.token_urlsafe(24)
        app.state.oauth_pending[new_state] = pend
        return page(request, "migration.html",
                    mig=store.get_migration(pend["migration_id"]),
                    conns={r: store.get_connection(pend["migration_id"], r)
                           for r in ("source", "target")},
                    runs=store.list_runs(pend["migration_id"]),
                    error="", oauth_ready=True,
                    active=store.active_run(pend["migration_id"]),
                    site_pick={"state": new_state, "sites": sites,
                               "role": pend["role"]})

    @app.post("/oauth/select")
    def oauth_select(state: str = Form(...), cloud_id: str = Form(...),
                     site_url: str = Form(...)):
        pend = app.state.oauth_pending.pop(state, None)
        if pend is None:
            return RedirectResponse("/", status_code=303)
        t = pend["tokens"]
        store.save_connection(
            pend["migration_id"], pend["role"], "oauth", site_url,
            cloud_id=cloud_id,
            secret={"access_token": t["access_token"],
                    "refresh_token": t.get("refresh_token"),
                    "expires_at": time.time() + float(t.get("expires_in", 3600))})
        return RedirectResponse(f"/migrations/{pend['migration_id']}",
                                status_code=303)

    # ----------------------------------------------------------------- runs
    @app.post("/migrations/{mid}/runs")
    def start_run(mid: int, projects: str = Form(""),
                  reuse_extracts_from: str = Form("")):
        if not (store.get_connection(mid, "source")
                and store.get_connection(mid, "target")):
            return RedirectResponse(
                f"/migrations/{mid}?error=Configure both connections first",
                status_code=303)
        params = {}
        keys = [k.strip() for k in projects.split(",") if k.strip()]
        if keys:
            params["projects"] = keys
        if reuse_extracts_from.strip().isdigit():
            params["reuse_extracts_from"] = int(reuse_extracts_from)
        try:
            rid = engine.start(mid, params)
        except RuntimeError as exc:
            return RedirectResponse(f"/migrations/{mid}?error={exc}",
                                    status_code=303)
        return RedirectResponse(f"/runs/{rid}", status_code=303)

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_page(request: Request, run_id: int):
        run = store.get_run(run_id)
        if run is None:
            return RedirectResponse("/", status_code=303)
        return page(request, "run.html", run=run,
                    mig=store.get_migration(run["migration_id"]))

    @app.post("/runs/{run_id}/cancel")
    def cancel_run(run_id: int):
        engine.cancel(run_id)
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.get("/runs/{run_id}/stream")
    def run_stream(run_id: int):
        def gen():
            last = 0
            while True:
                for e in store.get_events(run_id, after_id=last):
                    last = e["id"]
                    yield f"data: {json.dumps(e)}\n\n"
                run = store.get_run(run_id)
                if run is None or run["status"] != "running":
                    yield "event: done\ndata: {}\n\n"
                    return
                time.sleep(1.0)
        return StreamingResponse(gen(), media_type="text/event-stream")

    # ------------------------------------------------------------- analysis
    @app.get("/runs/{run_id}/analysis", response_class=HTMLResponse)
    @app.get("/runs/{run_id}/analysis/{view}", response_class=HTMLResponse)
    @app.get("/runs/{run_id}/analysis/projects/{project}",
             response_class=HTMLResponse)
    def analysis_page(request: Request, run_id: int, view: str = "overview",
                      project: str = ""):
        run = store.get_run(run_id)
        if run is None:
            return RedirectResponse("/", status_code=303)
        if project:
            view = "project"
        src = store.get_connection(run["migration_id"], "source") or {}
        tgt = store.get_connection(run["migration_id"], "target") or {}
        return page(request, "analysis.html", run=run, view=view,
                    project=project,
                    mig=store.get_migration(run["migration_id"]),
                    src_base=(src.get("site_url") or "").rstrip("/"),
                    tgt_base=(tgt.get("site_url") or "").rstrip("/"))

    # ------------------------------------------------------------ elevation
    @app.get("/runs/{run_id}/elevate", response_class=HTMLResponse)
    def elevate_confirm(request: Request, run_id: int):
        run = store.get_run(run_id)
        rows = [r for r in store.get_run_projects(run_id) if r["blind_spot"]]
        return page(request, "elevate.html", run=run, rows=rows,
                    undo_src=store.settings_get(f"elevation:{run_id}:source"),
                    undo_tgt=store.settings_get(f"elevation:{run_id}:target"))

    def _side_client(run, side):
        src, tgt = build_clients(store, run["migration_id"],
                                 http=app.state.http)
        return src if side == "source" else tgt

    @app.post("/runs/{run_id}/elevate")
    def elevate_apply(run_id: int, side: str = Form(...)):
        run = store.get_run(run_id)
        cl = _side_client(run, side)
        me = cl.myself()
        role_id = find_admin_role_id(cl)
        blind = {r["key"] for r in store.get_run_projects(run_id)
                 if r["blind_spot"]}
        projects, _ = cl.all_projects()
        ids = [p["id"] for p in projects if p.get("key") in blind]
        grants = apply_elevation(cl, ids, role_id, me["accountId"])
        store.settings_set(f"elevation:{run_id}:{side}", json.dumps(
            {"role_id": role_id, "account_id": me["accountId"],
             "grants": grants}))
        store.add_event(run_id, "permissions", "warn",
                        f"elevation applied on {side}: "
                        f"{sum(1 for g in grants if g['ok'])}/{len(grants)} "
                        f"projects (undo available)")
        return RedirectResponse(f"/runs/{run_id}/elevate", status_code=303)

    @app.post("/runs/{run_id}/elevate/undo")
    def elevate_undo(run_id: int, side: str = Form(...)):
        run = store.get_run(run_id)
        raw = store.settings_get(f"elevation:{run_id}:{side}")
        if raw:
            data = json.loads(raw)
            cl = _side_client(run, side)
            undo_elevation(cl, data["grants"], data["role_id"],
                           data["account_id"])
            store.settings_delete(f"elevation:{run_id}:{side}")
            store.add_event(run_id, "permissions", "info",
                            f"elevation undone on {side}")
        return RedirectResponse(f"/runs/{run_id}/elevate", status_code=303)

    return app


def cli():
    import argparse
    ap = argparse.ArgumentParser(prog="migration-auditor")
    ap.add_argument("command", choices=["serve"])
    ap.parse_args()
    cfg = load_config()
    uvicorn.run(create_app(cfg), host=cfg.bind_host, port=cfg.bind_port)
```

- [ ] **Step 4: Write the templates**

`webapp/templates/base.html`:
```html
<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Migration Auditor</title>
<link rel="stylesheet" href="/static/app.css">
</head><body>
<header class="masthead"><div class="wrap">
  <a class="brand" href="/">Migration Auditor</a>
  <nav><a href="/">Migrations</a><a href="/settings">Settings</a></nav>
</div></header>
<main class="wrap">
{% block content %}{% endblock %}
</main>
<script src="/static/app.js"></script>
{% block scripts %}{% endblock %}
</body></html>
```

`webapp/templates/index.html`:
```html
{% extends "base.html" %}
{% block content %}
{% if flash %}<div class="flash">{{ flash }}</div>{% endif %}
<h1>Migrations</h1>
<form method="post" action="/migrations" class="row">
  <input name="name" placeholder="e.g. acme -> globex UAT" required>
  <button>New migration</button>
</form>
<table class="data">
  <thead><tr><th>Name</th><th>Last run</th><th>Verdict</th></tr></thead>
  <tbody>
  {% for m in migrations %}
    <tr>
      <td><a href="/migrations/{{ m.id }}">{{ m.name }}</a></td>
      <td>{% if m.last_run %}<a href="/runs/{{ m.last_run.id }}">run #{{ m.last_run.id }}</a>
          · {{ m.last_run.status }}{% else %}—{% endif %}</td>
      <td>{% if m.last_run and m.last_run.verdict %}
          <span class="verdict v-{{ m.last_run.verdict }}">{{ m.last_run.verdict }}</span>
          {% endif %}</td>
    </tr>
  {% else %}<tr><td colspan="3">No migrations yet.</td></tr>{% endfor %}
  </tbody>
</table>
{% endblock %}
```

`webapp/templates/settings.html`:
```html
{% extends "base.html" %}
{% block content %}
<h1>Settings</h1>
{% if saved %}<div class="flash ok">Saved.</div>{% endif %}
<p class="lead">Atlassian OAuth app (optional — PAT connections work without it).
Callback URL to register: <code>{{ redirect_uri }}</code></p>
<form method="post" action="/settings" class="stack">
  <label>Client ID <input name="oauth_client_id" value="{{ client_id }}"></label>
  <label>Client secret
    <input name="oauth_client_secret" type="password"
           placeholder="{{ '•••••• (saved)' if has_secret else '' }}"></label>
  <button>Save</button>
</form>
{% endblock %}
```

`webapp/templates/migration.html`:
```html
{% extends "base.html" %}
{% block content %}
<h1>{{ mig.name }}</h1>
{% if error %}<div class="flash err">{{ error }}</div>{% endif %}
<div class="grid2">
{% for role in ["source", "target"] %}
  <section class="panel">
    <h3>{{ role|capitalize }}</h3>
    {% set c = conns[role] %}
    {% if c %}
      <p><code>{{ c.site_url }}</code><br>
      {{ c.auth_type|upper }} · {{ c.account_email or "" }} ·
      <span class="chip {{ 'ok' if c.status == 'verified' else '' }}">{{ c.status }}</span></p>
    {% endif %}
    {% if oauth_ready %}
      <a class="btn" href="/oauth/start?migration_id={{ mig.id }}&role={{ role }}">
        Connect with Atlassian</a>
      <p class="muted">…or enter a PAT:</p>
    {% endif %}
    <form method="post" action="/migrations/{{ mig.id }}/connections" class="stack">
      <input type="hidden" name="role" value="{{ role }}">
      <input name="site_url" placeholder="https://site.atlassian.net" required>
      <input name="email" placeholder="email" required>
      <input name="api_token" type="password" placeholder="API token" required>
      <button>Save {{ role }} (PAT)</button>
    </form>
  </section>
{% endfor %}
</div>
{% if site_pick %}
  <section class="panel">
    <h3>Pick the {{ site_pick.role }} site</h3>
    {% for s in site_pick.sites %}
    <form method="post" action="/oauth/select" class="row">
      <input type="hidden" name="state" value="{{ site_pick.state }}">
      <input type="hidden" name="cloud_id" value="{{ s.id }}">
      <input type="hidden" name="site_url" value="{{ s.url }}">
      <button>{{ s.name }} — {{ s.url }}</button>
    </form>
    {% endfor %}
  </section>
{% endif %}
<section class="panel">
  <h3>Run an audit</h3>
  {% if active %}
    <p>Run <a href="/runs/{{ active.id }}">#{{ active.id }}</a> is in progress.</p>
  {% else %}
  <form method="post" action="/migrations/{{ mig.id }}/runs" class="row">
    <input name="projects" placeholder="project keys, comma-separated (empty = all matched)">
    <button>Start audit</button>
  </form>
  {% endif %}
</section>
<section class="panel">
  <h3>Runs</h3>
  <table class="data"><thead>
    <tr><th>#</th><th>Status</th><th>Phase</th><th>Verdict</th><th></th></tr></thead>
  <tbody>
  {% for r in runs %}
    <tr><td><a href="/runs/{{ r.id }}">{{ r.id }}</a></td>
        <td>{{ r.status }}</td><td>{{ r.phase }}</td>
        <td>{% if r.verdict %}<span class="verdict v-{{ r.verdict }}">{{ r.verdict }}</span>{% endif %}</td>
        <td>{% if r.status == 'done' %}<a href="/runs/{{ r.id }}/analysis">analysis →</a>{% endif %}</td></tr>
  {% else %}<tr><td colspan="5">No runs yet.</td></tr>{% endfor %}
  </tbody></table>
</section>
{% endblock %}
```

`webapp/templates/run.html`:
```html
{% extends "base.html" %}
{% block content %}
<h1>Run #{{ run.id }} — {{ mig.name }}</h1>
<p>Status: <b id="status">{{ run.status }}</b> · Phase: <b id="phase">{{ run.phase }}</b>
 {% if run.status == 'running' %}
 <form method="post" action="/runs/{{ run.id }}/cancel" style="display:inline">
   <button class="danger">Cancel</button></form>{% endif %}
 {% if run.status == 'done' %}
   <a class="btn" href="/runs/{{ run.id }}/analysis">Open analysis →</a>{% endif %}
 <a class="btn" href="/runs/{{ run.id }}/elevate">Permissions / elevation</a>
 {% if run.status != 'running' %}
 <form method="post" action="/migrations/{{ mig.id }}/runs" style="display:inline">
   <input type="hidden" name="reuse_extracts_from" value="{{ run.id }}">
   <button>Re-run (reuse extracts)</button></form>{% endif %}</p>
<pre id="log" class="log"></pre>
{% endblock %}
{% block scripts %}
<script>
const log = document.getElementById("log");
const es = new EventSource("/runs/{{ run.id }}/stream");
es.onmessage = (e) => {
  const ev = JSON.parse(e.data);
  log.textContent += `[${ev.phase}] ${ev.message}\n`;
  log.scrollTop = log.scrollHeight;
  document.getElementById("phase").textContent = ev.phase;
};
es.addEventListener("done", () => { es.close(); location.reload(); });
</script>
{% endblock %}
```

`webapp/templates/elevate.html`:
```html
{% extends "base.html" %}
{% block content %}
<h1>Permission elevation — run #{{ run.id }}</h1>
<p class="lead">Blind-spotted projects (search sees fewer issues than exist).
Granting adds YOUR account to each project's Administrators role on that side.
It is recorded and undoable. Re-run the audit afterwards.</p>
<table class="data"><thead><tr><th>Project</th><th>src count</th><th>tgt count</th></tr></thead>
<tbody>
{% for r in rows %}<tr><td>{{ r.key }}</td><td>{{ r.src_count }}</td><td>{{ r.tgt_count }}</td></tr>
{% else %}<tr><td colspan="3">No blind spots recorded on this run.</td></tr>{% endfor %}
</tbody></table>
<div class="row">
{% for side in ["source", "target"] %}
  <form method="post" action="/runs/{{ run.id }}/elevate">
    <input type="hidden" name="side" value="{{ side }}">
    <button class="danger">Grant on {{ side }}</button>
  </form>
  <form method="post" action="/runs/{{ run.id }}/elevate/undo">
    <input type="hidden" name="side" value="{{ side }}">
    <button {% if (side == 'source' and not undo_src) or (side == 'target' and not undo_tgt) %}disabled{% endif %}>
      Undo {{ side }}</button>
  </form>
{% endfor %}
</div>
{% endblock %}
```

`webapp/templates/analysis.html`:
```html
{% extends "base.html" %}
{% block content %}
<div class="anav">
  <span class="kicker">{{ mig.name }} · run #{{ run.id }}</span>
  <nav>
    {% for v, label in [("overview","Overview"),("projects","Projects"),
                        ("config","Config parity"),("issues","All findings"),
                        ("log","Run log")] %}
      <a href="/runs/{{ run.id }}/analysis{{ '' if v == 'overview' else '/' + v }}"
         class="{{ 'active' if view == v }}">{{ label }}</a>
    {% endfor %}
  </nav>
</div>
<div id="app"
     data-run="{{ run.id }}" data-view="{{ view }}" data-project="{{ project }}"
     data-src-base="{{ src_base }}" data-tgt-base="{{ tgt_base }}"></div>
{% endblock %}
{% block scripts %}<script>Analysis.boot();</script>{% endblock %}
```

- [ ] **Step 5: Write the static assets**

`webapp/static/app.css` (broadsheet design system, condensed):
```css
:root{--bg:#FBF0E4;--paper:#FFFAF3;--panel:#fff;--ink:#241C16;--ink-soft:#6E5F52;
--ink-faint:#9A8B7C;--rule:#E7D7C5;--accent:#0F5257;--crit:#9C1A3D;--warn:#C0631B;
--ochre:#B68410;--ok:#3C7A4E;--eng:#2C4A7C}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 -apple-system,
"Segoe UI",sans-serif}
.wrap{max-width:1240px;margin:0 auto;padding:0 24px}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
h1{font-family:Georgia,serif;font-size:30px;margin:.6em 0 .4em}
h3{font-family:Georgia,serif;margin:.2em 0 .6em}
.masthead{border-top:6px solid var(--ink);background:var(--paper);
border-bottom:1px solid var(--rule);margin-bottom:18px}
.masthead .wrap{display:flex;align-items:center;justify-content:space-between;
padding:14px 24px}
.brand{font-family:Georgia,serif;font-weight:700;font-size:20px;color:var(--ink)}
.masthead nav a{margin-left:16px;font-size:13px;text-transform:uppercase;
letter-spacing:.06em;color:var(--ink-soft)}
.panel{background:var(--panel);border:1px solid var(--rule);border-radius:3px;
padding:16px 18px;margin:12px 0}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:860px){.grid2{grid-template-columns:1fr}}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:10px 0}
.stack{display:flex;flex-direction:column;gap:8px;max-width:420px}
input,select{padding:8px 10px;border:1px solid var(--rule);border-radius:3px;
background:#fff;font:inherit}
button,.btn{display:inline-block;background:var(--accent);color:#fff;border:0;
padding:8px 14px;border-radius:3px;font:inherit;cursor:pointer}
button.danger{background:var(--crit)}
button[disabled]{background:var(--ink-faint);cursor:not-allowed}
table.data{border-collapse:collapse;width:100%;font-size:13.5px;background:#fff;
border:1px solid var(--rule)}
table.data th{text-align:left;font-size:11px;text-transform:uppercase;
letter-spacing:.05em;color:var(--ink-soft);padding:9px 10px;
border-bottom:2px solid var(--rule);background:var(--paper)}
table.data td{padding:8px 10px;border-bottom:1px solid var(--rule)}
.flash{background:#fff;border-left:4px solid var(--accent);padding:10px 14px;
margin:10px 0}.flash.err{border-color:var(--crit)}.flash.ok{border-color:var(--ok)}
.chip{display:inline-block;font-size:11.5px;font-weight:600;padding:1px 9px;
border-radius:20px;background:#eee;color:var(--ink-soft)}
.chip.ok{background:#e3efe6;color:var(--ok)}
.verdict{font-weight:700;padding:2px 10px;border-radius:3px;font-size:12px}
.v-CLEAN{background:#e3efe6;color:var(--ok)}
.v-CLEAN_WITH_TAILS{background:#f5ecd6;color:var(--ochre)}
.v-GAPS_FOUND{background:#f7e3d2;color:var(--warn)}
.v-CRITICAL{background:#f6dde4;color:var(--crit)}
.log{background:var(--ink);color:#F3E9DA;padding:14px;border-radius:3px;
min-height:200px;max-height:480px;overflow:auto;font:12.5px/1.5 monospace}
.muted{color:var(--ink-faint)}.lead{color:var(--ink-soft);max-width:72ch}
.kicker{font-size:11px;letter-spacing:.25em;text-transform:uppercase;
color:var(--crit);font-weight:700}
.anav{display:flex;justify-content:space-between;align-items:baseline;
border-bottom:1px solid var(--rule);padding-bottom:8px;margin-bottom:14px}
.anav nav a{margin-left:14px;font-size:12.5px;text-transform:uppercase;
letter-spacing:.04em;color:var(--ink-soft);padding-bottom:8px}
.anav nav a.active{color:var(--crit);border-bottom:2px solid var(--crit)}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:14px 0}
@media(max-width:900px){.kpis{grid-template-columns:repeat(2,1fr)}}
.kpi{background:#fff;border:1px solid var(--rule);border-left:4px solid var(--accent);
border-radius:3px;padding:14px}
.kpi.crit{border-left-color:var(--crit)}.kpi.warn{border-left-color:var(--warn)}
.kpi.ok{border-left-color:var(--ok)}
.kpi .num{font:600 32px/1 monospace}.kpi .lbl{font-size:12px;font-weight:600;
text-transform:uppercase;margin-top:6px}.kpi .sub{font-size:11.5px;
color:var(--ink-faint);font-family:monospace}
.finding{background:var(--paper);border:1px solid var(--rule);
border-left:4px solid var(--crit);padding:10px 14px;margin:8px 0;font-size:14px}
.bar{display:grid;grid-template-columns:160px 1fr 44px;gap:8px;align-items:center;
margin:5px 0;font-size:13px}
.bar .track{background:#f0e5d6;height:14px;border-radius:2px;overflow:hidden}
.bar .fill{height:100%;background:var(--accent)}
.bar .val{font-family:monospace;text-align:right}
.pager{display:flex;gap:8px;align-items:center;margin:10px 0}
.kbadge{font-family:monospace;font-size:11px;padding:1px 7px;border-radius:3px;
background:#eee}
.k-missing_in_tgt{background:#f6dde4;color:var(--crit)}
.k-missing_in_src{background:#f6dde4;color:var(--crit)}
.k-key_collision{background:#f6dde4;color:var(--crit)}
.k-tail_post_cutover{background:#f5ecd6;color:var(--ochre)}
.k-field_mismatch{background:#f7e3d2;color:var(--warn)}
.k-content_mismatch{background:#f7e3d2;color:var(--warn)}
.k-comment_mismatch{background:#f7e3d2;color:var(--warn)}
.k-attachment_mismatch{background:#f7e3d2;color:var(--warn)}
.k-link_mismatch{background:#eee;color:var(--ink-soft)}
```

`webapp/static/app.js` (analysis renderer, vanilla):
```javascript
const Analysis = (() => {
  let R, V, P, SRC, TGT;
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
    (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  const get = async (p) => (await fetch(p)).json();
  const issueLink = (key, base) =>
    key && base ? `<a href="${base}/browse/${key}" target="_blank">${esc(key)}</a>`
                : esc(key ?? "—");
  const kb = (k) => `<span class="kbadge k-${esc(k)}">${esc(k)}</span>`;

  function kpi(num, lbl, sub, tone = "") {
    return `<div class="kpi ${tone}"><div class="num">${num}</div>
      <div class="lbl">${lbl}</div><div class="sub">${sub || ""}</div></div>`;
  }
  function bars(pairs) {
    const mx = Math.max(...pairs.map(([, v]) => v), 1);
    return pairs.map(([k, v]) => `<div class="bar"><div>${esc(k)}</div>
      <div class="track"><div class="fill" style="width:${100 * v / mx}%"></div></div>
      <div class="val">${v}</div></div>`).join("");
  }
  function issuesTable(d) {
    const rows = d.rows.map((r) => `<tr>
      <td>${kb(r.kind)}</td><td>${issueLink(r.src_key, SRC)}</td>
      <td>${issueLink(r.tgt_key, TGT)}</td><td>${esc(r.project)}</td>
      <td>${esc(r.field ?? "")}</td><td>${esc(r.summary ?? "")}</td>
      <td class="muted">${esc(JSON.stringify(r.detail)).slice(0, 160)}</td></tr>`);
    return `<table class="data"><thead><tr><th>Kind</th><th>Source</th>
      <th>Target</th><th>Project</th><th>Field</th><th>Summary</th><th>Detail</th>
      </tr></thead><tbody>${rows.join("") ||
      '<tr><td colspan="7">No findings.</td></tr>'}</tbody></table>`;
  }
  async function issuesView(el, fixed = {}) {
    let page = 1;
    const render = async () => {
      const q = new URLSearchParams({ page, size: 50, ...fixed });
      const sel = $("#f-kind"), txt = $("#f-q");
      if (sel && sel.value) q.set("kind", sel.value);
      if (txt && txt.value) q.set("q", txt.value);
      const d = await get(`/api/runs/${R}/issues?${q}`);
      $("#tbl").innerHTML = issuesTable(d);
      $("#count").textContent =
        `${d.total} finding(s) · page ${page}/${Math.max(1, Math.ceil(d.total / 50))}`;
    };
    const kinds = await get(`/api/runs/${R}/issues/kinds` +
      (fixed.project ? `?project=${fixed.project}` : ""));
    el.innerHTML = `<div class="row">
      <select id="f-kind"><option value="">All kinds</option>
        ${Object.entries(kinds).map(([k, n]) =>
          `<option value="${k}">${k} (${n})</option>`).join("")}</select>
      <input id="f-q" placeholder="search key / summary / field">
      <span id="count" class="muted"></span>
      <span class="pager"><button id="prev">‹</button><button id="next">›</button></span>
      </div><div id="tbl"></div>`;
    $("#f-kind").onchange = () => { page = 1; render(); };
    $("#f-q").oninput = () => { page = 1; render(); };
    $("#prev").onclick = () => { page = Math.max(1, page - 1); render(); };
    $("#next").onclick = () => { page += 1; render(); };
    render();
  }

  const views = {
    async overview(el) {
      const s = await get(`/api/runs/${R}/summary`);
      const st = s.stats;
      el.innerHTML = `
        <p><span class="verdict v-${esc(s.verdict)}">${esc(s.verdict)}</span></p>
        <div class="kpis">
          ${kpi(st.issues_src_total ?? 0, "Source issues", `${st.projects} project(s)`)}
          ${kpi(st.issues_tgt_total ?? 0, "Target issues", "")}
          ${kpi(st.holes ?? 0, "Genuine holes", "missing below cutover", st.holes ? "crit" : "ok")}
          ${kpi(st.tails ?? 0, "Post-cutover tails", "expected drift", "warn")}
          ${kpi(st.issues_with_mismatches ?? 0, "Issues w/ mismatches", "", st.issues_with_mismatches ? "warn" : "ok")}
          ${kpi(st.collisions ?? 0, "Key collisions", "", st.collisions ? "crit" : "ok")}
          ${kpi((st.config_missing ?? 0) + (st.config_other ?? 0), "Config gaps", "")}
          ${kpi(st.blind_spots ?? 0, "Blind spots", "permissions", st.blind_spots ? "crit" : "ok")}
        </div>
        ${s.headlines.map((h) => `<div class="finding">${esc(h)}</div>`).join("")}
        <div class="panel"><h3>Fidelity by project</h3>
        ${bars(Object.entries(s.project_stats).map(([k, v]) =>
          [k + " (fidelity %)", Math.round(v.fidelity_pct ?? 100)]))}</div>`;
    },
    async projects(el) {
      const rows = await get(`/api/runs/${R}/projects`);
      el.innerHTML = `<table class="data"><thead><tr><th>Project</th>
        <th>Src</th><th>Tgt</th><th>Holes</th><th>Tails</th><th>Fidelity</th>
        <th>Blind spot</th></tr></thead><tbody>` + rows.map((r) => `<tr>
        <td><a href="/runs/${R}/analysis/projects/${esc(r.key)}">${esc(r.key)}</a>
            ${esc(r.name ?? "")}</td>
        <td>${r.src_count ?? "?"}</td><td>${r.tgt_count ?? "?"}</td>
        <td>${r.missing ?? "—"}</td><td>${r.tail_count ?? "—"}</td>
        <td>${r.fidelity_pct != null ? r.fidelity_pct + "%" : "—"}</td>
        <td>${r.blind_spot ? "⚠ YES" : ""}</td></tr>`).join("") +
        "</tbody></table>";
    },
    async project(el) { await issuesView(el, { project: P }); },
    async issues(el) { await issuesView(el); },
    async config(el) {
      const { areas } = await get(`/api/runs/${R}/config`);
      const s = await get(`/api/runs/${R}/summary`);
      const blocks = [];
      for (const a of areas) {
        const { rows } = await get(`/api/runs/${R}/config?area=${a}`);
        const meta = (s.areas || {})[a];
        blocks.push(`<div class="panel"><h3>${esc(a)}
          ${meta ? `<span class="muted">src ${meta.src ?? "?"} · tgt ${meta.tgt ?? "?"}</span>` : ""}</h3>
          <table class="data"><thead><tr><th>Object</th><th>Kind</th><th>Detail</th>
          </tr></thead><tbody>${rows.map((r) => `<tr><td>${esc(r.name)}</td>
          <td>${kb(r.kind)}</td><td class="muted">${esc(JSON.stringify(r.detail))
          .slice(0, 200)}</td></tr>`).join("")}</tbody></table></div>`);
      }
      el.innerHTML = blocks.join("") ||
        '<div class="finding">No config gaps found.</div>';
    },
    async log(el) {
      const evs = await get(`/api/runs/${R}/events`);
      el.innerHTML = `<pre class="log">${evs.map((e) =>
        `[${esc(e.phase)}] ${esc(e.message)}`).join("\n")}</pre>`;
    },
  };

  return {
    boot() {
      const el = document.getElementById("app");
      R = el.dataset.run; V = el.dataset.view || "overview";
      P = el.dataset.project; SRC = el.dataset.srcBase; TGT = el.dataset.tgtBase;
      (views[V] || views.overview)(el);
    },
  };
})();
```

- [ ] **Step 6: Run the tests**

Run: `python3 -m pytest tests/test_main.py -q`
Expected: `10 passed`.

- [ ] **Step 7: Run the FULL suite**

Run: `python3 -m pytest -q`
Expected: all tests pass, 0 failures.

- [ ] **Step 8: Commit**

```bash
git add webapp/main.py webapp/templates webapp/static tests/test_main.py
git commit -m "feat: web app — wizard, OAuth flow, runs with SSE, elevation, multi-page analysis UI"
```

---


> NOTE from Task 7 review: a new finding kind comment_uncheckable exists — add a .k-comment_uncheckable badge style (muted/gray) alongside the other kind badges.

> NOTE from Task 8 review: config finding kind area_error exists — badge it as a warning/critical color; it means "could not verify this area," not "clean."

## Post-review amendments (applied)

- **Async SSE with disconnect detection** — `/runs/{run_id}/stream` converted from a sync generator (blocked `time.sleep` in threadpool) to an `async def` route with `await request.is_disconnected()` + `await asyncio.sleep(1.0)`; closes a running-run thread leak where a closed browser tab would hold a thread and fire 1 DB query/sec until the run finished.
- **Uniform app.js escaping** — kind `<option>` value and label now wrapped with `esc(...)`; project filter value in the `/issues/kinds` URL now wrapped with `encodeURIComponent(...)`.
- **CSRF documented as accepted localhost-only risk** — state-changing POSTs have no CSRF token by design (localhost single-user tool, MA_BIND defaults to 127.0.0.1); revisit if hosted (the MA_AUTH_MODE seam). Comment added above elevation routes in main.py. Known limitation: no CSRF tokens on state-changing POSTs (localhost single-user tool, MA_BIND defaults to 127.0.0.1; revisit if hosted).
