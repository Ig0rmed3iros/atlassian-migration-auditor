# Confluence DC/Server → Cloud migration checks (env audit)

Research-grounded catalog of common Confluence **Data Center/Server → Cloud**
(CCMA / JCMA) migration problems, focused on **page-tree hierarchy** and
**page visibility**, with **count-only, privacy-safe** detection. Drives the
Confluence env-audit checks in `auditor/envaudit/confluence_checks.py`.

## Two ground-truth correctness traps (verified against Atlassian docs)

1. **There is NO `restricted` CQL field on Cloud.** The documented CQL fields are
   `ancestor, parent, macro, label, type, space, title, text`
   (https://developer.atlassian.com/cloud/confluence/cql-fields/). Counting
   restricted pages via `cql=restricted=true` is unreliable — use the per-page
   `/wiki/rest/api/content/{id}/restriction/byOperation/read` endpoint instead.
2. **Lifecycle status is NOT a CQL `status` field.** Filter draft/archived/trashed
   via the v1 `cqlcontext.contentStatuses` parameter or the v2 `?status=` filter.
   v1 `trashed` via cqlcontext is unreliable; prefer v2 `?status=trashed`.

Count primitive: `GET /wiki/rest/api/search?cql=<…>&limit=1` → `totalSize`.
Reliable for content (`type=page/blogpost/attachment/comment`); **not** for
`type=user`/group; undocumented backend caps exist (treat huge counts as "≥ N").

## What does NOT migrate (the root of most "invisible page" issues)

Space permissions, anonymous-access settings, personal (non-shared) drafts,
watchers, trash, user-created macros, removed/legacy-editor macros, and
Marketplace-app macros (vendor-dependent). **Page restrictions, labels, comments,
history DO migrate** — but the user↔group membership a restriction depends on may
not, which is the single biggest cause of "the page is here but nobody can see
it." Source: https://support.atlassian.com/migration/docs/what-migrates-with-the-confluence-cloud-migration-assistant/

## Implemented this batch

| kind | what it catches | detection (count-only) |
|------|-----------------|------------------------|
| **`orphaned_pages`** (Structure, high/medium) | Pages OUTSIDE the space homepage subtree don't render in the Cloud sidebar tree — reachable only by search/direct link. The classic DC→Cloud breakage when a trashed/unmigrated/restricted parent promotes its children to root. | Per space: `totalSize(space="K" and type=page)` − `totalSize(space="K" and type=page and ancestor=HOMEPAGE_ID)` − 1. The homepage **content id** (not a title) is used transiently and never stored. |
| **`unsupported_macro_usage`** (DataQuality, high/medium) | Pages using a macro that renders as "Unknown macro"/blank in Cloud: Marketplace-app macros with no installed Cloud renderer (Gliffy, draw.io — HIGH) or built-ins Atlassian removed (Chart, Gallery, Page Index — MEDIUM). CCMA moves the markup, not the app. | Per curated key in `RISKY_MACROS`: `totalSize(macro="<key>")`. The `macro` CQL field is a confirmed Cloud field (a type name, never page content). |
| **`cross_space_include_risk`** (DataQuality, medium) **[DONE 2026-06-16]** | Pages using **Include Page** (`include`) or **Excerpt Include** (`excerpt-include`). These macros are SUPPORTED on Cloud, so they are NOT `unsupported_macro_usage` — but the reference targets a page by title/space. If the referenced page migrates in a different batch, lands in a renamed space, or is left behind, the include resolves to nothing and the consumer page renders a **blank section**: content silently disappears. | The two macro keys are added to `RISKY_MACROS` under a `content_visibility` category; the check special-cases that category to emit a distinct kind (right guidance: migrate referenced pages in the same batch, spot-check after cutover). Same `totalSize(macro="<key>")` probe — counts only. |

All three are **human-tier** (re-parenting / app installation / content rework /
include verification are manual). Privacy: counts + macro type names + space
keys only — never page titles/bodies, identities, or the homepage id.

## Already covered (prior batches)

`space_no_homepage` (collapsed tree), `empty_space` (failed/timed-out space
import), `anonymous_space_access` (public→private surface), `space_no_admin`,
`drafts_pileup` (unpublished drafts), `archived_space_clutter`,
`personal_space_sprawl`, `space_count_near_guardrail`.

## Deferred (future batches — feasible, lower priority or heavier)

- `restricted_page_count` (B1) — per-page restriction sweep (boolean-per-page →
  aggregate count). High value, but a per-page probe; needs a capped sweep.
  **Held back deliberately**: the Cloud restriction sub-resource behaviour
  (`/rest/api/content/{id}/restriction/byOperation/read` after the
  `/rest/api/content` removal vs the v2 `/api/v2/pages/{id}/restrictions`)
  isn't verifiable from this environment, and the count-only audit avoids
  per-page calls by design — won't ship an unverified per-page Cloud call.
- `space_view_grant_missing` (B3) — a space with zero VIEW grants is invisible to
  all; needs the v1 `?expand=permissions` view-grant count.
- `anonymous_access_lost` (B4) — public KB spaces silently gone private; v1
  `?expand=permissions` anonymous-view boolean (v2 omits it).
- `archived_page_count` (B6) / `draft_page_count` (B7, instance-wide variant) —
  via `cqlcontext.contentStatuses` / v2 `?status=`.
- `space_count_reconciliation` (D3) — dropped/collided/mis-typed spaces; needs a
  source-vs-target space-count diff (the migration-audit side).

Sources: Atlassian migration docs (support.atlassian.com/migration),
"space and pages are missing after migration" KB, "learn which macros are being
removed", CCMA "what migrates" / "assess apps", and the Cloud CQL-fields doc.
