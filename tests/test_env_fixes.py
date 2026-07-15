"""Tests for auditor.envaudit.fixes — Task 5.

Covers:
  - COMPLETENESS: every kind run_checks / run_checks_confluence can emit is
    present in _FIXES and has a valid tier, matching tier_label, and a valid
    category. Covers BOTH the Jira and the Confluence finding kinds (the fix
    registry is shared across products).
  - TIER assignments: app (12 kinds), unfixable (1 kind), human (55 kinds).
  - CATEGORY assignments: Performance / Security / Structure / Coverage /
    Hygiene / DataQuality.
  - annotate_fixes: mutates findings in-place; original keys preserved.
  - Guard: an unknown/future kind passed to annotate_fixes does NOT raise and the
    finding is left without a fix entry (or a safe default) — behaviour is
    documented here.
"""
from __future__ import annotations

import copy
import pytest

from auditor.envaudit.fixes import _FIXES, annotate_fixes, category_for

# ---------------------------------------------------------------------------
# Authoritative list — must match checks.py + confluence_checks.py exactly
# (68 kinds total: 55 Jira + 13 Confluence-specific).
# ---------------------------------------------------------------------------
ALL_KINDS = [
    # existing 8 (project_missing_scheme removed — see checks.py: the aggregate
    # projects_using shape mixes project IDs vs project KEYS and cannot see the
    # default workflow scheme, so the check was an unfixable false positive).
    "capability_gap",
    "area_error",
    "duplicate_field",
    "unused_custom_field",
    "empty_screen",
    "workflow_no_transitions",
    "status_not_in_workflow",
    "scheme_unused",
    # 14
    "field_sprawl",
    "large_option_set",
    "workflow_sprawl",
    "status_sprawl",
    "screen_sprawl",
    "permission_scheme_sprawl",
    "unused_issue_type_scheme",
    "unused_issue_type_screen_scheme",
    "empty_group",
    "version_overdue",
    "version_archived_unreleased",
    "component_no_lead",
    "permission_grant_overly_broad",
    "migration_artifact",
    # 7 broadened real-world coverage
    "resolution_sprawl",
    "priority_sprawl",
    "issue_type_sprawl",
    "link_type_sprawl",
    "large_workflow",
    "public_browse_grant",
    "component_unassigned_default",
    # 12 Section-1 comprehensive coverage (catalog docs/superpowers)
    "unused_resolution",
    "near_field_limit",
    "duplicate_status_name",
    "duplicate_issue_type_name",
    "redundant_priority_set",
    "many_overdue_versions_in_project",
    "large_group_admin_bloat",
    "anonymous_write_grant",
    "admin_grant_to_logged_in",
    "board_count_exceeds_projects",
    "dashboard_filter_volume_high",
    "version_naming_inconsistent",
    # 5 Section-2 project-activity + shared-object-ownership coverage
    "empty_project",
    "inactive_project",
    "shared_object_owned_by_inactive",
    "public_shared_filter",
    "public_shared_dashboard",
    # 5 workflow-structure + scheme-mapping coverage (Batch-A deferrals)
    "unreachable_status",
    "dead_end_status",
    "global_transition_overuse",
    "workflow_unreferenced",
    "screen_not_in_scheme",
    # 4 Section-3 issue-level / data-quality coverage (count-only queries)
    "done_but_unresolved",
    "resolved_but_open",
    "stale_open_issues",
    "unassigned_unresolved_high",
    # 2 Jira DC->Cloud migration (JCMA) checks, gated to deployment == dc
    "group_name_collision_reserved",
    "unsupported_custom_field_type",
    "apps_to_assess_for_cloud",
    "script_app_present",
    # Cloud guardrail-proximity checks (both deployments). near_field_limit /
    # near_issue_type_limit are per-project hard-limit disclosures (see checks.py);
    # near_priority_limit / near_workflow_limit are site-wide soft guardrails.
    # (No near_project_limit: Atlassian publishes no citable hard project limit.)
    "near_issue_type_limit",
    "near_priority_limit",
    "near_workflow_limit",
    # 13 Confluence environment-audit kinds (spec R2/R3). Two app-tier
    # (empty_space, confluence_empty_group); the rest human. anonymous_write_
    # grant / capability_gap / area_error are SHARED with Jira (listed above).
    "empty_space",
    "large_space",
    "space_no_homepage",
    "archived_space_clutter",
    "personal_space_sprawl",
    "space_count_near_guardrail",
    "space_no_admin",
    "anonymous_space_access",
    "space_permission_to_anyone",
    "stale_page_ratio_high",
    "drafts_pileup",
    "label_sprawl",
    "confluence_empty_group",
    # DC->Cloud migration: page hierarchy + render visibility
    "orphaned_pages",
    "unsupported_macro_usage",
    "cross_space_include_risk",
    "restricted_pages",
    # 2 general Confluence coverage: template hygiene + empty-group escalation
    "template_sprawl",
    "permission_grant_to_empty_group",
]

_VALID_TIERS = {"app", "human", "unfixable"}
_VALID_TIER_LABELS = {
    "app": "Fixable by the app",
    "human": "Fixable by a human",
    "unfixable": "Re-migration suggested",
}
_VALID_CATEGORIES = {"Performance", "Security", "Structure", "Coverage",
                     "Hygiene", "DataQuality"}

# ---------------------------------------------------------------------------
# Tier assignments (explicit)
# ---------------------------------------------------------------------------
_APP_KINDS = {"scheme_unused", "unused_issue_type_scheme",
              "unused_issue_type_screen_scheme", "empty_group",
              # Expanded app-tier cleanup deletes (guarded by built-in
              # protection + per-kind TOCTOU re-verify, incl. the field
              # value-check) — see auditor/envaudit/apply.py.
              "empty_screen", "screen_not_in_scheme", "workflow_unreferenced",
              "unused_custom_field", "empty_project",
              # status_not_in_workflow: the status is in NO workflow, so the
              # delete is clean. Guarded by built-in status protection +
              # a TOCTOU workflow re-read + an issues-in-status value-check.
              # (unreachable_status / dead_end_status stay HUMAN: they are
              # INSIDE a workflow and removing them needs a live workflow-edit
              # that cannot be made provably safe — Tier-2 detect-and-guide.)
              "status_not_in_workflow",
              # Confluence app-tier (reversible archive / delete).
              "empty_space", "confluence_empty_group"}
_UNFIXABLE_KINDS = {"migration_artifact"}
_HUMAN_KINDS = set(ALL_KINDS) - _APP_KINDS - _UNFIXABLE_KINDS

# ---------------------------------------------------------------------------
# Category assignments (explicit)
# ---------------------------------------------------------------------------
_PERFORMANCE_KINDS = {
    "field_sprawl", "large_option_set", "workflow_sprawl",
    "status_sprawl", "screen_sprawl", "permission_scheme_sprawl",
    "resolution_sprawl", "priority_sprawl", "issue_type_sprawl",
    "link_type_sprawl", "large_workflow",
    "near_field_limit", "dashboard_filter_volume_high",
    # Cloud guardrail proximity
    "near_issue_type_limit", "near_priority_limit", "near_workflow_limit",
    # Confluence
    "large_space", "space_count_near_guardrail",
}
_SECURITY_KINDS = {
    "permission_grant_overly_broad", "public_browse_grant",
    "large_group_admin_bloat", "anonymous_write_grant",
    "admin_grant_to_logged_in",
    "shared_object_owned_by_inactive", "public_shared_filter",
    "public_shared_dashboard",
    # Confluence (anonymous_write_grant shared with Jira above)
    "space_no_admin", "anonymous_space_access", "space_permission_to_anyone",
    "restricted_pages", "permission_grant_to_empty_group",
    # DC->Cloud migration
    "group_name_collision_reserved",
}
_STRUCTURE_KINDS = {"workflow_no_transitions", "status_not_in_workflow",
                    "unreachable_status", "dead_end_status",
                    # Confluence
                    "space_no_homepage", "orphaned_pages",
                    "apps_to_assess_for_cloud", "script_app_present"}
_COVERAGE_KINDS = {"capability_gap", "area_error"}
_DATAQUALITY_KINDS = {"done_but_unresolved", "resolved_but_open",
                      "stale_open_issues", "unassigned_unresolved_high",
                      # Confluence
                      "stale_page_ratio_high", "unsupported_macro_usage",
                      "cross_space_include_risk",
                      # DC->Cloud migration
                      "unsupported_custom_field_type"}
_HYGIENE_KINDS = (set(ALL_KINDS) - _PERFORMANCE_KINDS - _SECURITY_KINDS
                  - _STRUCTURE_KINDS - _COVERAGE_KINDS - _DATAQUALITY_KINDS)


# ===========================================================================
# COMPLETENESS TESTS
# ===========================================================================

class TestCompleteness:
    def test_fixes_covers_all_kinds(self):
        assert len(ALL_KINDS) == 81, "contract must enumerate exactly 81 kinds"
        missing = [k for k in ALL_KINDS if k not in _FIXES]
        assert not missing, f"_FIXES is missing kinds: {missing}"

    def test_no_extra_kinds_in_fixes(self):
        extra = [k for k in _FIXES if k not in ALL_KINDS]
        assert not extra, f"_FIXES has unrecognised kinds: {extra}"

    def test_every_entry_has_valid_tier(self):
        bad = {k: v["tier"] for k, v in _FIXES.items() if v.get("tier") not in _VALID_TIERS}
        assert not bad, f"Invalid tiers: {bad}"

    def test_every_entry_tier_label_matches_tier(self):
        mismatches = {}
        for k, v in _FIXES.items():
            expected = _VALID_TIER_LABELS.get(v.get("tier"))
            if v.get("tier_label") != expected:
                mismatches[k] = (v.get("tier_label"), expected)
        assert not mismatches, f"tier_label mismatches: {mismatches}"

    def test_every_entry_has_required_keys(self):
        required = {"tier", "tier_label", "label", "title", "detail",
                    "api_hint", "risk", "reversible"}
        bad = {k: required - set(v) for k, v in _FIXES.items() if required - set(v)}
        assert not bad, f"Entries missing required keys: {bad}"

    def test_every_entry_has_nonempty_generic_label(self):
        # Every _FIXES entry carries a short GENERIC problem-type label (used by
        # the UI to group findings of the same kind under one problem card).
        # It must be a non-empty string, and — being copy-paste-bound for some
        # surfaces — must not contain quotation marks or em-dashes.
        bad = {k: v.get("label") for k, v in _FIXES.items()
               if not isinstance(v.get("label"), str) or not v.get("label").strip()}
        assert not bad, f"Entries missing a non-empty label: {bad}"
        tainted = {k: v["label"] for k, v in _FIXES.items()
                   if ('"' in v["label"] or "'" in v["label"]
                       or "—" in v["label"] or "–" in v["label"])}
        assert not tainted, f"labels with quotes / em-dashes: {tainted}"

    def test_label_is_generic_not_personalised_after_annotate(self):
        # The label is the GENERIC problem type, NOT the per-object title. After
        # annotate_fixes personalises `title` with the finding name, `label`
        # must remain the bare generic string (no object name appended).
        findings = [_make_finding("component_no_lead", name="Backend")]
        annotate_fixes(findings)
        fix = findings[0]["detail"]["fix"]
        assert fix["label"] == _FIXES["component_no_lead"]["label"]
        assert "Backend" not in fix["label"]
        # while the title DID get personalised (sanity anchor)
        assert "Backend" in fix["title"]

    def test_every_entry_risk_valid(self):
        bad = {k: v["risk"] for k, v in _FIXES.items() if v.get("risk") not in {"low", "medium", "high"}}
        assert not bad, f"Invalid risk values: {bad}"

    def test_every_entry_reversible_is_bool(self):
        bad = {k for k, v in _FIXES.items() if not isinstance(v.get("reversible"), bool)}
        assert not bad, f"reversible is not bool for: {bad}"

    def test_category_for_covers_all_kinds(self):
        results = {k: category_for(k) for k in ALL_KINDS}
        bad = {k: v for k, v in results.items() if v not in _VALID_CATEGORIES}
        assert not bad, f"Invalid categories returned: {bad}"


# ===========================================================================
# TIER TESTS
# ===========================================================================

class TestTiers:
    @pytest.mark.parametrize("kind", sorted(_APP_KINDS))
    def test_app_tier(self, kind):
        assert _FIXES[kind]["tier"] == "app", f"{kind} should be tier=app"

    def test_migration_artifact_is_unfixable(self):
        assert _FIXES["migration_artifact"]["tier"] == "unfixable"

    def test_unfixable_not_reversible(self):
        assert _FIXES["migration_artifact"]["reversible"] is False

    @pytest.mark.parametrize("kind", sorted(_HUMAN_KINDS))
    def test_human_tier(self, kind):
        assert _FIXES[kind]["tier"] == "human", f"{kind} should be tier=human"

    def test_tier_counts(self):
        tiers = [v["tier"] for v in _FIXES.values()]
        assert tiers.count("app") == 12
        assert tiers.count("unfixable") == 1
        assert tiers.count("human") == 68

    # Spot checks for a handful of specific human kinds
    def test_duplicate_field_is_human(self):
        assert _FIXES["duplicate_field"]["tier"] == "human"

    def test_unused_custom_field_is_app(self):
        # Promoted to app-tier: only auto-deleted when on no screen AND zero
        # values (the value-check); see apply.py.
        assert _FIXES["unused_custom_field"]["tier"] == "app"

    def test_empty_screen_is_app(self):
        # Promoted to app-tier with a TOCTOU field re-fetch before delete.
        assert _FIXES["empty_screen"]["tier"] == "app"

    def test_newly_promoted_app_kinds_are_app(self):
        for k in ("screen_not_in_scheme", "workflow_unreferenced",
                  "empty_project"):
            assert _FIXES[k]["tier"] == "app", f"{k} should be tier=app"

    def test_large_option_set_is_human(self):
        assert _FIXES["large_option_set"]["tier"] == "human"

    def test_workflow_no_transitions_is_human(self):
        assert _FIXES["workflow_no_transitions"]["tier"] == "human"

    def test_status_not_in_workflow_is_app(self):
        # Promoted to app-tier: the status is in NO workflow, so deleting it is
        # clean. Guarded by built-in status protection + a live workflow re-read
        # + an issues-in-status value-check (see apply.py).
        assert _FIXES["status_not_in_workflow"]["tier"] == "app"

    def test_unreachable_and_dead_end_status_stay_human(self):
        # These two are INSIDE a workflow but disconnected; removing them needs a
        # live workflow edit that cannot be made provably safe, so they remain
        # Tier-2 human detect-and-guide findings.
        assert _FIXES["unreachable_status"]["tier"] == "human"
        assert _FIXES["dead_end_status"]["tier"] == "human"

    def test_component_no_lead_is_human(self):
        assert _FIXES["component_no_lead"]["tier"] == "human"

    def test_version_overdue_is_human(self):
        assert _FIXES["version_overdue"]["tier"] == "human"

    def test_version_archived_unreleased_is_human(self):
        assert _FIXES["version_archived_unreleased"]["tier"] == "human"

    def test_permission_grant_overly_broad_is_human(self):
        assert _FIXES["permission_grant_overly_broad"]["tier"] == "human"

    def test_capability_gap_is_human(self):
        assert _FIXES["capability_gap"]["tier"] == "human"

    def test_area_error_is_human(self):
        assert _FIXES["area_error"]["tier"] == "human"

    # The 7 broadened-coverage kinds are all tier=human.
    @pytest.mark.parametrize("kind", [
        "resolution_sprawl", "priority_sprawl", "issue_type_sprawl",
        "link_type_sprawl", "large_workflow", "public_browse_grant",
        "component_unassigned_default",
    ])
    def test_new_kinds_are_human(self, kind):
        assert _FIXES[kind]["tier"] == "human", f"{kind} should be tier=human"

    # App-tier entries must carry a real api_hint
    @pytest.mark.parametrize("kind", sorted(_APP_KINDS))
    def test_app_tier_has_api_hint(self, kind):
        hint = _FIXES[kind].get("api_hint")
        assert hint and isinstance(hint, str) and len(hint) > 5, \
            f"app-tier kind {kind} must have a non-empty api_hint"

    # unfixable entry must NOT have an api_hint
    def test_unfixable_no_api_hint(self):
        assert _FIXES["migration_artifact"].get("api_hint") is None


# ===========================================================================
# CATEGORY TESTS
# ===========================================================================

class TestCategories:
    @pytest.mark.parametrize("kind", sorted(_PERFORMANCE_KINDS))
    def test_performance_category(self, kind):
        assert category_for(kind) == "Performance", f"{kind} -> Performance"

    def test_security_category(self):
        assert category_for("permission_grant_overly_broad") == "Security"

    @pytest.mark.parametrize("kind", sorted(_STRUCTURE_KINDS))
    def test_structure_category(self, kind):
        assert category_for(kind) == "Structure", f"{kind} -> Structure"

    @pytest.mark.parametrize("kind", sorted(_COVERAGE_KINDS))
    def test_coverage_category(self, kind):
        assert category_for(kind) == "Coverage", f"{kind} -> Coverage"

    @pytest.mark.parametrize("kind", sorted(_HYGIENE_KINDS))
    def test_hygiene_category(self, kind):
        assert category_for(kind) == "Hygiene", f"{kind} -> Hygiene"

    @pytest.mark.parametrize("kind", sorted(_DATAQUALITY_KINDS))
    def test_dataquality_category(self, kind):
        assert category_for(kind) == "DataQuality", f"{kind} -> DataQuality"

    # Spot checks repeated for clarity
    def test_field_sprawl_is_performance(self):
        assert category_for("field_sprawl") == "Performance"

    def test_duplicate_field_is_hygiene(self):
        assert category_for("duplicate_field") == "Hygiene"

    # The 7 broadened-coverage kinds map to the right category.
    @pytest.mark.parametrize("kind", [
        "resolution_sprawl", "priority_sprawl", "issue_type_sprawl",
        "link_type_sprawl", "large_workflow",
    ])
    def test_new_sprawl_kinds_are_performance(self, kind):
        assert category_for(kind) == "Performance"

    def test_public_browse_grant_is_security(self):
        assert category_for("public_browse_grant") == "Security"

    def test_component_unassigned_default_is_hygiene(self):
        assert category_for("component_unassigned_default") == "Hygiene"

    def test_unknown_kind_returns_hygiene(self):
        # Safe default — future kinds fallback gracefully.
        assert category_for("future_kind_xyz") == "Hygiene"


# ===========================================================================
# annotate_fixes TESTS
# ===========================================================================

def _make_finding(kind, name="SomeName", **extra):
    return {"area": "custom_fields", "name": name, "kind": kind,
            "severity": "medium", "detail": dict(extra)}


class TestAnnotateFixes:
    def test_fix_added_to_each_finding(self):
        findings = [
            _make_finding("duplicate_field", name="Severity"),
            _make_finding("migration_artifact", name="Open (migrated)"),
            _make_finding("scheme_unused", name="Legacy Scheme"),
        ]
        annotate_fixes(findings)
        for f in findings:
            assert "fix" in f["detail"], f"fix missing for kind={f['kind']}"
            assert "category" in f["detail"], f"category missing for kind={f['kind']}"

    def test_tier_values_correct_after_annotate(self):
        findings = [
            _make_finding("scheme_unused"),
            _make_finding("migration_artifact"),
            _make_finding("duplicate_field"),
        ]
        annotate_fixes(findings)
        by_kind = {f["kind"]: f["detail"]["fix"]["tier"] for f in findings}
        assert by_kind["scheme_unused"] == "app"
        assert by_kind["migration_artifact"] == "unfixable"
        assert by_kind["duplicate_field"] == "human"

    def test_category_values_correct_after_annotate(self):
        findings = [
            _make_finding("field_sprawl"),
            _make_finding("permission_grant_overly_broad"),
            _make_finding("workflow_no_transitions"),
            _make_finding("capability_gap"),
            _make_finding("empty_group"),
        ]
        annotate_fixes(findings)
        by_kind = {f["kind"]: f["detail"]["category"] for f in findings}
        assert by_kind["field_sprawl"] == "Performance"
        assert by_kind["permission_grant_overly_broad"] == "Security"
        assert by_kind["workflow_no_transitions"] == "Structure"
        assert by_kind["capability_gap"] == "Coverage"
        assert by_kind["empty_group"] == "Hygiene"

    def test_original_detail_keys_preserved(self):
        findings = [_make_finding("duplicate_field", collides_with="severity")]
        annotate_fixes(findings)
        assert findings[0]["detail"]["collides_with"] == "severity"
        assert "fix" in findings[0]["detail"]

    def test_fix_is_a_copy_not_the_registry_object(self):
        findings = [_make_finding("scheme_unused")]
        annotate_fixes(findings)
        fix = findings[0]["detail"]["fix"]
        # Mutating the returned fix must not affect the registry.
        original_title = _FIXES["scheme_unused"]["title"]
        fix["title"] = "MUTATED"
        assert _FIXES["scheme_unused"]["title"] == original_title

    def test_fix_has_tier_label(self):
        findings = [_make_finding("empty_group")]
        annotate_fixes(findings)
        assert findings[0]["detail"]["fix"]["tier_label"] == "Fixable by the app"

    def test_annotate_empty_list(self):
        annotate_fixes([])  # must not raise

    def test_finding_name_referenced_in_fix_title(self):
        findings = [_make_finding("scheme_unused", name="Legacy Scheme")]
        annotate_fixes(findings)
        title = findings[0]["detail"]["fix"]["title"]
        # The title should reference the finding name somewhere for context.
        assert "Legacy Scheme" in title or "scheme" in title.lower()

    def test_all_kinds_annotated_without_error(self):
        findings = [_make_finding(k) for k in ALL_KINDS]
        annotate_fixes(findings)
        for f in findings:
            assert "fix" in f["detail"]
            assert "category" in f["detail"]


# ===========================================================================
# GUARD: unknown kind
# ===========================================================================

class TestUnknownKindGuard:
    def test_unknown_kind_does_not_raise(self):
        """An unknown/future kind must not crash annotate_fixes.

        Behaviour: the finding is left without a fix entry (or receives a safe
        Hygiene/human default), but no exception is raised. This protects
        against future kinds added to checks.py before _FIXES is updated.
        """
        findings = [_make_finding("future_kind_xyz")]
        # Must not raise.
        annotate_fixes(findings)
        # category should still be set (safe Hygiene default via category_for).
        assert findings[0]["detail"]["category"] == "Hygiene"

    def test_known_kinds_still_annotated_when_unknown_present(self):
        """Known kinds are annotated even when an unknown kind is in the list."""
        findings = [
            _make_finding("duplicate_field"),
            _make_finding("future_kind_xyz"),
        ]
        annotate_fixes(findings)
        assert "fix" in findings[0]["detail"]


class TestAdminDeepLinks:
    """annotate_fixes attaches a deployment-aware admin deep-link when it knows
    the deployment + site_url, and stays backward-compatible without them."""

    def _finding(self, kind, name=""):
        return {"kind": kind, "name": name, "detail": {}}

    def test_no_link_without_deployment_context(self):
        # Backward-compatible: the 1-arg call (legacy callers) adds no link.
        f = self._finding("apps_to_assess_for_cloud")
        annotate_fixes([f])
        assert "admin_link" not in f["detail"]

    def test_global_link_attached_when_context_present(self):
        f = self._finding("apps_to_assess_for_cloud")
        annotate_fixes([f], deployment="dc",
                       site_url="https://acme.atlassian.net")
        assert f["detail"]["admin_link"]["url"] == \
            "https://acme.atlassian.net/plugins/servlet/upm"

    def test_space_scoped_link_uses_finding_name(self):
        f = self._finding("space_no_homepage", name="DEV")
        annotate_fixes([f], deployment="cloud",
                       site_url="https://acme.atlassian.net")
        assert f["detail"]["admin_link"]["url"] == \
            "https://acme.atlassian.net/wiki/spaces/DEV"

    def test_kind_with_no_link_table_entry_is_fine(self):
        # A finding kind absent from the deep-link table just gets no link.
        f = self._finding("ai_disabled_or_unconfigured")
        annotate_fixes([f], deployment="cloud",
                       site_url="https://acme.atlassian.net")
        assert "admin_link" not in f["detail"]
