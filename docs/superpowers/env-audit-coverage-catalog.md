# Environment Audit — Coverage Gap Catalog

Authoritative catalog of high-value Jira instance-health / configuration-audit
checks that the Environment Audit does **not** yet implement, derived from
Atlassian's own guidance (Clean up your Jira instance, Instance Optimizer,
Portfolio Insights instance-health, data limits & guardrails, the 700-field
limit) and the feature sets of the leading audit/optimizer Marketplace apps
(Optimizer for Jira, Healthchecks, Instance Auditor).

**Scope rules honoured by every proposal below**

- Privacy: counts, structural booleans, holder **types**, config-object names,
  dates, and issue **keys** only. Never issue content (summary / description /
  comments / field values), member identities, lead names, emails, or accountIds.
- We gather **config admin objects**, not issue content. Issue-level checks use
  `maxResults=0`/`approximate-count` JQL that returns a **count or keys**, never
  field values.
- The 30 existing finding kinds are **not** duplicated:
  `capability_gap, area_error, duplicate_field, unused_custom_field,
  empty_screen, workflow_no_transitions, status_not_in_workflow, scheme_unused,
  project_missing_scheme, field_sprawl, large_option_set, workflow_sprawl,
  status_sprawl, screen_sprawl, permission_scheme_sprawl,
  unused_issue_type_scheme, unused_issue_type_screen_scheme, empty_group,
  version_overdue, version_archived_unreleased, component_no_lead,
  permission_grant_overly_broad, migration_artifact, resolution_sprawl,
  priority_sprawl, issue_type_sprawl, link_type_sprawl, large_workflow,
  public_browse_grant, component_unassigned_default`.

**Fix-tier legend** — `app` = the auto-fixer can do it safely and reversibly;
`human` = surface only, needs admin review; `unfixable` = structural / re-migrate.

---

## Section 1 — CHEAP (computable from data we ALREADY gather)

These read only existing snapshot areas. Sorted by value-to-cost (highest first).

| # | kind | category | severity | detection rule | data source | fix tier | rationale + source |
|---|------|----------|----------|----------------|-------------|----------|--------------------|
| 1 | `unused_resolution` | Hygiene | low | `resolutions.count` greatly exceeds the default set AND no workflow transition references most of them — approximate via: resolution names not equal to the canonical default set (`Done, Won't Do, Duplicate, Cannot Reproduce, Won't Fix`) flagged for review | existing snapshot area: `resolutions` (names) | human | Surplus resolutions are a top migration artefact; Atlassian's Instance Optimizer scans resolutions for cleanup. We already count them (`resolution_sprawl`) but never name the *individual* surplus values an admin must inspect. [Clean up your Jira instance](https://confluence.atlassian.com/clean/clean-up-your-jira-instance-1018788592.html) |
| 2 | `screen_not_in_scheme` | Hygiene | low | a screen `name` in `screens.names` appears in **no** screen scheme — i.e. it is not referenced by any `screen_schemes` membership we can see; report orphaned screens | existing snapshot area: `screens` + `screen_schemes` | human | Orphaned screens are dead config that still loads in the screen editor and inflates `screen_sprawl`. Complements `empty_screen` (which is about emptiness, not orphaning). [Clean up your Jira instance](https://confluence.atlassian.com/clean/clean-up-your-jira-instance-1018788592.html) |
| 3 | `workflow_unreferenced` | Hygiene | low | a workflow `name` in `workflows.names` is not the value of any workflow-scheme mapping — approximate: workflow not appearing in any `workflow_schemes` projects_using chain | existing snapshot area: `workflows` + `workflow_schemes` | human | Unreferenced workflows are pure clutter and a classic sprawl source; AppFox Optimizer's headline feature is finding unused workflows. Distinct from `workflow_no_transitions` / `large_workflow` (those are about shape). [Optimizer for Jira](https://marketplace.atlassian.com/apps/1217194/optimizer-for-jira) |
| 4 | `global_transition_overuse` | Structure | low | for a workflow in `workflows.detail`, count transitions whose effective from-set is "all statuses" (heuristic: transition count ≈ status count and many share one target) — flag workflows where global/all-status transitions dominate | existing snapshot area: `workflows{detail:transitions,statuses}` | human | Atlassian's "don't let workflows become a maze" guidance warns that over-broad global transitions defeat process control and confuse reporting. Heuristic-only without per-transition from-status data. [Don't let your Jira workflows become a maze](https://uwaterloo.ca/atlassian/blog/atlassian-best-practice-week-dont-let-your-jira-workflows) |
| 5 | `field_config_scheme_unused` | Hygiene | low | a `field_config_schemes` name with empty `projects_using` | existing snapshot area: `field_config_schemes{projects_using}` | human | Field-configuration schemes are already gathered with projects_using but only the generic `scheme_unused` covers them; a dedicated kind lets the report group field-config cleanup separately. Confirm this is not already emitted by `scheme_unused` before implementing (it likely is — see "Possibly-redundant" note). [Clean up your Jira instance](https://confluence.atlassian.com/clean/clean-up-your-jira-instance-1018788592.html) |
| 6 | `permission_grant_to_empty_group` | Security | medium | a `permission_scheme_grants` grant with `holder_type == "group"` whose group name (if exposed) maps to a `groups.member_counts` value of 0 | existing snapshot areas: `permission_scheme_grants` + `groups{member_counts}` | human | A permission granted to an empty group is a silent governance hole: nobody holds the permission today, so a future add to that group silently escalates. Feasible only if the grant holder name is captured; current gather stores `holder_type` only, so this may need a tiny gather tweak (still cheap). [Control anonymous user access](https://confluence.atlassian.com/adminjiraserver082/control-anonymous-user-access-975042557.html) |
| 7 | `admin_grant_to_anyone_logged_in` | Security | medium | `permission_scheme_grants` grant where `holder_type in {"applicationRole","loggedInUser"}` and `permission in {ADMINISTER, ADMINISTER_PROJECTS}` | existing snapshot area: `permission_scheme_grants{by_scheme:[{permission,holder_type}]}` | human | Project/global admin granted to "any logged-in user" is admin-group bloat by another name and a real audit finding. `permission_grant_overly_broad` only catches `holder_type=="anyone"`; this catches the logged-in-user variant. [Control anonymous user access](https://support.atlassian.com/jira/kb/how-to-control-anonymous-user-access-in-a-public-jira-server-or-data-center-instance/) |
| 8 | `public_share_global_create` | Security | medium | `permission_scheme_grants` grant with `holder_type == "anyone"` and `permission in {CREATE_ISSUES, ADD_COMMENTS, EDIT_ISSUES}` | existing snapshot area: `permission_scheme_grants` | human | Anonymous write access (create/comment/edit) is a higher-severity exposure than the read-only `public_browse_grant` we already flag. Same data, new permission keys. [Anonymous users able to see shared filters/dashboards](https://support.atlassian.com/jira/kb/anonymous-users-able-to-see-shared-filters-dashboards-or-project-issues-in-jira/) |
| 9 | `near_field_limit` | Performance | high | `custom_fields.count` projected against the 700-field-per-config Cloud guardrail — warn at >560 (80%), high at >700 | existing snapshot area: `custom_fields` (count) | human | Cloud enforces a hard 700-field-per-field-configuration limit (Feb/Mar 2026); crossing it blocks adding fields. Our `field_sprawl` thresholds (300/800) predate this guardrail and don't align to it. Add an explicit guardrail-aligned check. [Data limits and guardrails](https://support.atlassian.com/jira-cloud-administration/docs/data-limits-and-guardrails/) · [700-field limit](https://community.atlassian.com/forums/App-Central-articles/How-to-Audit-and-Reduce-Custom-Fields-in-Jira-Before-the-700/ba-p/3202130) |
| 10 | `duplicate_status_name` | Correctness | medium | two entries in `statuses.names` normalise to the same name (case/whitespace-insensitive) | existing snapshot area: `statuses` (names) | human | Duplicate status names (e.g. "In Review" vs "In review") break board mapping and JQL `status =` queries and are a frequent migration artefact. We do this for custom fields (`duplicate_field`) but not statuses. [10 Jira status anti-patterns](https://community.atlassian.com/forums/App-Central-articles/10-Jira-Status-Anti-Patterns-and-the-10-Minute-Fix-for-Each/ba-p/3138593) |
| 11 | `duplicate_issue_type_name` | Correctness | medium | two entries in `issue_types.names` normalise to the same name | existing snapshot area: `issue_types` (names) | human | Same root cause as duplicate statuses; duplicate issue-type names confuse issue-type schemes and reporting. [Clean up your Jira instance](https://confluence.atlassian.com/clean/clean-up-your-jira-instance-1018788592.html) |
| 12 | `redundant_priority_set` | Hygiene | low | `priorities.count` exceeds the default 5 AND priority names include obvious near-duplicates (normalised collisions or `Major`/`High` style overlaps from the canonical list) | existing snapshot area: `priorities` (names) | human | Bloated priority lists fragment reporting and SLAs. Complements the count-only `priority_sprawl` by naming the suspect values. [Clean up your Jira instance](https://confluence.atlassian.com/clean/clean-up-your-jira-instance-1018788592.html) |
| 13 | `version_naming_inconsistent` | DataQuality | info | within one project's `versions.by_project`, version names mix obviously different conventions (heuristic: some match `\d+\.\d+` semver and others are free text) | existing snapshot area: `versions{by_project}` | human | Inconsistent version naming breaks release reporting and "fix version" rollups. Low-confidence heuristic, so `info`. [Clean up your Jira instance](https://confluence.atlassian.com/clean/clean-up-your-jira-instance-1018788592.html) |
| 14 | `many_overdue_versions_in_project` | Hygiene | medium | a project in `versions.by_project` has ≥ N (e.g. 5) versions with `overdue && !released` | existing snapshot area: `versions{by_project}` | human | One overdue version is noise (`version_overdue`); a project with many overdue unreleased versions signals an abandoned release calendar. Aggregates existing per-version data into a project-level signal. [Clean up your Jira instance](https://confluence.atlassian.com/clean/clean-up-your-jira-instance-1018788592.html) |
| 15 | `large_group_admin_bloat` | Security | low | a `groups.member_counts` entry whose name matches an admin-group pattern (`*admin*`, `site-admins`, `jira-administrators`) AND member count exceeds a threshold (e.g. > 20) | existing snapshot area: `groups{member_counts}` | human | Admin-group bloat is a named governance anti-pattern; an oversized admin group widens the blast radius. Uses counts + name pattern only — no identities. [Clean up your Jira instance](https://confluence.atlassian.com/clean/clean-up-your-jira-instance-1018788592.html) |
| 16 | `board_count_exceeds_projects` | Hygiene | info | `boards.count` is large relative to `len(projects)` (e.g. > 3× projects) | existing snapshot areas: `boards` (count) + `projects` | human | A board explosion (often duplicate boards per team) clutters navigation and indicates ungoverned board creation. Pure count ratio. [Optimizer for Jira](https://marketplace.atlassian.com/apps/1217194/optimizer-for-jira) |
| 17 | `dashboard_filter_volume_high` | Performance | info | `filters.count` or `dashboards.count` at/near the gather cap (500), indicating a very large shared-object population | existing snapshot areas: `filters` / `dashboards` (counts, capped flag) | human | High filter/dashboard counts are an indexing and governance cost and a common cleanup target. We already collect counts + a `capped` flag; surface it as a finding. [Healthchecks for Jira](https://marketplace.atlassian.com/apps/1185092213/healthchecks-monitor-audit-clean-up-and-optimize-for-jira) |

---

## Section 2 — NEW GATHER (needs a new endpoint)

Each row names the exact Jira REST endpoint to add. Sorted by value-to-cost.

| # | kind | category | severity | detection rule | data source (new gather) | fix tier | rationale + source |
|---|------|----------|----------|----------------|--------------------------|----------|--------------------|
| 18 | `inactive_project` | Hygiene | medium | a project whose last issue update is older than N months — read `project.lastIssueUpdateTime` if present, else 0-result recency probe (see #33) | new gather: `GET /rest/api/3/project/search?expand=insight` (returns `insight.lastIssueUpdateTime` + `totalIssueCount`, no content) | human | Inactive/abandoned projects are Atlassian's #1 cleanup recommendation and the headline feature of every optimizer app. `insight` expansion gives last-update timestamp and issue count — dates + counts only, privacy-safe. [How to identify inactive projects](https://support.atlassian.com/jira/kb/how-to-identify-inactive-projects-finding-projects-with-no-recent-issues-created-or-updated/) · [Clean up your Jira instance](https://confluence.atlassian.com/clean/clean-up-your-jira-instance-1018788592.html) |
| 19 | `empty_project` | Hygiene | low | a project with `insight.totalIssueCount == 0` | new gather: `GET /rest/api/3/project/search?expand=insight` (same call as #18) | human | Zero-issue projects are dead config that still carry schemes, boards, and permission overhead. Atlassian explicitly lists "projects with no issues at all" as a cleanup target. Free once #18's call is added. [Clean up your Jira instance](https://confluence.atlassian.com/clean/clean-up-your-jira-instance-1018788592.html) |
| 20 | `shared_object_owned_by_inactive` | Security | high | a filter or dashboard whose `owner.active == false` (boolean only) — count + object name + owner-active flag, never the owner identity | new gather: `GET /rest/api/3/filter/search?expand=owner` and `GET /rest/api/3/dashboard?expand=owner` (read only `owner.active`) | human | Shared filters/dashboards owned by deactivated users are a named governance risk: they keep running, can't be edited, and may leak data. Atlassian and Healthchecks both call this out. Capture `owner.active` boolean only — no name/accountId. [Healthchecks for Jira](https://marketplace.atlassian.com/apps/1185092213/healthchecks-monitor-audit-clean-up-and-optimize-for-jira) · [bulk-restrict shared filters/dashboards](https://support.atlassian.com/jira/kb/how-to-bulk-restrict-filters-and-dashboards-shared-with-anyone-on-the-web-or-logged-in-users-in-jira/) |
| 21 | `public_shared_filter` / `public_shared_dashboard` | Security | high | a filter/dashboard whose `sharePermissions` include a `{type: "global"}` or `{type: "loggedin"}`/`authenticated` entry — count + object name + share-type only | new gather: `GET /rest/api/3/filter/search?expand=sharePermissions` and `GET /rest/api/3/dashboard` (read `sharePermissions[].type`) | human | Filters/dashboards shared "with anyone on the web" leak their JQL (and on public sites, results) to anonymous users — a documented data-exposure path. Read share-permission **type** only. [Anonymous users able to see shared filters/dashboards](https://confluence.atlassian.com/jirakb/anonymous-users-able-to-see-shared-filters-dashboard-or-project-issues-273875447.html) · [Securely configure filters and dashboards](https://seibert.group/blog/en/atlassian-jira-securely-configure-filters-and-dashboards/) |
| 22 | `notification_scheme_unused` | Hygiene | low | a notification scheme returned by the list endpoint whose project-association list is empty | new gather: `GET /rest/api/3/notificationscheme?expand=projectMappings` (or cross-ref `GET /rest/api/3/notificationscheme/project`) | human | Unused notification schemes are scheme clutter exactly like the workflow/screen schemes we already flag; closes a gap in our scheme-coverage matrix. [Configure notification schemes](https://support.atlassian.com/jira-cloud-administration/docs/configure-notification-schemes/) |
| 23 | `issue_security_scheme_unused` | Security | low | an issue-security scheme not associated with any project | new gather: `GET /rest/api/3/issuesecurityschemes` then `GET /rest/api/3/issuesecurityschemes/{id}` for project links | human | Orphaned issue-security schemes are dead config and can mask intended access controls; the new v3 issue-security-scheme APIs make this auditable. Names + association booleans only. [Issue security schemes REST API](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-security-schemes/) |
| 24 | `board_missing_filter` | Correctness | high | a board whose backing saved filter no longer exists / is inaccessible (board config returns no filter or a 404 on the filter id) | new gather: `GET /rest/agile/1.0/board/{boardId}/configuration` (read `filter.id`, then validate against the filters list) | human | A board pointing at a deleted/inaccessible filter silently shows nothing or errors — Atlassian's Portfolio Insights ships a dedicated "boards with missing filters" check. We already list boards; add the config probe. [Jira boards with missing filters](https://confluence.atlassian.com/enterprise/jira-boards-with-missing-filters-1627455553.html) |
| 25 | `notification_scheme_sprawl` | Hygiene | low | total notification-scheme count exceeds a threshold (e.g. > 30) | new gather: `GET /rest/api/3/notificationscheme` (count) | human | Per-project notification-scheme copies are a classic migration artefact, mirroring our existing `permission_scheme_sprawl`. One list call → one count. [Configure notification schemes](https://support.atlassian.com/jira-cloud-administration/docs/configure-notification-schemes/) |
| 26 | `field_used_by_few_projects` | Performance | medium | a custom field whose context restricts it to a small project set yet it sits on global field configs (or vice-versa) — flag fields with exactly one project context as "consolidatable" | new gather: `GET /rest/api/3/field/{id}/context` (we already call this for options — also read `projectIds`) | human | Atlassian's optimizer guidance: a field used by < 10 projects should be context-limited to reduce its runtime cost against the 700-field guardrail. We already fetch field contexts for option counting — read `projectIds` from the same response. [Optimize fields in your site](https://support.atlassian.com/jira-cloud-administration/docs/optimize-your-custom-fields/) · [Managing number of custom fields](https://confluence.atlassian.com/enterprise/managing-number-of-custom-fields-in-jira-data-center-1488597907.html) |
| 27 | `locked_or_app_field_orphan` | Hygiene | info | a custom field of a locked/managed type (`schema.custom` indicates an app namespace) whose owning app is not installed / field is on no screen | new gather: `GET /rest/api/3/field` already gives `schema.custom`; cross-ref with `GET /rest/atlassian-connect/1/addons` or the apps list | human | Fields left behind by uninstalled apps are a well-known cleanup category (locked fields the admin can't delete via the normal UI). Type namespace + screen-membership only. [Clean up your Jira instance](https://confluence.atlassian.com/clean/clean-up-your-jira-instance-1018788592.html) |
| 28 | `required_field_not_on_create_screen` | Correctness | high | a field marked Required in a field configuration that is **not** present on the create screen of a project using that configuration | new gather: `GET /rest/api/3/fieldconfiguration` + `GET /rest/api/3/fieldconfiguration/{id}/fields` (read `isRequired`), cross-ref existing `screens.fields` | app-assisted/human | A required field absent from the create screen **blocks issue creation** — a hard, user-facing breakage Atlassian documents explicitly. High-value correctness check; needs field-config field-level required flags (new) joined to screen membership (already gathered). [Can't create issues because of required fields](https://support.atlassian.com/jira/kb/cant-create-issues-because-of-required-fields-in-jira-cloud/) |
| 29 | `workflow_scheme_maps_missing_status` | Correctness | medium | a workflow-scheme issue-type mapping references a workflow whose status set excludes a status the project's board/columns expect — approximate: scheme maps a workflow not in `workflows.names` (dangling reference) | new gather: `GET /rest/api/3/workflowscheme/{id}` (read `issueTypeMappings` → workflow names) | human | Schemes mapped to nonexistent or renamed workflows/statuses are a named anti-pattern that produces unreachable issues and board warnings. Names + mapping structure only. [Agile board column configuration missing statuses](https://jira.atlassian.com/browse/JSWCLOUD-26662) |

---

## Section 3 — ISSUE-LEVEL / DATA QUALITY (needs issue queries)

All use `GET /rest/api/3/search/approximate-count` or `GET /rest/api/3/search/jql?maxResults=0`
(returns a **total** and optionally issue **keys** — no field values). Per-project
scoping keeps queries cheap. Sorted by value-to-cost.

| # | kind | category | severity | detection rule | data source (issue query) | fix tier | rationale + source |
|---|------|----------|----------|----------------|---------------------------|----------|--------------------|
| 30 | `done_but_unresolved` | DataQuality | high | count of issues in a Done-category status with empty resolution > 0, per project | issue query: `statusCategory = Done AND resolution = EMPTY` (approximate-count per project) | human | The single most-cited Jira data-quality defect: Done issues with no resolution break "Unresolved" filters, release warnings, velocity, and burndown. Returns a count (and optionally keys) only. [Fix Jira resolution issues](https://support.atlassian.com/jira/kb/fix-jira-resolution-issues/) |
| 31 | `unresolved_but_done_status` (inverse) / `resolved_but_open_status` | DataQuality | medium | count of issues with a non-empty resolution but a status in the To-Do/In-Progress category, per project | issue query: `resolution != EMPTY AND statusCategory != Done` (approximate-count) | human | The mirror defect: an issue marked resolved while sitting in an open-category status. Also corrupts reporting and reopens. Count only. [Using Not Equals on a Resolution](https://support.atlassian.com/jira/kb/using-not-equals-on-a-resolution-does-not-return-unresolved-issues/) |
| 32 | `unassigned_in_active_sprint` | DataQuality | medium | count of unassigned issues currently in an open sprint, per board/project | issue query: `sprint in openSprints() AND assignee is EMPTY` (approximate-count) | human | Unassigned work in an active sprint is a hygiene red flag for sprint planning. `assignee is EMPTY` + `openSprints()` — no identities, count only. [Master Jira JQL](https://www.rvssoftek.com/blog/how-to-use-jira-jql) · [JQL for unassigned issues](https://community.atlassian.com/forums/Jira-questions/JQL-Query-to-filter-out-backlog-in-a-project-not-using-sprint/qaq-p/1265069) |
| 33 | `stale_open_issues` | DataQuality | medium | count of unresolved issues not updated in > N days (e.g. 365), per project | issue query: `resolution = EMPTY AND updated <= -365d` (approximate-count) | human | Stale open issues are Atlassian's named archive/cleanup target ("issues not updated for a long time"). Pure count over a date threshold. [Clean up your Jira instance](https://confluence.atlassian.com/clean/clean-up-your-jira-instance-1018788592.html) · [JQL for stale issues](https://community.atlassian.com/forums/Jira-questions/JQL-for-stale-issues-since-their-creation/qaq-p/1527827) |
| 34 | `overdue_open_issues` | DataQuality | low | count of unresolved issues whose due date has passed, per project | issue query: `resolution = EMPTY AND duedate < now()` (approximate-count) | human | Past-due open issues indicate planning drift; a simple count signals where date hygiene has lapsed. Count only, no dates exposed beyond the aggregate. [Master Jira JQL](https://www.rvssoftek.com/blog/how-to-use-jira-jql) |
| 35 | `orphan_issues_no_epic` | DataQuality | info | count of story/task issues in an epic-using project with no parent/epic link | issue query: `issuetype in (Story, Task) AND "Epic Link" is EMPTY AND parent is EMPTY` (approximate-count) | human | Issues not linked to an epic create reporting gaps in roadmaps; a count flags projects with weak hierarchy hygiene. Heuristic per project, so `info`. [Master Jira JQL](https://www.rvssoftek.com/blog/how-to-use-jira-jql) |
| 36 | `issues_in_unreachable_status` | Correctness | medium | count of issues whose current status is **not** part of the project's mapped workflow (status orphaned by a workflow edit), per project | issue query: per orphan status from `status_not_in_workflow`, `status = "<name>"` (approximate-count) | human | Promotes the config-level `status_not_in_workflow` finding to an **impact** measure: how many real issues are stranded in a status no transition can move them out of. Count per orphaned status. [10 Jira status anti-patterns](https://community.atlassian.com/forums/App-Central-articles/10-Jira-Status-Anti-Patterns-and-the-10-Minute-Fix-for-Each/ba-p/3138593) |
| 37 | `unused_custom_field_zero_issues` | Performance | medium | a custom field used by **zero** issues across the instance (`<field> is not EMPTY` returns 0) | issue query: `cf[NNNNN] is not EMPTY` (approximate-count) per candidate field | app | Atlassian's Instance Optimizer defines "unused custom field" by **issue usage**, not screen membership. Our `unused_custom_field` only checks screen presence; this is the stronger, optimizer-grade signal (a field can be on a screen yet never filled). Count only — never reads values. [Optimize fields in your site](https://support.atlassian.com/jira-cloud-administration/docs/optimize-your-custom-fields/) · [Instance optimizer](https://confluence.atlassian.com/enterprise/improve-jira-performance-with-instance-optimizer-1540232164.html) |
| 38 | `unused_issue_type_in_project` | Hygiene | low | an issue type in a project's issue-type scheme that has zero issues in that project | issue query: `project = X AND issuetype = Y` (approximate-count == 0) | human | Issue types offered but never used clutter the create dialog and reporting; optimizer apps flag these. Count only. [Optimizer for Jira](https://marketplace.atlassian.com/apps/1217194/optimizer-for-jira) |
| 39 | `unused_priority_value` | Hygiene | low | a priority value with zero issues across the instance | issue query: `priority = "<name>"` (approximate-count == 0) | human | Turns the count-only `priority_sprawl` into evidence: which priority values are genuinely dead and safe to retire. Count only. [Clean up your Jira instance](https://confluence.atlassian.com/clean/clean-up-your-jira-instance-1018788592.html) |
| 40 | `unused_resolution_value` | Hygiene | low | a resolution value with zero issues across the instance | issue query: `resolution = "<name>"` (approximate-count == 0) | human | Evidence layer under `resolution_sprawl` / proposal #1: which resolutions have never been applied and can be deleted. Count only. [Clean up your Jira instance](https://confluence.atlassian.com/clean/clean-up-your-jira-instance-1018788592.html) |

---

## Infeasible / should-be `capability_gap`

These were considered and rejected as full checks — record as `capability_gap`
(info) when the relevant area is touched, rather than guessing:

- **Automation rules** — no stable, paginated list API across Cloud/DC; the
  automation REST surface is unofficial and tenant-scoped. Cannot enumerate
  rule sprawl / disabled-actor rules reliably. → `capability_gap`.
- **Inactive *users* / last-login** — requires reading user identities and
  login timestamps; violates the privacy rule (no member identities). Atlassian
  recommends it, but it is out of scope for this tool by design. → note as a
  `capability_gap` so the report explains the deliberate omission.
- **App / plugin health, version currency, vendor support status** — needs the
  Marketplace + UPM surfaces and per-app metadata; large separate gather, and
  partly identity-bearing. Defer. → `capability_gap`.
- **Performance / JVM / index / DB metrics** (Apdex, GC, slow queries) — only
  available on DC via JMX/monitoring endpoints, not the config REST surface we
  use; not portable to Cloud. → out of scope; `capability_gap`.
- **Per-transition from-status detail on DC** — DC `/workflow` does not expand
  transitions/statuses the way Cloud `/workflow/search` does, so #4
  (`global_transition_overuse`) and #36 are **Cloud-only**; emit
  `capability_gap` on DC.

---

## Possibly-redundant — verify before implementing

- **#5 `field_config_scheme_unused`** likely already fires via the generic
  `scheme_unused` rule (which loops `field_config_schemes`). Only add a distinct
  kind if the report needs to group field-config cleanup separately; otherwise
  drop it. (Net new value: presentation only.)
- **#1 / #39 / #40** overlap in intent (surplus resolutions). #1 is the
  cheap name-only heuristic; #40 is the issue-count-backed proof. Ship #40 if
  issue queries are in scope; ship #1 as the cheap fallback otherwise — not both.

---

## Summary

| Section | Proposals | Highest-value theme |
|---------|-----------|---------------------|
| 1 — Cheap (existing data) | 17 (#1–#17) | naming the individual surplus objects behind our count-only sprawl checks; guardrail-aligned field limit; anonymous-write + admin-to-loggedin security grants |
| 2 — New gather (one endpoint each) | 12 (#18–#29) | inactive/empty projects; shared filters/dashboards owned by inactive users or shared publicly; board-missing-filter; required-field-not-on-create-screen |
| 3 — Issue-level / data quality | 11 (#30–#40) | done-but-unresolved (flagship); stale/overdue/unassigned-in-sprint; issue-usage-backed unused field/priority/resolution |
| **Total** | **40** | |

### Top 10 highest-value picks (across all sections)

1. **#30 `done_but_unresolved`** (DataQuality, high) — the single most common,
   most damaging Jira data defect; one JQL count per project.
2. **#18 `inactive_project`** (Hygiene, medium) — Atlassian's #1 cleanup target
   and every optimizer app's headline; one `project/search?expand=insight` call.
3. **#20 `shared_object_owned_by_inactive`** (Security, high) — governance hole
   that leaks data and can't be self-corrected; `owner.active` boolean only.
4. **#28 `required_field_not_on_create_screen`** (Correctness, high) — actively
   **blocks issue creation**; joins new field-config required flags to screens we
   already gather.
5. **#21 `public_shared_filter/dashboard`** (Security, high) — anonymous JQL/data
   exposure; share-permission **type** only.
6. **#9 `near_field_limit`** (Performance, high) — aligns to Cloud's hard 700-field
   guardrail that blocks adding fields; pure count we already have.
7. **#37 `unused_custom_field_zero_issues`** (Performance, medium) — optimizer-grade
   "unused field" by issue usage, stronger than our screen-only signal.
8. **#24 `board_missing_filter`** (Correctness, high) — boards silently show nothing;
   shipped as a dedicated Atlassian Portfolio Insights check.
9. **#19 `empty_project`** (Hygiene, low) — free once #18's call exists; dead config
   carrying full scheme overhead.
10. **#7/#8 anonymous & logged-in write/admin grants** (Security, medium) — closes the
    gap our read-only `public_browse_grant` leaves; same data, new permission keys.
