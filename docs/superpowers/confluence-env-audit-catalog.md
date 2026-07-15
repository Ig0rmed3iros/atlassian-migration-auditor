# Confluence Environment Audit — Check Catalog

Authoritative catalog of high-value **Confluence** instance-health / admin-audit
checks for the Confluence Environment Audit (point at one live Confluence
instance; audit spaces, permissions, content health, hygiene), mirroring the
Jira Environment Audit. Every `kind` below is **Confluence-specific and new** —
none duplicate the 56 existing Jira finding kinds.

Derived from Atlassian's own guidance (Confluence guardrails, Managing the
number of spaces in DC, Portfolio Insights spaces-with-too-many-pages /
nesting-levels, Manage your content tree / Content Manager, Archive a space,
Permissions best practices, Manage global permissions, OpenSearch space limit)
and the feature sets of the leading governance/archiving Marketplace apps
(Better Content Archiving & Analytics by Midori, Doctor Pro, Permission
Reporting, Inspect Permissions native, Auditor for Confluence).

## Scope rules honoured by every proposal below

- **Categories** (reused from the Jira audit, 6 only): `Performance` |
  `Security` | `Hygiene` | `Structure` | `Coverage` | `DataQuality`.
- **Privacy**: we may use counts, structural booleans, principal/permission
  **types** (group / user / anonymous), space **type** (global / personal /
  collaboration), space **status** (current / archived), space **keys** and
  **names** as config identifiers, and dates reduced to booleans. We may **NOT**
  forward: page content / titles / bodies, user identities, member lists,
  space-admin names, emails, accountIds. **Personal-space exception**: a
  personal space key/name embeds a username/accountId — for personal spaces we
  forward only the **type** and **count**, never the key or name.
- Issue-equivalent content checks use `GET /rest/api/search?cql=...&limit=1` →
  `totalSize` (Cloud + DC), which returns a **count**, never page bodies/titles.
- **Fix-tier legend**: `app` = the auto-fixer can do it safely and reversibly
  via API (e.g. archive an empty space — reversible via restore); `human` =
  surface only, needs admin review; `unfixable` = structural / out of band.

## Data-source primitives (confirmed)

- `GET /wiki/api/v2/spaces` (Cloud) → `results[]` with `key, name, type`
  (`global|personal|collaboration`), `status` (`current|archived`),
  `homepageId`, `authorId`, `createdAt`. `GET /rest/api/space?expand=...` (DC).
- `GET /wiki/api/v2/spaces/{id}` with `include-homepage`(via `homepageId`),
  `include-permissions`, `include-labels`, `include-operations`,
  `include-role-assignments`. DC: `GET /rest/api/space/{key}?expand=homepage,permissions`.
- `GET /wiki/api/v2/spaces/{id}/permissions` (Cloud, cursor-paginated) →
  `results[]` of `{id, principal{type,id}, operation{key, targetType}}`.
  `principal.type` ∈ {`user`, `group`} (anonymous is **not** a v2 principal
  type — see COVERAGE §5). `operation.key:targetType` enum:
  `read:space`, `create:{page|blogpost|comment|attachment}`,
  `delete:{page|blogpost|comment|attachment|space}`, `export:space`,
  `administer:space`, `archive:page`, `restrict_content:space`.
  DC: `GET /rest/api/space/{key}/permission` → subjects (user/group) + operation
  + `anonymousAccess` flag.
- `GET /rest/api/search?cql=...&limit=1` → `totalSize` (Cloud + DC). CQL fields:
  `type, space, space.key, space.type, space.category, lastmodified, created,
  label, title, parent, ancestor, fileExtension, pageStatus`; date math
  `now("-365d")`.
- `GET /rest/api/group` (both) → groups + counts. `GET /rest/api/label?type=global`
  (both). `GET /rest/api/template/page` + `/template/blueprint` (both).

---

## Section 1 — SPACES & HYGIENE

| # | kind | category | severity | detection rule | data source | fix tier | rationale + source |
|---|------|----------|----------|----------------|-------------|----------|--------------------|
| 1 | `space_no_homepage` | Structure | medium | a `current` space whose `homepageId` is null/absent (or DC `expand=homepage` returns none) | `GET /api/v2/spaces` field `homepageId` (Cloud) / `GET /rest/api/space/{key}?expand=homepage` (DC) | human | A space with no homepage means pages outside the (missing) homepage never appear in the space sidebar — content is effectively unnavigable. One field per space, no content read. [Unable to set homepage for a space](https://support.atlassian.com/confluence/kb/unable-to-set-homepage-for-a-space/) |
| 2 | `empty_space` | Hygiene | low | a `current` space whose page count is 0 (or ≤1, i.e. homepage only): `type=page AND space="<KEY>"` → `totalSize ≤ 1` | CQL count `type=page AND space="<KEY>"` | app | Zero-content spaces are dead config carrying permission, scheme, and search-index overhead; Atlassian lists them as a cleanup target. Reversibly archivable by the app (restore is a single API call). Count only. [Archive a space](https://support.atlassian.com/confluence-cloud/docs/archive-a-space/) |
| 3 | `stale_space` | Hygiene | medium | a `current` space whose most-recent page activity is older than N (default 365d): `space="<KEY>" AND lastmodified > now("-365d")` → `totalSize == 0` (nothing touched in the window) | CQL count with `lastmodified` date math | human | Inactive/abandoned spaces bloat the instance, pollute search, and count toward the space guardrail. Atlassian's Content Manager defines inactivity by no view/update/comment in N days; we approximate with last-modified. Dates reduced to a boolean. [Managing the number of spaces in Confluence DC](https://confluence.atlassian.com/enterprise/managing-the-number-of-spaces-in-confluence-data-center-1607598774.html) · [Manage your content tree](https://support.atlassian.com/confluence-cloud/docs/manage-your-content-tree/) |
| 4 | `large_space` | Performance | medium | a `current` space whose page count exceeds a threshold (default 5,000): `type=page AND space="<KEY>"` → `totalSize > N` | CQL count `type=page AND space="<KEY>"` | human | Spaces with too many pages degrade page-tree navigation, space view, and page view/edit — a dedicated Atlassian Portfolio Insights instance-health check ("Confluence spaces with too many pages"). Atlassian doesn't publish the exact number ("guardrail set quite high"), so threshold is configurable. Count only. [Spaces with too many pages](https://support.atlassian.com/portfolio-insights/docs/confluence-spaces-with-too-many-pages/) |
| 5 | `archived_space_clutter` | Hygiene | low | count of spaces with `status == archived` exceeds a threshold (default 50), or ratio archived:current is high | `GET /api/v2/spaces?status=archived` (count) | human | Archived spaces remain accessible by direct link and (if public) stay externally indexed; a large archived population signals deferred cleanup and ongoing storage cost (archiving does **not** free Cloud storage). Status + count only. [Archive a space](https://support.atlassian.com/confluence-cloud/docs/archive-a-space/) · [Manage your storage usage](https://support.atlassian.com/confluence-cloud/docs/manage-your-storage-usage/) |
| 6 | `personal_space_sprawl` | Hygiene | low | count of spaces with `type == personal` exceeds a threshold relative to the instance (e.g. > 0.5× user-ish baseline, or absolute > N) | `GET /api/v2/spaces?type=personal` (count) | human | Personal spaces accumulate with the user base, often abandoned, and count toward the 8,000/10,000 space guardrail. There is no native auto-archive on user deactivation. **Privacy-critical**: forward only the personal-space **count** and **type**, never any personal-space key/name (they embed usernames). [Managing the number of spaces in Confluence DC](https://confluence.atlassian.com/enterprise/managing-the-number-of-spaces-in-confluence-data-center-1607598774.html) · [Archive personal spaces when users are deactivated](https://community.atlassian.com/forums/Confluence-questions/Archive-personal-spaces-when-users-are-deactivated/qaq-p/2905706) |
| 7 | `space_count_near_guardrail` | Performance | high | total space count (global + personal + archived) approaching Atlassian's published guardrail: warn ≥ 8,000, high ≥ 10,000 | `GET /api/v2/spaces` (total count, all statuses/types) | human | Atlassian's guardrail is **10,000 spaces** (optimal < 8,000); beyond it, permission-check overhead and search degrade, and the OpenSearch ceiling (≈131,072) eventually breaks search entirely. Aggregate count only. [Confluence guardrails](https://confluence.atlassian.com/spaces/CONF92/pages/1477577327/Confluence+guardrails) · [Managing the number of spaces](https://confluence.atlassian.com/enterprise/managing-the-number-of-spaces-in-confluence-data-center-1607598774.html) · [OpenSearch limit for Confluence spaces](https://support.atlassian.com/confluence/kb/opensearch-limit-for-confluence-spaces/) |
| 8 | `space_missing_category` | Structure | info | a `current` `global` space with no category/label assigned: space `labels` empty (or DC CQL `space.category is empty`) | `GET /api/v2/spaces/{id}?include-labels=true` / CQL `space.category` (DC) | human | Uncategorised spaces don't appear in the categorised space directory and weaken navigation/governance; categorising spaces is an Atlassian organisation recommendation. Boolean (has-category) only — never the label text if it could be identifying (space labels are config, safe). [Confluence organisation and clean-up recommendations](https://success.atlassian.com/solution-resources/work-management/wm-topics/confluence-organization-and-clean-up-recommendations) |
| 9 | `attachment_sprawl_large_files` | Performance | low | count of large/heavy attachments in a space exceeds a threshold: `type=attachment AND space="<KEY>" AND fileExtension in ("zip","mov","psd","iso","mp4")` → high `totalSize` | CQL count `type=attachment ... fileExtension in (...)` | human | Heavy attachments drive storage toward the plan cap (Standard 250 GB) and can make individual pages load slowly or not at all (Atlassian "pages with too many current attachments" insight). Extension + count only, never filenames. [Pages with too many current attachments](https://confluence.atlassian.com/enterprise/confluence-pages-with-too-many-current-attachments-1489809667.html) · [Manage your storage usage](https://support.atlassian.com/confluence-cloud/docs/manage-your-storage-usage/) |
| 10 | `trash_backlog` | Hygiene | info | (DC / where reachable) a space whose trash holds a large number of items pending purge | `GET /rest/api/space/{key}/content/trash` count (DC) — else `capability_gap` | human | Un-emptied trash retains content versions and consumes storage; Doctor Pro surfaces trash backlog as a cleanup opportunity. Count only. Mark Cloud as `capability_gap` if no reliable trash-count endpoint. [Doctor Pro for Confluence](https://marketplace.atlassian.com/apps/1235500/status-view-for-confluence) |

---

## Section 2 — PERMISSIONS & SECURITY

| # | kind | category | severity | detection rule | data source | fix tier | rationale + source |
|---|------|----------|----------|----------------|-------------|----------|--------------------|
| 11 | `space_no_admin` | Security | high | a `current` space where **no** permission grant has `operation.key == "administer"` (target `space`) | `GET /api/v2/spaces/{id}/permissions` → scan for `administer:space` (Cloud) / `GET /rest/api/space/{key}/permission` SETSPACEPERMISSIONS (DC) | human | A space with no space admin is orphaned: nobody can manage its access; recovery needs a site-admin "Recover Permissions" action. Marketplace permission apps (Permission Reporting) don't even check this — a real gap. Counts grants by operation type only, no identities. [Assign space permissions](https://support.atlassian.com/confluence-cloud/docs/assign-space-permissions/) · [Permissions best practices](https://confluence.atlassian.com/security/permissions-best-practices-1409093142.html) |
| 12 | `anonymous_space_access` | Security | high | a space granting `read:space` to the anonymous principal (DC: `anonymousAccess == true`; Cloud: surfaced via unlicensed/anonymous space settings, see note) | DC `GET /rest/api/space/{key}/permission` → `anonymousAccess`; Cloud limited (see COVERAGE §5) | human | Anonymous read makes the space internet-public and Google-indexable ("all or nothing"); the single highest-impact space security finding. Boolean per space, never content. **Cloud caveat**: anonymous is not a v2 principal type — reliably detectable on DC; Cloud emit as `capability_gap` where the setting isn't REST-listable. [Set up public access](https://support.atlassian.com/confluence-cloud/docs/set-up-public-access/) · [Anonymous access DB queries](https://support.atlassian.com/confluence/kb/how-to-find-various-permissions-on-the-spaces-which-have-anonymous-access-enabled-with-database-queries/) |
| 13 | `anonymous_write_grant` | Security | high | a space granting any `create:{page\|blogpost\|comment\|attachment}` (or `delete:*`) to the anonymous principal | DC `GET /rest/api/space/{key}/permission` (anonymous subject + COMMENT/CREATEATTACHMENT/EDITSPACE) | human | Unauthenticated create/comment/attachment is a spam and defacement vector; anonymous should be View-only at most. Higher severity than read-only anonymous access. Operation type + anonymous-type only. [Restrict permission available to anonymous users in spaces](https://support.atlassian.com/confluence/kb/restrict-permission-available-to-anonymous-users-in-spaces/) |
| 14 | `anonymous_export_grant` | Security | high | a space granting `export:space` to the anonymous principal | DC `GET /rest/api/space/{key}/permission` (anonymous + EXPORTSPACE) | human | Export-to-anonymous lets anyone bulk-exfiltrate an entire space in one zip — mass data leak. Operation + principal type only. [Anonymous access DB queries](https://support.atlassian.com/confluence/kb/how-to-find-various-permissions-on-the-spaces-which-have-anonymous-access-enabled-with-database-queries/) |
| 15 | `anonymous_admin_grant` | Security | high | a space granting `administer:space` or `restrict_content:space` to the anonymous principal | DC `GET /rest/api/space/{key}/permission` (anonymous + SETSPACEPERMISSIONS) | human | Catastrophic: anonymous users could rewrite the space's permissions. Should never exist; near-zero false-positive. Operation + principal type only. [Space permissions REST v1](https://developer.atlassian.com/cloud/confluence/rest/v1/api-group-space-permissions/) |
| 16 | `space_permission_to_anyone` | Security | medium | a space granting broad write/admin operations (`create:*`, `delete:*`, `administer:space`, `export:space`) to the all-users group (`confluence-users`) | `GET /api/v2/spaces/{id}/permissions` → `principal.type==group` && name == all-users group && broad operation | human | "Open for view" is fine, but write/admin/export granted to *all logged-in users* is over-exposure that violates least-privilege. Principal type + group name (config) + operation type only. [Permissions best practices](https://confluence.atlassian.com/security/permissions-best-practices-1409093142.html) |
| 17 | `overly_broad_group_admin_grant` | Security | medium | a space granting `administer:space` or `export:space` to a group whose member count exceeds a threshold (e.g. > 50) | `GET /api/v2/spaces/{id}/permissions` (operation + group) cross-ref `GET /rest/api/group` (member count) | human | Wide groups holding space-admin or export rights widen the blast radius; Atlassian recommends delegating admin via a small dedicated space-admins group. Operation type + group name + member count only, never members. [Permissions best practices](https://confluence.atlassian.com/doc/permissions-best-practices-992678945.html) |
| 18 | `permission_grant_to_empty_group` | Security | medium | a space permission grant whose `principal.type==group` maps to a group with member count 0 | `GET /api/v2/spaces/{id}/permissions` cross-ref `GET /rest/api/group` member count | human | A permission granted to an empty group is a silent governance hole: nobody holds it today, but a future add to that group silently escalates. Group name + count only. [Permissions best practices](https://confluence.atlassian.com/security/permissions-best-practices-1409093142.html) |
| 19 | `space_user_grant_sprawl` | Security | low | a space with many direct `principal.type==user` grants (e.g. > 10) instead of group-based grants | `GET /api/v2/spaces/{id}/permissions` → count grants where `principal.type==user` | human | Atlassian best practice is to grant via groups; many per-user grants don't scale, are missed when people leave, and are a leaver-access risk. **Count of user-type grants only** — never the user ids. [Permissions best practices](https://confluence.atlassian.com/doc/permissions-best-practices-992678945.html) · [Inspect permissions](https://confluence.atlassian.com/doc/inspect-permissions-992678939.html) |
| 20 | `space_permission_drift` | Security | info | the per-principal-**type** operation profile of a space differs markedly from the instance's modal profile (heuristic: outlier in granted-operation-set across spaces) | derived from gathered `GET /api/v2/spaces/{id}/permissions` across all spaces | human | Permission inconsistency/drift across spaces (many admins editing over time) is a named audit target; an outlier space may be accidentally over- or under-exposed. Compares operation-type sets only, no identities. Low confidence → `info`. [Mastering Permissions in Confluence](https://community.atlassian.com/forums/Confluence-Cloud-Admins-articles/Mastering-Permissions-in-Confluence-Guide-to-a-Secure-and/ba-p/3132516) |

---

## Section 3 — CONTENT & DATA QUALITY

All use `GET /rest/api/search?cql=...&limit=1` → `totalSize` (a count, never
page bodies/titles). Per-space scoping keeps queries cheap.

| # | kind | category | severity | detection rule | data source (CQL count) | fix tier | rationale + source |
|---|------|----------|----------|----------------|-------------------------|----------|--------------------|
| 21 | `stale_page_ratio_high` | DataQuality | medium | per space, fraction of pages older than N (default 365d) is high (e.g. > 50%): `space="<KEY>" AND type=page AND lastmodified < now("-365d")` ÷ total pages | two CQL counts (`lastmodified < now("-365d")` and total) | human | The #1 content-lifecycle signal: a space dominated by year-plus-stale pages erodes trust and clutters search. Better Content Archiving's canonical criterion is "not updated for N days". Two counts only, dates reduced to a ratio. [Manage your content tree](https://support.atlassian.com/confluence-cloud/docs/manage-your-content-tree/) · [Better Content Archiving — page archiving](https://www.midori-global.com/products/better-content-archiving-for-confluence/server/documentation/page-archiving) |
| 22 | `orphaned_pages` | Structure | low | per space, count of non-homepage pages with no parent (top-level, not the homepage): `space="<KEY>" AND type=page AND parent != "<homepageId>"` filtered to those with no parent (approximation; true link-orphans not CQL-expressible) | CQL count with `parent`/`ancestor`; homepage id from §1 | human | Orphaned pages (not in the page tree) are unreachable via navigation and become forgotten content; Confluence ships a native per-space "Orphaned Pages" view. **Approximation note**: CQL can express "no parent" but not "no incoming links" — flag the parentless-page count; mark the link-orphan part as `capability_gap`. Count only. [Orphaned pages](https://confluence.atlassian.com/conf59/orphaned-pages-792498978.html) |
| 23 | `draft_pages_lingering` | Hygiene | low | per space, count of unpublished/rough-draft pages above a threshold: `space="<KEY>" AND type=page AND pageStatus="Rough draft"` (Cloud) | CQL count `pageStatus="Rough draft"` (Cloud); DC dialect varies → probe | human | Lingering never-published drafts clutter search and aren't in trash (can't be bulk-deleted natively). Count only. **Dialect caveat**: `pageStatus`/`status` differs Cloud vs DC; probe the target and emit `capability_gap` if unsupported. [Removing orphaned draft](https://support.atlassian.com/confluence/kb/removing-orphaned-draft/) |
| 24 | `duplicate_page_titles` | DataQuality | low | within a space, ≥ N pages share a normalised title cluster — detected via repeated `title ~ "..."` collisions, OR cross-space identical titles | CQL `title ~`/`title =` counts (titles are content → see privacy note) | human | Duplicate/near-duplicate pages cause wrong-version edits and maintenance overhead; Confluence forbids exact duplicate titles per space, so dupes surface as "(1)" suffixes or cross-space. **PRIVACY**: this risks reading titles. Implement as a **count of collision clusters per space only** (e.g. "3 title-collision clusters"), never forwarding the titles themselves; if that can't be guaranteed, drop to `capability_gap`. [Duplicate page titles (K15t)](https://help.k15t.com/scroll-versions/4.8/duplicate-page-titles) |
| 25 | `unlabeled_page_ratio_high` | DataQuality | info | per space, fraction of pages with no label is high: (total pages) − (pages matching any of the space's known labels) ÷ total, above a threshold | CQL counts (`label in (...)` vs total) | human | Unlabeled pages don't surface in label macros or curated views, weakening discoverability. Approximate via labeled-vs-total counts. Counts only, no titles. [ScriptRunner — labels for content management](https://www.scriptrunnerhq.com/inspiration/blog/confluence-content-management-using-automation) · [Detect/remove unused labels (CONFCLOUD-36423)](https://jira.atlassian.com/browse/CONFCLOUD-36423) |
| 26 | `archivable_by_age_and_label` | Hygiene | medium | per space, count of pages that are stale AND lack a keep/no-archive label: `space="<KEY>" AND type=page AND lastmodified < now("-365d") AND label not in (keep, noarchive, do-not-archive)` | CQL count combining `lastmodified` + `label not in (...)` | app | Encodes the industry-validated Better Content Archiving rule (`age` minus opt-out labels) to size the safe archive backlog per space. Archiving is reversible (restore), so `app`-tier sizing/auto-archive is defensible. Count only. [Better Content Archiving — page archiving](https://www.midori-global.com/products/better-content-archiving-for-confluence/server/documentation/page-archiving) · [Manage your stale pages in bulk](https://community.atlassian.com/forums/Confluence-Cloud-Admins-articles/Manage-your-stale-pages-in-bulk/ba-p/2592287) |

---

## Section 4 — TEMPLATES / LABELS / CONFIG

| # | kind | category | severity | detection rule | data source | fix tier | rationale + source |
|---|------|----------|----------|----------------|-------------|----------|--------------------|
| 27 | `global_template_sprawl` | Hygiene | low | total global page-template count exceeds a threshold (e.g. > 30) | `GET /rest/api/template/page` (count) | human | A large global-template population confuses authors picking a template and signals ungoverned template creation; mirrors our Jira sprawl checks. Count + template names (config) only, no bodies. [Manage global templates & blueprints](https://support.atlassian.com/confluence-cloud/docs/delete-or-disable-a-global-template/) |
| 28 | `unused_global_template` | Hygiene | low | a global page template whose name is never referenced by any space's create flow / not derived-from on any page (best-effort cross-ref) | `GET /rest/api/template/page` cross-ref usage; else `capability_gap` | human | Dead global templates are clutter authors must wade through; deleting one is permanent so surfacing-only is appropriate. Template name (config) only. **Note**: per-page "created from template" provenance is not reliably queryable → if usage can't be determined, emit `capability_gap`. [Manage global templates & blueprints](https://support.atlassian.com/confluence-cloud/docs/delete-or-disable-a-global-template/) |
| 29 | `unused_global_label` | Hygiene | low | a global label with zero page usage: for each label from the global-label list, `label="<name>"` → `totalSize == 0` | `GET /rest/api/label?type=global` + CQL `label="<name>"` count | human | Confluence has **no native UI** to find/remove unused labels (open issue CONFCLOUD-36423); unused/inconsistent global labels fragment discoverability. Label name (config) + count only. [Detect/remove unused labels (CONFCLOUD-36423)](https://jira.atlassian.com/browse/CONFCLOUD-36423) · [ScriptRunner — labels](https://www.scriptrunnerhq.com/inspiration/blog/confluence-content-management-using-automation) |
| 30 | `inconsistent_global_labels` | DataQuality | info | the global-label set contains normalised near-duplicates (case, hyphen-vs-underscore, regional spelling): two labels collide after normalisation | `GET /rest/api/label?type=global` (names) | human | Inconsistent label conventions (e.g. `how-to` vs `howto` vs `How_To`) silently split content across near-identical labels and kill label-macro discoverability. Label names are config, safe to compare. Low confidence → `info`. [ScriptRunner — labels for content management](https://www.scriptrunnerhq.com/inspiration/blog/confluence-content-management-using-automation) |
| 31 | `empty_group` | Hygiene | low | a Confluence group with member count 0 (Confluence-side check, distinct from the Jira `empty_group`) | `GET /rest/api/group` (member counts) | human | Empty Confluence groups are dead config and a permission-escalation risk when later populated (see #18). Distinct from the Jira `empty_group` kind — this audits the Confluence directory. Count only, no members. [Permissions best practices](https://confluence.atlassian.com/security/permissions-best-practices-1409093142.html) |

---

## Section 5 — COVERAGE (Cloud-removed APIs / capability gaps)

These were considered and are recorded as `capability_gap` (category
`Coverage`, severity `info`) when the relevant area is touched, rather than
guessing or violating privacy. They explain deliberate omissions to the reader.

| # | kind | category | severity | gap | why it's a gap / source |
|---|------|----------|----------|-----|--------------------------|
| 32 | `capability_gap` (global permissions) | Coverage | info | **No bulk global-permission list endpoint exists on Cloud.** Anonymous global "Use Confluence", Create Space, Personal Space, and admin grants can't be enumerated via REST — admin-UI only. | Confirmed: there is no documented Cloud global-permissions read API (the RBAC `space-permissions/transition` endpoint is a bulk **write**, not a read). So checks like "anonymous global access enabled" / "site allows spaces to self-enable anonymous" are **not REST-detectable on Cloud** — surface as a deliberate gap. [Manage global permissions](https://support.atlassian.com/confluence-cloud/docs/manage-global-permissions/) · [Get global permissions (dev community, no endpoint)](https://community.developer.atlassian.com/t/get-global-permissions/60970) |
| 33 | `capability_gap` (anonymous on Cloud) | Coverage | info | **Anonymous is not a v2 `principal.type`** on Cloud space permissions. `anonymous_space_access` (#12) and the anonymous-grant checks (#13–15) are **reliably detectable on DC** (`anonymousAccess` flag / DB) but not via the Cloud v2 permissions list. | The Cloud v2 `principal.type` enum is `{user, group}` only; anonymous access lives in space settings, not the permissions array. Emit `capability_gap` for the anonymous checks on Cloud targets. [Space permissions REST v2](https://developer.atlassian.com/cloud/confluence/rest/v2/api-group-space-permissions/) · [Anonymous access DB queries](https://support.atlassian.com/confluence/kb/how-to-find-various-permissions-on-the-spaces-which-have-anonymous-access-enabled-with-database-queries/) |
| 34 | `capability_gap` (never-viewed content) | Coverage | info | **Page view counts are not a CQL field.** "Never-/rarely-viewed pages" — a core Better Content Archiving signal — can't be computed from the search API; it needs Confluence Analytics (Premium) or an archiving app's own tracking. | We can size staleness by `lastmodified` (#21) but not by readership. Record as a gap so the report explains why "unused page" is age-based, not view-based. [Better Content Archiving & Analytics](https://marketplace.atlassian.com/apps/123/better-content-archiving-and-analytics-for-confluence) · [CQL fields](https://developer.atlassian.com/cloud/confluence/cql-fields/) |
| 35 | `capability_gap` (deprecated-macro / page-body scans) | Coverage | info | **Deprecated/legacy-macro usage and broken-link / dead-attachment-link detection need full page-body extraction**, which the env gather deliberately avoids (privacy + cost). | CQL `macro=<name>` can count *some* macro usage by name, but a full deprecated-macro / broken-link audit requires reading page storage-format bodies — out of scope by the privacy rule. Record as a gap / out-of-scope. [Advanced searching using CQL](https://developer.atlassian.com/server/confluence/advanced-searching-using-cql/) |
| 36 | `capability_gap` (deactivated-owner content) | Coverage | info | **"Pages owned by deactivated users" requires reading user identity/active state**, which the privacy rule forbids. Native Content Manager offers this filter (Premium/Enterprise) but it is identity-bearing. | Atlassian recommends reassigning deactivated-owner pages, but doing so needs owner identities — out of scope for this tool by design (same stance as the Jira audit's inactive-user omission). [Manage your content tree](https://support.atlassian.com/confluence-cloud/docs/manage-your-content-tree/) |
| 37 | `capability_gap` (per-page attachment-size / nesting-depth guardrails) | Coverage | info | **Atlassian doesn't publish numeric thresholds** for pages-with-too-many-attachments or spaces-with-too-many-nesting-levels ("guardrail set quite high"), and attachment **size** + page-tree **depth** aren't CQL fields. | We can count attachments by extension (#9) and approximate depth via `ancestor`, but the precise Portfolio-Insights nesting/attachment guardrails need the DC SQL the insight ships, not the REST/CQL surface. Record as a gap. [Spaces with too many nesting levels](https://support.atlassian.com/portfolio-insights/docs/confluence-spaces-with-too-many-nesting-levels/) · [Pages with too many current attachments](https://confluence.atlassian.com/enterprise/confluence-pages-with-too-many-current-attachments-1489809667.html) |

---

## Possibly-redundant / verify before implementing

- **#13 / #14 / #15 anonymous write/export/admin** overlap with **#12
  anonymous read** in mechanism. Ship #12 as the base detector; ship #13–15
  only if the gather captures the specific anonymous operation set (DC does via
  the permission `operation`; Cloud cannot — see #33). Don't emit all four as
  separate findings on the same space when only `read` is present.
- **#16 `space_permission_to_anyone`** vs **#17 `overly_broad_group_admin_grant`**:
  #16 is keyed on the *all-users* group specifically; #17 on *any large* group.
  A grant to a large `confluence-users` group could match both — dedupe so one
  finding fires per (space, grant).
- **#27 `global_template_sprawl`** (count) vs **#28 `unused_global_template`**
  (per-template usage): ship #27 always (cheap); ship #28 only if template-usage
  provenance is determinable, else it collapses to `capability_gap` (#35-style).
- **#24 `duplicate_page_titles`** must be implemented as a **collision-cluster
  count only**. If the implementation can't avoid surfacing the titles
  themselves, drop it to `capability_gap` rather than risk content leakage.

### Shipped

- **#27 — shipped as `template_sprawl`** (not `global_template_sprawl`): the
  kind name mirrors the existing `label_sprawl` so the two hygiene-sprawl checks
  read consistently. Reads the already-gathered `templates.global_count` (no new
  API surface); warn threshold 100 global templates. Hygiene / low / human.
- **#18 — shipped as `permission_grant_to_empty_group`**: cross-references the
  per-space group GRANT names against the groups area's member counts. The
  DC/Cloud asymmetry is load-bearing — DC's v1 permission list names the granted
  group, but Cloud's v2 principal exposes only an opaque group **id**, so the
  join is impossible on Cloud. Rather than clean-bill, a Cloud space with group
  grants emits a `capability_gap` (group grants exist, names unavailable —
  verify manually). A group beyond the member-count cap is `None`, never `0`, so
  an UNKNOWN group is never mistaken for an EMPTY one. Security / medium / human.

---

## Summary

| Section | Proposals | Highest-value theme |
|---------|-----------|---------------------|
| 1 — Spaces & Hygiene | 10 (#1–#10) | no-homepage / empty / stale / large spaces; space-count guardrail (10,000); personal-space sprawl (privacy-guarded) |
| 2 — Permissions & Security | 10 (#11–#20) | space-with-no-admin (flagship, apps miss it); anonymous read/write/export/admin grants (DC); broad/empty-group grants; user-grant sprawl |
| 3 — Content & Data Quality | 6 (#21–#26) | stale-page ratio; orphaned/draft pages; archivable-by-age-and-label backlog sizing |
| 4 — Templates / Labels / Config | 5 (#27–#31) | global-template sprawl; unused & inconsistent global labels; empty Confluence groups |
| 5 — Coverage (capability gaps) | 6 (#32–#37) | no Cloud global-perms API; anonymous not a Cloud v2 principal; views not CQL; body-scan/identity out of scope |
| **Total** | **37** | **31 implementable checks + 6 documented capability gaps** |

### Top 10 highest-value picks (across all sections)

1. **#11 `space_no_admin`** (Security, high) — orphaned spaces nobody can
   manage; even leading Marketplace permission apps don't check this. One
   operation-type scan per space.
2. **#12 `anonymous_space_access`** (Security, high) — internet-public,
   Google-indexable content; the single highest-impact space exposure.
   Boolean per space (DC-reliable; Cloud → gap).
3. **#7 `space_count_near_guardrail`** (Performance, high) — aligns to
   Atlassian's hard 10,000-space guardrail and the OpenSearch ceiling that
   eventually breaks search; one aggregate count.
4. **#3 `stale_space`** (Hygiene, medium) — abandoned spaces bloat the
   instance and dilute search; Atlassian's #1 space-cleanup target. One CQL
   recency count per space.
5. **#1 `space_no_homepage`** (Structure, medium) — content outside the
   missing homepage is unnavigable; one `homepageId` field per space.
6. **#21 `stale_page_ratio_high`** (DataQuality, medium) — the canonical
   content-lifecycle signal (Better Content Archiving's core criterion); two
   CQL counts per space.
7. **#13 `anonymous_write_grant`** (Security, high) — unauthenticated
   create/comment/attachment is a spam/defacement vector; operation + principal
   type only (DC).
8. **#4 `large_space`** (Performance, medium) — too-many-pages spaces degrade
   navigation; a shipped Atlassian Portfolio Insights instance-health check.
   One CQL count.
9. **#26 `archivable_by_age_and_label`** (Hygiene, medium) — sizes the safe,
   reversible archive backlog using the industry-validated age-minus-opt-out-label
   rule; app-tier auto-archive defensible.
10. **#2 `empty_space`** (Hygiene, low) — dead config carrying full scheme +
    index overhead; reversibly archivable by the app. One CQL count.
