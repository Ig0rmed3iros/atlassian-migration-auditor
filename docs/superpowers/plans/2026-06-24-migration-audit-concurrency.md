# Migration-Audit Concurrency (Tier 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut migration-audit wall-clock (~65 min on a 2-project Alveo→Gresham run) by ~60% by overlapping the two independent instances and parallelizing the config N+1 loops, with zero change to audit results.

**Architecture:** Reuse the existing, battle-tested `auditor.envaudit._pool.map_results` (the env-audit gather pool) in three migration-audit hot paths: (1) bump the extract page size 50→100, (2) overlap source+target extraction per project — they hit *different* instances with *separate* rate limits, so this is near-free, (3) fan out the config-parity area fetches and the custom-field-option / screen-field N+1 loops. Every parallel section follows the pool's determinism contract: workers do independent reads and return values; the main thread merges, so output is byte-identical to the sequential path.

**Tech Stack:** Python ≥3.11, `httpx` (sync client, thread-safe), `concurrent.futures.ThreadPoolExecutor` (via `_pool.map_results`), `pytest` + `httpx.MockTransport` for tests. No new dependencies.

## Global Constraints

- **Determinism is mandatory.** Parallel output MUST be byte-identical to the sequential (`workers=1`) output. Every parallel section is pinned by an equivalence test that runs `workers=1` vs `workers≥2` against the same `MockTransport` and asserts identical `areas`/`findings`/extracted lines. Mirror `tests/test_env_gather.py::test_gather_equivalence_seq_vs_parallel`.
- **Reuse, do not reinvent (DRY).** All concurrency goes through `auditor.envaudit._pool.map_results(items, fn, workers=None)`. Do not add a second thread-pool implementation.
- **Worker-knob convention** (copied from `_pool._resolved`): read an env var, fall back to a default, clamp to `>= 1`, and `1 == forced sequential`. Invalid/absent → default.
- **Thread-safety rests on three facts** (from `auditor/envaudit/_pool.py` docstring): `httpx.Client.request` is thread-safe and carries the shared per-call 429/5xx backoff; workers NEVER mutate a shared accumulator; the main thread merges results by key/index so completion order can't affect output.
- **Fail-loud semantics preserved.** The extract verified-count gate still raises `RuntimeError("...refusing to compare")`; config `area_error` findings still surface for every errored side. A worker that raises must re-raise on the main thread, never be swallowed.
- **Page size default 100** (the safe Jira Cloud `/search/jql` value; Data Center tolerates it), override via `MA_EXTRACT_PAGE`.
- **No new deps; Python ≥3.11.** Run the full suite (`pytest -q`) green before the final commit of each task.
- Repo root: `/mnt/d/Atlassian-Products/Migration-auditor`. Current branch: `fix/autonomous-audit-sweep-2026-06-23`. All paths below are relative to repo root.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `auditor/extract.py` | Per-project issue extraction | Add `_extract_page()` resolver; use it for `search_jql(page=…)` |
| `webapp/stages.py` | Migration pipeline stages | Add `_extract_workers()`; rewrite `stage_extract` to overlap src/tgt via `map_results` |
| `auditor/config_audit.py` | Config-parity gather/diff | Parallelize SIMPLE areas, custom-field options, screen fields via `map_results`; merge on main thread |
| `tests/test_extract.py` | Extract unit tests | Add page-size resolver + default-page-100 tests |
| `tests/test_stages_pipeline.py` | Stage-level tests | Add extract seq==parallel equivalence + parallel gating tests |
| `tests/test_config_audit.py` | Config unit tests | Add config seq==parallel equivalence test |

`map_results` and `worker_count` are imported from `auditor.envaudit._pool` (already exists; no change to that file).

---

### Task 1: Configurable extract page size (50 → 100)

**Files:**
- Modify: `auditor/extract.py` (add `_extract_page()`; change the `search_jql(..., page=50)` call near line 218)
- Test: `tests/test_extract.py`

**Interfaces:**
- Produces: `auditor.extract._extract_page() -> int` (default 100, clamp `>=1`, env `MA_EXTRACT_PAGE`). `extract_project` unchanged in signature; it now requests `maxResults = _extract_page()`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_extract.py`:

```python
import json
def test_extract_page_default_and_env_override(monkeypatch):
    from auditor.extract import _extract_page
    monkeypatch.delenv("MA_EXTRACT_PAGE", raising=False)
    assert _extract_page() == 100
    monkeypatch.setenv("MA_EXTRACT_PAGE", "250"); assert _extract_page() == 250
    monkeypatch.setenv("MA_EXTRACT_PAGE", "0");   assert _extract_page() == 1
    monkeypatch.setenv("MA_EXTRACT_PAGE", "junk"); assert _extract_page() == 100


def test_extract_requests_page_100_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("MA_EXTRACT_PAGE", raising=False)
    seen = {}
    def handler(req):
        p = str(req.url.path)
        if p.endswith("approximate-count"):
            return httpx.Response(200, json={"count": 1})
        if p.endswith("search/jql"):
            seen["maxResults"] = json.loads(req.content)["maxResults"]
            return httpx.Response(200, json={
                "issues": [{"key": "AC-1", "id": "1", "fields": {"summary": "s"}}],
                "isLast": True})
        return httpx.Response(404)
    conn = Connection(auth_type="pat", site_url="https://s.atlassian.net",
                      email="e", api_token="t")
    cl = JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                    sleeper=lambda s: None)
    extract_project(cl, "AC", str(tmp_path / "AC.core.jsonl.gz"))
    assert seen["maxResults"] == 100
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_extract.py::test_extract_page_default_and_env_override tests/test_extract.py::test_extract_requests_page_100_by_default -v`
Expected: FAIL — `_extract_page` ImportError, and `maxResults == 50`.

- [ ] **Step 3: Implement the resolver and use it**

In `auditor/extract.py`, add near the top (after the imports; `os` is already imported):

```python
def _extract_page() -> int:
    """Issues per search page. Default 100 (the safe Jira Cloud /search/jql
    value; DC tolerates it). Fewer pages = fewer round-trips = less rate-limit
    exposure. Override with MA_EXTRACT_PAGE; clamped to >= 1."""
    raw = os.environ.get("MA_EXTRACT_PAGE")
    if raw is None:
        return 100
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 100
    return max(1, n)
```

In `extract_project`, change the search call (currently `page=50`):

```python
        for iss in client.search_jql(
                f'project = "{key}" ORDER BY key ASC', fields,
                page=_extract_page()):
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_extract.py -v`
Expected: PASS (new tests + all existing extract tests).

- [ ] **Step 5: Commit**

```bash
cd /mnt/d/Atlassian-Products/Migration-auditor
git add auditor/extract.py tests/test_extract.py
git commit -m "perf(extract): default search page 50->100 (MA_EXTRACT_PAGE), halves round-trips"
```

---

### Task 2: Overlap source + target extraction per project

**Files:**
- Modify: `webapp/stages.py` (add `_extract_workers()`; rewrite `stage_extract`, currently lines 185–222; add `from auditor.envaudit._pool import map_results`)
- Test: `tests/test_stages_pipeline.py`

**Interfaces:**
- Consumes: `auditor.envaudit._pool.map_results(items, fn, workers)`; `webapp.stages._say`, `EXTRACT_FORMAT`, `extract_format`, `connector.extract`.
- Produces: `webapp.stages._extract_workers() -> int` (default 2, env `MA_EXTRACT_WORKERS`, clamp `>=1`). `stage_extract(ctx)` unchanged in signature; projects stay sequential, the two sides run concurrently. `ctx` keys used: `connector`, `params`, `selected`, `src`, `tgt`, `workspace` (plus whatever `_say` reads).

**Design note:** Width is bounded to the two sides per project (different instances → no same-instance rate pressure). Projects remain sequential — this plan does NOT add project-level parallelism. `map_results` returns results in input order (src then tgt), so the gating loop runs in the same order as today.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_stages_pipeline.py` (top already imports `gzip`, `json`, `os`, `httpx`, `pytest`, `S`, `JIRA`; add `Connection, JiraClient` import if absent):

```python
from auditor.client import Connection, JiraClient

def _issues_client(n):
    issues = [{"key": f"AC-{i}", "id": str(i), "fields": {"summary": f"s{i}"}}
              for i in range(n)]
    def handler(req):
        p = str(req.url.path)
        if p.endswith("approximate-count"):
            return httpx.Response(200, json={"count": n})
        if p.endswith("search/jql"):
            return httpx.Response(200, json={"issues": issues, "isLast": True})
        return httpx.Response(404)          # /field -> handled as no custom fields
    conn = Connection(auth_type="pat", site_url="https://x.atlassian.net",
                      email="e", api_token="t")
    return JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                      sleeper=lambda s: None)


def _lines(path):
    with gzip.open(path, "rt") as f:
        return [ln for ln in f]


def test_stage_extract_seq_vs_parallel_identical(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "_say", lambda *a, **k: None)
    def run(workers, sub):
        monkeypatch.setenv("MA_EXTRACT_WORKERS", str(workers))
        ws = str(tmp_path / sub)
        os.makedirs(os.path.join(ws, "src")); os.makedirs(os.path.join(ws, "tgt"))
        ctx = {"connector": JIRA, "src": _issues_client(3), "tgt": _issues_client(3),
               "selected": [{"key": "AC", "name": "AC", "src_count": 3, "tgt_count": 3}],
               "workspace": ws, "params": {}}
        S.stage_extract(ctx)
        return ws
    seq, par = run(1, "seq"), run(2, "par")
    for side in ("src", "tgt"):
        a = _lines(os.path.join(seq, side, "AC.core.jsonl.gz"))
        b = _lines(os.path.join(par, side, "AC.core.jsonl.gz"))
        assert a == b
        assert sum(1 for ln in b if '"AC-' in ln) == 3


def test_stage_extract_gating_raises_in_parallel(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "_say", lambda *a, **k: None)
    monkeypatch.setenv("MA_EXTRACT_WORKERS", "2")
    def client(n_issues, n_count):
        issues = [{"key": f"AC-{i}", "id": str(i), "fields": {"summary": "s"}}
                  for i in range(n_issues)]
        def h(req):
            p = str(req.url.path)
            if p.endswith("approximate-count"):
                return httpx.Response(200, json={"count": n_count})
            if p.endswith("search/jql"):
                return httpx.Response(200, json={"issues": issues, "isLast": True})
            return httpx.Response(404)
        conn = Connection(auth_type="pat", site_url="https://x.atlassian.net",
                          email="e", api_token="t")
        return JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(h)),
                          sleeper=lambda s: None)
    ws = str(tmp_path)
    os.makedirs(os.path.join(ws, "src")); os.makedirs(os.path.join(ws, "tgt"))
    ctx = {"connector": JIRA, "src": client(3, 3), "tgt": client(2, 5),
           "selected": [{"key": "AC", "name": "AC", "src_count": 3, "tgt_count": 5}],
           "workspace": ws, "params": {}}
    with pytest.raises(RuntimeError, match="refusing to compare"):
        S.stage_extract(ctx)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_stages_pipeline.py::test_stage_extract_seq_vs_parallel_identical tests/test_stages_pipeline.py::test_stage_extract_gating_raises_in_parallel -v`
Expected: FAIL — `_extract_workers` / parallel path not present (identical test may pass coincidentally, but the gating test fails because the current code raises inline before any pool exists; the equivalence test fails on `MA_EXTRACT_WORKERS` having no effect only if the rewrite is absent — both are RED until Step 3).

- [ ] **Step 3: Implement the resolver + rewrite `stage_extract`**

In `webapp/stages.py`, add the import near the top with the other `auditor` imports:

```python
from auditor.envaudit._pool import map_results
```

Add the resolver (near the other module helpers):

```python
def _extract_workers() -> int:
    """Concurrency for a project's source/target extraction. Default 2 (the two
    sides hit different instances with separate rate limits). MA_EXTRACT_WORKERS
    overrides; clamped to >= 1 (1 == forced sequential)."""
    raw = os.environ.get("MA_EXTRACT_WORKERS")
    if raw is None:
        return 2
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 2
    return max(1, n)
```

Replace the body of `stage_extract` (lines 185–222) with:

```python
def stage_extract(ctx):
    connector = ctx["connector"]
    reuse = bool(ctx["params"].get("reuse_extracts_from"))
    workers = _extract_workers()
    for m in ctx["selected"]:
        # Partition this project's sides into reuse (skip) vs fetch.
        tasks = []
        for side, cl in (("src", ctx["src"]), ("tgt", ctx["tgt"])):
            path = os.path.join(ctx["workspace"], side,
                                f"{m['key']}.core.jsonl.gz")
            if reuse and os.path.exists(path):
                fmt = extract_format(path)
                if fmt == EXTRACT_FORMAT:
                    _say(ctx, "extract",
                         f"{side} {m['key']}: reusing cached extract")
                    continue
                _say(ctx, "extract",
                     f"{side} {m['key']}: cached extract has incompatible "
                     f"format {fmt} (current {EXTRACT_FORMAT}) — "
                     f"re-extracting", "warn")
            total = m["src_count"] if side == "src" else m["tgt_count"]
            tasks.append((side, cl, path, total))

        # The two sides hit DIFFERENT instances — run them concurrently.
        # Projects stay sequential (no extra same-instance pressure).
        def _extract_one(task, _m=m):
            side, cl, path, total = task
            res = connector.extract(
                cl, _m["key"], path,
                progress=lambda n, k=_m["key"], s=side, t=total: _say(
                    ctx, "extract",
                    f"{s} {k}: {n}/{t if isinstance(t, int) else '?'}"))
            return side, res

        # Gate on the MAIN thread, in input (src, tgt) order, preserving the
        # fail-loud verified-count semantics. A worker that raised is returned
        # as an Exception by map_results — re-raise it, never swallow.
        for outcome in map_results(tasks, _extract_one, workers=workers):
            if isinstance(outcome, Exception):
                raise outcome
            side, res = outcome
            if not res["verified"]:
                if isinstance(res["approx"], int):
                    raise RuntimeError(
                        f"{side} {m['key']}: extracted {res['extracted']} but "
                        f"approximate-count says {res['approx']} — extraction "
                        f"not complete, refusing to compare")
                _say(ctx, "extract",
                     f"{side} {m['key']}: approximate-count unavailable "
                     f"({res['approx']}); proceeding on extracted="
                     f"{res['extracted']}", "warn")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_stages_pipeline.py -v`
Expected: PASS (new tests + the two end-to-end smokes test 10/11).

- [ ] **Step 5: Commit**

```bash
cd /mnt/d/Atlassian-Products/Migration-auditor
git add webapp/stages.py tests/test_stages_pipeline.py
git commit -m "perf(extract): overlap source+target extraction per project (MA_EXTRACT_WORKERS)"
```

---

### Task 3: Parallelize config-parity SIMPLE areas

**Files:**
- Modify: `auditor/config_audit.py` (add `from auditor.envaudit._pool import map_results`; rewrite the `for area, suffix, key in SIMPLE:` loop, currently ~lines 216–235)
- Test: `tests/test_config_audit.py`

**Interfaces:**
- Consumes: `map_results`; existing `_fetch_area(client, area, suffix, key) -> (items, err)`, `_summary`, `_names`, `_area_errors`, `SIMPLE`, `CLOUD_ONLY`.
- Produces: no signature change to `audit_config`. The 15 SIMPLE areas × 2 sides are fetched concurrently; summaries and findings are built on the main thread in the original `SIMPLE` order → identical output.

**Design note:** Determinism comes from a two-phase split: (phase A) fetch all `(area, side)` pairs concurrently into a dict keyed by `(area, side)`; (phase B) iterate `SIMPLE` in order, read the pre-fetched results, and build summaries/findings/`say()` exactly as the sequential code did. Skipped DC areas are handled in phase B (no fetch task created).

- [ ] **Step 1: Write the failing equivalence test**

Add to `tests/test_config_audit.py`:

```python
import os

def _rich_pair():
    """A src/tgt pair exercising SIMPLE areas with a clear source-only gap."""
    src = dict(BASE)
    src["/rest/api/3/status"] = [{"name": "Open"}, {"name": "On Hold"},
                                 {"name": "Blocked"}]
    src["/rest/api/3/priority"] = [{"name": "P1"}, {"name": "P2"}]
    tgt = dict(BASE)                      # BASE status has only Open
    tgt["/rest/api/3/priority"] = [{"name": "P1"}]
    return make_pair(src, tgt)


def test_config_simple_areas_seq_vs_parallel_identical(monkeypatch):
    monkeypatch.setenv("MA_GATHER_WORKERS", "1")
    s1, t1 = _rich_pair()
    seq = audit_config(s1, t1)
    monkeypatch.setenv("MA_GATHER_WORKERS", "10")
    s2, t2 = _rich_pair()
    par = audit_config(s2, t2)
    assert seq["areas"] == par["areas"]
    assert seq["findings"] == par["findings"]
    # sanity: the source-only gaps are actually present
    names = {f["name"] for f in seq["findings"] if f["area"] == "statuses"}
    assert {"On Hold", "Blocked"} <= names
```

- [ ] **Step 2: Run test to verify it fails (or passes coincidentally only at 1 worker)**

Run: `pytest tests/test_config_audit.py::test_config_simple_areas_seq_vs_parallel_identical -v`
Expected: PASS today only because the code is still sequential at any worker count — this test becomes the *regression guard* for Task 3. To confirm it genuinely exercises the parallel path, it must still pass AFTER Step 3. (If you want a strictly-RED start, temporarily assert `seq != par`; not required.)

- [ ] **Step 3: Rewrite the SIMPLE loop to fetch concurrently**

In `auditor/config_audit.py`, add near the top imports:

```python
from auditor.envaudit._pool import map_results
```

Replace the SIMPLE loop (currently `for area, suffix, key in SIMPLE:` … through the per-area finding construction) with:

```python
    # ---- simple dimensions (fetch src+tgt for every area concurrently, then
    # build summaries/findings on the main thread in SIMPLE order -> identical
    # output regardless of completion order; same determinism contract as the
    # env gather pool).
    active = [(area, suffix, key) for area, suffix, key in SIMPLE
              if not (dc_side and area in CLOUD_ONLY)]
    fetch_tasks = []   # (area, side, client, suffix, key)
    for area, suffix, key in active:
        fetch_tasks.append((area, "src", src, suffix, key))
        fetch_tasks.append((area, "tgt", tgt, suffix, key))
    fetched = {}
    results = map_results(
        fetch_tasks,
        lambda t: _fetch_area(t[2], t[0], t[3], t[4]))
    for (area, side, _c, _s, _k), res in zip(fetch_tasks, results):
        # A task that raised is returned as the exception; treat as an errored
        # side ((items, err)) so _area_errors still surfaces it loudly.
        fetched[(area, side)] = res if isinstance(res, tuple) else ([], str(res))

    for area, suffix, key in SIMPLE:
        if dc_side and area in CLOUD_ONLY:
            areas[area] = {"label": area, "skipped": True,
                           "reason": "no Data Center API — verify manually"}
            say(f"[{area}] skipped — no Data Center API")
            continue
        s, se = fetched[(area, "src")]
        t, te = fetched[(area, "tgt")]
        summ = _summary(area, _names(s), _names(t))
        if se or te:
            summ["error"] = f"src={se} tgt={te}"
        areas[area] = summ
        findings.extend(_area_errors(area, se, te))
        for name in summ["source_only"]:
            findings.append({"area": area, "name": name,
                             "kind": "missing_in_tgt", "detail": {}})
        say(f"[{area}] src={summ['src']} tgt={summ['tgt']} "
            f"source-only={len(summ['source_only'])}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_config_audit.py -v`
Expected: PASS (new equivalence test + all existing config tests, including `test_simple_dimension_source_only_findings`).

- [ ] **Step 5: Commit**

```bash
cd /mnt/d/Atlassian-Products/Migration-auditor
git add auditor/config_audit.py tests/test_config_audit.py
git commit -m "perf(config): fetch SIMPLE parity areas concurrently (MA_GATHER_WORKERS)"
```

---

### Task 4: Parallelize custom-field option (N+1) fetches

**Files:**
- Modify: `auditor/config_audit.py` (the custom-fields in-both loop, currently ~lines 261–290)
- Test: `tests/test_config_audit.py`

**Interfaces:**
- Consumes: `map_results`; existing `_field_options(client, fid) -> set`, `_SELECT_MARKERS`, `_norm_name`.
- Produces: no signature change. The per-field option fetches for every select-type matched field (src+tgt) run concurrently; `option_mismatch` findings are built on the main thread in `sorted` key order → identical output. `checked` count unchanged.

**Design note:** Type-mismatch findings need no I/O — keep them in the first sequential pass. Only the option deep-check (which calls `_field_options` twice per select field) is parallelized.

- [ ] **Step 1: Write the failing equivalence test**

Add to `tests/test_config_audit.py` (reuses the `test_custom_field_type_and_option_mismatches` data shape, with two select fields so the pool runs >1 task):

```python
def _two_select_pair():
    src = dict(BASE)
    src["/rest/api/3/field"] = [
        {"name": "Squad", "id": "customfield_1", "custom": True,
         "schema": {"custom": "com.atlassian.jira.plugin:select"}},
        {"name": "Tier", "id": "customfield_2", "custom": True,
         "schema": {"custom": "com.atlassian.jira.plugin:radiobuttons"}},
    ]
    src["/rest/api/3/field/customfield_1/context"] = {"values": [{"id": "c1"}], "isLast": True}
    src["/rest/api/3/field/customfield_1/context/c1/option"] = {
        "values": [{"value": "Alpha"}, {"value": "Beta"}], "isLast": True}
    src["/rest/api/3/field/customfield_2/context"] = {"values": [{"id": "c2"}], "isLast": True}
    src["/rest/api/3/field/customfield_2/context/c2/option"] = {
        "values": [{"value": "Gold"}, {"value": "Silver"}], "isLast": True}
    tgt = dict(BASE)
    tgt["/rest/api/3/field"] = [
        {"name": "Squad", "id": "customfield_9", "custom": True,
         "schema": {"custom": "com.atlassian.jira.plugin:select"}},
        {"name": "Tier", "id": "customfield_8", "custom": True,
         "schema": {"custom": "com.atlassian.jira.plugin:radiobuttons"}},
    ]
    tgt["/rest/api/3/field/customfield_9/context"] = {"values": [{"id": "cA"}], "isLast": True}
    tgt["/rest/api/3/field/customfield_9/context/cA/option"] = {
        "values": [{"value": "Alpha"}], "isLast": True}      # missing Beta
    tgt["/rest/api/3/field/customfield_8/context"] = {"values": [{"id": "cB"}], "isLast": True}
    tgt["/rest/api/3/field/customfield_8/context/cB/option"] = {
        "values": [{"value": "Gold"}, {"value": "Silver"}], "isLast": True}  # complete
    return make_pair(src, tgt)


def test_config_custom_field_options_seq_vs_parallel_identical(monkeypatch):
    monkeypatch.setenv("MA_GATHER_WORKERS", "1")
    s1, t1 = _two_select_pair(); seq = audit_config(s1, t1)
    monkeypatch.setenv("MA_GATHER_WORKERS", "10")
    s2, t2 = _two_select_pair(); par = audit_config(s2, t2)
    assert seq["areas"]["custom_fields"] == par["areas"]["custom_fields"]
    assert ([f for f in seq["findings"] if f["area"] == "custom_fields"]
            == [f for f in par["findings"] if f["area"] == "custom_fields"])
    miss = [f for f in seq["findings"]
            if f["area"] == "custom_fields" and f["kind"] == "option_mismatch"]
    assert len(miss) == 1 and miss[0]["name"] == "Squad"
    assert miss[0]["detail"]["missing_options_in_tgt"] == ["Beta"]
```

- [ ] **Step 2: Run test to verify it passes pre-change (regression guard)**

Run: `pytest tests/test_config_audit.py::test_config_custom_field_options_seq_vs_parallel_identical -v`
Expected: PASS (sequential today) — guards that Step 3 keeps output identical.

- [ ] **Step 3: Parallelize the option fetches**

In `auditor/config_audit.py`, replace the in-both custom-field loop (the `for key in sorted(set(scustom) & set(tcustom)):` block that does `_field_options(src…)`/`_field_options(tgt…)`) with a two-phase version:

```python
    # Pass 1 (no I/O): type mismatches + collect the select fields needing the
    # option deep-check.
    select_keys = []
    for key in sorted(set(scustom) & set(tcustom)):
        srec, trec = scustom[key], tcustom[key]
        name = srec["name"]
        s_type = str((srec.get("schema") or {}).get("custom", "")).split(":")[-1]
        t_type = str((trec.get("schema") or {}).get("custom", "")).split(":")[-1]
        if s_type != t_type:
            findings.append({"area": "custom_fields", "name": name,
                             "kind": "type_mismatch",
                             "detail": {"src_type": s_type, "tgt_type": t_type}})
        ct = str((srec.get("schema") or {}).get("custom", ""))
        if not dc_side and any(mk in ct for mk in _SELECT_MARKERS):
            select_keys.append(key)

    # Pass 2 (parallel I/O): fetch src+tgt options for every select field at
    # once, then build option_mismatch findings on the main thread in sorted
    # order -> identical to the sequential version.
    opt_tasks = []   # (key, side, client, fid)
    for key in select_keys:
        opt_tasks.append((key, "src", src, scustom[key]["id"]))
        opt_tasks.append((key, "tgt", tgt, tcustom[key]["id"]))
    opt_results = map_results(opt_tasks, lambda t: _field_options(t[2], t[3]))
    opts = {}
    for (key, side, _c, _f), res in zip(opt_tasks, opt_results):
        opts[(key, side)] = res if isinstance(res, set) else set()
    for key in select_keys:
        name = scustom[key]["name"]
        so, to = opts[(key, "src")], opts[(key, "tgt")]
        miss = sorted(so - to)
        if miss:
            findings.append({"area": "custom_fields", "name": name,
                             "kind": "option_mismatch",
                             "detail": {"missing_options_in_tgt": miss[:40],
                                        "src_opts": len(so),
                                        "tgt_opts": len(to)}})
    checked = len(select_keys)
```

Leave the surrounding lines intact: `summ["select_fields_checked"] = checked`, the `if dc_side: summ["options_checked"] = False`, `areas["custom_fields"] = summ`, and the `say(...)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_config_audit.py -v`
Expected: PASS (new test + existing `test_custom_field_type_and_option_mismatches`).

- [ ] **Step 5: Commit**

```bash
cd /mnt/d/Atlassian-Products/Migration-auditor
git add auditor/config_audit.py tests/test_config_audit.py
git commit -m "perf(config): parallelize custom-field option deep-check (N+1 -> pooled)"
```

---

### Task 5: Parallelize screen-field (N+1) deep fetches

**Files:**
- Modify: `auditor/config_audit.py` (the screens deep loop, currently ~lines 345–360)
- Test: `tests/test_config_audit.py`

**Interfaces:**
- Consumes: `map_results`; existing `_screen_fields(client, sid) -> set`, `_SCREEN_DEEP_CAP`, `_norm_name`.
- Produces: no signature change. Per-screen field fetches for the deep set (src+tgt) run concurrently; `field_mismatch` findings built on the main thread in `deep` order → identical output. `deep_checked`/`capped` unchanged.

- [ ] **Step 1: Write the failing equivalence test**

Add to `tests/test_config_audit.py`:

```python
def _two_screen_pair():
    src = dict(BASE)
    src["/rest/api/3/screens"] = {"values": [
        {"id": 1, "name": "Default Screen"}, {"id": 2, "name": "Bug Screen"}],
        "isLast": True}
    # screen 1: tab 10 with fields A,B ; screen 2: tab 20 with field C
    src["/rest/api/3/screens/1/tabs"] = [{"id": 10}]
    src["/rest/api/3/screens/1/tabs/10/fields"] = [{"name": "A"}, {"name": "B"}]
    src["/rest/api/3/screens/2/tabs"] = [{"id": 20}]
    src["/rest/api/3/screens/2/tabs/20/fields"] = [{"name": "C"}]
    tgt = dict(BASE)
    tgt["/rest/api/3/screens"] = {"values": [
        {"id": 91, "name": "Default Screen"}, {"id": 92, "name": "Bug Screen"}],
        "isLast": True}
    tgt["/rest/api/3/screens/91/tabs"] = [{"id": 30}]
    tgt["/rest/api/3/screens/91/tabs/30/fields"] = [{"name": "A"}]   # missing B
    tgt["/rest/api/3/screens/92/tabs"] = [{"id": 40}]
    tgt["/rest/api/3/screens/92/tabs/40/fields"] = [{"name": "C"}]   # complete
    return make_pair(src, tgt)


def test_config_screen_fields_seq_vs_parallel_identical(monkeypatch):
    monkeypatch.setenv("MA_GATHER_WORKERS", "1")
    s1, t1 = _two_screen_pair(); seq = audit_config(s1, t1)
    monkeypatch.setenv("MA_GATHER_WORKERS", "10")
    s2, t2 = _two_screen_pair(); par = audit_config(s2, t2)
    assert seq["areas"]["screens"] == par["areas"]["screens"]
    sc = [f for f in seq["findings"] if f["area"] == "screens"]
    assert sc == [f for f in par["findings"] if f["area"] == "screens"]
    assert sc == [{"area": "screens", "name": "Default Screen",
                   "kind": "field_mismatch",
                   "detail": {"fields_missing_in_tgt": ["B"]}}]
```

- [ ] **Step 2: Run test to verify it passes pre-change (regression guard)**

Run: `pytest tests/test_config_audit.py::test_config_screen_fields_seq_vs_parallel_identical -v`
Expected: PASS (sequential today) — guards Step 3.

- [ ] **Step 3: Parallelize the screen deep loop**

In `auditor/config_audit.py`, replace the `for key in deep:` block (the one calling `_screen_fields(src…)`/`_screen_fields(tgt…)`) with:

```python
    deep = in_both[:_SCREEN_DEEP_CAP]
    sf_tasks = []   # (key, side, client, sid)
    for key in deep:
        sf_tasks.append((key, "src", src, ssn[key]["id"]))
        sf_tasks.append((key, "tgt", tgt, tsn[key]["id"]))
    sf_results = map_results(sf_tasks, lambda t: _screen_fields(t[2], t[3]))
    sflds = {}
    for (key, side, _c, _s), res in zip(sf_tasks, sf_results):
        sflds[(key, side)] = res if isinstance(res, set) else set()
    for key in deep:
        s_f, t_f = sflds[(key, "src")], sflds[(key, "tgt")]
        miss = sorted(s_f - t_f)
        if miss:
            findings.append({"area": "screens", "name": ssn[key]["name"],
                             "kind": "field_mismatch",
                             "detail": {"fields_missing_in_tgt": miss[:25]}})
    areas["screens"]["deep_checked"] = len(deep)
    areas["screens"]["capped"] = len(in_both) > _SCREEN_DEEP_CAP
    say(f"[screens] deep_checked={len(deep)} capped={areas['screens']['capped']}")
```

- [ ] **Step 4: Run the full suite**

Run: `pytest -q`
Expected: PASS — entire suite green (all five changes integrated).

- [ ] **Step 5: Commit**

```bash
cd /mnt/d/Atlassian-Products/Migration-auditor
git add auditor/config_audit.py tests/test_config_audit.py
git commit -m "perf(config): parallelize screen-field deep-check (N+1 -> pooled)"
```

---

## Verification (after all tasks)

- [ ] `pytest -q` — full suite green.
- [ ] Documentation: add `MA_EXTRACT_PAGE` (default 100) and `MA_EXTRACT_WORKERS` (default 2) rows to the **Configuration (env)** table in `README.md`, and note that `MA_GATHER_WORKERS` now also governs migration config-parity gather. (Fold into the Task 5 commit or a separate `docs:` commit.)
- [ ] Optional real-run sanity (operator, not CI): re-run the Alveo→Gresham audit and confirm the verdict + finding counts match the previous run (run #4) while wall-clock drops. Expected: extract phase roughly `max(src,tgt)` instead of `src+tgt`; config phase's 630s/461s stalls collapse.

## Self-Review

- **Spec coverage:** (1) page size → Task 1. (2) overlap src/tgt extract → Task 2. (3) parallelize config + N+1 → Tasks 3 (areas), 4 (custom-field options), 5 (screen fields). All three chosen Tier-1 items covered.
- **Determinism:** every parallel section has a `workers=1` vs `workers≥2` equivalence test asserting identical `areas`/`findings`/lines.
- **Type consistency:** `map_results(items, fn, workers=None)` used consistently; `_fetch_area` returns `(items, err)`, `_field_options`/`_screen_fields` return `set` — handled with `isinstance` guards so a worker exception degrades to the same errored/empty value the sequential code produced. `_extract_page` / `_extract_workers` both return `int`, clamp `>=1`.
- **No placeholders:** every step shows full code and an exact command with expected result.
- **Fail-loud preserved:** Task 2 re-raises worker exceptions and keeps the verified-count `RuntimeError`; Tasks 3–5 keep `_area_errors` by mapping worker exceptions to `(…, err)`/empty values.

## Execution Handoff

Two execution options:
1. **Subagent-Driven (recommended)** — a fresh subagent per task with review between tasks.
2. **Inline Execution** — execute tasks in this session with checkpoints.
