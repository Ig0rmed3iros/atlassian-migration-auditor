# Environment Audit: AI-powered single-environment health & consistency audit

**Status:** approved for build (user directive: reposition the product, build the feature, autonomous — no brainstorm)
**Date:** 2026-06-13
**Builds on:** the audit engine (connectors, RunEngine phase machine, Store, config_audit readers, analysis UI) and the Find Solutions feature (the `anthropic` SDK client + Fernet key + metadata-only AI boundary).

## 1. Problem & repositioning

Today the app does exactly one thing — audit a Jira/Confluence **migration** (source → target). The product is being repositioned: **migration audit becomes one feature among several**, and the second flagship feature is an **Environment Audit** — point at a *single* live Jira environment (Cloud or DC) and run an **AI-powered** audit that gathers its configuration, runs deterministic health checks, and has Claude analyze the whole picture to surface inconsistencies, risks, root causes, and prioritized recommendations. "Like the migration audit, but on one live environment."

Two cross-cutting requirements from the directive:
- **R-PLATFORM:** the app presents as a multi-feature **audit platform** (a landing/dashboard offering "Migration audit" and "Environment audit"), not a migration-only tool. Existing migration flows keep working unchanged.
- **R-WORKFLOW:** every audit shows a **clear, visible workflow** of the steps it follows (connect → gather → checks → AI analysis → report), both as live run progress and as an explainer.

## 2. Requirements

### Data model & back-compat
- **R1 — audit type.** `migrations` rows gain `audit_type TEXT NOT NULL DEFAULT 'migration'` (the entity is now a generic *audit project*). An **environment audit** project has `audit_type='environment'` and exactly one connection (role `'source'` = the live environment; no target). Existing rows default to `migration`. `_migrate()` upgrades in place.
- **R2 — env-audit run kind.** Environment-audit runs are RunEngine runs of `kind='env_audit'` with phases `verify → scope → gather → checks → analysis → finalize`. The audit/fix run kinds are untouched. v1 scope is **Jira only** (Cloud + DC); Confluence environment audit is a documented non-goal.

### The pipeline
- **R3 — gather.** `auditor/envaudit/gather.py::gather_config(client, project_keys, progress)` pulls the environment's configuration via the existing `JiraClient` and `config_audit` readers — statuses, issue types, priorities, resolutions, link types, roles, screens (+tabs/fields where readable), screen schemes, workflow schemes, field configurations, custom fields (+contexts/options on Cloud), workflows (+transitions/statuses on Cloud), and per-project JSM request types/queues — into a single **config snapshot** dict. Honest about deployment: DC areas with no list API are recorded as `skipped` (reuse `config_audit`'s CLOUD_ONLY / capability gates). Count-verified where possible; loud `area_error` on an unreachable area (never silent).
- **R4 — deterministic health checks.** `auditor/envaudit/checks.py::run_checks(snapshot)` runs rule-based checks over the snapshot, emitting findings `{area, name, kind, severity, detail}`. v1 rules (each with a test):
  - `unused_custom_field` — a custom field on no screen (Cloud, where screen membership is readable).
  - `duplicate_field` — two custom fields whose normalized names collide.
  - `status_not_in_workflow` — a status defined but in no workflow (Cloud).
  - `workflow_no_transitions` — a workflow with statuses but zero transitions (Cloud, structure-readable).
  - `unused_status` *(advisory)* — a status not referenced by any workflow scheme/project (Cloud).
  - `scheme_unused` — a screen/workflow/field-configuration scheme not associated with any project (Cloud).
  - `project_missing_scheme` — a project with no workflow scheme association (Cloud).
  - `empty_screen` — a screen with no fields.
  - `capability_gap` — informational: an area was `skipped` on DC, so this check could not run (honest coverage, mirrors migration `skipped`).
  Each rule degrades to "not evaluated" when its source area is `skipped`/`area_error` rather than emitting a false clean.
- **R5 — AI analysis (metadata-only).** `auditor/envaudit/analysis.py::analyze(snapshot_summary, findings, client, *, model, effort)` sends a **metadata-only** summary (area counts, object names, the deterministic findings) to Claude (the `anthropic` SDK client from `webapp/anthropic_key.anthropic_client`) and returns a structured assessment: `{health_score: 0-100, grade: A-F, summary, themes: [{title, why, severity, recommendation, related: [finding refs]}], top_risks, quick_wins, model, error?}`. **Privacy boundary (hard, identical to Find Solutions):** only configuration metadata — area counts, object names, scheme/workflow/field names, the rule findings — leaves the machine. **Never** issue/page content, descriptions, comments, user PII, or any value data. Enforced in a `summarize_for_ai(snapshot, findings)` builder with a leak test. JSON requested via the prompt (not `output_config.format`); parsed defensively; `pause_turn`/`refusal`/typed-error handling and a no-key path identical to `solutions.py`. The AI pass is **optional** — if no Anthropic key is set the run still completes with the deterministic findings and an "AI analysis skipped (no key)" note; never blocks.
- **R6 — report/verdict.** `auditor/envaudit/report.py::build_env_summary(findings, ai)` → `{verdict, stats, headlines}`. Verdict ladder by worst deterministic severity + AI grade: `CRITICAL` (any high-severity structural finding) / `NEEDS_ATTENTION` (mediums / AI grade ≤ C) / `HEALTHY_WITH_NOTES` (only advisories) / `HEALTHY`. Stats: counts per check kind, areas evaluated/skipped, AI health score. Headlines: prose, AI-aware.

### Engine, store, web
- **R7 — run engine.** RunEngine gains the `env_audit` phase list + an `_finalize_env` builder (mirrors `_finalize_fix`): persists findings (reuse `findings_config` with `area`/`kind`), stores the AI report + stats in `stats_json`, sets the verdict. Env stages injected like audit/fix stages (`webapp/env_stages.py`).
- **R8 — store.** `audit_type` column + `create_migration(..., audit_type='migration')`; env-audit findings reuse `findings_config`; the AI report rides in run `stats_json` under `ai`. Back-compat: existing DBs upgrade; existing suite green.
- **R9 — web + routes.** Create-audit flow chooses the audit type. An environment-audit project configures one connection (reuse the PAT/deployment connection form, target hidden). `POST /migrations/{id}/env-runs` starts an `env_audit` run. The analysis page renders the env-audit result (health score, findings by area/severity, the AI themes/risks/quick-wins, evaluated/skipped coverage). Reuse the existing run-progress + analysis infrastructure; the analysis JS branches on audit type.
- **R10 — platform landing & workflow viz (R-PLATFORM + R-WORKFLOW).** The dashboard presents the two audit types as entry cards (Migration audit / Environment audit) with one-line descriptions and a "how it works" workflow strip per type (the ordered steps). The run page shows the live phase stepper labeled with env-audit step names + a short description per step. A static "Audit workflow" explainer (the connect→gather→checks→AI→report sequence) appears on the env-audit create page. App brand/title updated from "Migration Auditor" to an audit-platform name with a migration/environment subtitle.

### Quality
- **R11 — tests, no live API.** New tests: gather (MockTransport over the config endpoints, Cloud + DC honesty), each check rule over snapshot fixtures, `summarize_for_ai` privacy leak test, `analyze` (injected fake client: JSON parse, pause_turn, refusal, no-key skip), report verdict ladder, env-audit run end-to-end through RunEngine (MockTransport), store audit_type round-trip + back-compat, the env-run route, and the landing/analysis render. The existing suite stays green. **No live Anthropic or Jira calls in the suite.**

## 3. Architecture

```
auditor/envaudit/
  gather.py      gather_config(client, keys, progress) -> snapshot
  checks.py      run_checks(snapshot) -> [finding]; one pure function per rule
  analysis.py    summarize_for_ai(snapshot, findings) -> dict (metadata only);
                 analyze(summary, findings, client, ...) -> assessment
  report.py      build_env_summary(findings, ai) -> {verdict, stats, headlines}
webapp/
  env_stages.py  stage_env_verify/scope/gather/checks/analysis + build_env_stages
  runs.py        ENV_PHASES + _finalize_env (kind dispatch already present)
  store.py       audit_type column + create_migration param
  main.py        create-audit type select; env-run route; analysis context
  templates/     index landing (two types + workflow strips), audit create,
                 run (env step labels), analysis (env view); brand copy
  static/app.js  analysis renderer branches on audit_type; workflow strip
```

Reuse, don't duplicate: `JiraClient`, `config_audit` readers (extract the per-area fetchers so both migration parity and env gather call them), `RunEngine`, `Store`, `anthropic_key`, the analysis page shell, and the metadata-only AI discipline from `solutions.py`.

## 4. Privacy boundary (load-bearing)

The AI analysis is the only outbound path. `summarize_for_ai` assembles ONLY: per-area object **names**, **counts**, scheme/workflow/field/screen **names**, the boolean structure of workflows (status/transition **names**), and the deterministic findings (kind + names). It NEVER reads issue/page bodies, descriptions, comments, field **values**, user emails/PII, or anything from issue data. A unit test seeds a snapshot whose every text-bearing field contains a sentinel secret and asserts none of it appears in the summary. Same `pause_turn`/`refusal`/typed-error/no-key handling as Find Solutions; nothing raises into the run thread.

## 5. Non-goals (v1)

- Confluence environment audit (Jira only; documented).
- Auto-remediating environment findings (read-only assessment; the operator acts — remediation could be a fast-follow reusing the fix engine).
- Deep per-issue data mining (config + lightweight signals only; no issue-content analysis — also the privacy boundary).
- Historical trend tracking across runs (single point-in-time audit; re-run for a new snapshot).
- Scheduling/cron (manual, on-demand).

## 6. Acceptance

1. Full suite green (existing + new); no live API calls.
2. A simulated Jira Cloud environment audit (MockTransport through RunEngine): gather → checks produce the expected rule findings → AI analysis (fake client) attaches themes → finalize yields a verdict + health score; a DC environment records `skipped`/`capability_gap` honestly with no false-clean.
3. The privacy test: a snapshot full of sentinel secrets in value/body fields produces an AI summary containing the object names but NONE of the secrets.
4. No-key path: an env audit with no Anthropic key completes with deterministic findings and an "AI skipped" note (never errors/blocks).
5. UI: the dashboard presents both audit types with workflow strips; an environment audit can be created with one connection; the run page shows the labeled env-audit steps; the analysis page renders the health score, findings, and AI assessment.
6. Existing migration audit + fix + find-solutions behavior unchanged.
