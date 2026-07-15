# Confluence Environment Audit — Design

**Date:** 2026-06-14
**Goal:** Make the Environment Audit work for **Confluence** instances too, so the
app is the go-to tool for Jira AND Confluence admins. Point at one live Confluence
site (Cloud or DC) and audit spaces, permissions, content health, and hygiene —
mirroring the Jira env audit's gather → checks → 3-tier fixes → AI → render flow.
Catalog: `docs/superpowers/confluence-env-audit-catalog.md` (31 kinds + 6 gaps).

## Architecture — product dispatch (minimal, reuses everything)

The env pipeline is already product-agnostic EXCEPT gather/checks/analysis, which
assume Jira. Dispatch on `ctx["connector"].product` (set in `stage_env_verify`):

- `webapp/env_stages.py`:
  - `stage_env_gather` → `confluence_gather.gather_confluence(...)` when product==confluence, else Jira `gather_config`.
  - `stage_env_checks` → `confluence_checks.run_checks_confluence(...)` else Jira `run_checks`. Then `annotate_fixes` (shared registry — kinds are unique across products).
  - `stage_env_analysis` → `analyze(..., product=connector.product)`.
- Everything else is unchanged and reused: RunEngine `kind="env_audit"`, `_finalize_env`,
  `findings_config` store, the summary-API findings reconstruction, the EnvAnalysis
  `findingsByCategory` renderer (same 6 categories + 3 fix tiers), verdict ladder.

## R1 — Confluence gather (`auditor/envaudit/confluence_gather.py`)

`gather_confluence(client, space_keys, progress) -> snapshot` with the SAME outer
shape `{deployment, projects: [space keys], areas: {...}}`. Reuses `ConfluenceClient`
(`all_spaces`, `count_pages`/CQL `/rest/api/search?cql=...&limit=1` → `totalSize`,
`req`, `_follow`). Each area independent + guarded; DC-skip where Cloud-only; caps.

Areas (privacy: counts/booleans/types/space-keys only — NEVER page content, user
identities, member lists, admin names, emails, or PERSONAL-space keys/names):
- `spaces`: per global space `{key, name, type, status(current|archived), has_homepage:bool, page_count:int|None}`; plus aggregate `{count, personal_count, archived_count}`. Personal spaces counted only — key/name NEVER stored. (`/api/v2/spaces` Cloud incl. type/status/homepage; `/rest/api/space` DC.)
- `space_permissions`: per global space the set of `{principal_type, operation}` pairs (principal_type ∈ group/user/anonymous/access-class) and a `has_admin:bool` (any space-admin/SETSPACEPERMISSIONS grant present) and `anonymous:bool`. NEVER a principal value/name. (`/api/v2/spaces/{id}/permissions` Cloud; `/rest/api/space/{key}/permission` DC w/ anonymousAccess. Cloud anonymous is indirect → record capability_gap where unknowable.)
- `groups`: `{names, count, member_counts(capped probe)}` — NEVER member identities. (`/rest/api/group`.)
- `templates`: `{global_count, blueprint_count}`. (`/rest/api/template/page`, `/template/blueprint`.)
- `labels`: `{global_count}`. (`/rest/api/label?type=global`.)
- `content_quality`: instance/space-level CQL totalSize counts — `{pages_total, stale_pages(lastmodified < now-2y), drafts, orphaned_pages}` (counts only via `/rest/api/search?cql=...&limit=1`). NEVER titles.

## R2 — Confluence checks (`auditor/envaudit/confluence_checks.py`)

`run_checks_confluence(snapshot) -> list[findings]` using the SAME finding shape
`{area,name,kind,severity,detail}` and `_evaluable` discipline (no false clean, no
false finding; unevaluable when area skipped/errored/DC-count-only). Implement the
catalog's Section 1-4 kinds, e.g.:
- Spaces/Hygiene: `empty_space`, `stale_space`, `archived_space_clutter`, `space_no_homepage`, `personal_space_sprawl`, `archivable_by_age_and_label`, `space_count_near_guardrail` (Performance, ~10k guardrail), `large_space` (Performance).
- Permissions/Security: `space_no_admin` (high), `anonymous_space_access` (high, DC-reliable; Cloud→capability_gap when unknowable), `anonymous_write_grant` (high), `space_permission_to_anyone` (broad principal-class grant).
- Content/DataQuality: `stale_page_ratio_high`, `orphaned_pages_high`, `drafts_pileup`.
- Templates/Labels/Config: `unused_global_template`, `label_sprawl`, `empty_group` (Confluence groups; distinct from the Jira empty_group? kinds must be unique → use `confluence_empty_group`).
Use the catalog's exact kinds/severities; skip the 6 capability_gap items (emit `capability_gap` where an area is unknowable, e.g. Cloud anonymous-global).

## R3 — 3-tier fixes for every Confluence kind (`auditor/envaudit/fixes.py`)

Extend `_FIXES` + `category_for` with an entry for EVERY new Confluence kind (tier
per catalog: e.g. `empty_space`/`archived_space_clutter` → app "archive the space"
[reversible]; security/permission kinds → human; capability gaps → human). Update
the completeness test `tests/test_env_fixes.py` (`ALL_KINDS` + count). App-tier
Confluence fixes (archive space) are NOT auto-applied in this spec (apply wiring is
a follow-up) — they render as app-tier suggestions; the env-fix apply path stays
Jira-only for now (or gains a guarded Confluence archive in a later batch).

## R4 — Product-aware AI (`auditor/envaudit/analysis.py`)

- `summarize_for_ai(snap, findings, product="jira")` — add Confluence area branches
  (spaces/permissions/content_quality/templates/labels: counts/booleans/types only,
  NO identities/content). Keep the Jira branches unchanged.
- `_SYSTEM_CONFLUENCE` — a Confluence-admin system prompt (spaces, permissions,
  content hygiene, security); `analyze(..., product=...)` selects the prompt + the
  "Audit this Confluence environment configuration" user message. Return shape and
  the metadata-only boundary unchanged.

## R5 — UI: offer Confluence environment audits

- `webapp/main.py` `/environments` route: `allow_confluence=True`; update `section_sub`
  + `name_placeholder` to be product-neutral.
- `webapp/templates/index.html`: the `new_audit_form` already branches on
  `allow_confluence` — the env create form now offers Jira | Confluence; update the
  empty-state copy to mention both.
- The connect step (`migration.html` env flow) is already product-aware. The env-run
  trigger + render are product-agnostic.

## R6 — Tests
- gather: per-area Cloud shape + DC behavior + cap + error-preserve + PRIVACY (no
  page content, no personal-space key, no principal/member identity in snapshot).
- checks: positive + negative + unevaluable-guard per kind.
- fixes: completeness covers Jira + Confluence kinds; tiers/categories.
- analysis: Confluence allowlist forwards only metadata (leak tests inject a page
  title / admin name / personal-space key → absent); product prompt selected.
- routes/e2e: create a Confluence environment audit, run it (MockTransport), assert
  verdict/stats/findings persisted with fix+category and ZERO writes during audit.

## Out of scope (v1)
- Confluence env-fix APPLY (archive space) — render app-tier suggestion only.
- Deprecated-macro/broken-link audits (need full page-body extraction — privacy).
- Cloud global-permission list (no bulk endpoint) — capability_gap.
- Page view analytics (not a CQL field).
