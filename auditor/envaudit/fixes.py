"""3-tier suggested fix and category annotation for every environment-audit finding.

Tier taxonomy (spec R3/R6):
  app        -- Fixable by the app.  The app can auto-fix via a deterministic,
                reversible API call when the user consents.
  human      -- Fixable by a human.  Requires review, a design decision, or a
                manual action in Jira (screen config, which options to keep,
                workflow design, picking a lead, etc.).
  unfixable  -- Re-migration suggested.  Post-migration corruption that cannot
                be safely resolved in place; the recommended action is to
                re-run the migration cleanly.

Category taxonomy (spec R6):
  Performance  -- object-count thresholds that degrade Jira indexing / JQL.
  Security     -- permission or access-control risks.
  Structure    -- broken workflow or scheme relationships.
  Coverage     -- areas that could not be evaluated (skip or error).
  Hygiene      -- duplicate, unused, stale, or leaked migration objects.
  DataQuality  -- issue-level data defects surfaced by count-only JQL probes
                  (done-but-unresolved, stale/unassigned open issues, and the
                  resolved-but-open mirror defect). Counts only — no issue
                  content, keys, or identities are ever read.
"""
from __future__ import annotations

import copy

from auditor.envaudit.deeplinks import deep_link

# ---------------------------------------------------------------------------
# Category mapping
# ---------------------------------------------------------------------------

_CATEGORY_MAP: dict[str, str] = {
    # Performance
    "field_sprawl": "Performance",
    "large_option_set": "Performance",
    "workflow_sprawl": "Performance",
    "status_sprawl": "Performance",
    "screen_sprawl": "Performance",
    "permission_scheme_sprawl": "Performance",
    "resolution_sprawl": "Performance",
    "priority_sprawl": "Performance",
    "issue_type_sprawl": "Performance",
    "link_type_sprawl": "Performance",
    "large_workflow": "Performance",
    "near_field_limit": "Performance",
    "near_issue_type_limit": "Performance",
    "near_priority_limit": "Performance",
    "near_workflow_limit": "Performance",
    "dashboard_filter_volume_high": "Performance",
    # Security
    "permission_grant_overly_broad": "Security",
    "public_browse_grant": "Security",
    "group_name_collision_reserved": "Security",
    "unsupported_custom_field_type": "DataQuality",
    "large_group_admin_bloat": "Security",
    "anonymous_write_grant": "Security",
    "admin_grant_to_logged_in": "Security",
    "shared_object_owned_by_inactive": "Security",
    "public_shared_filter": "Security",
    "public_shared_dashboard": "Security",
    # Structure
    "workflow_no_transitions": "Structure",
    "status_not_in_workflow": "Structure",
    "unreachable_status": "Structure",
    "dead_end_status": "Structure",
    # Coverage
    "capability_gap": "Coverage",
    "area_error": "Coverage",
    # DataQuality — issue-level defects surfaced by count-only JQL probes.
    "done_but_unresolved": "DataQuality",
    "resolved_but_open": "DataQuality",
    "stale_open_issues": "DataQuality",
    "unassigned_unresolved_high": "DataQuality",
    # Hygiene (everything else)
    "duplicate_field": "Hygiene",
    "unused_custom_field": "Hygiene",
    "empty_screen": "Hygiene",
    "scheme_unused": "Hygiene",
    "unused_issue_type_scheme": "Hygiene",
    "unused_issue_type_screen_scheme": "Hygiene",
    "empty_group": "Hygiene",
    "version_overdue": "Hygiene",
    "version_archived_unreleased": "Hygiene",
    "component_no_lead": "Hygiene",
    "component_unassigned_default": "Hygiene",
    "migration_artifact": "Hygiene",
    "unused_resolution": "Hygiene",
    "duplicate_status_name": "Hygiene",
    "duplicate_issue_type_name": "Hygiene",
    "redundant_priority_set": "Hygiene",
    "many_overdue_versions_in_project": "Hygiene",
    "board_count_exceeds_projects": "Hygiene",
    "version_naming_inconsistent": "Hygiene",
    "empty_project": "Hygiene",
    "inactive_project": "Hygiene",
    "global_transition_overuse": "Hygiene",
    "workflow_unreferenced": "Hygiene",
    "screen_not_in_scheme": "Hygiene",
    # ------------------------------------------------------------------
    # Confluence environment-audit kinds (spec R2/R3). Categories reuse the
    # same 6-category taxonomy. anonymous_write_grant / capability_gap /
    # area_error are shared with Jira and already mapped above.
    # ------------------------------------------------------------------
    # Performance
    "large_space": "Performance",
    "space_count_near_guardrail": "Performance",
    # Security
    "space_no_admin": "Security",
    "anonymous_space_access": "Security",
    "space_permission_to_anyone": "Security",
    "restricted_pages": "Security",
    "permission_grant_to_empty_group": "Security",
    # Structure
    "space_no_homepage": "Structure",
    "orphaned_pages": "Structure",
    "apps_to_assess_for_cloud": "Structure",
    "script_app_present": "Structure",
    # DataQuality
    "stale_page_ratio_high": "DataQuality",
    "unsupported_macro_usage": "DataQuality",
    "cross_space_include_risk": "DataQuality",
    # Hygiene
    "empty_space": "Hygiene",
    "archived_space_clutter": "Hygiene",
    "personal_space_sprawl": "Hygiene",
    "drafts_pileup": "Hygiene",
    "label_sprawl": "Hygiene",
    "template_sprawl": "Hygiene",
    "confluence_empty_group": "Hygiene",
}


def category_for(kind: str) -> str:
    """Return the category for a finding kind.

    Returns one of: Performance, Security, Structure, Coverage, Hygiene.
    Unknown kinds fall back to Hygiene (safe default).
    """
    return _CATEGORY_MAP.get(kind, "Hygiene")


# ---------------------------------------------------------------------------
# Fix registry
# ---------------------------------------------------------------------------

# Each entry shape:
#   tier:        "app" | "human" | "unfixable"
#   tier_label:  human-facing label matching the tier
#   label:       short GENERIC problem-type name (NOT the per-object title and
#                NOT personalised). Used by the UI to group findings of the
#                same kind under one problem-card header, e.g. "Component has
#                no lead". One label per kind, shared by every affected object.
#   title:       short action label (used as heading in the UI)
#   detail:      paragraph explaining what to do and why
#   api_hint:    REST path for app-tier entries; None for human/unfixable
#   risk:        "low" | "medium" | "high" (risk of taking the action)
#   reversible:  bool — True when the action can be undone without data loss
#   caveat:      optional extra warning; None if not applicable

_FIXES: dict[str, dict] = {

    # ------------------------------------------------------------------
    # tier: app — deletable scheme with no project attached
    # ------------------------------------------------------------------

    "scheme_unused": {
        "tier": "app",
        "tier_label": "Fixable by the app",
        "label": "Unused scheme",
        "title": "Delete unused scheme",
        "detail": (
            "This scheme is not attached to any project and can be safely deleted. "
            "Removing it reduces configuration noise and shortens scheme picker lists. "
            "The app will call the scheme-delete API; the scheme can be recreated from "
            "scratch if needed."
        ),
        "api_hint": "DELETE /rest/api/3/workflowscheme/{id}",
        "risk": "low",
        "reversible": True,
        "caveat": (
            "Verify the scheme is truly unused before deleting — "
            "Jira project membership data is sampled from the current project list."
        ),
    },

    "unused_issue_type_scheme": {
        "tier": "app",
        "tier_label": "Fixable by the app",
        "label": "Unused issue type scheme",
        "title": "Delete unused issue type scheme",
        "detail": (
            "This issue type scheme has no projects associated with it. "
            "Deleting it trims the scheme catalogue and prevents accidental reuse. "
            "The app will call the issue type scheme delete endpoint."
        ),
        "api_hint": "DELETE /rest/api/3/issuetypescheme/{issueTypeSchemeId}",
        "risk": "low",
        "reversible": True,
        "caveat": None,
    },

    "unused_issue_type_screen_scheme": {
        "tier": "app",
        "tier_label": "Fixable by the app",
        "label": "Unused issue type screen scheme",
        "title": "Delete unused issue type screen scheme",
        "detail": (
            "This issue type screen scheme is associated with no projects. "
            "The app can delete it to reduce scheme sprawl. "
            "It can be recreated if a project needs it in the future."
        ),
        "api_hint": "DELETE /rest/api/3/issuetypescreenscheme/{issueTypeScreenSchemeId}",
        "risk": "low",
        "reversible": True,
        "caveat": None,
    },

    "empty_group": {
        "tier": "app",
        "tier_label": "Fixable by the app",
        "label": "Empty group",
        "title": "Delete empty group",
        "detail": (
            "This group has zero members. Empty groups accumulate during migrations "
            "and can clutter permission pickers and group lists. "
            "The app will call the group delete API. "
            "The group can be recreated and repopulated at any time."
        ),
        "api_hint": "DELETE /rest/api/3/group?groupId={groupId}",
        "risk": "low",
        "reversible": True,
        "caveat": (
            "Only groups from the probed subset are evaluated. "
            "Groups with a zero count in the snapshot may still hold indirect memberships "
            "not surfaced by the API; confirm before deleting."
        ),
    },

    # ------------------------------------------------------------------
    # tier: unfixable — post-migration corruption
    # ------------------------------------------------------------------

    "migration_artifact": {
        "tier": "unfixable",
        "tier_label": "Re-migration suggested",
        "label": "Migration artifact (duplicate)",
        "title": "Remove migration duplicate — re-run migration",
        "detail": (
            "A migrated object with a migration suffix collides with an existing object "
            "of the same base name. This indicates a partial or duplicate migration. "
            "Renaming or deleting the artifact risks data loss if issue history references it. "
            "The safest remediation is to clean the target environment and re-run the migration "
            "from a known-good source snapshot."
        ),
        "api_hint": None,
        "risk": "high",
        "reversible": False,
        "caveat": (
            "Before re-migrating, archive or export issue data linked to the duplicate "
            "object to avoid losing history."
        ),
    },

    "group_name_collision_reserved": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Group name collides with a reserved Cloud group",
        "title": "Rename the group before migrating to Cloud",
        "detail": (
            "This Data Center group shares its name with a group Cloud reserves "
            "(e.g. administrators, site-admins, jira-administrators). On a "
            "DC-to-Cloud migration Cloud MERGES same-named groups, so this "
            "group's members silently inherit the reserved group's "
            "permissions — a permission escalation and a source of unexpected "
            "paid access. Rename the Data Center group to something unique "
            "before migrating (a mandatory JCMA pre-migration fix). Who should "
            "be in which group is an admin decision."
        ),
        "api_hint": None,
        "risk": "high",
        "reversible": True,
        "caveat": None,
    },

    "apps_to_assess_for_cloud": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "User-installed apps need a Cloud-compatibility check",
        "title": "Assess installed apps for Cloud before migrating",
        "detail": (
            "This instance has user-installed Marketplace apps. Apps are the #1 "
            "migration blocker: each must be assessed for a Cloud equivalent — "
            "an app with no Cloud version (or whose data doesn't auto-migrate) "
            "blocks or fragments the migration, and its config/data must be "
            "rebuilt or replaced. Run the Cloud Migration Assistant's 'Assess "
            "apps' step, uninstall apps you no longer need, and plan a "
            "per-app migration path. Which apps are essential is an admin "
            "decision."
        ),
        "api_hint": None, "risk": "low", "reversible": True, "caveat": None,
    },

    "script_app_present": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "A scripting app is installed (config not migrated)",
        "title": "Plan to rebuild scripted automation for Cloud",
        "detail": (
            "A scripting app (ScriptRunner, JSU, JMWE, …) is installed. Its "
            "scripted fields, listeners, behaviours, and non-native workflow "
            "post-functions/conditions/validators do NOT migrate to Cloud — they "
            "must be rebuilt with the app's Cloud edition (where APIs differ) or "
            "replaced with native Cloud automation. Inventory every script and "
            "workflow rule before migrating; a missed one is silent feature loss. "
            "The rebuild is an engineering task scoped by an admin."
        ),
        "api_hint": None, "risk": "medium", "reversible": True, "caveat": None,
    },

    "unsupported_custom_field_type": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Custom fields use a type Cloud may not migrate",
        "title": "Review app-provided custom field types before migrating",
        "detail": (
            "These custom fields use a type provided by an app or a Data "
            "Center-only field type rather than a built-in Jira type. The Cloud "
            "Migration Assistant migrates the field definition but SILENTLY "
            "DROPS the stored values for unsupported types, so the data is lost "
            "without an error. Before migrating, install and migrate the "
            "equivalent Cloud app (or convert the fields to a supported built-in "
            "type), and confirm each type is supported. Which fields matter and "
            "how to convert them is a content decision for an admin."
        ),
        "api_hint": None,
        "risk": "low",
        "reversible": True,
        "caveat": None,
    },

    # ------------------------------------------------------------------
    # tier: human — all other 25 kinds
    # ------------------------------------------------------------------

    "duplicate_field": {
        # INTENTIONALLY human-tier: merging two custom fields is destructive data
        # migration (which field to keep, then bulk-moving every issue's values
        # onto it) and the correct field to keep is a business decision the app
        # cannot infer. We deliberately do NOT auto-merge or auto-populate; this
        # stays guidance. (unused_custom_field is the auto-deletable sibling — it
        # only fires when the field is on no screen AND holds no values.)
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Duplicate custom field",
        "title": "Merge or rename duplicate custom field",
        "detail": (
            "Two custom fields normalise to the same name, suggesting a migration "
            "created a copy of an existing field. "
            "Review which field holds live issue data, migrate values to the canonical "
            "field using a bulk-edit or script, then delete the redundant field. "
            "This cannot be automated because the correct field to keep is a business decision."
        ),
        "api_hint": None,
        "risk": "medium",
        "reversible": True,
        "caveat": (
            "Deleting a custom field removes all its stored values from every issue. "
            "Export or migrate values before deletion."
        ),
    },

    "unused_custom_field": {
        "tier": "app",
        "tier_label": "Fixable by the app",
        "label": "Custom field not on any screen",
        "title": "Delete unused custom field",
        "detail": (
            "This custom field does not appear on any screen, meaning it cannot be "
            "viewed or edited through the standard Jira UI. "
            "The app can delete it, but ONLY after re-confirming at apply time that "
            "the field is on no screen AND holds no values on any issue (a "
            "cf[id] is not EMPTY count of zero). A field that holds data, or is on "
            "a low-id system field, is skipped for manual review."
        ),
        "api_hint": "DELETE /rest/api/3/field/{id}",
        "risk": "high",
        "reversible": False,
        "caveat": (
            "Deleting a custom field is irreversible and destroys its stored values "
            "on every issue. The app only applies this when the field is on no "
            "screen and holds no values; deleting a field with data is irreversible."
        ),
    },

    "empty_screen": {
        "tier": "app",
        "tier_label": "Fixable by the app",
        "label": "Empty screen",
        "title": "Delete empty screen",
        "detail": (
            "This screen has no fields configured. An empty screen shows a blank "
            "form to users during issue creation or editing. "
            "The app can delete it; at apply time it re-fetches the screen's "
            "tabs and fields and aborts if the screen has regained any field."
        ),
        "api_hint": "DELETE /rest/api/3/screens/{id}",
        "risk": "low",
        "reversible": True,
        "caveat": (
            "A screen still referenced by a screen scheme cannot be deleted by "
            "Jira; built-in/default screens (e.g. Default Screen) are never "
            "touched. Confirm the screen is genuinely empty before deleting."
        ),
    },

    "workflow_no_transitions": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Workflow has no transitions",
        "title": "Add transitions to workflow",
        "detail": (
            "This workflow has statuses but no transitions, making issues permanently "
            "stuck in their initial status. "
            "Open the workflow editor in Jira and add the required transitions between statuses. "
            "The correct transition design depends on the team process and cannot be inferred automatically."
        ),
        "api_hint": None,
        "risk": "medium",
        "reversible": True,
        "caveat": (
            "Changing workflow transitions on a live workflow affects all projects using it. "
            "Test in a staging environment first."
        ),
    },

    "status_not_in_workflow": {
        "tier": "app",
        "tier_label": "Fixable by the app",
        "label": "Status not used by any workflow",
        "title": "Delete a status used by no workflow",
        "detail": (
            "This status exists in Jira but is not referenced by any workflow, so "
            "no issue can ever reach it; it is migration clutter. The app can "
            "delete it, but ONLY after re-confirming at apply time that the status "
            "is still in NO workflow AND that zero issues currently sit in it (a "
            "status = count of zero). A built-in/default status, a status that "
            "has been wired into a workflow since the audit, or one that holds "
            "issues is skipped for manual review."
        ),
        "api_hint": "DELETE /rest/api/3/statuses?id={id}",
        "risk": "medium",
        "reversible": True,
        "caveat": (
            "Deleting a status is a global, instance-wide change. The app only "
            "applies it when the status is in no workflow and holds no issues, "
            "and never touches a built-in/default status (low id or a well-known "
            "system name). A status can be recreated, but deleting one that holds "
            "issues would lose their state, so the app refuses in that case."
        ),
    },

    "field_sprawl": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Too many custom fields",
        "title": "Reduce custom field count",
        "detail": (
            "The number of custom fields exceeds the Atlassian health threshold, "
            "which degrades JQL query performance and indexing. "
            "Audit the field list for duplicates, unused fields, and migration leftovers. "
            "Consolidate related fields and delete those that carry no live issue data. "
            "Prioritise high-severity findings in the field category first."
        ),
        "api_hint": None,
        "risk": "medium",
        "reversible": True,
        "caveat": (
            "Deleting a custom field is permanent and removes all stored values. "
            "Export data before deletion."
        ),
    },

    "large_option_set": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Field option list too large",
        "title": "Trim field option list",
        "detail": (
            "This select or multi-select field has a very large number of options. "
            "Jira loads all options when rendering issue forms, which slows page load "
            "and degrades the picker UX. "
            "Review the option list and remove stale, duplicate, or migrated options. "
            "Deciding which options to keep is a business decision."
        ),
        "api_hint": None,
        "risk": "low",
        "reversible": True,
        "caveat": (
            "Options that have been used on issues cannot be deleted without "
            "first bulk-updating those issues to a different value."
        ),
    },

    "workflow_sprawl": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Too many workflows",
        "title": "Consolidate workflows",
        "detail": (
            "The number of workflows exceeds the Atlassian health threshold. "
            "Excessive workflows increase administrative overhead and degrade instance performance. "
            "Identify workflows with identical or near-identical structures and merge them. "
            "Consolidation requires reviewing project requirements and scheme assignments."
        ),
        "api_hint": None,
        "risk": "medium",
        "reversible": True,
        "caveat": None,
    },

    "status_sprawl": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Too many statuses",
        "title": "Reduce status count",
        "detail": (
            "The number of statuses exceeds the Atlassian health threshold. "
            "Too many statuses make boards hard to read and slow down status-based JQL queries. "
            "Review statuses for duplicates (especially migration copies), consolidate where "
            "semantics overlap, and delete orphan statuses not referenced by any workflow."
        ),
        "api_hint": None,
        "risk": "low",
        "reversible": True,
        "caveat": None,
    },

    "screen_sprawl": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Too many screens",
        "title": "Reduce screen count",
        "detail": (
            "The number of screens exceeds the Atlassian health threshold. "
            "Screen sprawl makes administration more complex and slows scheme lookups. "
            "Identify screens that are identical or no longer attached to any screen scheme "
            "and remove them."
        ),
        "api_hint": None,
        "risk": "low",
        "reversible": True,
        "caveat": None,
    },

    "permission_scheme_sprawl": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Too many permission schemes",
        "title": "Consolidate permission schemes",
        "detail": (
            "The number of permission schemes exceeds the Atlassian health threshold. "
            "Excess schemes are usually a sign of per-project copies created during migration. "
            "Audit schemes for identical or near-identical grant sets, consolidate them into "
            "shared schemes, and reassign projects. "
            "Consolidation requires reviewing security requirements per project."
        ),
        "api_hint": None,
        "risk": "medium",
        "reversible": True,
        "caveat": None,
    },

    "version_overdue": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Overdue unreleased version",
        "title": "Release or reschedule overdue version",
        "detail": (
            "This project version is past its due date and has not been released. "
            "Mark the version as released if all planned work is done, "
            "update the due date to reflect the new target, "
            "or archive it if the release has been cancelled. "
            "The appropriate action depends on the team roadmap."
        ),
        "api_hint": None,
        "risk": "low",
        "reversible": True,
        "caveat": None,
    },

    "version_archived_unreleased": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Archived version never released",
        "title": "Release or delete archived unreleased version",
        "detail": (
            "This version is archived but was never formally released. "
            "Archived-unreleased versions accumulate from cancelled milestones or "
            "migration leftovers and clutter the version picker. "
            "Either release it retroactively with the correct date or delete it "
            "after moving any open issues to an active version."
        ),
        "api_hint": None,
        "risk": "low",
        "reversible": True,
        "caveat": None,
    },

    "component_no_lead": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Component has no lead",
        "title": "Assign a lead to the component",
        "detail": (
            "This project component has no lead assigned. "
            "A component without a lead cannot automatically assign issues reported "
            "against it to a responsible person. "
            "Go to the project component settings and assign the appropriate team member "
            "as the component lead."
        ),
        "api_hint": None,
        "risk": "low",
        "reversible": True,
        "caveat": None,
    },

    "permission_grant_overly_broad": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Overly broad permission grant",
        "title": "Restrict overly broad permission grant",
        "detail": (
            "A sensitive administrative permission in this scheme is granted to anyone "
            "(the built-in public holder type), meaning every authenticated and anonymous "
            "user has that permission. "
            "Open the permission scheme editor and change the grant to a specific group, "
            "role, or user. "
            "The correct target group or role depends on your security policy."
        ),
        "api_hint": None,
        "risk": "high",
        "reversible": True,
        "caveat": (
            "Removing a broad permission grant may immediately lock out users who "
            "relied on it. Coordinate with your Jira administrator before changing."
        ),
    },

    "resolution_sprawl": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Too many resolutions",
        "title": "Reduce resolution count",
        "detail": (
            "The number of resolutions exceeds the Atlassian health threshold. "
            "Resolutions are a global, shared list, so a bloated catalogue is "
            "almost always per-project copies left behind by a migration. "
            "Review the list, map duplicate or overlapping resolutions onto a "
            "small canonical set, bulk-update the affected issues, then delete "
            "the redundant resolutions. Deciding which resolutions to keep is a "
            "reporting decision that an admin must make."
        ),
        "api_hint": None,
        "risk": "medium",
        "reversible": True,
        "caveat": (
            "Resolutions in use on issues cannot be deleted until those issues "
            "are bulk-updated to a kept resolution. Export first."
        ),
    },

    "priority_sprawl": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Too many priorities",
        "title": "Reduce priority count",
        "detail": (
            "The number of priorities exceeds the Atlassian health threshold. "
            "Priorities are a global, shared list; an oversized set usually "
            "comes from merging several source instances during migration. "
            "Too many priorities make triage inconsistent and clutter every "
            "issue form. Consolidate to a concise scheme, remap issues onto the "
            "kept priorities, and delete the rest. Choosing the canonical set is "
            "a process decision for an admin."
        ),
        "api_hint": None,
        "risk": "medium",
        "reversible": True,
        "caveat": (
            "Changing or removing a priority affects existing issues and any "
            "JQL or automation that filters on it. Review dependencies first."
        ),
    },

    "issue_type_sprawl": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Too many issue types",
        "title": "Reduce issue type count",
        "detail": (
            "The number of issue types exceeds the Atlassian health threshold. "
            "A large global issue type catalogue slows issue creation pickers "
            "and is typically a sign of per-team or per-migration duplicates. "
            "Audit for near-identical types, consolidate them via issue type "
            "schemes, migrate issues onto the kept types, then delete the "
            "redundant ones. Which types to keep depends on team process and "
            "must be decided by an admin."
        ),
        "api_hint": None,
        "risk": "medium",
        "reversible": True,
        "caveat": (
            "An issue type cannot be deleted while issues still use it; move "
            "those issues to a kept type first."
        ),
    },

    "link_type_sprawl": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Too many issue link types",
        "title": "Reduce issue link type count",
        "detail": (
            "The number of issue link types exceeds the Atlassian health "
            "threshold. Link types are global, and an inflated list is commonly "
            "a migration artefact from combining instances. Excess link types "
            "confuse users and clutter the link picker. Review for duplicate or "
            "synonymous link types, consolidate them, and delete the rest. "
            "Picking the canonical link vocabulary is an admin decision."
        ),
        "api_hint": None,
        "risk": "low",
        "reversible": True,
        "caveat": (
            "Deleting a link type removes the links of that type from all "
            "issues. Confirm no live relationships depend on it first."
        ),
    },

    "large_workflow": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Oversized workflow",
        "title": "Simplify oversized workflow",
        "detail": (
            "This workflow has an unusually large number of statuses or "
            "transitions, which makes it hard to maintain, slows the workflow "
            "editor, and can degrade transition evaluation. Open the workflow in "
            "the editor and look for redundant statuses, parallel paths that can "
            "be merged, and transitions that are never used. Simplifying a live "
            "workflow is a design decision that depends on the team process, so "
            "an admin must drive it."
        ),
        "api_hint": None,
        "risk": "medium",
        "reversible": True,
        "caveat": (
            "Editing a shared workflow affects every project that uses it. "
            "Test the simplified design in a staging environment first."
        ),
    },

    "public_browse_grant": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Public browse access",
        "title": "Review public browse access",
        "detail": (
            "This permission scheme grants Browse Projects to anyone, the "
            "built-in public holder type, which can expose projects and every "
            "issue in them to anonymous, unauthenticated users. Confirm whether "
            "public visibility is intentional. If it is not, open the permission "
            "scheme editor and change the Browse Projects grant to a specific "
            "group, role, or application access. Whether public access is "
            "acceptable is a security decision for an admin."
        ),
        "api_hint": None,
        "risk": "medium",
        "reversible": True,
        "caveat": (
            "Removing public browse access immediately hides the affected "
            "projects from anonymous users and any unauthenticated integration "
            "that relied on it. Verify external consumers first."
        ),
    },

    "component_unassigned_default": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Component default assignee unassigned",
        "title": "Set a default assignee for the component",
        "detail": (
            "This component uses an Unassigned default assignee, so issues "
            "created against it are left with no assignee and can fall through "
            "the cracks. Open the project component settings and set a more "
            "useful default, such as Project Lead or Component Lead, so new "
            "issues route to a responsible person. The right default depends on "
            "how the team divides ownership, so an admin must choose it."
        ),
        "api_hint": None,
        "risk": "low",
        "reversible": True,
        "caveat": (
            "Changing the default assignee only affects newly created issues; "
            "existing unassigned issues are not reassigned automatically."
        ),
    },

    "near_field_limit": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Site-wide fields exceed the per-project limit",
        "title": "Verify no project nears the 700-field limit",
        "detail": (
            "Jira Cloud enforces a hard limit of 700 custom fields PER "
            "company-managed project. The site-wide field count exceeds 700, so "
            "a per-project breach is possible — but it cannot be confirmed from "
            "the site total alone. Check each company-managed project's field "
            "count, and reduce sprawl: audit the field list for duplicates, "
            "unused fields, and migration leftovers, delete those carrying no "
            "live issue data, and context-limit fields used by only a few "
            "projects."
        ),
        "api_hint": None,
        "risk": "medium",
        "reversible": True,
        "caveat": (
            "Deleting a custom field is permanent and removes all its stored "
            "values from every issue. Export data before deletion."
        ),
    },

    "near_issue_type_limit": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Site-wide issue types exceed the per-project limit",
        "title": "Verify no project nears the 150 issue-type limit",
        "detail": (
            "Jira Cloud enforces a hard limit of 150 issue (work) types PER "
            "company-managed project. The site-wide issue-type count exceeds "
            "150, so a per-project breach is possible — but it cannot be "
            "confirmed from the site total alone. Check each company-managed "
            "project, then merge duplicate or near-identical issue types, retire "
            "unused ones, and share a common issue-type scheme across projects "
            "rather than per-project copies. Which types to keep is a process "
            "decision."
        ),
        "api_hint": None, "risk": "low", "reversible": True, "caveat": None,
    },

    "near_priority_limit": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Past the recommended priority guardrail",
        "title": "Consolidate priorities toward the guardrail",
        "detail": (
            "The site-wide priority count is past Atlassian's recommended "
            "guardrail of 100 priorities (a performance recommendation, not a "
            "hard block). Collapse redundant or migrated priority variants onto "
            "a single canonical priority scheme and remove unused priorities; "
            "this also restores trustworthy priority reporting. The canonical "
            "set is a process decision for the admin."
        ),
        "api_hint": None, "risk": "low", "reversible": True, "caveat": None,
    },

    "near_workflow_limit": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Past the recommended workflow guardrail",
        "title": "Consolidate workflows toward the guardrail",
        "detail": (
            "The site-wide workflow count is past the recommended guardrail of "
            "150 workflows (a performance recommendation, not a hard block). "
            "Retire unreferenced workflows, merge near-duplicate ones, and share "
            "workflows across projects via a common workflow scheme instead of "
            "per-project copies. Confirm a workflow is unused before deleting it."
        ),
        "api_hint": None, "risk": "low", "reversible": True, "caveat": None,
    },

    "unused_resolution": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Non-standard resolution",
        "title": "Review non-standard resolution",
        "detail": (
            "This resolution is outside the canonical default set that ships "
            "with Jira, so it is most likely a per-project value left behind by "
            "a migration. Surplus resolutions fragment reporting and the "
            "Unresolved filter. Confirm whether any issues still use it, remap "
            "those issues onto a kept resolution, then delete the redundant "
            "value. Which resolutions to keep is a reporting decision for an admin."
        ),
        "api_hint": None,
        "risk": "medium",
        "reversible": True,
        "caveat": (
            "A resolution still applied to issues cannot be deleted until those "
            "issues are bulk-updated to a kept resolution. Export first."
        ),
    },

    "duplicate_status_name": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Duplicate status name",
        "title": "Resolve duplicate status name",
        "detail": (
            "Two statuses normalise to the same name, differing only in casing "
            "or whitespace. Duplicate status names break board column mapping "
            "and JQL status queries, and are a frequent migration artefact. "
            "Decide which status is canonical, move issues off the duplicate, "
            "and delete or rename the redundant one. The correct status to keep "
            "depends on the team workflow."
        ),
        "api_hint": None,
        "risk": "medium",
        "reversible": True,
        "caveat": (
            "A status in use cannot be deleted until issues are transitioned "
            "off it. Review board and workflow references first."
        ),
    },

    "duplicate_issue_type_name": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Duplicate issue type name",
        "title": "Resolve duplicate issue type name",
        "detail": (
            "Two issue types normalise to the same name, differing only in "
            "casing or whitespace. Duplicate issue-type names confuse issue-type "
            "schemes and reporting and usually come from a partial migration. "
            "Choose the canonical type, migrate issues onto it, then delete the "
            "redundant type. Which type to keep is a process decision for an admin."
        ),
        "api_hint": None,
        "risk": "medium",
        "reversible": True,
        "caveat": (
            "An issue type cannot be deleted while issues still use it; move "
            "those issues to the kept type first."
        ),
    },

    "redundant_priority_set": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Near-duplicate priority",
        "title": "Consolidate near-duplicate priorities",
        "detail": (
            "The priority list is larger than the default set and contains a "
            "near-duplicate value, which fragments triage and SLA reporting. "
            "Map the overlapping priorities onto a concise canonical scheme, "
            "remap the affected issues, then delete the redundant value. "
            "Choosing the canonical priority set is a process decision for an admin."
        ),
        "api_hint": None,
        "risk": "medium",
        "reversible": True,
        "caveat": (
            "Changing or removing a priority affects existing issues and any "
            "JQL or automation that filters on it. Review dependencies first."
        ),
    },

    "many_overdue_versions_in_project": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Project with many overdue versions",
        "title": "Triage a project with many overdue versions",
        "detail": (
            "This project has several overdue, unreleased versions, which "
            "signals an abandoned or neglected release calendar. Stale versions "
            "clutter the version picker and distort release reporting. Review "
            "each version: release it if the work is done, reschedule it with a "
            "realistic due date, or archive or delete it if the milestone is "
            "cancelled. The right action per version depends on the team roadmap."
        ),
        "api_hint": None,
        "risk": "low",
        "reversible": True,
        "caveat": None,
    },

    "large_group_admin_bloat": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Oversized administrator group",
        "title": "Review oversized administrator group",
        "detail": (
            "This group matches an administrator naming pattern and has an "
            "unusually large membership, which widens the blast radius if any "
            "member account is compromised. Review the membership and remove "
            "anyone who no longer needs administrative access, applying the "
            "principle of least privilege. Who needs admin rights is a security "
            "decision for your Jira administrator."
        ),
        "api_hint": None,
        "risk": "medium",
        "reversible": True,
        "caveat": (
            "Only the probed subset of groups is evaluated, and member counts "
            "are sampled. Confirm the membership in the admin UI before removing "
            "anyone."
        ),
    },

    "anonymous_write_grant": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Anonymous write access",
        "title": "Restrict anonymous write access",
        "detail": (
            "This permission scheme grants a write permission such as create "
            "issue, add comment, or edit issue to anyone, the built-in public "
            "holder type. That lets unauthenticated users add or change issue "
            "content, a higher-severity exposure than read-only browse. Confirm "
            "whether anonymous contribution is intentional. If not, open the "
            "permission scheme editor and change the grant to a specific group, "
            "role, or application access."
        ),
        "api_hint": None,
        "risk": "high",
        "reversible": True,
        "caveat": (
            "Removing anonymous write access immediately blocks unauthenticated "
            "submissions and any anonymous integration that relied on it. Verify "
            "external consumers first."
        ),
    },

    "admin_grant_to_logged_in": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Admin granted to all logged-in users",
        "title": "Restrict admin granted to all logged-in users",
        "detail": (
            "This permission scheme grants Administer or Administer Projects to "
            "all logged-in users via an application-access role or the logged-in "
            "user holder type. That effectively hands administrative control to "
            "every licensed user and is admin-group bloat by another name. Open "
            "the permission scheme editor and change the grant to a specific "
            "administrator group or project role. The correct target is a "
            "security decision for your Jira administrator."
        ),
        "api_hint": None,
        "risk": "high",
        "reversible": True,
        "caveat": (
            "Removing a broad admin grant may immediately lock out users who "
            "relied on it. Coordinate with your Jira administrator first."
        ),
    },

    "board_count_exceeds_projects": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Board sprawl",
        "title": "Review board sprawl",
        "detail": (
            "The number of boards is large relative to the number of projects, "
            "which usually means duplicate or abandoned per-team boards created "
            "without governance. Excess boards clutter navigation and slow board "
            "selection. Review the board list, identify duplicates and unused "
            "boards, and delete or consolidate them. Which boards to keep is a "
            "team decision."
        ),
        "api_hint": None,
        "risk": "low",
        "reversible": True,
        "caveat": None,
    },

    "dashboard_filter_volume_high": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "High shared-object volume",
        "title": "Review high shared-object volume",
        "detail": (
            "The number of shared filters or dashboards reached the gather "
            "ceiling, indicating a very large shared-object population. A high "
            "volume of filters and dashboards is an indexing and governance cost "
            "and a common cleanup target. Audit for stale, duplicate, or "
            "orphaned objects and remove those no longer in use. Which to keep "
            "is an owner decision."
        ),
        "api_hint": None,
        "risk": "low",
        "reversible": True,
        "caveat": (
            "The count was capped at the gather ceiling, so the true total may "
            "be higher. Treat this as a lower bound."
        ),
    },

    "version_naming_inconsistent": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Inconsistent version naming",
        "title": "Standardise version naming in the project",
        "detail": (
            "Within this project, version names mix a numeric convention such "
            "as 1.0 or 2.1 with free-text names. Inconsistent naming breaks "
            "release reporting and fix-version rollups and makes versions hard "
            "to sort. Agree on one naming convention for the project and rename "
            "the outliers to match. This is a low-confidence heuristic, so "
            "review before renaming."
        ),
        "api_hint": None,
        "risk": "low",
        "reversible": True,
        "caveat": None,
    },

    "empty_project": {
        "tier": "app",
        "tier_label": "Fixable by the app",
        "label": "Empty project",
        "title": "Delete empty project",
        "detail": (
            "This project contains zero issues, so it is dead configuration "
            "that still carries schemes, boards, and permission overhead. "
            "The app can delete it; at apply time it re-counts the project's "
            "issues and aborts if any issue now exists. On Cloud the deleted "
            "project is moved to the trash and is recoverable for about 60 days."
        ),
        "api_hint": "DELETE /rest/api/3/project/{key}",
        "risk": "medium",
        "reversible": True,
        "caveat": (
            "Deleting a project removes its boards, components, and versions. "
            "On Cloud the project goes to the trash (recoverable about 60 days), "
            "but the app still re-verifies the project holds zero issues before "
            "deleting and refuses if any issue has appeared."
        ),
    },

    "inactive_project": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Inactive project",
        "title": "Archive or delete inactive project",
        "detail": (
            "This project has issues but none have been updated in over a year, "
            "which signals an abandoned project. Inactive projects clutter the "
            "project list and carry full scheme and board overhead. Review with "
            "the owning team: archive the project if the work is complete, or "
            "delete it if it was never really used. The right call depends on "
            "whether the historical issues still matter."
        ),
        "api_hint": None,
        "risk": "low",
        "reversible": True,
        "caveat": (
            "Activity is measured from the project's last issue-update "
            "timestamp; confirm in the project before archiving or deleting."
        ),
    },

    "shared_object_owned_by_inactive": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Shared object owned by an inactive user",
        "title": "Reassign shared objects owned by inactive users",
        "detail": (
            "One or more shared filters or dashboards are owned by a "
            "deactivated user. They keep running and feeding boards and "
            "subscriptions, but no active user can edit or fix them, and they "
            "can silently leak data through their existing shares. Reassign "
            "each affected object to an active owner (a Jira admin can change "
            "the owner) or delete it if it is no longer needed. Who should own "
            "each object is a decision for the relevant team."
        ),
        "api_hint": None,
        "risk": "medium",
        "reversible": True,
        "caveat": (
            "The audit reports only an aggregate count of affected objects, not "
            "owner identities. Use the Jira shared-objects admin screens to find "
            "and reassign the specific filters and dashboards."
        ),
    },

    "public_shared_filter": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Publicly shared filter",
        "title": "Restrict publicly shared filter",
        "detail": (
            "One or more shared filters are shared with anyone on the web or "
            "with all logged-in users. A public filter exposes its JQL (and, on "
            "a public site, its results) to anonymous or unauthenticated users, "
            "a documented data-exposure path. Open each affected filter's share "
            "settings and restrict it to a specific group, role, or project. "
            "Whether broad sharing is acceptable is a security decision for an "
            "admin."
        ),
        "api_hint": None,
        "risk": "high",
        "reversible": True,
        "caveat": (
            "Restricting a widely-shared filter can break dashboards, "
            "subscriptions, or boards that depend on it. Identify consumers "
            "before narrowing the share."
        ),
    },

    "public_shared_dashboard": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Publicly shared dashboard",
        "title": "Restrict publicly shared dashboard",
        "detail": (
            "One or more shared dashboards are shared with anyone on the web or "
            "with all logged-in users, exposing the gadgets and underlying "
            "filter data to a broad audience. Open each affected dashboard's "
            "share settings and restrict it to a specific group, role, or "
            "project. Whether broad sharing is intended is a security decision "
            "for an admin."
        ),
        "api_hint": None,
        "risk": "medium",
        "reversible": True,
        "caveat": (
            "Restricting a shared dashboard hides it from users who relied on "
            "the public share. Confirm the intended audience before narrowing."
        ),
    },

    "unreachable_status": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Unreachable status",
        "title": "Reconnect or remove an unreachable status",
        "detail": (
            "This status is in the workflow but no transition leads to it, and "
            "it is not the create status, so no issue can ever enter it. It is "
            "usually left behind when a transition was deleted during a "
            "redesign or migration. Open the workflow editor and either add a "
            "transition that targets this status or remove the status if it is "
            "no longer part of the process. The right reconnection depends on "
            "the intended team workflow, so an admin must decide."
        ),
        "api_hint": None,
        "risk": "medium",
        "reversible": True,
        "caveat": (
            "Editing a shared workflow affects every project that uses it. "
            "Test the change in a staging environment first."
        ),
    },

    "dead_end_status": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Dead-end status",
        "title": "Add an outbound transition to a dead-end status",
        "detail": (
            "This status has no outbound transition and its name does not look "
            "like a terminal state such as Done or Closed, so issues that reach "
            "it become stuck with no way forward. Open the workflow editor and "
            "add the transitions the process needs out of this status, or "
            "rename it if it really is a terminal state. The correct outgoing "
            "paths depend on the team workflow, so an admin must define them."
        ),
        "api_hint": None,
        "risk": "medium",
        "reversible": True,
        "caveat": (
            "Editing a shared workflow affects every project that uses it. "
            "Test the change in a staging environment first."
        ),
    },

    "global_transition_overuse": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Too many global transitions",
        "title": "Reduce over-broad global transitions",
        "detail": (
            "This workflow defines many global transitions, the kind that can "
            "fire from any status. A few are useful, but a large number lets "
            "issues jump between states freely, which defeats process control "
            "and muddles status reporting. Review each global transition and "
            "convert the ones that should only run from specific states into "
            "directed transitions. Which transitions need to stay global is a "
            "process decision for an admin."
        ),
        "api_hint": None,
        "risk": "low",
        "reversible": True,
        "caveat": (
            "Narrowing a global transition changes which statuses can use it. "
            "Confirm no automation or team habit relies on the broad behaviour "
            "first."
        ),
    },

    "workflow_unreferenced": {
        "tier": "app",
        "tier_label": "Fixable by the app",
        "label": "Unreferenced workflow",
        "title": "Delete unreferenced workflow",
        "detail": (
            "This workflow is not used by any workflow scheme, so no project "
            "runs it. Unreferenced workflows are pure clutter that inflate the "
            "workflow list and slow the editor. The app can delete it; at apply "
            "time it re-reads workflow-scheme usage and aborts if any scheme now "
            "references the workflow. The built-in jira workflow is never touched."
        ),
        "api_hint": "DELETE /rest/api/3/workflow/{entityId}",
        "risk": "low",
        "reversible": True,
        "caveat": (
            "Scheme membership is re-read live before deleting. A workflow that "
            "has become referenced by a scheme is skipped, and system/default "
            "workflows are never deleted."
        ),
    },

    "screen_not_in_scheme": {
        "tier": "app",
        "tier_label": "Fixable by the app",
        "label": "Orphaned screen",
        "title": "Delete orphaned screen",
        "detail": (
            "This screen is not referenced by any screen scheme, so it is dead "
            "configuration that still loads in the screen editor and inflates "
            "the screen count. The app can delete it; at apply time it re-reads "
            "screen-scheme membership and aborts if any scheme now uses the "
            "screen. Built-in/default screens are never touched."
        ),
        "api_hint": "DELETE /rest/api/3/screens/{id}",
        "risk": "low",
        "reversible": True,
        "caveat": (
            "Screen-scheme membership is re-read live before deleting. A screen "
            "that has become referenced by a scheme is skipped, and "
            "system/default screens are never deleted."
        ),
    },

    # ------------------------------------------------------------------
    # tier: human — Section-3 issue-level / data-quality findings.
    # These need a human to triage and bulk-edit issues; the app does not
    # mass-edit issue data. Counts only; no issue content was ever read.
    # ------------------------------------------------------------------

    "done_but_unresolved": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Done but unresolved issues",
        "title": "Set a resolution on done-but-unresolved issues",
        "detail": (
            "Some issues sit in a Done-category status but have an empty "
            "resolution field. This is the most common Jira data defect: it "
            "breaks the Unresolved filter, release and version warnings, "
            "velocity, and burndown charts, because Jira treats a blank "
            "resolution as still open. Find the affected issues with the JQL "
            "statusCategory = Done AND resolution = EMPTY, then bulk-edit them "
            "to set the correct resolution, and fix the workflow transitions so "
            "future moves into a done status set a resolution. Which resolution "
            "applies to each issue is a judgement call, so an admin must triage "
            "them. The audit reports only the count, not the issues."
        ),
        "api_hint": None,
        "risk": "medium",
        "reversible": True,
        "caveat": (
            "Bulk-setting a resolution changes issue history and can trigger "
            "automation or notifications. Review the affected issues and "
            "suppress notifications before the bulk edit."
        ),
    },

    "resolved_but_open": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Resolved but open issues",
        "title": "Clear or correct resolved-but-open issues",
        "detail": (
            "Some issues have a resolution set while their status is still in a "
            "To Do or In Progress category. This mirror defect corrupts "
            "reporting and causes issues to look closed in some views and open "
            "in others, and it commonly leads to surprise reopens. Find them "
            "with the JQL resolution is not EMPTY AND statusCategory != Done, "
            "then either transition each issue to a done status if the work is "
            "complete or clear the resolution if it is genuinely still open. "
            "The right call per issue depends on the real state of the work, so "
            "an admin must triage them. The audit reports only the count."
        ),
        "api_hint": None,
        "risk": "medium",
        "reversible": True,
        "caveat": (
            "Clearing a resolution or transitioning an issue changes its "
            "history and may fire automation. Review the affected issues first."
        ),
    },

    "stale_open_issues": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Stale open issues",
        "title": "Triage stale open issues",
        "detail": (
            "A significant number of unresolved issues have not been updated in "
            "over a year. Stale open issues inflate the backlog, distort "
            "reporting, and are Atlassian's named archive and cleanup target. "
            "Find them with the JQL statusCategory != Done AND updated <= "
            "-365d, then review each one with the owning team: close it if it "
            "is obsolete, reprioritise it if it still matters, or archive the "
            "project if the whole backlog is abandoned. Whether a stale issue "
            "is still relevant is a team decision, so a human must triage them. "
            "The audit reports only the count, never the issues."
        ),
        "api_hint": None,
        "risk": "low",
        "reversible": True,
        "caveat": (
            "Staleness is measured by last-updated date; a recently reviewed "
            "but unchanged issue still counts as stale. Confirm before bulk "
            "closing."
        ),
    },

    "unassigned_unresolved_high": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Unassigned open issues",
        "title": "Assign or triage unowned open issues",
        "detail": (
            "A large share of unresolved issues have no assignee. Unowned open "
            "work tends to stall because no one is accountable for it, and a "
            "high unassigned count is a planning and hygiene red flag. Find the "
            "issues with the JQL resolution = EMPTY AND assignee is EMPTY, then "
            "route them to owners in a triage session or set component or "
            "automation rules that assign new issues by default. Who should own "
            "each issue is a team decision, so a human must do the assignment. "
            "The audit reports only the count and never reads any assignee "
            "identity."
        ),
        "api_hint": None,
        "risk": "low",
        "reversible": True,
        "caveat": (
            "Bulk-assigning issues can trigger a wave of notifications. "
            "Suppress notifications during the bulk edit and confirm owners "
            "first."
        ),
    },

    # ------------------------------------------------------------------
    # Confluence environment-audit fixes (spec R3).
    # Two app-tier kinds (empty_space, confluence_empty_group) carry a real
    # reversible api_hint; every other Confluence kind is human-tier review.
    # ------------------------------------------------------------------

    "empty_space": {
        "tier": "app",
        "tier_label": "Fixable by the app",
        "label": "Empty space",
        "title": "Archive empty space",
        "detail": (
            "This space holds zero pages. Empty spaces are dead configuration "
            "that still carry permission, scheme, and search-index overhead. "
            "The app can archive the space, which is reversible: an archived "
            "space can be restored with a single call, and no content is lost."
        ),
        "api_hint": "PUT /wiki/api/v2/spaces/{id} status=archived",
        "risk": "low",
        "reversible": True,
        "caveat": (
            "The page count is a search-index count. Confirm the space is "
            "genuinely empty before archiving; archiving does not free Cloud "
            "storage but removes the space from active navigation."
        ),
    },

    "confluence_empty_group": {
        "tier": "app",
        "tier_label": "Fixable by the app",
        "label": "Empty Confluence group",
        "title": "Delete empty Confluence group",
        "detail": (
            "This Confluence directory group has zero members. Empty groups are "
            "dead configuration and a silent permission-escalation risk: a "
            "future member added to the group would inherit whatever space "
            "permissions reference it. The app can delete the group; it can be "
            "recreated and repopulated at any time."
        ),
        "api_hint": "DELETE /wiki/rest/api/group?name={name}",
        "risk": "low",
        "reversible": True,
        "caveat": (
            "Only the probed subset of groups is evaluated. Confirm the group "
            "holds no indirect memberships and no space permission depends on "
            "it before deleting."
        ),
    },

    "large_space": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Oversized space",
        "title": "Review oversized space",
        "detail": (
            "This space holds an unusually large number of pages, which can "
            "degrade page-tree navigation and slow space and page view and "
            "edit. Review the space with its owners: split it into focused "
            "spaces, archive stale page trees, or flatten deep nesting. How to "
            "restructure depends on how the team uses the content, so an admin "
            "must drive it."
        ),
        "api_hint": None,
        "risk": "low",
        "reversible": True,
        "caveat": (
            "The threshold is a configurable health guideline, not a hard "
            "limit. A large space is not necessarily broken; treat this as a "
            "review prompt."
        ),
    },

    "space_no_homepage": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Space has no homepage",
        "title": "Set a homepage for the space",
        "detail": (
            "This space has no homepage, so pages outside the missing homepage "
            "never appear in the space sidebar and the content is effectively "
            "unnavigable. Open the space and set an existing page as the "
            "homepage, or create one. Which page should be the landing page is "
            "a content decision for the space owner."
        ),
        "api_hint": None,
        "risk": "low",
        "reversible": True,
        "caveat": None,
    },

    "orphaned_pages": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Pages orphaned from the space tree",
        "title": "Re-parent pages that fell out of the space tree",
        "detail": (
            "These pages sit outside the space homepage's subtree, so they do "
            "not appear in the Confluence sidebar page tree and are reachable "
            "only by search or a direct link — a common Data Center to Cloud "
            "migration breakage when a parent page was trashed, not migrated, "
            "or restricted, which promotes its children to the space root. Open "
            "the space, move the orphaned pages back under the correct parent "
            "(or the homepage), and confirm the intended parent actually "
            "migrated. Which parent each page belongs under is a content "
            "decision for the space owner."
        ),
        "api_hint": None,
        "risk": "low",
        "reversible": True,
        "caveat": None,
    },

    "unsupported_macro_usage": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Pages use a macro that breaks in Cloud",
        "title": "Replace or re-enable a macro unsupported in Cloud",
        "detail": (
            "These pages use a macro that commonly fails after a Data Center to "
            "Cloud migration: either a Marketplace-app macro whose app is not "
            "installed in Cloud (e.g. Gliffy, draw.io), or a built-in macro "
            "Atlassian removed from Cloud (e.g. Chart, Gallery, Page Index). The "
            "macro markup migrated but has no renderer, so the page shows an "
            "'Unknown macro' error or a blank section. Install and migrate the "
            "equivalent Cloud app and run its data migration, or replace the "
            "macro with a supported Cloud alternative. App installation and "
            "content rework are manual decisions for an admin / content owner."
        ),
        "api_hint": None,
        "risk": "low",
        "reversible": True,
        "caveat": None,
    },

    "cross_space_include_risk": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Pages include content from elsewhere (may go blank)",
        "title": "Verify cross-space includes after migrating",
        "detail": (
            "These pages use the Include Page or Excerpt Include macro to pull "
            "content from another page. The macro is supported in Cloud, but the "
            "reference points at a specific page by title/space. If the "
            "referenced page migrates in a different batch, lands in a renamed "
            "space, or is left behind, the include resolves to nothing and the "
            "consumer page renders a blank section: the content silently "
            "disappears. Migrate referenced pages in the SAME batch as their "
            "consumers where possible, then spot-check included sections after "
            "cutover. Re-pointing broken includes is a manual content task."
        ),
        "api_hint": None,
        "risk": "low",
        "reversible": True,
        "caveat": None,
    },

    "archived_space_clutter": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Archived-space backlog",
        "title": "Clean up archived-space backlog",
        "detail": (
            "The instance carries a large population of archived spaces. "
            "Archived spaces remain reachable by direct link, stay indexed if "
            "public, and still consume storage, so a large backlog signals "
            "deferred cleanup. Review the archived spaces and permanently "
            "delete the ones that no longer need to be retained. Which spaces "
            "are safe to delete is a governance decision for an admin."
        ),
        "api_hint": None,
        "risk": "medium",
        "reversible": False,
        "caveat": (
            "Permanently deleting an archived space removes its content for "
            "good. Export anything that must be retained before deleting."
        ),
    },

    "personal_space_sprawl": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Personal-space sprawl",
        "title": "Review personal-space sprawl",
        "detail": (
            "The instance has a large number of personal spaces. Personal "
            "spaces accumulate with the user base, are often abandoned after a "
            "user leaves, and count toward the space guardrail, yet there is no "
            "native auto-archive on deactivation. Review the personal-space "
            "population and archive the ones belonging to deactivated users. "
            "The audit reports only the count and never any personal-space key "
            "or owner identity."
        ),
        "api_hint": None,
        "risk": "low",
        "reversible": True,
        "caveat": (
            "Personal-space keys embed usernames, so the audit deliberately "
            "withholds them. Identify the spaces to archive in the admin UI."
        ),
    },

    "space_count_near_guardrail": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Approaching the space guardrail",
        "title": "Reduce space count toward the guardrail",
        "detail": (
            "The total number of spaces is approaching or has crossed "
            "Atlassian's 10,000-space guardrail (optimal under 8,000). Beyond "
            "the guardrail, permission-check overhead and search degrade, and "
            "the search-index ceiling eventually breaks search entirely. "
            "Archive and delete empty, stale, and abandoned spaces to bring the "
            "count down. Which spaces to retire is a governance decision for an "
            "admin."
        ),
        "api_hint": None,
        "risk": "medium",
        "reversible": True,
        "caveat": (
            "The count aggregates global, personal, and archived spaces. "
            "Archiving alone does not reduce the count; spaces must be deleted "
            "to fall below the guardrail."
        ),
    },

    "space_no_admin": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Space has no administrator",
        "title": "Assign a space administrator",
        "detail": (
            "This space has no space-admin grant, so nobody can manage its "
            "permissions or settings; it is orphaned. Recovery needs a site "
            "admin to use Recover Permissions and then assign a space admin "
            "(ideally a dedicated space-admins group rather than an "
            "individual). Who should administer the space is a decision for an "
            "admin who knows the team."
        ),
        "api_hint": None,
        "risk": "medium",
        "reversible": True,
        "caveat": (
            "The audit reads grant operation types only, never principal "
            "identities. Confirm in the space-permissions UI before recovering "
            "access."
        ),
    },

    "anonymous_space_access": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Space allows anonymous access",
        "title": "Review anonymous access to the space",
        "detail": (
            "This space grants read access to anonymous users, making its "
            "content internet-public and search-engine indexable. Confirm "
            "whether public access is intentional. If it is not, open the space "
            "permissions and remove the anonymous grant. Whether a space should "
            "be public is a security decision for an admin."
        ),
        "api_hint": None,
        "risk": "high",
        "reversible": True,
        "caveat": (
            "Removing anonymous access immediately hides the space from "
            "unauthenticated visitors and any public link or integration that "
            "relied on it. Verify external consumers first."
        ),
    },

    "space_permission_to_anyone": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Broad space-permission grant",
        "title": "Restrict a broad space-permission grant",
        "detail": (
            "This space grants permissions to a broad principal class such as "
            "all logged-in users. Open for view can be fine, but write, admin, "
            "or export rights handed to everyone violate least-privilege and "
            "over-expose the space. Open the space permissions and narrow the "
            "grant to a specific group or role. Which audience is appropriate "
            "is a security decision for an admin."
        ),
        "api_hint": None,
        "risk": "medium",
        "reversible": True,
        "caveat": (
            "Narrowing a broad grant can remove access from users who relied "
            "on it. Confirm the intended audience before changing the grant."
        ),
    },

    "permission_grant_to_empty_group": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Space permission granted to an empty group",
        "title": "Review a space permission granted to a 0-member group",
        "detail": (
            "This space grants a permission to a group that currently has no "
            "members. The grant is dormant today, but it is a latent "
            "escalation hole: anyone added to that group later silently "
            "inherits this space access, with no further review. Decide "
            "whether the grant is intentional (and the group will be "
            "populated) or stale. If it is stale, open the space permissions "
            "and remove the grant; if the group itself is unused, remove the "
            "group. Whether the grant should stand is a security decision for "
            "an admin. The audit reports group names and member counts only, "
            "never the members themselves."
        ),
        "api_hint": None,
        "risk": "medium",
        "reversible": True,
        "caveat": (
            "An empty group can be populated intentionally just before use "
            "(for example as part of an onboarding flow). Confirm the grant is "
            "actually stale before removing it."
        ),
    },

    "restricted_pages": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Pages with view/edit restrictions",
        "title": "Verify page restrictions survive the migration",
        "detail": (
            "This space has pages with view or edit restrictions. Page "
            "restrictions are enforced per restricting USER or GROUP; if a "
            "restricting principal is not migrated (or its account id / group "
            "name changes), the page can become inaccessible to everyone after "
            "cutover, or — if the restriction is dropped — silently exposed. "
            "Confirm the restricting users and groups migrate, then re-check "
            "access on the restricted pages in the target. The count is sampled "
            "per space, so treat it as a floor, not a total."
        ),
        "api_hint": None,
        "risk": "medium",
        "reversible": True,
        "caveat": None,
    },

    "stale_page_ratio_high": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "High ratio of stale pages",
        "title": "Triage a backlog of stale pages",
        "detail": (
            "A high fraction of pages have not been updated in over a year. A "
            "space dominated by year-plus-stale content erodes trust and "
            "clutters search. Review the stale pages with their owners: update "
            "the ones that still matter, archive the rest, and apply a keep "
            "label to anything that should be retained as-is. Whether a stale "
            "page is still relevant is a team decision, so a human must triage "
            "it. The audit reports only counts, never page titles or bodies."
        ),
        "api_hint": None,
        "risk": "low",
        "reversible": True,
        "caveat": (
            "Staleness is measured by last-modified date; a reviewed but "
            "unedited page still counts as stale. Confirm before bulk "
            "archiving."
        ),
    },

    "drafts_pileup": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Lingering draft pages",
        "title": "Clear lingering draft pages",
        "detail": (
            "A large number of never-published draft pages are lingering in "
            "the instance. Drafts clutter search and are not in the trash, so "
            "they cannot be bulk-deleted natively. Review the drafts with their "
            "authors: publish the ones that are ready and discard the "
            "abandoned ones. Which drafts to keep is an author decision. The "
            "audit reports only the count, never draft titles or content."
        ),
        "api_hint": None,
        "risk": "low",
        "reversible": True,
        "caveat": None,
    },

    "label_sprawl": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Too many global labels",
        "title": "Consolidate the global-label set",
        "detail": (
            "The instance carries a very large global-label population. "
            "Inconsistent and unused labels fragment discoverability, and "
            "Confluence has no native UI to find or remove unused labels. "
            "Review the label set, merge near-duplicates onto a consistent "
            "convention, and remove labels that are no longer used. Which "
            "labels are canonical is a content-governance decision for an "
            "admin."
        ),
        "api_hint": None,
        "risk": "low",
        "reversible": True,
        "caveat": None,
    },

    "template_sprawl": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Too many global templates",
        "title": "Consolidate the global-template set",
        "detail": (
            "The instance carries a very large global-template population. A "
            "wall of near-duplicate templates makes the create-from-template "
            "picker hard to navigate, and authors stop trusting it. Review the "
            "global templates, merge near-duplicates onto a smaller canonical "
            "set, and remove templates that are no longer used. Which "
            "templates are canonical is a content-governance decision for an "
            "admin."
        ),
        "api_hint": None,
        "risk": "low",
        "reversible": True,
        "caveat": None,
    },

    "capability_gap": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Area could not be fully checked",
        "title": "Review skipped configuration area",
        "detail": (
            "This configuration area was skipped because the required API is not available "
            "on this deployment (typically a Data Center instance missing a Cloud-only endpoint). "
            "Manually review the area in the Jira admin UI to ensure no issues exist. "
            "No automated action is possible until API access is available."
        ),
        "api_hint": None,
        "risk": "low",
        "reversible": True,
        "caveat": None,
    },

    "area_error": {
        "tier": "human",
        "tier_label": "Fixable by a human",
        "label": "Area fetch error",
        "title": "Investigate fetch error for configuration area",
        "detail": (
            "An unexpected error occurred while fetching this configuration area. "
            "The audit could not evaluate it, so the finding coverage is incomplete. "
            "Check network connectivity, API permissions, and Jira health for this area. "
            "Re-run the audit once the error is resolved to get a full picture."
        ),
        "api_hint": None,
        "risk": "medium",
        "reversible": True,
        "caveat": None,
    },
}


# ---------------------------------------------------------------------------
# annotate_fixes
# ---------------------------------------------------------------------------

def annotate_fixes(findings: list, deployment: str | None = None,
                   site_url: str | None = None) -> None:
    """Mutate findings in-place: add detail[fix], detail[category], and (when
    the deployment + site_url are known) detail[admin_link].

    - detail[fix] is a shallow copy of the _FIXES template, lightly personalised
      with the finding name where it improves clarity.
    - detail[category] is the result of category_for(kind).
    - detail[admin_link] is a deployment-aware deep-link to the admin screen
      where the issue is fixed (see deeplinks.deep_link). Only attached when
      both deployment and site_url are supplied AND a link exists for the kind —
      legacy 1-arg callers and unknown kinds simply get no link.
    - An unknown kind does NOT raise; the finding receives the safe Hygiene
      category but no fix entry (logged only).  This protects against future
      kinds added to checks.py before _FIXES is updated.
    """
    for finding in findings:
        kind = finding.get("kind")
        name = finding.get("name") or ""

        # category always set (uses safe default for unknown kinds)
        finding["detail"]["category"] = category_for(kind)

        # Admin deep-link is independent of the fix template — a kind can have a
        # link without a registry entry and vice-versa.
        if deployment and site_url:
            link = deep_link(kind, deployment, site_url, name=name or None)
            if link:
                finding["detail"]["admin_link"] = link

        template = _FIXES.get(kind)
        if template is None:
            # Unknown kind: skip fix — do not raise.
            continue

        fix = copy.copy(template)

        # Light personalisation: incorporate the object name into the title
        # so the UI can show a specific object without extra parsing.
        if name and name not in fix["title"]:
            fix["title"] = f"{fix['title']}: {name}"

        finding["detail"]["fix"] = fix
