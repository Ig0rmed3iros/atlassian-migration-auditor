from auditor.remediation.guidance import guidance_for


def test_missing_issue_guidance_lists_keys_and_jql():
    findings = [{"project": "ACME", "kind": "missing_in_tgt", "src_key": "ACME-7"},
                {"project": "ACME", "kind": "missing_in_tgt", "src_key": "ACME-9"}]
    g = guidance_for("missing_issues", findings)
    assert "ACME-7" in g["selection_query"] and "ACME-9" in g["selection_query"]
    assert "re-migrate" in g["next_step"].lower()
    assert g["count"] == 2


def test_user_gap_guidance_explains_identity_plane():
    findings = [{"kind": "user_gap", "detail": {"account_id": "a1",
                 "display_name": "Ada"}}]
    g = guidance_for("user_gap", findings)
    assert "Ada" in g["summary"] or "Ada" in str(g["missing"])
    assert "invite" in g["next_step"].lower()


def test_unknown_kind_returns_none():
    assert guidance_for("nope", []) is None


# C3/I4: workflow_wire guidance — lists the status names that need manual wiring
def test_workflow_wire_guidance_lists_status_names():
    findings = [
        {"area": "statuses", "kind": "missing_in_tgt", "name": "Triage"},
        {"area": "statuses", "kind": "missing_in_tgt", "name": "Blocked"},
    ]
    g = guidance_for("workflow_wire", findings)
    assert g is not None
    assert "Triage" in str(g["missing"]) and "Blocked" in str(g["missing"])
    assert g["count"] == 2
    assert "workflow" in g["next_step"].lower()


def test_workflow_wire_guidance_none_when_no_status_findings():
    g = guidance_for("workflow_wire", [])
    assert g is None


# I6: key_collision guidance
def test_key_collision_guidance_lists_affected_keys():
    findings = [
        {"kind": "key_collision", "src_key": "ACME-3", "tgt_key": "ACME-3",
         "project": "ACME"},
        {"kind": "key_collision", "src_key": "ACME-5", "tgt_key": "ACME-5",
         "project": "ACME"},
    ]
    g = guidance_for("key_collision", findings)
    assert g is not None
    assert g["count"] == 2
    assert "ACME-3" in str(g["missing"]) or "ACME-3" in g.get("selection_query", "")
    assert "manual" in g["next_step"].lower() or "review" in g["next_step"].lower()


def test_key_collision_guidance_none_when_empty():
    g = guidance_for("key_collision", [])
    assert g is None


# I6: workflow_structure_mismatch guidance
def test_workflow_structure_mismatch_guidance_lists_workflows():
    findings = [
        {"kind": "structure_mismatch", "name": "Software Simplified Workflow",
         "area": "workflows"},
        {"kind": "structure_mismatch", "name": "Flow", "area": "workflows"},
    ]
    g = guidance_for("workflow_structure_mismatch", findings)
    assert g is not None
    assert g["count"] == 2
    assert "Software Simplified Workflow" in str(g["missing"])
    assert "workflow" in g["next_step"].lower()


def test_workflow_structure_mismatch_guidance_none_when_empty():
    g = guidance_for("workflow_structure_mismatch", [])
    assert g is None
