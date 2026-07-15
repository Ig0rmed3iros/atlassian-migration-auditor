# Environment Audit — Broad & Deep (Comprehensive Jira Config Audit) — Design

**Date:** 2026-06-13
**Status:** approved-by-standing-instruction (user: "plan, execute, test, present")
**Builds on:** `2026-06-13-environment-audit.md` (the v1 env audit)

## Goal

Turn the Environment Audit from a focused 9-rule health check into a **comprehensive,
deep** audit of a live Jira instance: cover the full breadth of admin
configuration objects (custom fields → screens → schemes → groups → components →
versions → boards/filters/dashboards), detect **performance risks** and
**config mistakes** (not just simple ones), and attach a **suggested fix to every
single finding**.

## Non-negotiable invariants (carried from v1)

- **I1 — Metadata-only AI boundary.** Only object *names, counts, structural
  booleans, and holder TYPES* may leave the machine to Anthropic. NEVER issue
  content, field values, descriptions, comments, member identities, component
  lead names, grant holder values, emails, or accountIds. Enforced by the
  `summarize_for_ai` allowlist + a leak test per new area.
- **I2 — Never a false clean.** An unreachable/errored area emits `area_error`
  (warning) or `capability_gap` (info), never a silent pass. DC areas with no
  list API record `{skipped, reason}`.
- **I3 — Suggestion-first, consent-gated apply.** Every finding carries a
  suggested fix. Applying any change to the live environment stays behind the
  existing explicit-consent fix-run gate. Destructive/irreversible operations
  (delete field, delete status, edit live workflow) are **guide-only** in v1 —
  never auto-applied.
- **I4 — Server reconstructs from stored rows.** The browser never sends finding
  content back; the summary API rebuilds findings + fixes from `findings_config`.
- **I5 — Bounded gather.** Every new area that requires per-object or per-project
  calls is **capped** (like `_SCREEN_DEEP_CAP=60`) and records an honest
  `capped: true` flag when truncated, so partial coverage never reads as total.

## Requirements

### R1 — Broaden the gatherer (`auditor/envaudit/gather.py`)

Add the following areas to the snapshot. Cloud-first; on DC, any area whose API
is unavailable records `{skipped: True, reason: "..."}`. Each area is independent
— a failure in one records `error`/`skipped` and never aborts the snapshot.

| New area key | Source (Cloud) | Per-object data gathered (NO PII) | Cap |
|---|---|---|---|
| `custom_field_options` | `/field` + `/field/{id}/context` + `/field/{id}/context/{cid}/option` | per select-type field: `{contexts: int, options: int}` | first 80 select fields |
| `permission_scheme_grants` | `/permissionscheme?expand=permissions` | per scheme: `[{permission, holder_type}]` (holder_type ∈ group/projectRole/applicationRole/anyone/user/…; **NO holder value**) | all schemes |
| `groups` | `/group/bulk` (names + count); member counts via `/group/member?groupId=` | `{names:[...], count:int, member_counts:{name:int}, capped:bool}` — **NO member identities** | member-count probe first 60 groups |
| `components` | per project `/project/{key}/components` | per project: `[{name, has_lead:bool, assignee_type}]` — **NO lead name** | audited projects |
| `versions` | per project `/project/{key}/versions` | per project: `[{name, released:bool, archived:bool, overdue:bool}]` | audited projects |
| `boards` | `/rest/agile/1.0/board` | `{names:[...], count:int}` | capped 500 |
| `filters` | `/filter/search` (owned/visible) | `{count:int}` (sprawl signal only) | capped 500 |
| `dashboards` | `/dashboard` | `{count:int}` | capped 500 |

Also **extend `projects_using`** to `issuetype_screen_schemes` and
`issuetype_schemes` (the existing pattern only tracks workflow/screen/field-config
schemes) so unused-scheme detection covers those families too.

Reuse existing helpers (`SIMPLE`, `CLOUD_ONLY`, `_dc_list_sliced`, `_names`,
`paginate_start_at`, `_field_options`). Add new constants where a new area is
Cloud-only or DC-keyed.

### R2 — Deepen the checks (`auditor/envaudit/checks.py`)

Keep all 9 existing rules. Add two new families.

**Performance-risk rules** (the "deep"/"performance" thrust). Thresholds are
named constants at the top of the module, citing Atlassian instance-health
guidance in a comment.

| kind | severity | trigger |
|---|---|---|
| `field_sprawl` | medium (>`FIELD_WARN`=300) / high (>`FIELD_CRIT`=800) | `custom_fields.count` over threshold |
| `large_option_set` | low (>`OPT_WARN`=100) / medium (>`OPT_CRIT`=500) | a select field's `options` over threshold |
| `workflow_sprawl` | medium (>`WORKFLOW_WARN`=100) | `workflows` count over threshold |
| `status_sprawl` | low (>`STATUS_WARN`=100) | `statuses.count` over threshold |
| `screen_sprawl` | low (>`SCREEN_WARN`=300) | `screens.count` over threshold |
| `permission_scheme_sprawl` | low (>`PERMSCHEME_WARN`=50) | `permission_schemes.count` over threshold |

**Config-mistake rules** (deeper hygiene):

| kind | severity | trigger |
|---|---|---|
| `unused_issue_type_scheme` | low | issuetype scheme in `projects_using` used by no project |
| `unused_issue_type_screen_scheme` | low | issuetype screen scheme used by no project |
| `empty_group` | low | a group in `groups.member_counts` with 0 members (only among probed) |
| `version_overdue` | low | a version `released=false, overdue=true` |
| `version_archived_unreleased` | low | a version `archived=true, released=false` |
| `component_no_lead` | low | a component `has_lead=false` |
| `permission_grant_overly_broad` | medium | a scheme grants a sensitive permission (`ADMINISTER`, `ADMINISTER_PROJECTS`) to holder_type `anyone` |
| `migration_artifact` | medium | a config object name carries a migration suffix (`(migrated)`, `(migrated 2)`, ` - copy`) **and** collides (post-`_norm_name`) with another object in the same area — the fingerprint of a re-run/partial migration. → the `unfixable` tier (re-migrate). Scanned across `custom_fields`, `statuses`, `workflows`, scheme areas. |

Existing `scheme_unused` extends to the new scheme families via the same loop.
Every rule is gated by `_evaluable` on its source area(s).

### R3 — Suggested fix for every finding, in 3 tiers (`auditor/envaudit/fixes.py`)

Every finding is assigned to exactly ONE of three fix tiers (the user's taxonomy):

1. **`app` — Fixable by the App/AI.** The app itself can auto-fix it via a
   deterministic, reversible API call when the user selects it (consent-gated,
   see R8). Reserved for safe, mechanical operations on a live instance.
2. **`human` — Fixable by a human.** Needs human review/judgment or manual action
   in the Jira UI (e.g. screen configuration, which custom-field options to keep,
   workflow transition design, picking a component lead, choosing a release date,
   deciding which fields to cull). The audit tells the operator exactly what to do.
3. **`unfixable` — Re-migration suggested.** Rare cases where the environment was
   recently migrated and an object is corrupted/duplicated as a migration artifact;
   no in-place fix is faithful, so the audit recommends re-running the migration
   cleanly (reuses the existing detect-and-guide → re-migrate posture).

New module. `annotate_fixes(findings: list) -> None` mutates each finding in
place, adding a `fix` dict:

```python
{
  "tier": "app" | "human" | "unfixable",
  "tier_label": "Fixable by the app" | "Fixable by a human"
              | "Re-migration suggested",
  "title": str,            # "Delete the unused workflow scheme"
  "detail": str,           # operator-facing instruction / what the app will do
  "api_hint": str | None,  # e.g. "DELETE /rest/api/3/workflowscheme/{id}" (app tier)
  "risk": "low" | "medium" | "high",
  "reversible": bool,
  "caveat": str | None,    # e.g. "Confirm no automation references this field."
}
```

A `_FIXES: dict[kind -> fix-template]` registry maps **every** finding kind that
`run_checks` can emit to a fix. **Completeness contract (tested):** for every kind
emitted by `run_checks`, `_FIXES` has an entry — no finding is ever left without a
suggestion, and every entry's `tier` is one of the three values.

Tier assignment (v1):
- **`app`:** `scheme_unused`, `unused_issue_type_scheme`,
  `unused_issue_type_screen_scheme` (delete a scheme used by no project),
  `empty_group` (delete a 0-member group). All reversible by recreation.
- **`human`:** `duplicate_field`, `unused_custom_field`, `empty_screen`,
  `large_option_set`, `workflow_no_transitions`, `status_not_in_workflow`,
  `project_missing_scheme`, `component_no_lead`, `version_overdue`,
  `version_archived_unreleased`, `permission_grant_overly_broad`, all `*_sprawl`,
  `capability_gap`, `area_error` (restore access / verify manually).
- **`unfixable`:** `migration_artifact` (see R2) → re-run the migration.

`annotate_fixes` is called in `stage_env_checks` after `run_checks`, so the `fix`
rides on the finding into `findings_config` nested inside `detail` (see R5).

### R4 — Deepen the AI analysis (`auditor/envaudit/analysis.py`)

- Extend the `summarize_for_ai` allowlist to forward the NEW area metadata:
  counts, structural booleans, holder TYPES, `capped` flags, and the perf
  thresholds crossed. **NO** PII (no member identities, lead names, grant holder
  values, emails, accountIds). Forward finding `fix.tier`/`fix.title` so the AI
  can reference suggested remediations.
- Deepen `_SYSTEM` to instruct the model to reason about **performance**
  (field/workflow/status/screen sprawl, large option sets, scheme bloat),
  **configuration hygiene** (unused objects, orphans, leaderless components,
  overdue/archived versions), and **security posture** (overly-broad permission
  grants) — and to prioritize the highest-leverage cleanups.
- Return shape unchanged (health_score/grade/summary/themes/top_risks/quick_wins).
- Model resolution unchanged (reuse existing default; do not introduce new model
  strings).

### R5 — Persist & reconstruct findings + fixes (`webapp/store.py`, summary API)

- Findings carry `fix` and `detail`. Persist `fix` (JSON) alongside the finding.
  Reuse the existing `fix_payload` TEXT column on `findings_config` to store the
  env `fix` dict (it is currently NULL for env runs), OR nest `fix` inside
  `detail`. **Decision:** nest `fix` inside `detail` to avoid a schema change and
  keep `fix_payload` semantics (migration apply) unpolluted. `insert_findings_config`
  already serializes `detail` → `detail_json`.
- The env summary API (`/api/runs/{id}/summary`) must include the env findings —
  reconstructed from `findings_config` rows server-side (I4) — as a `findings`
  array of `{area, name, kind, severity, category, fix}` (metadata + fix only),
  grouped/annotated by **category** (see R6). Migration runs are unaffected.

### R6 — Categorize findings (Performance / Hygiene / Security / Coverage)

Each finding gets a `category` derived from its kind:
- **Performance** — `*_sprawl`, `large_option_set`.
- **Security** — `permission_grant_overly_broad`.
- **Hygiene** — duplicate/unused/empty/orphan/version/component/scheme rules.
- **Structure** — `workflow_no_transitions`, `status_not_in_workflow`,
  `project_missing_scheme`.
- **Coverage** — `capability_gap`, `area_error`.

A pure `category_for(kind)` helper (in `fixes.py` or `checks.py`). Surfaced in the
summary payload + UI grouping.

### R7 — Render (UI) — `webapp/static/app.js` EnvAnalysis + `analysis.html`

Extend the env analysis renderer to:
- Render the real **findings list from the summary `findings` array** (not just
  the headline strings), grouped by **category** with a count per category.
- Each finding row shows: severity badge, area/name, the **suggested fix** with a
  **tier badge** — "Fixable by the app" (green) / "Fixable by a human" (amber) /
  "Re-migration suggested" (red) — plus `title` + `detail` + `caveat`.
- A dedicated **Performance** group surfaces the sprawl/option findings.
- The fix screen (R8) renders `app`-tier fixes as selectable checkboxes and
  `human`/`unfixable` fixes as read-only guidance.
- Keep the health dial, verdict banner, and AI assessment section.
- All AI/finding text HTML-escaped before insertion (existing `esc`).

### R8 — App-tier apply (consent-gated)

Only **`app`-tier** fixes are auto-appliable; `human` fixes render as guidance and
`unfixable` fixes render as a re-migration recommendation. The app-tier apply
reuses the existing fix-run machinery (`kind="fix"`, `FIX_PHASES`) but with an
env-specific apply that:
- operates on the SAME live environment (the single source connection), guarded by
  the `expected_api_base` identity check (writes only ever hit the audited
  instance);
- handles only reversible deletes of unused schemes / empty groups (the app-tier
  set), each with an idempotent pre-check (already-gone → logged no-op) and a
  full per-call log;
- proves closure by re-reading the object list (gone → closed).

The fix screen groups suggestions by tier and only renders selectable checkboxes
for `app`-tier items; `human`/`unfixable` items are read-only guidance. v1 may
ship app-apply for the scheme/group cleanup set; if scope must be trimmed, the
guidance-only fallback for every tier still satisfies "suggest fixes" — but the
3-tier split, labels, and per-finding suggestions are a hard requirement.

### R9 — Honest coverage & caps

Every capped area records `capped: true`; every check that consumed a capped area
notes partial coverage. The report/headlines surface "coverage is partial" when
caps or skips occurred (extends the existing capability_gap headline).

### R10 — Tests

- gather: one test per new area (Cloud shape + DC skip + cap behavior + error
  preservation).
- checks: one test per new rule (positive + guard); thresholds.
- fixes: completeness test (every emitted kind has a fix; every `tier` ∈
  {app, human, unfixable}) + tier/category mapping + `migration_artifact` →
  `unfixable`, scheme/group cleanups → `app`, screen/option/sprawl → `human`.
- analysis: a leak test per new area proving PII never reaches `summarize_for_ai`;
  allowlist forwards the new metadata.
- store/API: env summary includes reconstructed findings + fix; migration summary
  unchanged.
- render: app.js source-guard tests (findings grouped by category; fix rendered).
- end-to-end: a MockTransport env run producing perf + hygiene + security findings
  with fixes, verifying verdict, stats, and that NO writes occur.

## Architecture / file map

- `auditor/envaudit/gather.py` — new area gatherers (R1).
- `auditor/config_audit.py` — new constants/helpers if a new Cloud-only/DC-keyed
  area is added; reuse `_field_options`.
- `auditor/envaudit/checks.py` — perf + hygiene + security rules; threshold consts (R2).
- `auditor/envaudit/fixes.py` — NEW: `annotate_fixes`, `_FIXES`, `category_for` (R3, R6).
- `auditor/envaudit/analysis.py` — allowlist + deeper prompt (R4).
- `auditor/envaudit/report.py` — headlines reflect new categories/coverage (R9).
- `webapp/env_stages.py` — call `annotate_fixes` in checks stage.
- `webapp/analysis.py` (summary route) — include reconstructed env findings (R5).
- `webapp/static/app.js` (EnvAnalysis) + `webapp/templates/analysis.html` — render (R7).
- `tests/test_env_*.py` — extend; add `tests/test_env_fixes.py` (R10).

## Out of scope (v1)

- Automation-rule deep audit (no stable public REST API) — recorded as a
  `capability_gap` with guidance.
- JSM request-type/queue audit, Confluence env audit.
- Live workflow editing / field deletion auto-apply (guide-only).
- Real runtime performance metrics (only static perf-risk heuristics from config).
