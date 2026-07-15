from auditor.remediation.plan import build_plan, dry_run_preview


def _finding():
    return {"area": "custom_fields", "name": "Severity", "kind": "missing_in_tgt",
            "fix_payload": {"type": "select", "field_id": "customfield_1",
                            "contexts": [{"name": "Default",
                                          "options": ["High", "Low"]}],
                            "values_file": "fix/values/Severity.jsonl.gz",
                            "values_count": 3}}


def test_create_orders_before_wire_and_populate():
    plan = build_plan([_finding()],
                      ["jira.custom_field.create",
                       "jira.custom_field.wire_screen",
                       "jira.custom_field.populate"])
    tiers = [a.tier for a in plan.actions if a.object_name == "Severity"]
    assert tiers.index("create") < tiers.index("wire")
    assert tiers.index("wire") < tiers.index("populate")


def test_unselected_fix_is_absent():
    plan = build_plan([_finding()], ["jira.custom_field.create"])
    assert all(a.tier == "create" for a in plan.actions)


def test_finding_without_payload_is_skipped_with_reason():
    f = {"area": "statuses", "name": "Triage", "kind": "missing_in_tgt"}  # no payload
    plan = build_plan([f], ["jira.status.create"])
    assert plan.actions == []
    assert plan.skipped and plan.skipped[0]["reason"] == "no fix payload captured"


def test_preview_counts_objects_and_calls():
    plan = build_plan([_finding()],
                      ["jira.custom_field.create", "jira.custom_field.populate"])
    pv = dry_run_preview(plan)
    assert pv["objects"] == 1
    assert pv["issues_to_touch"] == 3      # values_count
    assert pv["calls"] >= 2
