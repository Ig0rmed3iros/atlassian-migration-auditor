### Task 12: `webapp/stages.py` — real stage wiring (core ↔ engine)

**Files:**
- Create: `webapp/stages.py`
- Test: `tests/test_stages.py`

Builds the production `stages` dict: constructs `Connection`/`JiraClient` for both sides from stored connections (with OAuth refresh persistence wired into the store), then calls the core functions and shapes ctx. This is the only file that knows both worlds.

- [ ] **Step 1: Write the failing tests**

`tests/test_stages.py`:
```python
import httpx
import pytest
from webapp.stages import build_clients, build_stages
from webapp.store import Store


@pytest.fixture()
def store(tmp_path):
    return Store(db_path=str(tmp_path / "t.db"), key_path=str(tmp_path / ".key"))


def test_build_clients_pat_and_oauth(store):
    mid = store.create_migration("m")
    store.save_connection(mid, "source", "pat", "https://s.atlassian.net",
                          secret={"email": "a@b.c", "token": "tok"})
    store.save_connection(mid, "target", "oauth", "https://t.atlassian.net",
                          cloud_id="cid-9",
                          secret={"access_token": "at", "refresh_token": "rt",
                                  "expires_at": 9e12})
    src, tgt = build_clients(store, mid, http=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))))
    assert src.conn.auth_type == "pat" and src.conn.email == "a@b.c"
    assert tgt.conn.auth_type == "oauth" and tgt.conn.cloud_id == "cid-9"
    assert src.conn.api_base == "https://s.atlassian.net"
    assert tgt.conn.api_base == "https://api.atlassian.com/ex/jira/cid-9"


def test_oauth_refresh_persists_back_to_store(store):
    mid = store.create_migration("m")
    store.save_connection(mid, "source", "oauth", "https://s.atlassian.net",
                          cloud_id="c1",
                          secret={"access_token": "old", "refresh_token": "rt1",
                                  "expires_at": 1})   # expired -> proactive refresh
    calls = {"n": 0}
    def handler(req):
        if "auth.atlassian.com" in str(req.url):
            calls["n"] += 1
            return httpx.Response(200, json={"access_token": "new",
                                             "refresh_token": "rt2",
                                             "expires_in": 3600})
        return httpx.Response(200, json={"ok": 1})
    store.settings_set("oauth_client_id", "cid")
    store.settings_set("oauth_client_secret_enc",
                       store.encrypt({"secret": "sec"}).decode())
    src, _tgt_missing = build_clients(store, mid,
                                      http=httpx.Client(
                                          transport=httpx.MockTransport(handler)),
                                      require_both=False)
    st, _ = src.req("/rest/api/3/myself")
    assert st == 200 and calls["n"] == 1
    row = store.get_connection(mid, "source")
    sec = store.connection_secret(row)
    assert sec["refresh_token"] == "rt2" and sec["access_token"] == "new"


def test_build_stages_returns_all_engine_phases():
    from webapp.runs import PHASES
    stages = build_stages()
    for p in PHASES:
        if p == "finalize":
            continue
        assert p in stages and callable(stages[p])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_stages.py -q`
Expected: `ModuleNotFoundError: No module named 'webapp.stages'`.

- [ ] **Step 3: Write the implementation**

`webapp/stages.py`:
```python
"""Production stage functions: the only module that knows both the core
library and the store. Each stage is fn(ctx); ctx comes from RunEngine.

ctx keys written here and consumed downstream:
  clients (src, tgt) · scope rows · blind_spots · project_results ·
  issue_findings · config_result
"""
from __future__ import annotations

import os

import httpx

from auditor import compare as compare_mod
from auditor import config_audit as config_mod
from auditor import extract as extract_mod
from auditor import permissions as perm_mod
from auditor import scope as scope_mod
from auditor.client import Connection, JiraClient
from . import oauth as oauth_mod
from .store import Store


def _oauth_secret(store: Store) -> tuple[str | None, str | None]:
    cid = store.settings_get("oauth_client_id")
    enc = store.settings_get("oauth_client_secret_enc")
    sec = store.decrypt(enc.encode())["secret"] if enc else None
    return cid, sec


def build_clients(store: Store, migration_id: int,
                  http: httpx.Client | None = None,
                  require_both: bool = True):
    out = []
    cid, csec = _oauth_secret(store)
    for role in ("source", "target"):
        row = store.get_connection(migration_id, role)
        if row is None:
            if require_both:
                raise RuntimeError(f"no {role} connection configured")
            out.append(None)
            continue
        secret = store.connection_secret(row)
        if row["auth_type"] == "pat":
            conn = Connection(auth_type="pat", site_url=row["site_url"],
                              email=secret["email"], api_token=secret["token"])
        else:
            conn = Connection(auth_type="oauth", site_url=row["site_url"],
                              cloud_id=row["cloud_id"],
                              access_token=secret.get("access_token"),
                              refresh_token=secret.get("refresh_token"),
                              expires_at=float(secret.get("expires_at") or 0))
            conn_id = row["id"]
            if cid and csec:
                conn.refresh_fn = lambda rt, _cid=cid, _cs=csec: \
                    oauth_mod.refresh_tokens(_cid, _cs, rt, http=http)
            conn.on_tokens_refreshed = lambda c, _id=conn_id: \
                store.update_connection_secret(_id, {
                    "access_token": c.access_token,
                    "refresh_token": c.refresh_token,
                    "expires_at": c.expires_at})
        out.append(JiraClient(conn, http=http))
    return out[0], out[1]


# ------------------------------------------------------------------ stages
def _say(ctx, phase, msg, level="info"):
    ctx["store"].add_event(ctx["run_id"], phase, level, msg)


def stage_verify(ctx):
    store: Store = ctx["store"]
    src, tgt = build_clients(store, ctx["migration_id"])
    ctx["src"], ctx["tgt"] = src, tgt
    for role, cl in (("source", src), ("target", tgt)):
        me = cl.myself()    # raises ClientError loudly on auth failure
        row = store.get_connection(ctx["migration_id"], role)
        store.mark_connection_verified(row["id"],
                                       me.get("emailAddress") or "")
        ctx[f"{role}_account_id"] = me.get("accountId")
        _say(ctx, "verify", f"{role}: authenticated as "
             f"{me.get('displayName', '?')}")


def stage_scope(ctx):
    src, tgt = ctx["src"], ctx["tgt"]
    sp, serr = src.all_projects()
    tp, terr = tgt.all_projects()
    if serr or terr:
        raise RuntimeError(f"project enumeration failed: src={serr} tgt={terr}")
    matched = scope_mod.match_projects(sp, tp)
    selected = ctx["params"].get("projects") or \
        [m["key"] for m in matched["matched"]]
    ctx["selected"] = [m for m in matched["matched"] if m["key"] in selected]
    ctx["scope"] = matched
    rows = []
    for m in ctx["selected"]:
        m["src_count"] = src.approx_count(f'project = "{m["key"]}"')
        m["tgt_count"] = tgt.approx_count(f'project = "{m["key"]}"')
        rows.append({"key": m["key"], "name": m["name"],
                     "src_count": m["src_count"] if isinstance(m["src_count"], int) else None,
                     "tgt_count": m["tgt_count"] if isinstance(m["tgt_count"], int) else None,
                     "status": "scoped"})
    ctx["store"].set_run_projects(ctx["run_id"], rows)
    _say(ctx, "scope", f"{len(ctx['selected'])} project(s) in scope; "
         f"{len(matched['source_only'])} source-only, "
         f"{len(matched['target_only'])} target-only")


def stage_permissions(ctx):
    keys = [m["key"] for m in ctx["selected"]]
    spots = []
    for side, cl in (("source", ctx["src"]), ("target", ctx["tgt"])):
        for s in perm_mod.detect_blind_spots(cl, keys):
            s["side"] = side
            spots.append(s)
            if s["blind_spot"]:
                _say(ctx, "permissions",
                     f"BLIND SPOT on {side} {s['key']}: search sees "
                     f"{s['search_count']} of {s['insight_count']}. Fix access "
                     f"(elevation) and re-run before trusting counts.", "warn")
    ctx["blind_spots"] = spots
    rows = ctx["store"].get_run_projects(ctx["run_id"])
    blind_keys = {s["key"] for s in spots if s["blind_spot"]}
    for r in rows:
        r["blind_spot"] = 1 if r["key"] in blind_keys else 0
    ctx["store"].set_run_projects(ctx["run_id"], rows)


def stage_extract(ctx):
    reuse = bool(ctx["params"].get("reuse_extracts_from"))
    for m in ctx["selected"]:
        for side, cl in (("src", ctx["src"]), ("tgt", ctx["tgt"])):
            path = os.path.join(ctx["workspace"], side,
                                f"{m['key']}.core.jsonl.gz")
            if reuse and os.path.exists(path):
                _say(ctx, "extract",
                     f"{side} {m['key']}: reusing cached extract")
                continue
            total = m["src_count"] if side == "src" else m["tgt_count"]
            res = extract_mod.extract_project(
                cl, m["key"], path,
                progress=lambda n, k=m["key"], s=side, t=total: _say(
                    ctx, "extract", f"{s} {k}: {n}/{t if isinstance(t, int) else '?'}"))
            if not res["verified"]:
                if isinstance(res["approx"], int):
                    # Hard mismatch gates the compare phase (spec §10): a
                    # silent pagination gap must never feed the diff.
                    raise RuntimeError(
                        f"{side} {m['key']}: extracted {res['extracted']} but "
                        f"approximate-count says {res['approx']} — extraction "
                        f"not complete, refusing to compare")
                _say(ctx, "extract",
                     f"{side} {m['key']}: approximate-count unavailable "
                     f"({res['approx']}); proceeding on extracted="
                     f"{res['extracted']}", "warn")


def stage_compare(ctx):
    results, all_findings = {}, []
    for m in ctx["selected"]:
        out = compare_mod.compare_project(
            m["key"],
            os.path.join(ctx["workspace"], "src", f"{m['key']}.core.jsonl.gz"),
            os.path.join(ctx["workspace"], "tgt", f"{m['key']}.core.jsonl.gz"))
        results[m["key"]] = out
        all_findings.extend(out["findings"])
        s = out["stats"]
        _say(ctx, "compare",
             f"{m['key']}: common={s['common']} holes={s['missing_in_tgt']} "
             f"tails={s['tails']} mismatched={s['issues_with_mismatches']} "
             f"fidelity={s['fidelity_pct']}%")
    ctx["project_results"] = results
    ctx["issue_findings"] = all_findings
    rows = ctx["store"].get_run_projects(ctx["run_id"])
    for r in rows:
        st = results.get(r["key"], {}).get("stats")
        if st:
            r.update({"missing": st["missing_in_tgt"], "tail_count": st["tails"],
                      "fidelity_pct": st["fidelity_pct"], "status": "compared"})
    ctx["store"].set_run_projects(ctx["run_id"], rows)


def stage_config(ctx):
    jsm = ctx["params"].get("jsm_projects") or \
        [m["key"] for m in ctx["selected"]]
    ctx["config_result"] = config_mod.audit_config(
        ctx["src"], ctx["tgt"], jsm_projects=jsm,
        progress=lambda msg: _say(ctx, "config", msg))


def build_stages() -> dict:
    return {"verify": stage_verify, "scope": stage_scope,
            "permissions": stage_permissions, "extract": stage_extract,
            "compare": stage_compare, "config": stage_config}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_stages.py -q`
Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add webapp/stages.py tests/test_stages.py
git commit -m "feat: production stage wiring (clients from store, core stages into engine ctx)"
```

---

> NOTE from Task 1 review: never share one JiraClient/Connection across concurrent threads — token refresh mutates Connection in place. One client pair per run thread (current design) is safe.

> NOTE from Task 6 review: extract_project writes atomically (tmp+rename), so stage_extract's exists()-based reuse check is sound as-is — do NOT add a redundant completeness check.

## Post-review amendments (applied)

- **warn on unmatched requested project keys** (`webapp/stages.py` `stage_scope`): after computing `ctx["selected"]`, any key in `params["projects"]` that is absent from `matched["matched"]` now emits a `warn` event via `_say`. Previously such keys vanished silently, giving operators no visibility into typos or source-only project selections.

- **added stage-level pipeline tests** (`tests/test_stages_pipeline.py`): four tests exercising the load-bearing seams that `tests/test_stages.py` did not cover:
  1. `test_stage_scope_stores_err_counts_as_none` — a non-int `approx_count` return (e.g. `"ERR500"`) is normalised to `None` before writing to the DB; confirms the `isinstance(..., int)` guard in `stage_scope`.
  2. `test_stage_scope_warns_on_unmatched_requested_key` — RED before the fix above; GREEN after; asserts a warn event containing the missing key (`"GHOST"`) and the word `"skipped"` is written to the event log.
  3. `test_stage_compare_writes_none_fidelity_row` — two gz files with no key overlap produce `fidelity_pct=None` (not `0.0`) in both the in-memory `project_results` dict and the `run_projects` DB row.
  4. `test_stage_verify_fails_loud_on_unreachable_side` — monkeypatches `build_clients` to return a fake that raises `ClientError(401)`; confirms `stage_verify` re-raises rather than swallowing.

No real bugs were uncovered — all four tests passed cleanly once Fix 1 was in place.
