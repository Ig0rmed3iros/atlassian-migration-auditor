# Environment Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add an AI-powered single-environment Jira health/consistency audit as a second top-level feature, reposition the app as a multi-feature audit platform, and make every audit's workflow visible.

**Architecture:** A new `auditor/envaudit/` package (gather → checks → AI analysis → report) reuses `JiraClient`, the `config_audit` area readers, the `anthropic` client, and the metadata-only AI boundary. A `kind='env_audit'` RunEngine flow (`verify→scope→gather→checks→analysis→finalize`) drives it; the dashboard/run/analysis UI gains a platform landing, env-audit views, and workflow step strips.

**Tech Stack:** Python 3.11, FastAPI, SQLite, the `anthropic` SDK (injected for tests — no live calls), pytest, vanilla JS. Spec: `docs/superpowers/specs/2026-06-13-environment-audit.md`. Run the suite with `python3 -m pytest -q` from `/mnt/d/Atlassian-Products/MA-env`.

**Conventions:** Jira only for v1. The AI pass is optional (no key → skipped, never blocks). Metadata-only AI boundary — names/counts/structure/findings leave the machine, never issue/page content or values. Reuse `config_audit` readers; do not re-implement pagination.

---

## File structure
**Create:** `auditor/envaudit/__init__.py`, `gather.py`, `checks.py`, `analysis.py`, `report.py`; `webapp/env_stages.py`; tests `tests/test_env_gather.py`, `tests/test_env_checks.py`, `tests/test_env_analysis.py`, `tests/test_env_report.py`, `tests/test_env_run.py`, `tests/test_env_routes.py`.
**Modify:** `webapp/store.py`, `webapp/runs.py`, `webapp/main.py`, `webapp/templates/{index,migration,run,analysis}.html`, `webapp/static/app.js`, `README.md`, `pyproject.toml` (version bump).

---

## Phase A — core pipeline

### Task 1: Store `audit_type`

**Files:** Modify `webapp/store.py`; Test `tests/test_store.py` (append).

- [ ] **Step 1: failing test**
```python
# tests/test_store.py (append)
def test_audit_type_column(tmp_path):
    from webapp.store import Store
    s = Store(str(tmp_path / "at.db"), str(tmp_path / "at.key"))
    mid = s.create_migration("Acme env", product="jira", audit_type="environment")
    assert s.get_migration(mid)["audit_type"] == "environment"
    mid2 = s.create_migration("Acme mig")
    assert s.get_migration(mid2)["audit_type"] == "migration"   # default
```
- [ ] **Step 2:** `python3 -m pytest tests/test_store.py -k audit_type -q` → FAIL.
- [ ] **Step 3:** In `_SCHEMA`, add `audit_type TEXT NOT NULL DEFAULT 'migration'` to the `migrations` table. In `_migrate()` add (idempotent): `if "audit_type" not in cols("migrations"): self.db.execute("ALTER TABLE migrations ADD COLUMN audit_type TEXT NOT NULL DEFAULT 'migration'")`. Extend `create_migration`:
```python
def create_migration(self, name: str, product: str = "jira",
                     audit_type: str = "migration") -> int:
    if product not in known_products():
        raise ValueError(f"unknown product {product!r}")
    if audit_type not in ("migration", "environment"):
        raise ValueError(f"unknown audit_type {audit_type!r}")
    return self._exec(
        "INSERT INTO migrations(name,product,audit_type,created_at) "
        "VALUES(?,?,?,?)", (name, product, audit_type, time.time())).lastrowid
```
- [ ] **Step 4:** PASS. **Step 5:** commit `feat(store): audit_type column`.

---

### Task 2: `gather_config`

**Files:** Create `auditor/envaudit/__init__.py`, `auditor/envaudit/gather.py`; Test `tests/test_env_gather.py`.

- [ ] **Step 1: failing test**
```python
# tests/test_env_gather.py
import httpx
from auditor.client import Connection, JiraClient
from auditor.envaudit.gather import gather_config


def mk(handler, deployment="cloud"):
    conn = Connection(auth_type="pat", site_url="https://t.atlassian.net",
                      deployment=deployment, email="a@b.c", api_token="x")
    return JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                      sleeper=lambda s: None)


def test_gather_cloud_collects_areas():
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/status"):
            return httpx.Response(200, json=[{"name": "Open"}, {"name": "Done"}])
        if p.endswith("/field"):
            return httpx.Response(200, json=[
                {"id": "customfield_1", "name": "Severity", "custom": True,
                 "schema": {"custom": "...:select"}}])
        if "/search" in p or p.endswith("/workflow/search"):
            return httpx.Response(200, json={"values": [], "isLast": True})
        return httpx.Response(200, json={"values": [], "isLast": True})
    snap = gather_config(mk(handler), ["ACME"], progress=lambda m: None)
    assert "Open" in snap["areas"]["statuses"]["names"]
    assert snap["areas"]["custom_fields"]["names"] == ["Severity"]
    assert snap["deployment"] == "cloud"


def test_gather_dc_marks_skipped():
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/status"):
            return httpx.Response(200, json=[{"name": "Open"}])
        return httpx.Response(200, json=[])
    snap = gather_config(mk(handler, "dc"), ["ACME"], progress=lambda m: None)
    # workflow_schemes has no DC list API -> recorded skipped, never a false []
    assert snap["areas"]["workflow_schemes"]["skipped"] is True
```
- [ ] **Step 2:** FAIL (module missing).
- [ ] **Step 3:** Implement, reusing `config_audit`'s area list + capability gates. `gather.py`:
```python
"""Gather a single Jira environment's configuration into a snapshot (spec R3).

Reuses config_audit's area readers and capability honesty: DC areas with no
list API are recorded {skipped:True}, never a false empty. Names/counts only —
no issue data is read here."""
from __future__ import annotations
from typing import Callable
from ..config_audit import (SIMPLE, CLOUD_ONLY, DC_KEYS, _dc_list_sliced,
                            _names, _norm_name, _wf_name)


def _area(client, area, suffix, key):
    if client.conn.deployment == "dc":
        if area == "screens":
            return _dc_list_sliced(client, f"{client.api_prefix}{suffix}")
        key = DC_KEYS.get(area, key)
    return client.paginate_start_at(f"{client.api_prefix}{suffix}", key=key)


def gather_config(client, project_keys, progress: Callable[[str], None]):
    say = progress or (lambda m: None)
    dc = client.conn.deployment == "dc"
    areas: dict = {}
    for area, suffix, key in SIMPLE:
        if dc and area in CLOUD_ONLY:
            areas[area] = {"skipped": True, "reason": "no Data Center API"}
            say(f"[{area}] skipped (no DC API)"); continue
        rows, err = _area(client, area, suffix, key)
        areas[area] = {"names": sorted(set(_names(rows))), "count": len(rows or []),
                       "error": err} if not err else {"error": err, "names": [],
                       "count": 0}
        say(f"[{area}] {areas[area].get('count', 0)}")
    # custom fields (type-aware)
    cf, cferr = client.paginate_start_at(f"{client.api_prefix}/field")
    customs = [f for f in (cf or []) if f.get("custom") and f.get("name")]
    areas["custom_fields"] = {
        "names": sorted(f["name"] for f in customs), "count": len(customs),
        "by_type": {f["name"]: str((f.get("schema") or {}).get("custom", ""))
                    .split(":")[-1] for f in customs}, "error": cferr}
    # workflows (structure on cloud)
    if dc:
        wf, wferr = client.paginate_start_at(f"{client.api_prefix}/workflow")
        areas["workflows"] = {"names": sorted(_wf_name(w) for w in (wf or [])
                              if _wf_name(w)), "structure_checked": False,
                              "error": wferr}
    else:
        wf, wferr = client.paginate_start_at(
            f"{client.api_prefix}/workflow/search",
            params={"expand": "transitions,statuses"})
        areas["workflows"] = {"names": sorted(_wf_name(w) for w in (wf or [])
                              if _wf_name(w)), "structure_checked": True,
                              "detail": {_wf_name(w): {
                                  "statuses": [s.get("name") for s in (w.get("statuses") or [])],
                                  "transitions": [t.get("name") for t in (w.get("transitions") or [])]}
                                  for w in (wf or []) if _wf_name(w)}, "error": wferr}
    say("[workflows] done")
    return {"deployment": client.conn.deployment, "projects": list(project_keys),
            "areas": areas}
```
(If `_names`/`_norm_name`/`_wf_name` are private in config_audit, import them; they exist there.)
- [ ] **Step 4:** PASS. **Step 5:** commit `feat(envaudit): gather environment config snapshot`.

---

### Task 3: deterministic health checks

**Files:** Create `auditor/envaudit/checks.py`; Test `tests/test_env_checks.py`.

- [ ] **Step 1: failing tests**
```python
# tests/test_env_checks.py
from auditor.envaudit.checks import run_checks


def _snap(**areas):
    base = {"deployment": "cloud", "projects": ["ACME"], "areas": {}}
    base["areas"].update(areas); return base


def test_duplicate_field_detected():
    snap = _snap(custom_fields={"names": ["Severity", "severity ", "Team"],
                 "count": 3, "by_type": {}})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "duplicate_field" in kinds


def test_empty_screen_and_workflow_no_transitions():
    snap = _snap(
        screens={"names": ["Default"], "count": 1, "fields": {"Default": []}},
        workflows={"names": ["WF"], "structure_checked": True,
                   "detail": {"WF": {"statuses": ["Open"], "transitions": []}}})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "empty_screen" in kinds and "workflow_no_transitions" in kinds


def test_skipped_area_yields_capability_gap_not_false_clean():
    snap = _snap(workflow_schemes={"skipped": True, "reason": "no DC API"})
    fs = run_checks(snap)
    assert any(f["kind"] == "capability_gap" and f["name"] == "workflow_schemes"
               for f in fs)


def test_unused_custom_field_when_screen_membership_known():
    snap = _snap(
        custom_fields={"names": ["Severity", "Team"], "count": 2, "by_type": {}},
        screens={"names": ["S"], "count": 1, "fields": {"S": ["Severity"]}})
    fs = run_checks(snap)
    assert any(f["kind"] == "unused_custom_field" and f["name"] == "Team"
               for f in fs)
```
- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** Implement — one pure helper per rule, each skipping when its source area is `skipped`/`error`:
```python
"""Deterministic health-check rules over a gathered config snapshot (spec R4).

Each rule reads only snapshot metadata. A rule whose source area is skipped or
errored emits a capability_gap (honest 'not evaluated'), never a false clean."""
from __future__ import annotations
from ..config_audit import _norm_name

def _area(snap, name):
    return (snap.get("areas") or {}).get(name) or {}

def _evaluable(a):
    return bool(a) and not a.get("skipped") and not a.get("error")

def _f(area, name, kind, severity, **detail):
    return {"area": area, "name": name, "kind": kind, "severity": severity,
            "detail": detail}

def run_checks(snap: dict) -> list[dict]:
    out: list[dict] = []
    areas = snap.get("areas") or {}
    # capability_gap for every skipped/errored area (honest coverage)
    for name, a in areas.items():
        if isinstance(a, dict) and (a.get("skipped") or a.get("error")):
            out.append(_f(name, name, "capability_gap", "info",
                          reason=a.get("reason") or a.get("error")))
    cf = _area(snap, "custom_fields")
    if _evaluable(cf):
        seen = {}
        for nm in cf.get("names", []):
            k = _norm_name(nm)
            if k in seen:
                out.append(_f("custom_fields", nm, "duplicate_field", "medium",
                              collides_with=seen[k]))
            else:
                seen[k] = nm
        scr = _area(snap, "screens")
        if _evaluable(scr) and scr.get("fields"):
            on_screen = {x for fields in scr["fields"].values() for x in fields}
            for nm in cf.get("names", []):
                if nm not in on_screen:
                    out.append(_f("custom_fields", nm, "unused_custom_field",
                                  "low", note="on no screen"))
    scr = _area(snap, "screens")
    if _evaluable(scr) and scr.get("fields"):
        for nm, fields in scr["fields"].items():
            if not fields:
                out.append(_f("screens", nm, "empty_screen", "low"))
    wf = _area(snap, "workflows")
    if _evaluable(wf) and wf.get("structure_checked") and wf.get("detail"):
        for nm, d in wf["detail"].items():
            if d.get("statuses") and not d.get("transitions"):
                out.append(_f("workflows", nm, "workflow_no_transitions",
                              "high", statuses=len(d["statuses"])))
    return out
```
(Add `status_not_in_workflow`, `scheme_unused`, `project_missing_scheme` similarly when the relevant areas are evaluable — each gated by `_evaluable`. Keep each behind a test.)
- [ ] **Step 4:** PASS. **Step 5:** commit `feat(envaudit): deterministic health-check rules`.

---

### Task 4: AI analysis (privacy + analyze)

**Files:** Create `auditor/envaudit/analysis.py`; Test `tests/test_env_analysis.py`.

- [ ] **Step 1: failing tests** (reuse the fake-client shape from `tests/test_solutions.py`)
```python
# tests/test_env_analysis.py
from auditor.envaudit.analysis import summarize_for_ai, analyze


class _Block:
    def __init__(self, type, **kw):
        self.type = type
        for k, v in kw.items(): setattr(self, k, v)
class _Resp:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content; self.stop_reason = stop_reason; self.stop_details = None
class _Msgs:
    def __init__(self, responses): self._r = list(responses); self.calls = []
    def create(self, **kw): self.calls.append(kw); return self._r.pop(0)
class _Client:
    def __init__(self, responses): self.messages = _Msgs(responses)


def test_summary_is_metadata_only():
    snap = {"deployment": "cloud", "projects": ["ACME"], "areas": {
        "statuses": {"names": ["Open"], "count": 1},
        "custom_fields": {"names": ["Severity"], "count": 1,
                          "by_type": {"Severity": "select"},
                          "secret_values": ["SECRET customer value"]}}}
    findings = [{"area": "custom_fields", "name": "Severity",
                 "kind": "unused_custom_field", "severity": "low"}]
    s = summarize_for_ai(snap, findings)
    text = str(s)
    assert "Severity" in text and "unused_custom_field" in text
    assert "SECRET customer value" not in text   # PRIVACY: values never leak


def test_analyze_parses_assessment():
    js = ('{"health_score": 72, "grade": "B", "summary": "ok", '
          '"themes": [{"title": "Field sprawl", "why": "x", "severity": "medium", '
          '"recommendation": "merge", "related": ["custom_fields/Severity"]}], '
          '"top_risks": ["r"], "quick_wins": ["w"]}')
    out = analyze({"areas": {}}, [], _Client([_Resp([_Block("text", text=js)])]))
    assert out["error"] is None and out["health_score"] == 72
    assert out["themes"][0]["title"] == "Field sprawl"


def test_analyze_no_key_returns_skipped():
    out = analyze({"areas": {}}, [], None)
    assert out["skipped"] is True and out["error"] is None


def test_analyze_refusal():
    out = analyze({"areas": {}}, [], _Client([_Resp([], stop_reason="refusal")]))
    assert out["themes"] == [] and "declin" in (out["error"] or "").lower()
```
- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** Implement. `summarize_for_ai` whitelists ONLY metadata keys (names/count/by_type/structure + findings) — never iterates unknown keys like `secret_values`. `analyze` mirrors `solutions.find_solutions` (no web search; system prompt asks for the JSON assessment; pause_turn/refusal/typed-error handling; `client is None → {skipped:True}`):
```python
"""AI environment assessment (spec R5). Metadata-only outbound boundary."""
from __future__ import annotations
import json, os

def summarize_for_ai(snap: dict, findings: list) -> dict:
    areas_out = {}
    for area, a in (snap.get("areas") or {}).items():
        if not isinstance(a, dict): continue
        entry = {"count": a.get("count"), "names": (a.get("names") or [])[:200]}
        if a.get("skipped"): entry["skipped"] = True
        if a.get("by_type"): entry["by_type"] = a["by_type"]
        if a.get("structure_checked") is not None:
            entry["structure_checked"] = a["structure_checked"]
        # workflow status/transition NAMES only (no values/bodies)
        if a.get("detail") and area == "workflows":
            entry["workflows"] = {k: {"statuses": v.get("statuses"),
                                      "transitions": v.get("transitions")}
                                  for k, v in a["detail"].items()}
        areas_out[area] = entry
    return {"deployment": snap.get("deployment"),
            "projects": snap.get("projects"), "areas": areas_out,
            "findings": [{"area": f.get("area"), "name": f.get("name"),
                          "kind": f.get("kind"), "severity": f.get("severity")}
                         for f in findings]}

_SYSTEM = ("You are a senior Atlassian Jira administrator auditing a single "
           "environment's configuration health. You are given ONLY configuration "
           "metadata (object names, counts, workflow structure) and a list of "
           "rule-based findings — never any issue content. Identify the most "
           "important inconsistencies, risks, and root causes, and give "
           "prioritized, actionable recommendations. Reply with ONLY a JSON "
           "object: {\"health_score\": 0-100, \"grade\": \"A\"-\"F\", \"summary\": "
           "str, \"themes\": [{\"title\": str, \"why\": str, \"severity\": "
           "\"high\"|\"medium\"|\"low\", \"recommendation\": str, \"related\": "
           "[\"area/name\"]}], \"top_risks\": [str], \"quick_wins\": [str]}.")

def _text(resp):
    return "".join(b.text for b in resp.content
                   if getattr(b, "type", None) == "text" and getattr(b, "text", None))

def analyze(snap: dict, findings: list, client, *, model=None, effort="medium") -> dict:
    if client is None:
        return {"skipped": True, "error": None, "health_score": None,
                "grade": None, "summary": "AI analysis skipped (no Anthropic key).",
                "themes": [], "top_risks": [], "quick_wins": [], "model": None}
    import anthropic
    model = model or os.environ.get("MA_SOLUTIONS_MODEL", "claude-opus-4-8")
    summary = summarize_for_ai(snap, findings)
    messages = [{"role": "user", "content":
                 "Audit this Jira environment configuration:\n"
                 + json.dumps(summary, default=str)}]
    try:
        for _ in range(5):
            resp = client.messages.create(
                model=model, max_tokens=4000, system=_SYSTEM,
                thinking={"type": "adaptive"},
                output_config={"effort": effort}, messages=messages)
            if getattr(resp, "stop_reason", None) == "refusal":
                return {"skipped": False, "error": "the model declined this request",
                        "health_score": None, "grade": None, "summary": "",
                        "themes": [], "top_risks": [], "quick_wins": [], "model": model}
            if getattr(resp, "stop_reason", None) == "pause_turn":
                messages = messages + [{"role": "assistant", "content": resp.content}]
                continue
            break
        return _parse(_text(resp), model)
    except anthropic.APIError as exc:
        return {"skipped": False, "error": f"AI analysis failed: {exc}",
                "health_score": None, "grade": None, "summary": "",
                "themes": [], "top_risks": [], "quick_wins": [], "model": model}

def _parse(text, model):
    base = {"skipped": False, "error": None, "model": model, "themes": [],
            "top_risks": [], "quick_wins": [], "health_score": None,
            "grade": None, "summary": ""}
    try:
        d = json.loads(text[text.index("{"):text.rindex("}") + 1])
        base.update({k: d.get(k, base[k]) for k in
                     ("health_score", "grade", "summary", "themes",
                      "top_risks", "quick_wins")})
    except (ValueError, json.JSONDecodeError):
        base["summary"] = text[:1500].strip() or "No structured assessment returned."
    return base
```
- [ ] **Step 4:** PASS. **Step 5:** commit `feat(envaudit): metadata-only AI assessment`.

---

### Task 5: report / verdict

**Files:** Create `auditor/envaudit/report.py`; Test `tests/test_env_report.py`.

- [ ] **Step 1: failing tests**
```python
# tests/test_env_report.py
from auditor.envaudit.report import build_env_summary


def test_high_severity_is_critical():
    findings = [{"area": "workflows", "name": "WF", "kind": "workflow_no_transitions",
                 "severity": "high"}]
    out = build_env_summary(findings, {"skipped": True, "health_score": None})
    assert out["verdict"] == "CRITICAL"


def test_only_advisories_is_healthy_with_notes():
    findings = [{"area": "custom_fields", "name": "X", "kind": "unused_custom_field",
                 "severity": "low"}]
    out = build_env_summary(findings, {"skipped": False, "grade": "A", "health_score": 95})
    assert out["verdict"] == "HEALTHY_WITH_NOTES"
    assert out["stats"]["health_score"] == 95


def test_clean_is_healthy():
    out = build_env_summary([], {"skipped": True, "health_score": None})
    assert out["verdict"] == "HEALTHY"
```
- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** Implement:
```python
"""Environment-audit run summary: verdict ladder + stats + headlines (R6)."""
from __future__ import annotations
from collections import Counter

def build_env_summary(findings: list, ai: dict) -> dict:
    sev = Counter(f.get("severity") for f in findings)
    kinds = Counter(f.get("kind") for f in findings)
    grade = (ai or {}).get("grade")
    if sev.get("high"):
        verdict = "CRITICAL"
    elif sev.get("medium") or grade in ("D", "F"):
        verdict = "NEEDS_ATTENTION"
    elif sev.get("low"):
        verdict = "HEALTHY_WITH_NOTES"
    else:
        verdict = "HEALTHY"
    headlines = []
    if (ai or {}).get("summary") and not ai.get("skipped"):
        headlines.append(ai["summary"])
    if sev.get("high"):
        headlines.append(f"{sev['high']} high-severity configuration issue(s) "
                         f"need attention.")
    caps = kinds.get("capability_gap", 0)
    if caps:
        headlines.append(f"{caps} area(s) could not be evaluated (no Data Center "
                         f"API) — coverage is partial.")
    if not headlines:
        headlines.append("No configuration issues detected.")
    return {"verdict": verdict, "headlines": headlines,
            "stats": {"findings": len(findings), "high": sev.get("high", 0),
                      "medium": sev.get("medium", 0), "low": sev.get("low", 0),
                      "capability_gaps": caps,
                      "by_kind": dict(kinds),
                      "health_score": (ai or {}).get("health_score"),
                      "grade": grade, "ai_skipped": bool((ai or {}).get("skipped"))}}
```
- [ ] **Step 4:** PASS. **Step 5:** commit `feat(envaudit): run summary + verdict ladder`.

---

## Phase B — engine, web, UI, repositioning

### Task 6: env stages + run engine

**Files:** Create `webapp/env_stages.py`; Modify `webapp/runs.py`; Test `tests/test_env_run.py`.

- [ ] **Step 1: failing test** — an env-audit run through RunEngine with injected stages, asserting `_finalize_env` persists findings + verdict + ai stats. Mirror `tests/test_fix_run.py::test_fix_run_uses_fix_phases_and_fix_finalize`:
```python
# tests/test_env_run.py
import time, json
from webapp.store import Store
from webapp.runs import RunEngine

def _wait(store, rid, t=5):
    end = time.time() + t
    while time.time() < end:
        r = store.get_run(rid)
        if r["status"] in ("done", "failed", "cancelled"): return r
        time.sleep(0.02)
    raise AssertionError("run did not finish")

def test_env_run_phases_and_finalize(tmp_path):
    store = Store(str(tmp_path / "e.db"), str(tmp_path / "e.key"))
    mid = store.create_migration("env", audit_type="environment")
    seen = []
    env_stages = {
        "verify": lambda ctx: seen.append("verify"),
        "scope": lambda ctx: None,
        "gather": lambda ctx: ctx.update(snapshot={"areas": {}}),
        "checks": lambda ctx: ctx.update(env_findings=[
            {"area": "workflows", "name": "WF", "kind": "workflow_no_transitions",
             "severity": "high", "detail": {}}]),
        "analysis": lambda ctx: ctx.update(ai={"skipped": True, "health_score": None}),
    }
    engine = RunEngine(store, str(tmp_path / "ws"), stages={}, env_stages=env_stages)
    rid = engine.start(mid, {}, kind="env_audit")
    r = _wait(store, rid)
    assert r["status"] == "done" and r["verdict"] == "CRITICAL" and "verify" in seen
    rows = store.query_config(rid, "workflows")
    assert rows and rows[0]["kind"] == "workflow_no_transitions"
```
- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** In `runs.py` add `ENV_PHASES = ["verify","scope","gather","checks","analysis","finalize"]`; `__init__` accepts `env_stages=None`; `_execute` selects `env_stages` + `ENV_PHASES` when `kind=="env_audit"` and calls `_finalize_env`:
```python
def _finalize_env(self, ctx):
    from auditor.envaudit.report import build_env_summary
    store, run_id = ctx["store"], ctx["run_id"]
    findings = ctx.get("env_findings", [])
    ai = ctx.get("ai", {"skipped": True})
    summary = build_env_summary(findings, ai)
    if findings:
        store.insert_findings_config(run_id, findings)
    stats = dict(summary["stats"]); stats["headlines"] = summary["headlines"]
    stats["ai"] = ai
    store.update_run(run_id, status="done", verdict=summary["verdict"], stats=stats)
    store.add_event(run_id, "finalize", "info", f"env audit complete: {summary['verdict']}")
```
`env_stages.py` real stages: `stage_env_verify` (build one client from the source connection via `build_clients(..., require_both=False)`; verify), `stage_env_scope` (list projects, set ctx selected), `stage_env_gather` (`gather_config(src, keys, progress)` → ctx['snapshot']), `stage_env_checks` (`run_checks(snapshot)` → ctx['env_findings']), `stage_env_analysis` (`analyze(snapshot, findings, anthropic_client(store))` → ctx['ai']). `build_env_stages()` returns the dict. Wire `env_stages=build_env_stages()` into the engine in `main.py`.
- [ ] **Step 4:** PASS (full suite green). **Step 5:** commit `feat(envaudit): env-audit run flow + finalize`.

---

### Task 7: routes — create env audit + start run

**Files:** Modify `webapp/main.py`; Test `tests/test_env_routes.py`.

- [ ] **Step 1: failing test** — POST `/migrations` with `audit_type=environment` creates an env project; POST `/migrations/{id}/env-runs` starts an `env_audit` run (engine stubbed/monkeypatched) and redirects to the run page; an env project needs only the source connection.
```python
# tests/test_env_routes.py
import httpx
from fastapi.testclient import TestClient
from webapp.main import create_app
from webapp.config import Config

def _app(tmp_path):
    cfg = Config(data_dir=str(tmp_path), bind_host="127.0.0.1", bind_port=8484,
                 public_base_url="http://localhost:8484", secret_key=None)
    return create_app(cfg, http=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(404))))

def test_create_environment_audit(tmp_path):
    app = _app(tmp_path); c = TestClient(app)
    r = c.post("/migrations", data={"name": "Acme env", "product": "jira",
               "audit_type": "environment"}, follow_redirects=False)
    assert r.status_code == 303
    mid = int(r.headers["location"].split("/")[-1])
    assert app.state.store.get_migration(mid)["audit_type"] == "environment"

def test_env_run_requires_source_only(tmp_path, monkeypatch):
    import webapp.main as m
    app = _app(tmp_path); store = app.state.store
    mid = store.create_migration("env", audit_type="environment")
    # no source connection -> error
    c = TestClient(app)
    r = c.post(f"/migrations/{mid}/env-runs", follow_redirects=False)
    assert r.status_code == 303 and "error" in r.headers["location"]
    store.save_connection(mid, "source", "pat", "https://acme.example",
                          {"token": "t", "email": "a@b.c"})
    started = {"n": 0}
    monkeypatch.setattr(app.state.engine, "start",
                        lambda mid_, params, **kw: (started.__setitem__("n", 1) or 7))
    r2 = c.post(f"/migrations/{mid}/env-runs", follow_redirects=False)
    assert r2.status_code == 303 and "/runs/7" in r2.headers["location"]
    assert started["n"] == 1
```
- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** Extend `create_migration` route to pass `audit_type` (Form, default "migration"). Add `POST /migrations/{mid}/env-runs`: require a source connection (else redirect with error), then `engine.start(mid, params, kind="env_audit")` → redirect `/runs/{rid}`. The connection-save route already supports a single side; the migration page renders a single connection panel when `audit_type=='environment'`.
- [ ] **Step 4:** PASS. **Step 5:** commit `feat(web): create + run environment audits`.

---

### Task 8: UI — platform landing + audit-type create + workflow strips

**Files:** Modify `webapp/templates/index.html`, `webapp/templates/migration.html`; Test `tests/test_env_routes.py` (append render asserts).

- [ ] **Step 1: failing test**
```python
# tests/test_env_routes.py (append)
def test_dashboard_offers_both_audit_types(tmp_path):
    app = _app(tmp_path)
    html = TestClient(app).get("/").text
    assert "Migration audit" in html and "Environment audit" in html
    assert "workflow" in html.lower()   # the steps strip is present
```
- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** `index.html`: a platform header + two entry cards (Migration audit / Environment audit), each with a one-line description and an ordered **workflow strip** (Migration: connect → scope → extract → compare → config → report; Environment: connect → gather config → health checks → AI analysis → report). The new-audit control offers an audit-type choice; environment audits route through the single-connection setup. `migration.html`: when `mig.audit_type=='environment'`, show a single "Environment" connection panel + a "Run environment audit" button posting to `/migrations/{id}/env-runs`, and an "Audit workflow" explainer listing the env steps. Reuse existing classes (glass styles apply automatically).
- [ ] **Step 4:** PASS. **Step 5:** commit `feat(ui): platform landing + environment-audit create + workflow strips`.

---

### Task 9: UI — env-audit run + analysis views + brand

**Files:** Modify `webapp/templates/run.html`, `webapp/templates/analysis.html`, `webapp/static/app.js`, `webapp/templates/base.html`; Test `tests/test_env_routes.py` (append).

- [ ] **Step 1: failing test**
```python
# tests/test_env_routes.py (append)
def test_env_analysis_renders_health(tmp_path):
    app = _app(tmp_path); store = app.state.store
    mid = store.create_migration("env", audit_type="environment")
    rid = store.create_run(mid, {}, kind="env_audit")
    store.update_run(rid, status="done", verdict="NEEDS_ATTENTION", stats={
        "health_score": 72, "grade": "B", "findings": 3, "high": 0, "medium": 1,
        "low": 2, "capability_gaps": 0, "by_kind": {"duplicate_field": 1},
        "headlines": ["Field sprawl detected."],
        "ai": {"skipped": False, "themes": [{"title": "Field sprawl",
               "severity": "medium", "recommendation": "merge", "why": "x"}],
               "top_risks": ["r"], "quick_wins": ["w"]}})
    html = TestClient(app).get(f"/runs/{rid}/analysis").text
    assert "72" in html and "audit_type" in html.lower() or "environment" in html.lower()
```
- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** The run page (`run.html`) shows env-audit phase labels (Connect / Gather config / Health checks / AI analysis / Report) with one-line descriptions when the run kind is `env_audit`. The analysis page passes `audit_type` to the template/JS; `app.js` adds an env-audit renderer: a **health score dial** (reuse the donut), verdict pill, findings grouped by area/severity (reuse the findings/kbadge classes), and an **AI assessment** section (summary, themes with recommendation, top risks, quick wins, and an "AI skipped — add a key in Settings" note when skipped). `base.html` brand updated to the platform name + "migration & environment audits" subtitle. The analysis route already returns `audit_type` via the migration row — surface it.
- [ ] **Step 4:** PASS. **Step 5:** commit `feat(ui): environment-audit run + analysis + platform brand`.

---

### Task 10: wiring, docs, e2e, full suite

**Files:** Modify `webapp/main.py` (pass `env_stages=build_env_stages()`), `README.md`, `pyproject.toml`; Test `tests/test_env_run.py` (append e2e).

- [ ] **Step 1: failing e2e test** — a full env-audit run through the REAL `build_env_stages` with a MockTransport Jira (cloud) seeded with a duplicate field + a transition-less workflow + a missing Anthropic key, asserting the run finishes with findings and `ai.skipped` true and a non-CRITICAL-or-CRITICAL verdict consistent with the seeded data. (Patch `build_clients` in env_stages to a MockTransport `JiraClient`, like `test_fix_run.py` does.)
- [ ] **Step 2:** FAIL until wiring done.
- [ ] **Step 3:** In `create_app`, build the engine with `env_stages=build_env_stages()`. README: a "## Audits" section repositioning the product (Migration audit + Environment audit), the env-audit workflow steps, the AI metadata-only boundary, the optional AI key, Jira-only v1. Bump `pyproject` version to 0.3.0 and broaden the description to "Atlassian audit platform — migration & environment audits".
- [ ] **Step 4:** `python3 -m pytest -q` → PASS (target ≥ 450 tests). **Step 5:** commit `feat: wire env-audit engine + docs + e2e (v0.3.0)`.

---

## Self-review notes
- Spec coverage: R1→T1, R2→T6, R3→T2, R4→T3, R5→T4, R6→T5, R7→T6, R8→T1/T6, R9→T7/T9, R10→T8/T9, R11→all tests. R-PLATFORM→T8/T9, R-WORKFLOW→T8/T9.
- Privacy: `summarize_for_ai` whitelists metadata keys only (T4 leak test with a `secret_values` field proves it). The AI pass is optional (no-key skip, T4).
- Reuse: gather/checks import config_audit helpers; env run reuses RunEngine + findings_config + analysis shell; AI reuses the anthropic client + solutions.py error discipline.
- Type consistency: snapshot = `{deployment, projects, areas:{<area>:{names,count,...}}}`; findings = `{area,name,kind,severity,detail}`; ai = `{skipped,error,health_score,grade,summary,themes,top_risks,quick_wins,model}`; used identically across checks/analysis/report/finalize/UI.
