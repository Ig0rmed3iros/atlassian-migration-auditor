# Jira DC/Server → Cloud migration checks (env audit)

Research-grounded catalog of common Jira **Data Center/Server → Cloud** (JCMA)
migration problems detectable by auditing the **source** instance's config via
the REST API, **count-only** and privacy-safe. Gated to `deployment == "dc"`.

## Foundational gate
All checks below are `dc_source_only` — they apply to a DC/Server SOURCE, not a
Cloud instance. The gather already records `snap["deployment"]`; the checks gate
on `== "dc"`. (Atlassian: `GET /rest/api/2/serverInfo` → `deploymentType` ∈
{Server, DataCenter, Cloud}.)

## Two privacy backstops (don't regress)
- **Never call `/user/search`** (leaks identities, caps at 1000). Use
  `GET /rest/api/2/applicationrole` → sum `userCount` for a global user count.
- For projects/groups, read the `total` field rather than enumerating keys.
- **DC global counts are SITE-WIDE; Cloud guardrails are PER-SPACE** — a DC count
  is an upper-bound proxy for a Cloud per-space limit. Say so in any guardrail
  finding.

## Implemented this batch (no new gather — reuse existing data)

| kind | what it catches | detection (count-only) |
|------|-----------------|------------------------|
| **`group_name_collision_reserved`** (Security, high) | A DC group whose name collides with a reserved Cloud group (administrators, site-admins, jira-administrators, jira-software-users, atlassian-addons-admin, …). Cloud MERGES same-named groups on migration → silent permission escalation / unexpected paid access. **A mandatory JCMA pre-migration fix.** | gather `groups.names` ∩ the reserved set. Group names are config identifiers. |
| **`unsupported_custom_field_type`** (DataQuality, high) | Custom fields whose type key is app-provided / outside JCMA's supported namespace. JCMA migrates the field shell but **silently drops the values**. | gather `custom_fields.by_type` (field→type key); count types not under `com.atlassian.jira.plugin.system.customfieldtypes:`. Type keys only, never values. |

Both human-tier (rename / app install / conversion are manual). Jira kinds now 56 → 58; total 70 → 72.

## Deferred (future batches — need a new gather endpoint)

Count-only and high-value, but require gather additions (UPM / applicationrole /
application-properties / serverInfo):
- ~~`user_installed_apps_count` (**#1 migration blocker**)~~ **DONE** (2026-06-15):
  shipped as `apps_to_assess_for_cloud` (medium). `_gather_plugins` calls
  `GET /rest/plugins/1.0/`, counts `userInstalled == true` (+ `enabled`). DC-only
  (`plugins` area is skipped on Cloud with a Marketplace reason). App keys are
  read to detect script apps but reduced to counts/booleans — never stored.
- ~~`scriptrunner_or_app_scripts_present`~~ **DONE** (2026-06-15): shipped as
  `script_app_present` (high). `_SCRIPT_APP_KEYS` matches ScriptRunner
  (`com.onresolve.jira.groovy.groovyrunner`), JSU, JMWE; scripted
  fields/behaviours/post-functions don't migrate.
- `user_license_seat_pressure` — `applicationrole` userCount sum.
- ~~guardrail counts → Cloud per-space limits~~ **DONE** (2026-06-15): the global
  guardrail family is implemented (NOT DC-gated — applies to both deployments,
  Cloud operational ceiling + DC migration sizing): `near_issue_type_limit` (150),
  `near_priority_limit` (100), `near_workflow_limit` (150), `near_project_limit`
  (8,400) — warn ~80%, high at the limit; reuse existing gather counts. Joins the
  existing `near_field_limit` (700). Still deferred (need finer gather): per-workflow
  `near_status_limit` (200/workflow — from the transition graph), per-project
  `near_component_limit` (10,000) / `near_version_limit` (15,000) from by_project.
- `anonymous_public_access_present` — `permissionscheme?expand=permissions` count
  `holder.type == "anyone"`. The gather has permission_scheme_grants by holder
  type; a DC-gated check could reuse it.
- `attachment_max_size_misconfig` — `application-properties?key=jira.attachment.size`.

## Honestly NOT count-only (advisory / manual only — never auto-collect)
total attachment volume; null-filename attachments; orphaned filters/boards owned
by inactive users (exposes owner identity — Atlassian's own method is a SQL
query); precise per-transition workflow-rule typing. Surface cheap proxies + point
at JCMA's scale export / SQL methods.

Sources: Atlassian migration docs (support.atlassian.com/migration) — assess-apps,
resolve-duplicate-group-names, JCMA-doesn't-migrate-all-custom-fields,
pre-migration-checklist; Data limits and guardrails; serverInfo JRASERVER-60416;
no-user-count-endpoint JRASERVER-37277.
