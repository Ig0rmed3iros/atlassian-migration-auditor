# Environment Audit — Broad & Deep — Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development. TDD every task.
> Steps use `- [ ]`. Spec: `docs/superpowers/specs/2026-06-13-environment-audit-deep.md`.

**Goal:** Comprehensive, deep Jira environment audit — broad object coverage,
performance + config-mistake + security detection, and a 3-tier suggested fix
(app / human / unfixable) on every finding.

**Architecture:** Extend the existing env pipeline (gather → checks → analysis →
report → render) in place. Preserve the metadata-only AI boundary and the
finding shape `{area,name,kind,severity,detail}`. Fixes nest in `detail.fix`.

**Tech:** Python 3.11, httpx MockTransport tests, FastAPI, vanilla JS, pytest.

---

## Phase A — Breadth: gather (one cohesive task, edits `gather.py` + `config_audit.py` consts + `tests/test_env_gather.py`)

### Task 1: New gather areas
**Contract:** extend `gather_config` so the snapshot `areas` dict gains the R1
areas. Cloud-first; DC → `{skipped, reason}`. Each area independent; errors
captured per-area (preserve the partial-row pattern). Caps per R1/I5 with a
`capped: true` flag when truncated.

New `areas` sub-shapes (literal):
```python
"permission_scheme_grants": {            # Cloud; DC skip
    "by_scheme": {"<scheme>": [{"permission": str, "holder_type": str}]},
    "count": int, "error": None|str }
"groups": {                              # Cloud; DC skip
    "names": [str], "count": int,
    "member_counts": {"<group>": int},   # only probed subset
    "capped": bool, "error": None|str }
"components": {                          # per audited project
    "by_project": {"<KEY>": [{"name": str, "has_lead": bool,
                              "assignee_type": str}]},
    "count": int, "error": None|str }
"versions": {                           # per audited project
    "by_project": {"<KEY>": [{"name": str, "released": bool,
                              "archived": bool, "overdue": bool}]},
    "count": int, "error": None|str }
"custom_field_options": {               # Cloud; DC skip; capped 80 select fields
    "by_field": {"<field>": {"contexts": int, "options": int}},
    "capped": bool, "error": None|str }
"boards":     {"names":[str], "count": int, "capped": bool, "error": None|str}
"filters":    {"count": int, "capped": bool, "error": None|str}
"dashboards": {"count": int, "capped": bool, "error": None|str}
```
Plus: extend the scheme `projects_using` capture to `issuetype_schemes` and
`issuetype_screen_schemes`.

- [ ] Tests first (`tests/test_env_gather.py`): one per area — Cloud shape via
  MockTransport, DC records `{skipped,reason}`, cap sets `capped=True`, mid-page
  error preserves partial + sets `error`. Permission grants forward holder_type
  but NEVER a holder value. Groups never include member identities.
- [ ] Implement in `gather.py`; add Cloud-only/DC-key constants to `config_audit.py`
  if needed; reuse `_field_options`, `paginate_start_at`, `_names`.
- [ ] Run `pytest tests/test_env_gather.py -q`; commit.

---

## Phase B — Depth: checks (edits `checks.py` + `tests/test_env_checks.py`)

### Task 2: Performance-risk rules
**Contract:** add threshold constants (cite Atlassian health guidance in a
comment) and rules: `field_sprawl`, `large_option_set`, `workflow_sprawl`,
`status_sprawl`, `screen_sprawl`, `permission_scheme_sprawl` (severities per
spec R2). Each gated by `_evaluable`.
- [ ] Tests: each rule fires above threshold, silent below, skipped when source
  area unevaluable. `field_sprawl` medium vs high at the two thresholds.
- [ ] Implement; run; commit.

### Task 3: Config-mistake + security rules
**Contract:** `unused_issue_type_scheme`, `unused_issue_type_screen_scheme`
(extend the `scheme_unused` loop), `empty_group`, `version_overdue`,
`version_archived_unreleased`, `component_no_lead`, `permission_grant_overly_broad`.
- [ ] Tests: positive + guard (unevaluable area) for each. `empty_group` only
  flags among probed groups. `permission_grant_overly_broad` only on
  sensitive-permission + holder_type `anyone`.
- [ ] Implement; run; commit.

### Task 4: Migration-corruption rule
**Contract:** `migration_artifact` (medium) — a name with a migration suffix
(`(migrated)`, `(migrated 2)`, ` - copy`) that, post-`_norm_name`, collides with
another object in the same area. Scan custom_fields, statuses, workflows, scheme
areas. This is the sole `unfixable` source.
- [ ] Tests: detects the migrated-duplicate pattern; does NOT fire on a lone
  `(migrated)` with no twin; reports each artifact once.
- [ ] Implement; run; commit.

---

## Phase C — Fixes: 3 tiers (NEW `auditor/envaudit/fixes.py` + `tests/test_env_fixes.py` + wire `env_stages.py`)

### Task 5: `annotate_fixes`, `_FIXES`, `category_for`
**Contract:** per spec R3/R6. `annotate_fixes(findings)` adds `detail["fix"]` (the
fix dict) and `detail["category"]` to each finding. `_FIXES` maps EVERY kind from
`run_checks` to a fix template with tier ∈ {app, human, unfixable}. `category_for`
maps kind → Performance/Security/Hygiene/Structure/Coverage.
- [ ] Test (completeness): collect every `kind` string `run_checks` can emit
  (enumerate from checks.py); assert `_FIXES` covers each and every tier is valid.
- [ ] Tests: scheme/group cleanups → `app`; screen/option/sprawl/version/component/
  permission/duplicate/unused-field → `human`; `migration_artifact` → `unfixable`;
  `capability_gap`/`area_error` → `human` (verify-manually). category mapping spot
  checks.
- [ ] Implement; wire `annotate_fixes` into `stage_env_checks` after `run_checks`.
- [ ] Run `pytest tests/test_env_fixes.py tests/test_env_checks.py -q`; commit.

---

## Phase D — AI depth + privacy (edits `analysis.py` + `tests/test_env_analysis.py`)

### Task 6: Allowlist extension + deeper prompt
**Contract:** extend `summarize_for_ai` to forward the new area metadata (counts,
structural booleans, holder TYPES, `capped` flags, sprawl thresholds crossed,
finding `fix.tier`) — NO PII. Deepen `_SYSTEM` to reason about performance,
hygiene, and security and to prioritize high-leverage cleanups. Return shape
unchanged; model resolution unchanged.
- [ ] Leak test PER new area: inject a fake member identity / lead name / grant
  holder value / email into the snapshot; assert the JSON payload to the AI
  contains the count/type but NOT the injected secret.
- [ ] Test: allowlist forwards the new metadata keys; `analyze` still parses.
- [ ] Implement; run `pytest tests/test_env_analysis.py -q`; commit.

---

## Phase E — Persist / API / Render

### Task 7: Env summary includes reconstructed findings (edits `webapp/analysis.py` summary route + `tests/test_env_routes.py`)
**Contract:** for an env run, the `/api/runs/{id}/summary` response gains a
`findings` array rebuilt SERVER-SIDE from `findings_config` rows (I4):
`[{area, name, kind, severity, category, fix}]` (metadata + fix only). Migration
runs unchanged.
- [ ] Test: env summary includes `findings` with fix.tier + category; a migration
  summary has no env `findings` array (unchanged).
- [ ] Implement; run; commit.

### Task 8: Render findings + fix tiers (edits `webapp/static/app.js` EnvAnalysis + `analysis.html` + source-guard tests in `tests/test_main.py`)
**Contract:** EnvAnalysis renders the `findings` array grouped by `category`
(with a Performance group), each row showing the severity badge + fix tier badge
("Fixable by the app/human" / "Re-migration suggested") + title/detail/caveat.
All text HTML-escaped.
- [ ] Source-guard tests: app.js EnvAnalysis reads `summary.findings`, groups by
  category, renders `fix.tier_label`, escapes text.
- [ ] Implement; run full suite; commit.

### Task 9: Headlines reflect categories/coverage (edits `report.py` + `tests/test_env_report.py`)
**Contract:** headlines mention performance/security findings and partial coverage
(caps/skips) per R9. Verdict ladder unchanged.
- [ ] Tests; implement; run; commit.

---

## Phase F — App-tier apply (stretch) + integration

### Task 10: App-tier env apply (edits fix machinery; gated) — STRETCH
**Contract:** per R8 — `app`-tier fixes (delete unused scheme / empty group) on
the single live connection, `expected_api_base`-guarded, idempotent, logged,
closure by re-read. Fix screen renders app-tier as checkboxes, human/unfixable as
read-only guidance. If scope must trim, ship guidance-only (still satisfies the
3-tier suggestion requirement) and record the gap.
- [ ] Tests (MockTransport): app-tier delete applies + logs + closes; human/
  unfixable never auto-apply; identity guard rejects a mismatched base.
- [ ] Implement; run; commit.

### Task 11: End-to-end + final review
- [ ] MockTransport env run producing performance + hygiene + security + corruption
  findings, each with a 3-tier fix; assert verdict, stats.by_kind, findings persisted
  with fix+category, and that NO writes occur during the AUDIT.
- [ ] Full `pytest -q` green.
- [ ] Dispatch adversarial review (privacy leak sweep on Opus + correctness +
  no-false-clean + tier-completeness). Fix all findings.
- [ ] Re-seed demo with rich findings; restart server; browser verify; push; update PR.

---

## Self-review checklist
- Every R1 area has a gather test; every R2 rule a check test; `_FIXES` covers every
  kind (completeness test); every new area has a leak test.
- No PII in the AI payload. No false-clean. Caps flagged. Migration findings/summary
  unaffected. Finding shape preserved; fix nested in `detail`.
