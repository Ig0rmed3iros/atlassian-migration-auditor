import pytest
from auditor.remediation.registry import fixes_for, get_fix, FIXES


def test_missing_custom_field_offers_create_wire_populate():
    finding = {"area": "custom_fields", "name": "Severity", "kind": "missing_in_tgt"}
    ids = {f.fix_id for f in fixes_for("jira", finding)}
    assert "jira.custom_field.create" in ids
    assert "jira.custom_field.wire_screen" in ids
    assert "jira.custom_field.populate" in ids


def test_tiers_and_risk_are_set():
    create = get_fix("jira.custom_field.create")
    populate = get_fix("jira.custom_field.populate")
    assert create.tier == "create" and create.risk == "low"
    assert populate.tier == "populate"


def test_holes_have_no_create_fix_only_guidance():
    finding = {"area": "", "project": "P", "kind": "missing_in_tgt",
               "src_key": "P-7"}
    # an issue-level hole maps to no Tier-1 fix
    assert fixes_for("jira", finding) == []


# C3/I4: wire_workflow is removed — detect-and-guide only
def test_wire_workflow_not_in_fixes():
    ids = {f.fix_id for f in FIXES}
    assert "jira.status.wire_workflow" not in ids


def test_fixes_for_status_does_not_return_wire_workflow():
    finding = {"area": "statuses", "name": "Triage", "kind": "missing_in_tgt"}
    ids = {f.fix_id for f in fixes_for("jira", finding)}
    assert "jira.status.wire_workflow" not in ids
    # create is still offered; wiring is guidance only
    assert "jira.status.create" in ids


# I5/I7: screen.create is removed — detect-and-guide only
def test_screen_create_not_in_fixes():
    ids = {f.fix_id for f in FIXES}
    assert "jira.screen.create" not in ids


def test_fixes_for_screen_returns_empty():
    finding = {"area": "screens", "name": "Default Screen", "kind": "missing_in_tgt"}
    assert fixes_for("jira", finding) == []


# C4: confluence.label.create is removed
def test_confluence_label_not_in_fixes():
    ids = {f.fix_id for f in FIXES}
    assert "confluence.label.create" not in ids


def test_fixes_for_confluence_label_returns_empty():
    f = {"area": "labels", "name": "x", "kind": "missing_in_tgt"}
    assert fixes_for("confluence", f) == []


# get_fix must raise for removed ids
def test_get_fix_raises_for_removed_wire_workflow():
    with pytest.raises(KeyError):
        get_fix("jira.status.wire_workflow")


def test_get_fix_raises_for_removed_screen_create():
    with pytest.raises(KeyError):
        get_fix("jira.screen.create")


def test_get_fix_raises_for_removed_confluence_label():
    with pytest.raises(KeyError):
        get_fix("confluence.label.create")
