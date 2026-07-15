from auditor.envaudit.checks import run_checks


def _snap(**areas):
    base = {"deployment": "cloud", "projects": ["ACME"], "areas": {}}
    base["areas"].update(areas)
    return base


def test_duplicate_field_detected():
    snap = _snap(custom_fields={"names": ["Severity", "severity ", "Team"],
                 "count": 3, "by_type": {}})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "duplicate_field" in kinds


def test_empty_screen_and_workflow_no_transitions():
    snap = _snap(
        screens={"names": ["Default"], "count": 1, "fields": {"Default": []}},
        workflows={"names": ["WF"], "structure_checked": True,
                   "detail": {"WF": {"statuses": ["Open"], "transitions": []}}})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "empty_screen" in kinds and "workflow_no_transitions" in kinds


def test_skipped_area_yields_capability_gap_not_false_clean():
    snap = _snap(workflow_schemes={"skipped": True, "reason": "no DC API"})
    fs = run_checks(snap)
    assert any(f["kind"] == "capability_gap" and f["name"] == "workflow_schemes"
               for f in fs)


def test_unused_custom_field_when_screen_membership_known():
    snap = _snap(
        custom_fields={"names": ["Severity", "Team"], "count": 2, "by_type": {}},
        screens={"names": ["S"], "count": 1, "fields": {"S": ["Severity"]}})
    fs = run_checks(snap)
    assert any(f["kind"] == "unused_custom_field" and f["name"] == "Team"
               for f in fs)


def test_status_not_in_workflow_flagged():
    snap = _snap(
        statuses={"names": ["Open", "Done", "Orphan"], "count": 3},
        workflows={"names": ["WF"], "structure_checked": True,
                   "detail": {"WF": {"statuses": ["Open", "Done"],
                                     "transitions": ["Start"]}}})
    fs = run_checks(snap)
    kinds_names = [(f["kind"], f["name"]) for f in fs]
    assert ("status_not_in_workflow", "Orphan") in kinds_names
    assert ("status_not_in_workflow", "Open") not in kinds_names


def test_status_not_in_workflow_skipped_when_workflows_not_evaluable():
    snap = _snap(
        statuses={"names": ["Open"], "count": 1},
        workflows={"skipped": True, "reason": "no DC API"})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "status_not_in_workflow" not in kinds


def test_scheme_unused_when_projects_using_present():
    snap = _snap(
        workflow_schemes={"names": ["Default WF Scheme", "Legacy Scheme"],
                          "count": 2,
                          "projects_using": {"Default WF Scheme": ["ACME"]}})
    fs = run_checks(snap)
    unused = [f for f in fs if f["kind"] == "scheme_unused"]
    assert any(f["name"] == "Legacy Scheme" for f in unused)
    assert not any(f["name"] == "Default WF Scheme" for f in unused)


def test_scheme_unused_skipped_when_projects_using_absent():
    snap = _snap(
        workflow_schemes={"names": ["Default WF Scheme"], "count": 1})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "scheme_unused" not in kinds


def test_project_missing_scheme_check_removed():
    """The project_missing_scheme rule was removed: it compared project KEYS
    against project IDs in workflow_schemes.projects_using (never equal) and
    could not see the default workflow scheme, so it false-fired for every
    project. It must NEVER be emitted now, even on a snapshot that previously
    triggered it (a project key absent from every scheme's projects_using)."""
    snap = _snap(
        workflow_schemes={"names": ["Default WF Scheme"],
                          "count": 1,
                          "projects_using": {"Default WF Scheme": ["10001"]}})
    snap["projects"] = ["ACME", "ORPHAN"]
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "project_missing_scheme" not in kinds


def test_errored_area_yields_area_error_not_capability_gap():
    """An area that errored during fetch must emit area_error/warning, not the
    capability_gap/info that signals an expected DC skip.  The two signals are
    distinct: capability_gap is informational, area_error demands attention."""
    snap = _snap(statuses={"names": [], "count": 0, "error": "ERR500:internal"})
    fs = run_checks(snap)
    assert any(f["kind"] == "area_error" and f["name"] == "statuses" for f in fs)
    assert not any(f["kind"] == "capability_gap" and f["name"] == "statuses" for f in fs)


def test_orphan_status_reported_exactly_once():
    """A status in no workflow must produce exactly ONE finding
    (status_not_in_workflow) — the redundant unused_status rule that used to
    double-report the same status was dropped."""
    snap = _snap(
        statuses={"names": ["Open", "Ghost"], "count": 2},
        workflows={"names": ["WF"], "structure_checked": True,
                   "detail": {"WF": {"statuses": ["Open"],
                                     "transitions": ["Start"]}}},
        workflow_schemes={"names": ["Default Scheme"], "count": 1,
                          "projects_using": {"Default Scheme": ["ACME"]}})
    ghost = [f for f in run_checks(snap) if f["name"] == "Ghost"]
    assert len(ghost) == 1 and ghost[0]["kind"] == "status_not_in_workflow"


# ---------------------------------------------------------------------------
# Task 2 — Performance rules
# ---------------------------------------------------------------------------

def test_field_sprawl_medium_at_301():
    snap = _snap(custom_fields={"names": [], "count": 301})
    fs = run_checks(snap)
    hits = [f for f in fs if f["kind"] == "field_sprawl"]
    assert hits and hits[0]["severity"] == "medium"


def test_field_sprawl_high_at_801():
    snap = _snap(custom_fields={"names": [], "count": 801})
    fs = run_checks(snap)
    hits = [f for f in fs if f["kind"] == "field_sprawl"]
    assert hits and hits[0]["severity"] == "high"


def test_field_sprawl_not_fired_below_threshold():
    snap = _snap(custom_fields={"names": [], "count": 100})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "field_sprawl" not in kinds


def test_field_sprawl_skipped_guard():
    snap = _snap(custom_fields={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "field_sprawl" not in kinds


def test_large_option_set_low_at_101():
    snap = _snap(custom_field_options={"by_field": {"Priority": {"options": 101}}})
    fs = run_checks(snap)
    hits = [f for f in fs if f["kind"] == "large_option_set"]
    assert hits and hits[0]["severity"] == "low"


def test_large_option_set_medium_at_501():
    snap = _snap(custom_field_options={"by_field": {"Priority": {"options": 501}}})
    fs = run_checks(snap)
    hits = [f for f in fs if f["kind"] == "large_option_set"]
    assert hits and hits[0]["severity"] == "medium"


def test_large_option_set_not_fired_below_threshold():
    snap = _snap(custom_field_options={"by_field": {"Priority": {"options": 50}}})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "large_option_set" not in kinds


def test_large_option_set_skipped_guard():
    snap = _snap(custom_field_options={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "large_option_set" not in kinds


def test_large_option_set_error_guard():
    snap = _snap(custom_field_options={"error": "ERR"})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "large_option_set" not in kinds


def test_workflow_sprawl_medium_at_101():
    snap = _snap(workflows={"names": [f"WF{i}" for i in range(101)]})
    fs = run_checks(snap)
    hits = [f for f in fs if f["kind"] == "workflow_sprawl"]
    assert hits and hits[0]["severity"] == "medium"


def test_workflow_sprawl_not_fired_at_50():
    snap = _snap(workflows={"names": [f"WF{i}" for i in range(50)]})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "workflow_sprawl" not in kinds


def test_workflow_sprawl_skipped_guard():
    snap = _snap(workflows={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "workflow_sprawl" not in kinds


def test_status_sprawl_low_at_101():
    snap = _snap(statuses={"names": [], "count": 101})
    fs = run_checks(snap)
    hits = [f for f in fs if f["kind"] == "status_sprawl"]
    assert hits and hits[0]["severity"] == "low"


def test_status_sprawl_not_fired_at_50():
    snap = _snap(statuses={"names": [], "count": 50})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "status_sprawl" not in kinds


def test_status_sprawl_error_guard():
    snap = _snap(statuses={"error": "ERR"})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "status_sprawl" not in kinds


def test_screen_sprawl_low_at_301():
    snap = _snap(screens={"names": [], "count": 301, "fields": {}})
    fs = run_checks(snap)
    hits = [f for f in fs if f["kind"] == "screen_sprawl"]
    assert hits and hits[0]["severity"] == "low"


def test_screen_sprawl_not_fired_at_100():
    snap = _snap(screens={"names": [], "count": 100, "fields": {}})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "screen_sprawl" not in kinds


def test_screen_sprawl_skipped_guard():
    snap = _snap(screens={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "screen_sprawl" not in kinds


def test_permission_scheme_sprawl_low_at_51():
    snap = _snap(permission_schemes={"names": [], "count": 51})
    fs = run_checks(snap)
    hits = [f for f in fs if f["kind"] == "permission_scheme_sprawl"]
    assert hits and hits[0]["severity"] == "low"


def test_permission_scheme_sprawl_not_fired_at_10():
    snap = _snap(permission_schemes={"names": [], "count": 10})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "permission_scheme_sprawl" not in kinds


def test_permission_scheme_sprawl_error_guard():
    snap = _snap(permission_schemes={"error": "ERR"})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "permission_scheme_sprawl" not in kinds


# ---------------------------------------------------------------------------
# Task 3 — Config-mistake + security rules
# ---------------------------------------------------------------------------

def test_unused_issue_type_scheme_fired():
    snap = _snap(issuetype_schemes={
        "names": ["Default ITS", "Orphan ITS"],
        "count": 2,
        "projects_using": {"Default ITS": ["ACME"], "Orphan ITS": []}
    })
    fs = run_checks(snap)
    hits = [f for f in fs if f["kind"] == "unused_issue_type_scheme"]
    assert any(f["name"] == "Orphan ITS" for f in hits)


def test_unused_issue_type_scheme_not_fired_when_used():
    snap = _snap(issuetype_schemes={
        "names": ["Default ITS"],
        "count": 1,
        "projects_using": {"Default ITS": ["ACME"]}
    })
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "unused_issue_type_scheme" not in kinds


def test_unused_issue_type_scheme_skipped_guard():
    snap = _snap(issuetype_schemes={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "unused_issue_type_scheme" not in kinds


def test_unused_issue_type_screen_scheme_fired():
    snap = _snap(issuetype_screen_schemes={
        "names": ["Default ITSS", "Orphan ITSS"],
        "count": 2,
        "projects_using": {"Default ITSS": ["ACME"], "Orphan ITSS": []}
    })
    fs = run_checks(snap)
    hits = [f for f in fs if f["kind"] == "unused_issue_type_screen_scheme"]
    assert any(f["name"] == "Orphan ITSS" for f in hits)


def test_unused_issue_type_screen_scheme_not_fired_when_used():
    snap = _snap(issuetype_screen_schemes={
        "names": ["Default ITSS"],
        "count": 1,
        "projects_using": {"Default ITSS": ["ACME"]}
    })
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "unused_issue_type_screen_scheme" not in kinds


def test_unused_issue_type_screen_scheme_skipped_guard():
    snap = _snap(issuetype_screen_schemes={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "unused_issue_type_screen_scheme" not in kinds


def test_empty_group_fired():
    snap = _snap(groups={"member_counts": {"admins": 3, "empty-team": 0}})
    fs = run_checks(snap)
    hits = [f for f in fs if f["kind"] == "empty_group"]
    assert any(f["name"] == "empty-team" for f in hits)


def test_empty_group_not_fired_when_all_have_members():
    snap = _snap(groups={"member_counts": {"admins": 3, "devs": 5}})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "empty_group" not in kinds


def test_empty_group_skipped_guard():
    snap = _snap(groups={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "empty_group" not in kinds


def test_version_overdue_fired():
    snap = _snap(versions={"by_project": {
        "ACME": [{"name": "v1.0", "released": False, "overdue": True, "archived": False}]
    }})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "version_overdue" in kinds


def test_version_overdue_not_fired_when_released():
    snap = _snap(versions={"by_project": {
        "ACME": [{"name": "v1.0", "released": True, "overdue": True, "archived": False}]
    }})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "version_overdue" not in kinds


def test_version_overdue_error_guard():
    snap = _snap(versions={"error": "ERR"})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "version_overdue" not in kinds


def test_version_archived_unreleased_fired():
    snap = _snap(versions={"by_project": {
        "ACME": [{"name": "v1.0", "released": False, "overdue": False, "archived": True}]
    }})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "version_archived_unreleased" in kinds


def test_version_archived_unreleased_not_fired_when_released():
    snap = _snap(versions={"by_project": {
        "ACME": [{"name": "v1.0", "released": True, "overdue": False, "archived": True}]
    }})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "version_archived_unreleased" not in kinds


def test_version_archived_unreleased_error_guard():
    snap = _snap(versions={"error": "ERR"})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "version_archived_unreleased" not in kinds


def test_component_no_lead_fired():
    snap = _snap(components={"by_project": {
        "ACME": [
            {"name": "Frontend", "has_lead": True},
            {"name": "Backend", "has_lead": False},
        ]
    }})
    fs = run_checks(snap)
    hits = [f for f in fs if f["kind"] == "component_no_lead"]
    assert any("Backend" in f["name"] for f in hits)


def test_component_no_lead_not_fired_when_all_have_lead():
    snap = _snap(components={"by_project": {
        "ACME": [{"name": "Frontend", "has_lead": True}]
    }})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "component_no_lead" not in kinds


def test_component_no_lead_skipped_guard():
    snap = _snap(components={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "component_no_lead" not in kinds


def test_permission_grant_overly_broad_administer():
    snap = _snap(permission_scheme_grants={"by_scheme": {
        "Default": [{"permission": "ADMINISTER", "holder_type": "anyone"}]
    }})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "permission_grant_overly_broad" in kinds


def test_permission_grant_overly_broad_administer_projects():
    snap = _snap(permission_scheme_grants={"by_scheme": {
        "Default": [{"permission": "ADMINISTER_PROJECTS", "holder_type": "anyone"}]
    }})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "permission_grant_overly_broad" in kinds


def test_permission_grant_overly_broad_not_fired_for_group():
    snap = _snap(permission_scheme_grants={"by_scheme": {
        "Default": [{"permission": "ADMINISTER", "holder_type": "group"}]
    }})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "permission_grant_overly_broad" not in kinds


def test_permission_grant_overly_broad_skipped_guard():
    snap = _snap(permission_scheme_grants={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "permission_grant_overly_broad" not in kinds


# ---------------------------------------------------------------------------
# Task 4 — Migration corruption rules
# ---------------------------------------------------------------------------

def test_migration_artifact_fired_custom_fields():
    snap = _snap(custom_fields={"names": ["Severity", "Severity (migrated)"], "count": 2})
    fs = run_checks(snap)
    hits = [f for f in fs if f["kind"] == "migration_artifact"]
    assert hits
    assert any("custom_fields" in f["detail"].get("source_area", "") for f in hits)


def test_migration_artifact_not_fired_suffix_only_no_base():
    # "Severity (migrated)" exists but "Severity" does NOT — should not fire
    snap = _snap(custom_fields={"names": ["Severity (migrated)", "Team"], "count": 2})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "migration_artifact" not in kinds


def test_migration_artifact_fired_statuses():
    snap = _snap(statuses={"names": ["Open", "Open (migrated 2)"], "count": 2})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "migration_artifact" in kinds


def test_migration_artifact_fired_workflows_copy():
    snap = _snap(workflows={"names": ["My WF", "My WF - copy"]})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "migration_artifact" in kinds


def test_migration_artifact_skipped_guard():
    snap = _snap(custom_fields={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "migration_artifact" not in kinds


def test_migration_artifact_error_guard():
    snap = _snap(custom_fields={"error": "ERR"})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "migration_artifact" not in kinds


# --- P3 nit 1: EXACT-duplicate custom-field names must be reported ----------

def test_duplicate_field_fired_for_exact_duplicate_names():
    # Two custom fields with the IDENTICAL name must produce a duplicate_field
    # finding (the raw names list carries both — they must not be collapsed).
    snap = _snap(custom_fields={"names": ["Severity", "Severity"], "count": 2})
    dups = [f for f in run_checks(snap) if f["kind"] == "duplicate_field"]
    assert dups, "exact-duplicate custom-field names must fire duplicate_field"


# --- P3 nit 2: a migrated twin must NOT double-report ------------------------

def test_migration_artifact_suppresses_duplicate_field_for_same_pair():
    # "Severity" + "Severity (migrated)" is a migration_artifact (the more
    # specific, actionable finding). It must NOT also emit duplicate_field for
    # the same name pair.
    snap = _snap(custom_fields={"names": ["Severity", "Severity (migrated)"],
                                "count": 2})
    fs = run_checks(snap)
    kinds = {f["kind"] for f in fs}
    assert "migration_artifact" in kinds
    assert "duplicate_field" not in kinds, \
        "a migrated twin must report migration_artifact only, not duplicate_field"


# ---------------------------------------------------------------------------
# Task 6 — broadened real-world coverage
# Performance: resolution / priority / issue_type / link_type sprawl,
#              large_workflow.  Security: public_browse_grant.
#              Hygiene: component_unassigned_default.
# ---------------------------------------------------------------------------

# --- resolution_sprawl (low, count > 30) ------------------------------------

def test_resolution_sprawl_low_at_31():
    snap = _snap(resolutions={"names": [], "count": 31})
    hits = [f for f in run_checks(snap) if f["kind"] == "resolution_sprawl"]
    assert hits and hits[0]["severity"] == "low"


def test_resolution_sprawl_not_fired_at_30():
    snap = _snap(resolutions={"names": [], "count": 30})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "resolution_sprawl" not in kinds


def test_resolution_sprawl_skipped_guard():
    snap = _snap(resolutions={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "resolution_sprawl" not in kinds


def test_resolution_sprawl_error_guard():
    snap = _snap(resolutions={"error": "ERR"})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "resolution_sprawl" not in kinds


# --- priority_sprawl (low, count > 15) --------------------------------------

def test_priority_sprawl_low_at_16():
    snap = _snap(priorities={"names": [], "count": 16})
    hits = [f for f in run_checks(snap) if f["kind"] == "priority_sprawl"]
    assert hits and hits[0]["severity"] == "low"


def test_priority_sprawl_not_fired_at_15():
    snap = _snap(priorities={"names": [], "count": 15})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "priority_sprawl" not in kinds


def test_priority_sprawl_skipped_guard():
    snap = _snap(priorities={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "priority_sprawl" not in kinds


def test_priority_sprawl_error_guard():
    snap = _snap(priorities={"error": "ERR"})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "priority_sprawl" not in kinds


# --- issue_type_sprawl (low, count > 40) ------------------------------------

def test_issue_type_sprawl_low_at_41():
    snap = _snap(issue_types={"names": [], "count": 41})
    hits = [f for f in run_checks(snap) if f["kind"] == "issue_type_sprawl"]
    assert hits and hits[0]["severity"] == "low"


def test_issue_type_sprawl_not_fired_at_40():
    snap = _snap(issue_types={"names": [], "count": 40})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "issue_type_sprawl" not in kinds


def test_issue_type_sprawl_skipped_guard():
    snap = _snap(issue_types={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "issue_type_sprawl" not in kinds


def test_issue_type_sprawl_error_guard():
    snap = _snap(issue_types={"error": "ERR"})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "issue_type_sprawl" not in kinds


# --- link_type_sprawl (low, count > 30) -------------------------------------

def test_link_type_sprawl_low_at_31():
    snap = _snap(link_types={"names": [], "count": 31})
    hits = [f for f in run_checks(snap) if f["kind"] == "link_type_sprawl"]
    assert hits and hits[0]["severity"] == "low"


def test_link_type_sprawl_not_fired_at_30():
    snap = _snap(link_types={"names": [], "count": 30})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "link_type_sprawl" not in kinds


def test_link_type_sprawl_skipped_guard():
    snap = _snap(link_types={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "link_type_sprawl" not in kinds


def test_link_type_sprawl_error_guard():
    snap = _snap(link_types={"error": "ERR"})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "link_type_sprawl" not in kinds


# --- large_workflow (per workflow when structure_checked) -------------------
# medium when statuses > 20 OR transitions > 40; else low when statuses > 12.

def _wf_detail(statuses_n, transitions_n):
    return {
        "names": ["Big WF"],
        "structure_checked": True,
        "detail": {"Big WF": {
            "statuses": [f"S{i}" for i in range(statuses_n)],
            "transitions": [f"T{i}" for i in range(transitions_n)],
        }},
    }


def test_large_workflow_medium_when_statuses_over_20():
    snap = _snap(workflows=_wf_detail(21, 10))
    hits = [f for f in run_checks(snap) if f["kind"] == "large_workflow"]
    assert hits and hits[0]["severity"] == "medium"
    assert hits[0]["name"] == "Big WF"


def test_large_workflow_medium_when_transitions_over_40():
    snap = _snap(workflows=_wf_detail(5, 41))
    hits = [f for f in run_checks(snap) if f["kind"] == "large_workflow"]
    assert hits and hits[0]["severity"] == "medium"


def test_large_workflow_low_when_statuses_between_13_and_20():
    # 13 statuses, few transitions -> low (statuses > 12 but not > 20)
    snap = _snap(workflows=_wf_detail(13, 5))
    hits = [f for f in run_checks(snap) if f["kind"] == "large_workflow"]
    assert hits and hits[0]["severity"] == "low"


def test_large_workflow_medium_boundary_at_20_statuses_is_low():
    # Exactly 20 statuses is NOT medium (needs > 20); it is > 12 so low.
    snap = _snap(workflows=_wf_detail(20, 10))
    hits = [f for f in run_checks(snap) if f["kind"] == "large_workflow"]
    assert hits and hits[0]["severity"] == "low"


def test_large_workflow_low_boundary_at_12_statuses_not_fired():
    # Exactly 12 statuses is NOT > 12 -> no finding.
    snap = _snap(workflows=_wf_detail(12, 5))
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "large_workflow" not in kinds


def test_large_workflow_not_fired_for_small_workflow():
    snap = _snap(workflows=_wf_detail(5, 8))
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "large_workflow" not in kinds


def test_large_workflow_skipped_when_structure_not_checked():
    snap = _snap(workflows={"names": ["Big WF"], "structure_checked": False})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "large_workflow" not in kinds


def test_large_workflow_skipped_guard():
    snap = _snap(workflows={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "large_workflow" not in kinds


# --- public_browse_grant (low) ----------------------------------------------
# Fires only for BROWSE_PROJECTS + holder_type == "anyone".

def test_public_browse_grant_fired():
    snap = _snap(permission_scheme_grants={"by_scheme": {
        "Default": [{"permission": "BROWSE_PROJECTS", "holder_type": "anyone"}]
    }})
    hits = [f for f in run_checks(snap) if f["kind"] == "public_browse_grant"]
    assert hits and hits[0]["severity"] == "low"
    assert hits[0]["name"] == "Default"


def test_public_browse_grant_not_fired_for_administer():
    # ADMINISTER + anyone is permission_grant_overly_broad, NOT public_browse_grant.
    snap = _snap(permission_scheme_grants={"by_scheme": {
        "Default": [{"permission": "ADMINISTER", "holder_type": "anyone"}]
    }})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "public_browse_grant" not in kinds


def test_public_browse_grant_not_fired_for_group_holder():
    # BROWSE_PROJECTS granted to a group is normal -> must not fire.
    snap = _snap(permission_scheme_grants={"by_scheme": {
        "Default": [{"permission": "BROWSE_PROJECTS", "holder_type": "group"}]
    }})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "public_browse_grant" not in kinds


def test_public_browse_grant_skipped_guard():
    snap = _snap(permission_scheme_grants={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "public_browse_grant" not in kinds


# --- component_unassigned_default (low) -------------------------------------

def test_component_unassigned_default_fired():
    snap = _snap(components={"by_project": {
        "ACME": [
            {"name": "Frontend", "has_lead": True, "assignee_type": "PROJECT_LEAD"},
            {"name": "Backend", "has_lead": True, "assignee_type": "UNASSIGNED"},
        ]
    }})
    hits = [f for f in run_checks(snap) if f["kind"] == "component_unassigned_default"]
    assert any("Backend" in f["name"] for f in hits)
    assert all(f["severity"] == "low" for f in hits)


def test_component_unassigned_default_not_fired_when_assignee_set():
    snap = _snap(components={"by_project": {
        "ACME": [{"name": "Frontend", "has_lead": True, "assignee_type": "PROJECT_LEAD"}]
    }})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "component_unassigned_default" not in kinds


def test_component_unassigned_default_skipped_guard():
    snap = _snap(components={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "component_unassigned_default" not in kinds


# ===========================================================================
# Section 1 — comprehensive coverage (catalog docs/superpowers)
# Names the individual surplus objects behind count-only rules, adds the
# guardrail-aligned field limit, duplicate-name correctness checks, and the
# anonymous-write / admin-to-logged-in security grants.
# ===========================================================================

# --- unused_resolution (low) ------------------------------------------------
# A resolution name outside the canonical default set is flagged for review.

def test_unused_resolution_flags_non_canonical_name():
    snap = _snap(resolutions={
        "names": ["Done", "Won't Do", "Migrated From Legacy"], "count": 3})
    hits = [f for f in run_checks(snap) if f["kind"] == "unused_resolution"]
    assert any(f["name"] == "Migrated From Legacy" for f in hits)
    assert all(f["severity"] == "low" for f in hits)


def test_unused_resolution_not_fired_for_canonical_only():
    snap = _snap(resolutions={
        "names": ["Done", "Won't Do", "Duplicate", "Cannot Reproduce",
                  "Won't Fix"], "count": 5})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "unused_resolution" not in kinds


def test_unused_resolution_canonical_match_is_case_insensitive():
    snap = _snap(resolutions={"names": ["done", "WON'T DO"], "count": 2})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "unused_resolution" not in kinds


def test_unused_resolution_skipped_guard():
    snap = _snap(resolutions={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "unused_resolution" not in kinds


def test_unused_resolution_error_guard():
    snap = _snap(resolutions={"error": "ERR"})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "unused_resolution" not in kinds


# --- near_field_limit ------------------------------------------------------
# The 700-field hard limit is PER company-managed PROJECT (Atlassian data
# limits, enforced 2026), not site-wide. A project's fields are a SUBSET of all
# custom fields, so a site-wide count <= 700 PROVES no project can exceed the
# limit (no false positive possible). Only a site-wide count > 700 makes a
# per-project violation possible — and even then it is unconfirmable from the
# site-wide total, so we disclose it (medium) rather than assert a HIGH block.

def test_near_field_limit_silent_within_per_project_limit():
    # 560 (old warn) and 700 (the limit itself) are provably safe site-wide:
    # no single project can hold more fields than the whole site has.
    for n in (560, 700):
        kinds = {f["kind"] for f in run_checks(
            _snap(custom_fields={"names": [], "count": n}))}
        assert "near_field_limit" not in kinds, f"false positive at count={n}"


def test_near_field_limit_discloses_above_per_project_limit():
    hits = [f for f in run_checks(_snap(custom_fields={"names": [], "count": 701}))
            if f["kind"] == "near_field_limit"]
    assert hits and hits[0]["severity"] == "medium"
    # Must disclose the per-project scope so it is not read as a confirmed block.
    assert "project" in str(hits[0]["detail"].get("note", "")).lower()


def test_near_field_limit_skipped_guard():
    snap = _snap(custom_fields={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "near_field_limit" not in kinds


# --- duplicate_status_name (medium) -----------------------------------------
# Two status names normalise to the same value (case/whitespace-insensitive).

def test_duplicate_status_name_fired():
    snap = _snap(statuses={"names": ["In Review", "in review ", "Done"],
                           "count": 3})
    hits = [f for f in run_checks(snap) if f["kind"] == "duplicate_status_name"]
    assert hits and hits[0]["severity"] == "medium"
    assert hits[0]["name"] == "in review "


def test_duplicate_status_name_not_fired_when_distinct():
    snap = _snap(statuses={"names": ["Open", "In Review", "Done"], "count": 3})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "duplicate_status_name" not in kinds


def test_duplicate_status_name_skipped_guard():
    snap = _snap(statuses={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "duplicate_status_name" not in kinds


def test_duplicate_status_name_error_guard():
    snap = _snap(statuses={"error": "ERR"})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "duplicate_status_name" not in kinds


# --- duplicate_issue_type_name (medium) -------------------------------------

def test_duplicate_issue_type_name_fired():
    snap = _snap(issue_types={"names": ["Bug", "bug", "Story"], "count": 3})
    hits = [f for f in run_checks(snap)
            if f["kind"] == "duplicate_issue_type_name"]
    assert hits and hits[0]["severity"] == "medium"


def test_duplicate_issue_type_name_not_fired_when_distinct():
    snap = _snap(issue_types={"names": ["Bug", "Story", "Task"], "count": 3})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "duplicate_issue_type_name" not in kinds


def test_duplicate_issue_type_name_skipped_guard():
    snap = _snap(issue_types={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "duplicate_issue_type_name" not in kinds


def test_duplicate_issue_type_name_error_guard():
    snap = _snap(issue_types={"error": "ERR"})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "duplicate_issue_type_name" not in kinds


# --- redundant_priority_set (low) -------------------------------------------
# Count exceeds the default 5 AND a normalised-collision near-duplicate exists.

def test_redundant_priority_set_fired_on_collision_over_default():
    snap = _snap(priorities={
        "names": ["Highest", "High", "high ", "Medium", "Low", "Lowest"],
        "count": 6})
    hits = [f for f in run_checks(snap)
            if f["kind"] == "redundant_priority_set"]
    assert hits and hits[0]["severity"] == "low"


def test_redundant_priority_set_not_fired_at_default_count():
    # Exactly the default 5 — never flag even if names look generic.
    snap = _snap(priorities={
        "names": ["Highest", "High", "Medium", "Low", "Lowest"], "count": 5})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "redundant_priority_set" not in kinds


def test_redundant_priority_set_not_fired_without_collision():
    # Over the default count but all distinct names — no near-duplicate.
    snap = _snap(priorities={
        "names": ["Highest", "High", "Medium", "Low", "Lowest", "Trivial"],
        "count": 6})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "redundant_priority_set" not in kinds


def test_redundant_priority_set_skipped_guard():
    snap = _snap(priorities={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "redundant_priority_set" not in kinds


def test_redundant_priority_set_error_guard():
    snap = _snap(priorities={"error": "ERR"})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "redundant_priority_set" not in kinds


# --- many_overdue_versions_in_project (medium) ------------------------------
# A project with >= 5 overdue & unreleased versions.

def _overdue_versions(n):
    return [{"name": f"v{i}", "released": False, "overdue": True,
             "archived": False} for i in range(n)]


def test_many_overdue_versions_fired_at_5():
    snap = _snap(versions={"by_project": {"ACME": _overdue_versions(5)}})
    hits = [f for f in run_checks(snap)
            if f["kind"] == "many_overdue_versions_in_project"]
    assert hits and hits[0]["severity"] == "medium"
    assert hits[0]["name"] == "ACME"


def test_many_overdue_versions_not_fired_at_4():
    snap = _snap(versions={"by_project": {"ACME": _overdue_versions(4)}})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "many_overdue_versions_in_project" not in kinds


def test_many_overdue_versions_ignores_released():
    vlist = [{"name": f"v{i}", "released": True, "overdue": True,
              "archived": False} for i in range(6)]
    snap = _snap(versions={"by_project": {"ACME": vlist}})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "many_overdue_versions_in_project" not in kinds


def test_many_overdue_versions_error_guard():
    snap = _snap(versions={"error": "ERR"})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "many_overdue_versions_in_project" not in kinds


# --- large_group_admin_bloat (low) ------------------------------------------
# Admin-pattern group name with member count over threshold (> 20).

def test_large_group_admin_bloat_fired():
    snap = _snap(groups={"member_counts": {"jira-administrators": 25}})
    hits = [f for f in run_checks(snap)
            if f["kind"] == "large_group_admin_bloat"]
    assert hits and hits[0]["severity"] == "low"
    assert hits[0]["name"] == "jira-administrators"


def test_large_group_admin_bloat_matches_site_admins():
    snap = _snap(groups={"member_counts": {"site-admins": 21}})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "large_group_admin_bloat" in kinds


def test_large_group_admin_bloat_not_fired_for_small_admin_group():
    snap = _snap(groups={"member_counts": {"jira-administrators": 20}})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "large_group_admin_bloat" not in kinds


def test_large_group_admin_bloat_not_fired_for_non_admin_group():
    # Large but not an admin group -> never flag.
    snap = _snap(groups={"member_counts": {"developers": 500}})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "large_group_admin_bloat" not in kinds


def test_large_group_admin_bloat_skipped_guard():
    snap = _snap(groups={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "large_group_admin_bloat" not in kinds


# --- anonymous_write_grant (medium) -----------------------------------------
# holder_type == "anyone" granted a write permission (create/comment/edit).

def test_anonymous_write_grant_create_issues():
    snap = _snap(permission_scheme_grants={"by_scheme": {
        "Default": [{"permission": "CREATE_ISSUES", "holder_type": "anyone"}]
    }})
    hits = [f for f in run_checks(snap) if f["kind"] == "anonymous_write_grant"]
    assert hits and hits[0]["severity"] == "medium"
    assert hits[0]["name"] == "Default"


def test_anonymous_write_grant_add_comments():
    snap = _snap(permission_scheme_grants={"by_scheme": {
        "Default": [{"permission": "ADD_COMMENTS", "holder_type": "anyone"}]
    }})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "anonymous_write_grant" in kinds


def test_anonymous_write_grant_not_fired_for_browse():
    # BROWSE_PROJECTS + anyone is public_browse_grant, NOT a write grant.
    snap = _snap(permission_scheme_grants={"by_scheme": {
        "Default": [{"permission": "BROWSE_PROJECTS", "holder_type": "anyone"}]
    }})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "anonymous_write_grant" not in kinds


def test_anonymous_write_grant_not_fired_for_group_holder():
    snap = _snap(permission_scheme_grants={"by_scheme": {
        "Default": [{"permission": "CREATE_ISSUES", "holder_type": "group"}]
    }})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "anonymous_write_grant" not in kinds


def test_anonymous_write_grant_skipped_guard():
    snap = _snap(permission_scheme_grants={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "anonymous_write_grant" not in kinds


# --- admin_grant_to_logged_in (medium) --------------------------------------
# ADMINISTER / ADMINISTER_PROJECTS granted to all logged-in users.

def test_admin_grant_to_logged_in_application_role():
    snap = _snap(permission_scheme_grants={"by_scheme": {
        "Default": [{"permission": "ADMINISTER", "holder_type": "applicationRole"}]
    }})
    hits = [f for f in run_checks(snap) if f["kind"] == "admin_grant_to_logged_in"]
    assert hits and hits[0]["severity"] == "medium"


def test_admin_grant_to_logged_in_user():
    snap = _snap(permission_scheme_grants={"by_scheme": {
        "Default": [{"permission": "ADMINISTER_PROJECTS",
                     "holder_type": "loggedInUser"}]
    }})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "admin_grant_to_logged_in" in kinds


def test_admin_grant_to_logged_in_not_fired_for_non_admin_perm():
    snap = _snap(permission_scheme_grants={"by_scheme": {
        "Default": [{"permission": "CREATE_ISSUES", "holder_type": "loggedInUser"}]
    }})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "admin_grant_to_logged_in" not in kinds


def test_admin_grant_to_logged_in_not_fired_for_group_holder():
    # admin to a group is normal -> never flag here.
    snap = _snap(permission_scheme_grants={"by_scheme": {
        "Default": [{"permission": "ADMINISTER", "holder_type": "group"}]
    }})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "admin_grant_to_logged_in" not in kinds


def test_admin_grant_to_logged_in_skipped_guard():
    snap = _snap(permission_scheme_grants={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "admin_grant_to_logged_in" not in kinds


# --- board_count_exceeds_projects (info) ------------------------------------
# boards.count > 3x the project count.

def test_board_count_exceeds_projects_fired():
    snap = _snap(boards={"names": [], "count": 11})  # 1 project, >3x AND >= floor
    hits = [f for f in run_checks(snap)
            if f["kind"] == "board_count_exceeds_projects"]
    assert hits and hits[0]["severity"] == "info"


def test_board_count_exceeds_projects_not_fired_at_ratio():
    snap = _snap(boards={"names": [], "count": 3})  # exactly 3x — not over
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "board_count_exceeds_projects" not in kinds


def test_board_count_not_fired_on_small_absolute_count():
    # 4 boards / 1 project exceeds 3x but is not an "explosion" — the absolute
    # floor suppresses it so small instances aren't flagged.
    snap = _snap(boards={"names": [], "count": 4})
    assert "board_count_exceeds_projects" not in {
        f["kind"] for f in run_checks(snap)}


def test_board_count_exceeds_projects_skipped_when_no_projects():
    # No project denominator -> cannot compute a ratio, never fire.
    snap = _snap(boards={"names": [], "count": 50})
    snap["projects"] = []
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "board_count_exceeds_projects" not in kinds


def test_board_count_exceeds_projects_skipped_guard():
    snap = _snap(boards={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "board_count_exceeds_projects" not in kinds


# --- dashboard_filter_volume_high (info) ------------------------------------
# filters or dashboards at/near the 500 gather cap (capped flag true).

def test_dashboard_filter_volume_high_fired_for_capped_filters():
    snap = _snap(filters={"count": 500, "capped": True})
    hits = [f for f in run_checks(snap)
            if f["kind"] == "dashboard_filter_volume_high"]
    assert hits and hits[0]["severity"] == "info"
    assert hits[0]["name"] == "filters"


def test_dashboard_filter_volume_high_fired_for_capped_dashboards():
    snap = _snap(dashboards={"count": 500, "capped": True})
    hits = [f for f in run_checks(snap)
            if f["kind"] == "dashboard_filter_volume_high"]
    assert any(f["name"] == "dashboards" for f in hits)


def test_dashboard_filter_volume_high_not_fired_when_not_capped():
    snap = _snap(filters={"count": 12, "capped": False},
                 dashboards={"count": 8, "capped": False})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "dashboard_filter_volume_high" not in kinds


def test_dashboard_filter_volume_high_skipped_guard():
    snap = _snap(filters={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "dashboard_filter_volume_high" not in kinds


# --- version_naming_inconsistent (info) -------------------------------------
# Within one project, version names mix semver (\d+\.\d+) and free text.

def _ver(name):
    return {"name": name, "released": False, "overdue": False, "archived": False}


def test_version_naming_inconsistent_fired_on_substantial_mix():
    # A REAL mix: 3 semver + 3 free-text version names in one project.
    snap = _snap(versions={"by_project": {"ACME": [
        _ver("1.0"), _ver("2.1"), _ver("3.0"),
        _ver("Sprint Backlog"), _ver("Future"), _ver("Icebox")]}})
    hits = [f for f in run_checks(snap)
            if f["kind"] == "version_naming_inconsistent"]
    assert hits and hits[0]["severity"] == "info"
    assert hits[0]["name"] == "ACME"


def test_version_naming_not_fired_on_a_couple_named_versions():
    # The COMMON case: semver releases + a couple of Backlog/Future versions is
    # normal usage and must NOT fire (cry-wolf noise on nearly every project).
    snap = _snap(versions={"by_project": {"ACME": [
        _ver("1.0"), _ver("1.1"), _ver("1.2"), _ver("2.0"), _ver("2.1"),
        _ver("Backlog"), _ver("Future")]}})
    assert "version_naming_inconsistent" not in {
        f["kind"] for f in run_checks(snap)}


def test_version_naming_inconsistent_not_fired_all_semver():
    snap = _snap(versions={"by_project": {"ACME": [
        {"name": "1.0", "released": True, "overdue": False, "archived": False},
        {"name": "2.1", "released": False, "overdue": False, "archived": False},
    ]}})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "version_naming_inconsistent" not in kinds


def test_version_naming_inconsistent_not_fired_all_freetext():
    snap = _snap(versions={"by_project": {"ACME": [
        {"name": "Alpha", "released": True, "overdue": False, "archived": False},
        {"name": "Beta", "released": False, "overdue": False, "archived": False},
    ]}})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "version_naming_inconsistent" not in kinds


def test_version_naming_inconsistent_error_guard():
    snap = _snap(versions={"error": "ERR"})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "version_naming_inconsistent" not in kinds


# ===========================================================================
# SECTION 2 — project activity + shared-object ownership checks
# Source areas: projects {by_project: {KEY: {issue_count, stale}}},
#               filters/dashboards {items: [{owner_active, public}]}.
# Privacy: checks read booleans/counts/KEYS only — no identity ever.
# ===========================================================================

# --- empty_project (Hygiene, low) -------------------------------------------

def test_empty_project_fired():
    snap = _snap(projects={"by_project": {
        "ACME": {"issue_count": 0, "stale": False},
        "BETA": {"issue_count": 12, "stale": False},
    }, "count": 2})
    hits = [f for f in run_checks(snap) if f["kind"] == "empty_project"]
    assert hits and hits[0]["severity"] == "low"
    assert hits[0]["name"] == "ACME"
    assert not any(f["name"] == "BETA" for f in hits)


def test_empty_project_not_fired_when_has_issues():
    snap = _snap(projects={"by_project": {
        "BETA": {"issue_count": 1, "stale": False}}, "count": 1})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "empty_project" not in kinds


def test_empty_project_not_fired_when_issue_count_none():
    # DC: issue_count None means unknown, never "empty".
    snap = _snap(projects={"by_project": {
        "ACME": {"issue_count": None, "stale": False}}, "count": 1})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "empty_project" not in kinds


def test_empty_project_skipped_guard():
    snap = _snap(projects={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "empty_project" not in kinds


def test_empty_project_error_guard():
    snap = _snap(projects={"error": "ERR"})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "empty_project" not in kinds


# --- inactive_project (Hygiene, medium) -------------------------------------

def test_inactive_project_fired():
    snap = _snap(projects={"by_project": {
        "OLD": {"issue_count": 5, "stale": True},
        "ACME": {"issue_count": 12, "stale": False},
    }, "count": 2})
    hits = [f for f in run_checks(snap) if f["kind"] == "inactive_project"]
    assert hits and hits[0]["severity"] == "medium"
    assert hits[0]["name"] == "OLD"
    assert not any(f["name"] == "ACME" for f in hits)


def test_inactive_project_not_fired_when_not_stale():
    snap = _snap(projects={"by_project": {
        "ACME": {"issue_count": 12, "stale": False}}, "count": 1})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "inactive_project" not in kinds


def test_inactive_project_requires_issues():
    # stale True but zero issues -> empty_project, NOT inactive_project.
    snap = _snap(projects={"by_project": {
        "ACME": {"issue_count": 0, "stale": True}}, "count": 1})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "inactive_project" not in kinds


def test_inactive_project_skipped_guard():
    snap = _snap(projects={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "inactive_project" not in kinds


def test_inactive_project_error_guard():
    snap = _snap(projects={"error": "ERR"})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "inactive_project" not in kinds


# --- shared_object_owned_by_inactive (Security, high) -----------------------
# A filter OR dashboard with owner_active == False. Generic name ("filter"/
# "dashboard"), detail counts how many — NO identity.

def test_shared_object_owned_by_inactive_filter():
    snap = _snap(filters={"count": 2, "capped": False, "items": [
        {"owner_active": False, "public": False},
        {"owner_active": True, "public": False},
    ]})
    hits = [f for f in run_checks(snap)
            if f["kind"] == "shared_object_owned_by_inactive"]
    assert hits and hits[0]["severity"] == "high"
    assert hits[0]["name"] == "filter"
    assert hits[0]["detail"]["count"] == 1


def test_shared_object_owned_by_inactive_dashboard():
    snap = _snap(dashboards={"count": 3, "capped": False, "items": [
        {"owner_active": False, "public": False},
        {"owner_active": False, "public": True},
        {"owner_active": True, "public": False},
    ]})
    hits = [f for f in run_checks(snap)
            if f["kind"] == "shared_object_owned_by_inactive"]
    assert any(f["name"] == "dashboard" and f["detail"]["count"] == 2
               for f in hits)


def test_shared_object_owned_by_inactive_not_fired_all_active():
    snap = _snap(
        filters={"count": 1, "capped": False,
                 "items": [{"owner_active": True, "public": False}]},
        dashboards={"count": 1, "capped": False,
                    "items": [{"owner_active": True, "public": False}]})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "shared_object_owned_by_inactive" not in kinds


def test_shared_object_owned_by_inactive_ignores_ownerless_object():
    # The built-in System Dashboard has NO owner; gather stores owner_active=None
    # (unknown), which must NOT count as inactive-owned. Regression for the false
    # HIGH finding that escalated every site's audit to CRITICAL.
    snap = _snap(
        dashboards={"count": 2, "capped": False, "items": [
            {"owner_active": None, "public": False},   # System Dashboard, no owner
            {"owner_active": True, "public": False}]})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "shared_object_owned_by_inactive" not in kinds


def test_shared_object_owned_by_inactive_no_identity_in_finding():
    snap = _snap(filters={"count": 1, "capped": False,
                          "items": [{"owner_active": False, "public": True}]})
    hits = [f for f in run_checks(snap)
            if f["kind"] == "shared_object_owned_by_inactive"]
    # Name and detail must carry NO identity — only the generic object label
    # and an aggregate count.
    assert hits[0]["name"] in ("filter", "dashboard")
    assert set(hits[0]["detail"]) <= {"count", "category", "fix"}


def test_shared_object_owned_by_inactive_skipped_guard():
    snap = _snap(filters={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "shared_object_owned_by_inactive" not in kinds


def test_shared_object_owned_by_inactive_dc_count_only_no_items():
    # DC filters carry no items -> cannot evaluate -> must not fire.
    snap = _snap(filters={"count": 5, "capped": False})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "shared_object_owned_by_inactive" not in kinds


# --- public_shared_filter (Security, high) ----------------------------------

def test_public_shared_filter_fired():
    snap = _snap(filters={"count": 2, "capped": False, "items": [
        {"owner_active": True, "public": True},
        {"owner_active": True, "public": False},
    ]})
    hits = [f for f in run_checks(snap) if f["kind"] == "public_shared_filter"]
    assert hits and hits[0]["severity"] == "high"
    assert hits[0]["detail"]["count"] == 1


def test_public_shared_filter_not_fired_when_none_public():
    snap = _snap(filters={"count": 1, "capped": False,
                          "items": [{"owner_active": True, "public": False}]})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "public_shared_filter" not in kinds


def test_public_shared_filter_skipped_guard():
    snap = _snap(filters={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "public_shared_filter" not in kinds


def test_public_shared_filter_dc_count_only_no_items():
    snap = _snap(filters={"count": 5, "capped": False})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "public_shared_filter" not in kinds


# --- public_shared_dashboard (Security, medium) -----------------------------

def test_public_shared_dashboard_fired():
    snap = _snap(dashboards={"count": 2, "capped": False, "items": [
        {"owner_active": True, "public": True},
        {"owner_active": True, "public": True},
    ]})
    hits = [f for f in run_checks(snap)
            if f["kind"] == "public_shared_dashboard"]
    assert hits and hits[0]["severity"] == "medium"
    assert hits[0]["detail"]["count"] == 2


def test_public_shared_dashboard_not_fired_when_none_public():
    snap = _snap(dashboards={"count": 1, "capped": False,
                             "items": [{"owner_active": True, "public": False}]})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "public_shared_dashboard" not in kinds


def test_public_shared_dashboard_skipped_guard():
    snap = _snap(dashboards={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "public_shared_dashboard" not in kinds


def test_public_shared_dashboard_dc_count_only_no_items():
    snap = _snap(dashboards={"count": 5, "capped": False})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "public_shared_dashboard" not in kinds


# ===========================================================================
# WORKFLOW-STRUCTURE + SCHEME-MAPPING checks (catalog Batch-A deferrals)
# All strictly no-false-positive: structure checks fire ONLY when the graph is
# fully known (structure_checked True AND edges present). Scheme-mapping checks
# fire ONLY when the relevant *_used set is present.
# ===========================================================================


def _wf_with_edges(name, statuses, edges, transitions=None):
    """Build a workflow area whose detail carries a transition graph (edges).
    edges: list of {to, from, global}. transitions defaults to one per edge."""
    return {
        "names": [name],
        "structure_checked": True,
        "detail": {name: {
            "statuses": statuses,
            "transitions": transitions if transitions is not None
            else [f"t{i}" for i in range(len(edges))],
            "edges": edges,
        }},
    }


# --- unreachable_status (Structure, medium) ---------------------------------
# A status that is never the `to` of any transition AND is not the initial/
# create status. Fires only when edges are present.

def test_unreachable_status_fired():
    # To Do is the create destination; In Progress is reachable; Lost is not the
    # `to` of any transition and is not initial -> unreachable.
    edges = [
        {"to": "To Do", "from": [], "global": False},        # create/initial
        {"to": "In Progress", "from": ["To Do"], "global": False},
    ]
    snap = _snap(workflows=_wf_with_edges(
        "WF", ["To Do", "In Progress", "Lost"], edges))
    hits = [f for f in run_checks(snap) if f["kind"] == "unreachable_status"]
    assert hits and hits[0]["severity"] == "medium"
    assert any(f["name"] == "WF / Lost" or "Lost" in f["name"] for f in hits)


def test_unreachable_status_not_fired_for_initial_status():
    # The create destination (To Do) is never another transition's `to`, but it
    # is the initial status -> must NOT be flagged unreachable.
    edges = [
        {"to": "To Do", "from": [], "global": False},
        {"to": "Done", "from": ["To Do"], "global": False},
    ]
    snap = _snap(workflows=_wf_with_edges("WF", ["To Do", "Done"], edges))
    hits = [f for f in run_checks(snap) if f["kind"] == "unreachable_status"]
    names = [f["name"] for f in hits]
    assert not any("To Do" in n for n in names)


def test_unreachable_status_not_fired_when_fully_reachable():
    # Every non-initial status is the `to` of some transition.
    edges = [
        {"to": "Open", "from": [], "global": False},          # initial
        {"to": "In Progress", "from": ["Open"], "global": False},
        {"to": "Done", "from": ["In Progress"], "global": False},
    ]
    snap = _snap(workflows=_wf_with_edges(
        "WF", ["Open", "In Progress", "Done"], edges))
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "unreachable_status" not in kinds


def test_unreachable_status_global_makes_reachable():
    # A global transition into Done makes Done reachable from any status.
    edges = [
        {"to": "Open", "from": [], "global": False},
        {"to": "Done", "from": [], "global": True},
    ]
    snap = _snap(workflows=_wf_with_edges("WF", ["Open", "Done"], edges))
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "unreachable_status" not in kinds


def test_unreachable_status_not_fired_without_edges():
    # structure_checked but no edges (older snapshot) -> unevaluable.
    snap = _snap(workflows={
        "names": ["WF"], "structure_checked": True,
        "detail": {"WF": {"statuses": ["A", "B"], "transitions": ["t"]}}})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "unreachable_status" not in kinds


def test_unreachable_status_not_fired_on_dc():
    # DC: structure_checked False -> never fire.
    snap = _snap(workflows={"names": ["WF"], "structure_checked": False})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "unreachable_status" not in kinds


def test_unreachable_status_skipped_guard():
    snap = _snap(workflows={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "unreachable_status" not in kinds


# --- dead_end_status (Structure, low) ---------------------------------------
# A non-terminal status with NO outbound transition (never a `from`, ignoring
# globals which are outbound-from-everywhere). Done/closed/resolved/cancelled
# names are exempt (legitimate terminal states).

def test_dead_end_status_fired():
    # "Stuck" has no outbound transition and is not a done-style name.
    edges = [
        {"to": "Open", "from": [], "global": False},
        {"to": "Stuck", "from": ["Open"], "global": False},
    ]
    snap = _snap(workflows=_wf_with_edges("WF", ["Open", "Stuck"], edges))
    hits = [f for f in run_checks(snap) if f["kind"] == "dead_end_status"]
    assert hits and hits[0]["severity"] == "low"
    assert any("Stuck" in f["name"] for f in hits)


def test_dead_end_status_not_fired_for_done_status():
    # "Done" has no outbound transition but its name matches the terminal
    # pattern -> a legitimate terminal status, must NOT be flagged.
    edges = [
        {"to": "Open", "from": [], "global": False},
        {"to": "Done", "from": ["Open"], "global": False},
    ]
    snap = _snap(workflows=_wf_with_edges("WF", ["Open", "Done"], edges))
    hits = [f for f in run_checks(snap) if f["kind"] == "dead_end_status"]
    assert not any("Done" in f["name"] for f in hits)


def test_dead_end_status_terminal_patterns_exempt():
    # Closed / Resolved / Cancelled are all exempt terminal names.
    edges = [
        {"to": "Open", "from": [], "global": False},
        {"to": "Closed", "from": ["Open"], "global": False},
        {"to": "Resolved", "from": ["Open"], "global": False},
        {"to": "Cancelled", "from": ["Open"], "global": False},
    ]
    snap = _snap(workflows=_wf_with_edges(
        "WF", ["Open", "Closed", "Resolved", "Cancelled"], edges))
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "dead_end_status" not in kinds


def test_dead_end_status_not_fired_when_has_outbound():
    # Every status is a `from` of some transition -> no dead ends.
    edges = [
        {"to": "Open", "from": [], "global": False},
        {"to": "In Progress", "from": ["Open"], "global": False},
        {"to": "Open", "from": ["In Progress"], "global": False},  # loop back
    ]
    snap = _snap(workflows=_wf_with_edges("WF", ["Open", "In Progress"], edges))
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "dead_end_status" not in kinds


def test_dead_end_status_global_counts_as_outbound():
    # A global transition gives every status an outbound path -> no dead ends.
    edges = [
        {"to": "Open", "from": [], "global": False},
        {"to": "Limbo", "from": ["Open"], "global": False},
        {"to": "Done", "from": [], "global": True},  # global outbound for all
    ]
    snap = _snap(workflows=_wf_with_edges("WF", ["Open", "Limbo", "Done"], edges))
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "dead_end_status" not in kinds


def test_dead_end_status_not_fired_without_edges():
    snap = _snap(workflows={
        "names": ["WF"], "structure_checked": True,
        "detail": {"WF": {"statuses": ["A"], "transitions": []}}})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "dead_end_status" not in kinds


def test_dead_end_status_skipped_guard():
    snap = _snap(workflows={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "dead_end_status" not in kinds


# --- global_transition_overuse (Hygiene, low) -------------------------------
# A workflow with more than GLOBAL_TRANSITION_WARN (3) global transitions.

def _globals(n):
    edges = [{"to": "Open", "from": [], "global": False}]  # initial
    edges += [{"to": f"G{i}", "from": [], "global": True} for i in range(n)]
    return edges


def test_global_transition_overuse_fired_at_4():
    edges = _globals(4)
    statuses = ["Open"] + [f"G{i}" for i in range(4)]
    snap = _snap(workflows=_wf_with_edges("WF", statuses, edges))
    hits = [f for f in run_checks(snap) if f["kind"] == "global_transition_overuse"]
    assert hits and hits[0]["severity"] == "low"
    assert hits[0]["name"] == "WF"


def test_global_transition_overuse_not_fired_at_3():
    edges = _globals(3)
    statuses = ["Open"] + [f"G{i}" for i in range(3)]
    snap = _snap(workflows=_wf_with_edges("WF", statuses, edges))
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "global_transition_overuse" not in kinds


def test_global_transition_overuse_not_fired_without_edges():
    snap = _snap(workflows={
        "names": ["WF"], "structure_checked": True,
        "detail": {"WF": {"statuses": ["A"], "transitions": ["t"]}}})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "global_transition_overuse" not in kinds


def test_global_transition_overuse_skipped_guard():
    snap = _snap(workflows={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "global_transition_overuse" not in kinds


# --- workflow_unreferenced (Hygiene, low) -----------------------------------
# A workflow name in workflows.names not present in
# workflow_schemes.workflows_used. Only when workflows_used is present.

def test_workflow_unreferenced_fired():
    snap = _snap(
        workflows={"names": ["Used WF", "Orphan WF"], "structure_checked": True},
        workflow_schemes={"names": ["Scheme A"], "count": 1,
                          "workflows_used": ["Used WF"]})
    hits = [f for f in run_checks(snap) if f["kind"] == "workflow_unreferenced"]
    assert hits and hits[0]["severity"] == "low"
    assert any(f["name"] == "Orphan WF" for f in hits)
    assert not any(f["name"] == "Used WF" for f in hits)


def test_workflow_unreferenced_not_fired_when_all_referenced():
    snap = _snap(
        workflows={"names": ["Used WF"], "structure_checked": True},
        workflow_schemes={"names": ["Scheme A"], "count": 1,
                          "workflows_used": ["Used WF"]})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "workflow_unreferenced" not in kinds


def test_workflow_unreferenced_skipped_when_workflows_used_absent():
    # No workflows_used -> unevaluable, never flag every workflow.
    snap = _snap(
        workflows={"names": ["Orphan WF"], "structure_checked": True},
        workflow_schemes={"names": ["Scheme A"], "count": 1})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "workflow_unreferenced" not in kinds


def test_workflow_unreferenced_skipped_when_workflows_errored():
    snap = _snap(
        workflows={"error": "ERR"},
        workflow_schemes={"names": ["Scheme A"], "count": 1,
                          "workflows_used": ["Used WF"]})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "workflow_unreferenced" not in kinds


def test_workflow_unreferenced_skipped_when_schemes_skipped():
    snap = _snap(
        workflows={"names": ["Orphan WF"], "structure_checked": True},
        workflow_schemes={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "workflow_unreferenced" not in kinds


# --- screen_not_in_scheme (Hygiene, low) ------------------------------------
# A screen name in screens.names not in screen_schemes.screens_used.

def test_screen_not_in_scheme_fired():
    snap = _snap(
        screens={"names": ["Default Screen", "Orphan Screen"], "count": 2,
                 "fields": {}},
        screen_schemes={"names": ["Default SS"], "count": 1,
                        "screens_used": ["Default Screen"]})
    hits = [f for f in run_checks(snap) if f["kind"] == "screen_not_in_scheme"]
    assert hits and hits[0]["severity"] == "low"
    assert any(f["name"] == "Orphan Screen" for f in hits)
    assert not any(f["name"] == "Default Screen" for f in hits)


def test_screen_not_in_scheme_not_fired_when_all_used():
    snap = _snap(
        screens={"names": ["Default Screen"], "count": 1, "fields": {}},
        screen_schemes={"names": ["Default SS"], "count": 1,
                        "screens_used": ["Default Screen"]})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "screen_not_in_scheme" not in kinds


def test_screen_not_in_scheme_skipped_when_screens_used_absent():
    snap = _snap(
        screens={"names": ["Orphan Screen"], "count": 1, "fields": {}},
        screen_schemes={"names": ["Default SS"], "count": 1})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "screen_not_in_scheme" not in kinds


def test_screen_not_in_scheme_skipped_when_screens_errored():
    snap = _snap(
        screens={"error": "ERR"},
        screen_schemes={"names": ["Default SS"], "count": 1,
                        "screens_used": ["Default Screen"]})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "screen_not_in_scheme" not in kinds


def test_screen_not_in_scheme_skipped_when_schemes_skipped():
    snap = _snap(
        screens={"names": ["Orphan Screen"], "count": 1, "fields": {}},
        screen_schemes={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "screen_not_in_scheme" not in kinds


# ===========================================================================
# SECTION 3 (ISSUE-LEVEL / DATA QUALITY) checks
# Source area: issue_quality {done_unresolved, stale_open,
#   unassigned_unresolved, resolved_but_open, total_unresolved, error}.
# Every metric is an int OR None. A None metric is UNEVALUABLE (never a false
# clean). Checks read integers only — no issue content, keys, or identities.
# ===========================================================================


def _iq(**metrics):
    """Build an issue_quality area with sensible defaults (all None) overridden
    by the supplied metrics. error defaults to None (evaluable)."""
    base = {"done_unresolved": None, "stale_open": None,
            "unassigned_unresolved": None, "resolved_but_open": None,
            "total_unresolved": None, "error": None}
    base.update(metrics)
    return base


# --- done_but_unresolved (DataQuality, high) --------------------------------
# done_unresolved > 0.

def test_done_but_unresolved_fired():
    snap = _snap(issue_quality=_iq(done_unresolved=4, total_unresolved=100))
    hits = [f for f in run_checks(snap) if f["kind"] == "done_but_unresolved"]
    assert hits and hits[0]["severity"] == "high"
    assert hits[0]["detail"]["count"] == 4


def test_done_but_unresolved_not_fired_at_zero():
    snap = _snap(issue_quality=_iq(done_unresolved=0, total_unresolved=100))
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "done_but_unresolved" not in kinds


def test_done_but_unresolved_unevaluable_when_none():
    # None metric is unevaluable -> never a false clean and never a finding.
    snap = _snap(issue_quality=_iq(done_unresolved=None))
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "done_but_unresolved" not in kinds


def test_done_but_unresolved_skipped_guard():
    snap = _snap(issue_quality={"skipped": True})
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "done_but_unresolved" not in kinds


def test_done_but_unresolved_error_guard():
    snap = _snap(issue_quality=_iq(done_unresolved=5, error="ERR"))
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "done_but_unresolved" not in kinds


# --- resolved_but_open (DataQuality, medium) --------------------------------
# resolved_but_open > 0 (the inverse defect: resolution set, status not Done).

def test_resolved_but_open_fired():
    snap = _snap(issue_quality=_iq(resolved_but_open=3, total_unresolved=100))
    hits = [f for f in run_checks(snap) if f["kind"] == "resolved_but_open"]
    assert hits and hits[0]["severity"] == "medium"
    assert hits[0]["detail"]["count"] == 3


def test_resolved_but_open_not_fired_at_zero():
    snap = _snap(issue_quality=_iq(resolved_but_open=0))
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "resolved_but_open" not in kinds


def test_resolved_but_open_unevaluable_when_none():
    snap = _snap(issue_quality=_iq(resolved_but_open=None))
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "resolved_but_open" not in kinds


def test_resolved_but_open_error_guard():
    snap = _snap(issue_quality=_iq(resolved_but_open=9, error="ERR"))
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "resolved_but_open" not in kinds


# --- stale_open_issues (DataQuality, medium) --------------------------------
# stale_open > STALE_ISSUE_WARN (50) OR a high fraction of total_unresolved.

def test_stale_open_issues_fired_over_absolute_threshold():
    snap = _snap(issue_quality=_iq(stale_open=60, total_unresolved=1000))
    hits = [f for f in run_checks(snap) if f["kind"] == "stale_open_issues"]
    assert hits and hits[0]["severity"] == "medium"
    assert hits[0]["detail"]["count"] == 60


def test_stale_open_issues_fired_on_high_fraction():
    # Below the absolute warn (50) but a high fraction of all unresolved.
    snap = _snap(issue_quality=_iq(stale_open=40, total_unresolved=50))
    hits = [f for f in run_checks(snap) if f["kind"] == "stale_open_issues"]
    assert hits, "high stale fraction must fire even below the absolute warn"


def test_stale_open_issues_not_fired_below_threshold():
    snap = _snap(issue_quality=_iq(stale_open=10, total_unresolved=1000))
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "stale_open_issues" not in kinds


def test_stale_open_issues_not_fired_at_zero():
    snap = _snap(issue_quality=_iq(stale_open=0, total_unresolved=1000))
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "stale_open_issues" not in kinds


def test_stale_open_issues_unevaluable_when_none():
    snap = _snap(issue_quality=_iq(stale_open=None, total_unresolved=100))
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "stale_open_issues" not in kinds


def test_stale_open_issues_error_guard():
    snap = _snap(issue_quality=_iq(stale_open=99, error="ERR"))
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "stale_open_issues" not in kinds


# --- unassigned_unresolved_high (DataQuality, low) --------------------------
# unassigned_unresolved > UNASSIGNED_WARN (100) OR a high fraction.

def test_unassigned_unresolved_high_fired_over_absolute():
    snap = _snap(issue_quality=_iq(unassigned_unresolved=120,
                                   total_unresolved=10000))
    hits = [f for f in run_checks(snap)
            if f["kind"] == "unassigned_unresolved_high"]
    assert hits and hits[0]["severity"] == "low"
    assert hits[0]["detail"]["count"] == 120


def test_unassigned_unresolved_high_fired_on_high_fraction():
    # Below the absolute warn (100) but most unresolved issues are unassigned.
    snap = _snap(issue_quality=_iq(unassigned_unresolved=80,
                                   total_unresolved=100))
    hits = [f for f in run_checks(snap)
            if f["kind"] == "unassigned_unresolved_high"]
    assert hits, "high unassigned fraction must fire even below the absolute warn"


def test_unassigned_unresolved_high_not_fired_below_threshold():
    snap = _snap(issue_quality=_iq(unassigned_unresolved=10,
                                   total_unresolved=10000))
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "unassigned_unresolved_high" not in kinds


def test_unassigned_unresolved_high_unevaluable_when_none():
    snap = _snap(issue_quality=_iq(unassigned_unresolved=None,
                                   total_unresolved=100))
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "unassigned_unresolved_high" not in kinds


def test_unassigned_unresolved_high_error_guard():
    snap = _snap(issue_quality=_iq(unassigned_unresolved=999, error="ERR"))
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "unassigned_unresolved_high" not in kinds


def test_issue_quality_no_issue_content_in_findings():
    """The findings emitted from issue_quality carry only counts — no issue
    key, summary, or identity can appear (the area only ever held integers)."""
    snap = _snap(issue_quality=_iq(done_unresolved=4, stale_open=60,
                                   unassigned_unresolved=120,
                                   resolved_but_open=3, total_unresolved=200))
    import json as _json
    for f in run_checks(snap):
        if f["area"] == "issue_quality":
            blob = _json.dumps(f)
            assert "SECRET" not in blob
            # detail must be integer counts/thresholds only.
            for v in f["detail"].values():
                assert isinstance(v, (int, str)), \
                    "issue_quality finding detail must be counts/labels only"


# ===========================================================================
# DC->Cloud MIGRATION checks (gated to deployment == "dc")
# ===========================================================================

def _dc_snap(**areas):
    return {"deployment": "dc", "projects": ["ACME"], "areas": areas}


def test_group_name_collision_reserved_dc():
    snap = _dc_snap(groups={"names": ["dev-team", "administrators",
                                      "site-admins"], "count": 3,
                            "member_counts": {}})
    hits = [f for f in run_checks(snap)
            if f["kind"] == "group_name_collision_reserved"]
    assert {f["name"] for f in hits} == {"administrators", "site-admins"}
    assert all(f["severity"] == "high" for f in hits)


def test_group_name_collision_not_evaluated_on_cloud():
    snap = {"deployment": "cloud", "projects": ["ACME"], "areas": {
        "groups": {"names": ["administrators"], "count": 1,
                   "member_counts": {}}}}
    assert "group_name_collision_reserved" not in {f["kind"]
                                                   for f in run_checks(snap)}


def test_unsupported_custom_field_type_dc():
    # The check reads gather's precomputed app_provided_count — classification
    # happens at gather time where the full type key is in hand. by_type holds
    # only the readable suffix and must NOT drive the verdict (review Bug 1: the
    # old check re-derived it from the suffix and flagged ~100% of fields).
    snap = _dc_snap(custom_fields={"names": ["A", "B", "C"], "count": 50,
        "app_provided_count": 2,
        "by_type": {"A": "textarea", "B": "scripted-field",
                    "C": "accounts.customfield"}})
    hits = [f for f in run_checks(snap)
            if f["kind"] == "unsupported_custom_field_type"]
    assert hits and hits[0]["severity"] == "high"
    assert hits[0]["detail"]["count"] == 2          # the two app-provided types
    assert hits[0]["detail"]["total"] == 50         # of all custom fields


def test_unsupported_custom_field_type_clean_when_all_standard():
    # Suffix by_type that LOOKS app-ish must not matter: only app_provided_count
    # decides. 0 app-provided -> no finding (guards the false-positive regression).
    snap = _dc_snap(custom_fields={"names": ["A", "B"], "count": 2,
        "app_provided_count": 0,
        "by_type": {"A": "select", "B": "scripted-field"}})
    assert "unsupported_custom_field_type" not in {f["kind"]
                                                   for f in run_checks(snap)}


def test_unsupported_custom_field_type_unevaluable_when_count_absent():
    # A snapshot without app_provided_count (pre-fix / unevaluable) must not
    # fire — never a false positive from missing data.
    snap = _dc_snap(custom_fields={"names": ["A"], "count": 1,
        "by_type": {"A": "scripted-field"}})
    assert "unsupported_custom_field_type" not in {f["kind"]
                                                   for f in run_checks(snap)}


def test_migration_checks_skipped_when_areas_missing():
    snap = {"deployment": "dc", "projects": ["ACME"], "areas": {}}
    kinds = {f["kind"] for f in run_checks(snap)}
    assert "group_name_collision_reserved" not in kinds
    assert "unsupported_custom_field_type" not in kinds


# ===========================================================================
# CLOUD GUARDRAIL review (near a hard Atlassian Cloud limit) — both deploys
# ===========================================================================

def test_near_issue_type_limit_silent_within_per_project_limit():
    # 150 issue types is the hard limit PER company-managed project; a project's
    # issue types are a subset of all of them, so a site-wide count <= 150 is
    # provably safe (and 130, the old medium tier, is now a false positive).
    for n in (130, 150):
        assert "near_issue_type_limit" not in {f["kind"] for f in run_checks(
            _snap(issue_types={"count": n, "names": []}))}, f"false pos at {n}"


def test_near_issue_type_limit_discloses_above_per_project_limit():
    hits = [f for f in run_checks(_snap(issue_types={"count": 152, "names": []}))
            if f["kind"] == "near_issue_type_limit"]
    assert hits and hits[0]["severity"] == "medium"
    assert "project" in str(hits[0]["detail"].get("note", "")).lower()


def test_near_priority_limit():
    hi = [f for f in run_checks(_snap(priorities={"count": 105, "names": []}))
          if f["kind"] == "near_priority_limit"]
    assert hi and hi[0]["severity"] == "high"


def test_near_workflow_limit():
    hi = [f for f in run_checks(_snap(workflows={"count": 160, "names": []}))
          if f["kind"] == "near_workflow_limit"]
    assert hi and hi[0]["severity"] == "high"


def test_workflow_structure_unchecked_discloses_capability_gap():
    # On DC the workflow transition structure can't be introspected
    # (structure_checked False), so the workflow-structure checks
    # (workflow_no_transitions / unreachable_status / dead_end_status / ...)
    # silently don't run. The area is still evaluable (not skipped/errored), so
    # nothing else discloses it — a broken DC workflow would get a clean bill.
    # That must surface a capability_gap.
    snap = _snap(workflows={"count": 5, "names": ["W1", "W2"],
                            "structure_checked": False})
    gaps = [f for f in run_checks(snap)
            if f["kind"] == "capability_gap" and f["name"] == "workflows"]
    assert gaps, "unchecked workflow structure must be disclosed, not clean-billed"


def test_workflow_structure_checked_no_spurious_gap():
    # When structure IS checked (Cloud), no structure capability_gap.
    snap = _snap(workflows={"count": 2, "names": ["W1"],
                            "structure_checked": True,
                            "detail": {"W1": {"statuses": ["To Do"],
                                              "transitions": ["Go"], "edges": []}}})
    gaps = [f for f in run_checks(snap)
            if f["kind"] == "capability_gap" and f["name"] == "workflows"]
    assert not gaps


def test_near_project_limit_removed_no_false_positive():
    # Atlassian publishes no hard project-count LIMIT (project sprawl is a soft
    # performance guardrail, not a citable block), so the 8,400 threshold was a
    # fabricated number. The check is removed; it must never fire.
    for n in (7000, 9000, 50000):
        assert "near_project_limit" not in {f["kind"] for f in run_checks(
            _snap(projects={"count": n, "by_project": {}}))}


def test_guardrail_unevaluable_when_count_none_or_area_errored():
    assert "near_issue_type_limit" not in {f["kind"] for f in run_checks(
        _snap(issue_types={"count": None, "names": []}))}
    assert "near_priority_limit" not in {f["kind"] for f in run_checks(
        _snap(priorities={"error": "boom"}))}


# --- app/plugin migration risk (DC source) ---------------------------------

def test_apps_to_assess_for_cloud_dc():
    hits = [f for f in run_checks(_dc_snap(plugins={
        "user_installed_count": 23, "enabled_count": 20,
        "script_apps_present": False})) if f["kind"] == "apps_to_assess_for_cloud"]
    assert hits and hits[0]["detail"]["count"] == 23


def test_apps_not_assessed_on_cloud():
    snap = {"deployment": "cloud", "projects": ["A"], "areas": {
        "plugins": {"skipped": True, "reason": "x"}}}
    assert "apps_to_assess_for_cloud" not in {f["kind"] for f in run_checks(snap)}


def test_script_app_present_dc_high():
    hits = [f for f in run_checks(_dc_snap(plugins={
        "user_installed_count": 5, "enabled_count": 5,
        "script_apps_present": True})) if f["kind"] == "script_app_present"]
    assert hits and hits[0]["severity"] == "high"


def test_apps_unevaluable_when_count_none():
    kinds = {f["kind"] for f in run_checks(_dc_snap(plugins={
        "user_installed_count": None, "enabled_count": None,
        "script_apps_present": None}))}
    assert "apps_to_assess_for_cloud" not in kinds
    assert "script_app_present" not in kinds
