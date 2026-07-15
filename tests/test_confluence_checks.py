"""Tests for auditor.envaudit.confluence_checks.run_checks_confluence.

Mirrors the Jira test_env_checks discipline: positive + negative + an
unevaluable (skipped / error / None) guard per kind, strictly no-false-positive
and no-false-clean. Privacy: snapshots carry counts / booleans / types / space
KEYS only — never page content, member lists, principal identities, or
personal-space keys.

The Confluence snapshot areas consumed (Batch-1 shape):
  spaces: {by_space:{KEY:{name,type,status,has_homepage,page_count}}, count,
           personal_count, archived_count}
  space_permissions: {by_space:{KEY:{principal_types,operations,has_admin,
           anonymous}}}  | {skipped:True}
  groups: {names, count, member_counts:{name:int}, capped}
  templates: {global_count, blueprint_count}
  labels: {global_count}
  content_quality: {pages_total, stale_pages, drafts, orphaned_pages}
"""
from __future__ import annotations

from auditor.envaudit.confluence_checks import (
    run_checks_confluence,
    LARGE_SPACE_WARN, ARCHIVED_CLUTTER_WARN, PERSONAL_SPRAWL_WARN,
    SPACE_WARN, SPACE_CRIT, STALE_RATIO_WARN, DRAFTS_WARN, LABEL_WARN,
    TEMPLATE_WARN,
)


def _snap(**areas):
    base = {"deployment": "cloud", "projects": ["DOCS"], "areas": {}}
    base["areas"].update(areas)
    return base


def _kinds(snap):
    return {f["kind"] for f in run_checks_confluence(snap)}


# ===========================================================================
# SPACES & HYGIENE
# ===========================================================================

# --- empty_space (Hygiene, low, app) ----------------------------------------

def test_empty_space_fired():
    snap = _snap(spaces={"by_space": {
        "DOCS": {"name": "Docs", "type": "global", "status": "current",
                 "has_homepage": True, "page_count": 0},
        "TEAM": {"name": "Team", "type": "global", "status": "current",
                 "has_homepage": True, "page_count": 12},
    }, "count": 2, "personal_count": 0, "archived_count": 0})
    hits = [f for f in run_checks_confluence(snap) if f["kind"] == "empty_space"]
    assert hits and hits[0]["severity"] == "low"
    assert hits[0]["name"] == "DOCS"
    assert not any(f["name"] == "TEAM" for f in hits)


def test_empty_space_not_fired_when_populated():
    snap = _snap(spaces={"by_space": {
        "DOCS": {"name": "Docs", "type": "global", "status": "current",
                 "has_homepage": True, "page_count": 5},
    }, "count": 1, "personal_count": 0, "archived_count": 0})
    assert "empty_space" not in _kinds(snap)


def test_empty_space_not_fired_when_page_count_none():
    # DC count-only / unknowable -> never fire (no false positive).
    snap = _snap(spaces={"by_space": {
        "DOCS": {"name": "Docs", "type": "global", "status": "current",
                 "has_homepage": True, "page_count": None},
    }, "count": 1, "personal_count": 0, "archived_count": 0})
    assert "empty_space" not in _kinds(snap)


def test_empty_space_not_fired_for_archived_space():
    # Only `current` spaces are evaluated for emptiness.
    snap = _snap(spaces={"by_space": {
        "OLD": {"name": "Old", "type": "global", "status": "archived",
                "has_homepage": True, "page_count": 0},
    }, "count": 1, "personal_count": 0, "archived_count": 1})
    assert "empty_space" not in _kinds(snap)


def test_empty_space_skipped_guard():
    snap = _snap(spaces={"skipped": True})
    assert "empty_space" not in _kinds(snap)


def test_empty_space_error_guard():
    snap = _snap(spaces={"error": "ERR500"})
    assert "empty_space" not in _kinds(snap)


# --- large_space (Performance, medium, human) -------------------------------

def test_large_space_fired_over_threshold():
    snap = _snap(spaces={"by_space": {
        "BIG": {"name": "Big", "type": "global", "status": "current",
                "has_homepage": True, "page_count": LARGE_SPACE_WARN + 1},
    }, "count": 1, "personal_count": 0, "archived_count": 0})
    hits = [f for f in run_checks_confluence(snap) if f["kind"] == "large_space"]
    assert hits and hits[0]["severity"] == "medium"
    assert hits[0]["name"] == "BIG"


def test_large_space_not_fired_at_threshold():
    snap = _snap(spaces={"by_space": {
        "BIG": {"name": "Big", "type": "global", "status": "current",
                "has_homepage": True, "page_count": LARGE_SPACE_WARN},
    }, "count": 1, "personal_count": 0, "archived_count": 0})
    assert "large_space" not in _kinds(snap)


def test_large_space_not_fired_when_page_count_none():
    snap = _snap(spaces={"by_space": {
        "BIG": {"name": "Big", "type": "global", "status": "current",
                "has_homepage": True, "page_count": None},
    }, "count": 1, "personal_count": 0, "archived_count": 0})
    assert "large_space" not in _kinds(snap)


# --- sampling-cap disclosure ------------------------------------------------
# gather_confluence probes only the first 250 GLOBAL spaces for per-space page
# counts and permissions, setting `capped: True` on the area. Beyond the cap the
# remaining spaces are silently unaudited, which would hand a large-instance
# cutover a false clean. The checks layer must DISCLOSE this as a capability_gap.

def test_capped_space_pagecount_discloses_capability_gap():
    snap = _snap(spaces={"by_space": {}, "count": 400, "personal_count": 0,
                         "archived_count": 0, "capped": True})
    gaps = [f for f in run_checks_confluence(snap)
            if f["kind"] == "capability_gap" and f["area"] == "spaces"]
    assert gaps, "a capped spaces probe must disclose a capability_gap"
    assert gaps[0]["severity"] == "info"


def test_capped_space_permissions_discloses_capability_gap():
    snap = _snap(space_permissions={"by_space": {}, "capped": True})
    gaps = [f for f in run_checks_confluence(snap)
            if f["kind"] == "capability_gap" and f["area"] == "space_permissions"]
    assert gaps and gaps[0]["severity"] == "info"


def test_uncapped_spaces_no_capability_gap():
    snap = _snap(spaces={"by_space": {}, "count": 10, "personal_count": 0,
                         "archived_count": 0, "capped": False})
    gaps = [f for f in run_checks_confluence(snap)
            if f["kind"] == "capability_gap" and f["area"] == "spaces"]
    assert not gaps


def test_capped_disclosed_even_when_permissions_area_partially_errored():
    # A 250+-space instance commonly hits one transient per-space probe error,
    # which last-writer-wins poisons the area `error` and makes it non-evaluable.
    # The sampling-cap disclosure must STILL fire (capped is always trustworthy:
    # it can only be True if the probe actually saw >250 spaces) — otherwise the
    # large instance silently reads as clean.
    snap = _snap(space_permissions={"by_space": {}, "capped": True,
                                    "error": "503 on one space"})
    gaps = [f for f in run_checks_confluence(snap)
            if f["kind"] == "capability_gap" and f["area"] == "space_permissions"
            and "batch" in str(f["detail"].get("note", ""))]
    assert gaps, "capped must be disclosed even when the area partially errored"


def test_capped_disclosed_even_when_spaces_area_partially_errored():
    snap = _snap(spaces={"by_space": {}, "count": 400, "personal_count": 0,
                         "archived_count": 0, "capped": True,
                         "error": "503 on one space"})
    gaps = [f for f in run_checks_confluence(snap)
            if f["kind"] == "capability_gap" and f["area"] == "spaces"
            and "batch" in str(f["detail"].get("note", ""))]
    assert gaps, "capped must be disclosed even when the area partially errored"


def test_large_space_skipped_guard():
    snap = _snap(spaces={"skipped": True})
    assert "large_space" not in _kinds(snap)


# --- space_no_homepage (Structure, medium, human) ---------------------------

def test_space_no_homepage_fired():
    snap = _snap(spaces={"by_space": {
        "NOHP": {"name": "No HP", "type": "global", "status": "current",
                 "has_homepage": False, "page_count": 3},
    }, "count": 1, "personal_count": 0, "archived_count": 0})
    hits = [f for f in run_checks_confluence(snap)
            if f["kind"] == "space_no_homepage"]
    assert hits and hits[0]["severity"] == "medium"
    assert hits[0]["name"] == "NOHP"


def test_space_no_homepage_not_fired_when_present():
    snap = _snap(spaces={"by_space": {
        "OK": {"name": "OK", "type": "global", "status": "current",
               "has_homepage": True, "page_count": 3},
    }, "count": 1, "personal_count": 0, "archived_count": 0})
    assert "space_no_homepage" not in _kinds(snap)


def test_space_no_homepage_not_fired_when_unknown():
    # has_homepage None means unknowable (DC) -> never fire.
    snap = _snap(spaces={"by_space": {
        "X": {"name": "X", "type": "global", "status": "current",
              "has_homepage": None, "page_count": 3},
    }, "count": 1, "personal_count": 0, "archived_count": 0})
    assert "space_no_homepage" not in _kinds(snap)


def test_space_no_homepage_not_fired_for_archived():
    snap = _snap(spaces={"by_space": {
        "OLD": {"name": "Old", "type": "global", "status": "archived",
                "has_homepage": False, "page_count": 3},
    }, "count": 1, "personal_count": 0, "archived_count": 1})
    assert "space_no_homepage" not in _kinds(snap)


def test_space_no_homepage_skipped_guard():
    snap = _snap(spaces={"skipped": True})
    assert "space_no_homepage" not in _kinds(snap)


# --- archived_space_clutter (Hygiene, low, human) ---------------------------

def test_archived_space_clutter_fired():
    snap = _snap(spaces={"by_space": {}, "count": ARCHIVED_CLUTTER_WARN + 5,
                         "personal_count": 0,
                         "archived_count": ARCHIVED_CLUTTER_WARN + 1})
    hits = [f for f in run_checks_confluence(snap)
            if f["kind"] == "archived_space_clutter"]
    assert hits and hits[0]["severity"] == "low"


def test_archived_space_clutter_not_fired_at_threshold():
    snap = _snap(spaces={"by_space": {}, "count": ARCHIVED_CLUTTER_WARN,
                         "personal_count": 0,
                         "archived_count": ARCHIVED_CLUTTER_WARN})
    assert "archived_space_clutter" not in _kinds(snap)


def test_archived_space_clutter_skipped_guard():
    snap = _snap(spaces={"skipped": True})
    assert "archived_space_clutter" not in _kinds(snap)


# --- personal_space_sprawl (Hygiene, low, human) ----------------------------

def test_personal_space_sprawl_fired():
    snap = _snap(spaces={"by_space": {}, "count": PERSONAL_SPRAWL_WARN + 10,
                         "personal_count": PERSONAL_SPRAWL_WARN + 1,
                         "archived_count": 0})
    hits = [f for f in run_checks_confluence(snap)
            if f["kind"] == "personal_space_sprawl"]
    assert hits and hits[0]["severity"] == "low"


def test_personal_space_sprawl_not_fired_at_threshold():
    snap = _snap(spaces={"by_space": {}, "count": PERSONAL_SPRAWL_WARN,
                         "personal_count": PERSONAL_SPRAWL_WARN,
                         "archived_count": 0})
    assert "personal_space_sprawl" not in _kinds(snap)


def test_personal_space_sprawl_no_personal_keys_in_finding():
    # Privacy: a personal-space sprawl finding must carry only counts, never a
    # personal-space key/name (which embeds a username).
    snap = _snap(spaces={"by_space": {}, "count": PERSONAL_SPRAWL_WARN + 10,
                         "personal_count": PERSONAL_SPRAWL_WARN + 5,
                         "archived_count": 0})
    hits = [f for f in run_checks_confluence(snap)
            if f["kind"] == "personal_space_sprawl"]
    assert hits and hits[0]["name"] == "spaces"  # generic area name, not a key


def test_personal_space_sprawl_skipped_guard():
    snap = _snap(spaces={"skipped": True})
    assert "personal_space_sprawl" not in _kinds(snap)


# --- space_count_near_guardrail (Performance, medium/high) -------------------

def test_space_count_near_guardrail_medium_over_warn():
    snap = _snap(spaces={"by_space": {}, "count": SPACE_WARN + 1,
                         "personal_count": 0, "archived_count": 0})
    hits = [f for f in run_checks_confluence(snap)
            if f["kind"] == "space_count_near_guardrail"]
    assert hits and hits[0]["severity"] == "medium"


def test_space_count_near_guardrail_high_over_crit():
    snap = _snap(spaces={"by_space": {}, "count": SPACE_CRIT + 1,
                         "personal_count": 0, "archived_count": 0})
    hits = [f for f in run_checks_confluence(snap)
            if f["kind"] == "space_count_near_guardrail"]
    assert hits and hits[0]["severity"] == "high"


def test_space_count_near_guardrail_not_fired_at_warn():
    snap = _snap(spaces={"by_space": {}, "count": SPACE_WARN,
                         "personal_count": 0, "archived_count": 0})
    assert "space_count_near_guardrail" not in _kinds(snap)


def test_space_count_near_guardrail_single_finding_at_crit():
    # Above CRIT must emit ONE high finding, not also a medium one.
    snap = _snap(spaces={"by_space": {}, "count": SPACE_CRIT + 1,
                         "personal_count": 0, "archived_count": 0})
    hits = [f for f in run_checks_confluence(snap)
            if f["kind"] == "space_count_near_guardrail"]
    assert len(hits) == 1


def test_space_count_near_guardrail_skipped_guard():
    snap = _snap(spaces={"skipped": True})
    assert "space_count_near_guardrail" not in _kinds(snap)


# ===========================================================================
# PERMISSIONS & SECURITY
# ===========================================================================

# --- space_no_admin (Security, high, human) ---------------------------------

def test_space_no_admin_fired():
    snap = _snap(space_permissions={"by_space": {
        "DOCS": {"principal_types": ["group"], "operations": ["read"],
                 "has_admin": False, "anonymous": False},
    }})
    hits = [f for f in run_checks_confluence(snap)
            if f["kind"] == "space_no_admin"]
    assert hits and hits[0]["severity"] == "high"
    assert hits[0]["name"] == "DOCS"


def test_space_no_admin_not_fired_when_admin_present():
    snap = _snap(space_permissions={"by_space": {
        "DOCS": {"principal_types": ["group"], "operations": ["administer"],
                 "has_admin": True, "anonymous": False},
    }})
    assert "space_no_admin" not in _kinds(snap)


def test_space_no_admin_skipped_guard_cloud():
    # Cloud anonymous/permissions skipped -> NEVER fire (no false positive).
    snap = _snap(space_permissions={"skipped": True,
                                    "reason": "Cloud anonymous unknowable"})
    assert "space_no_admin" not in _kinds(snap)


def test_space_no_admin_error_guard():
    snap = _snap(space_permissions={"error": "ERR"})
    assert "space_no_admin" not in _kinds(snap)


# --- anonymous_space_access (Security, high, human) --------------------------

def test_anonymous_space_access_fired():
    snap = _snap(space_permissions={"by_space": {
        "PUB": {"principal_types": ["anonymous"], "operations": ["read"],
                "has_admin": True, "anonymous": True},
    }})
    hits = [f for f in run_checks_confluence(snap)
            if f["kind"] == "anonymous_space_access"]
    assert hits and hits[0]["severity"] == "high"
    assert hits[0]["name"] == "PUB"


def test_anonymous_space_access_not_fired_when_private():
    snap = _snap(space_permissions={"by_space": {
        "DOCS": {"principal_types": ["group"], "operations": ["read"],
                 "has_admin": True, "anonymous": False},
    }})
    assert "anonymous_space_access" not in _kinds(snap)


def test_cloud_anonymous_unevaluable_emits_capability_gap():
    # On Cloud the v2 permissions API can't surface anonymous access, so every
    # space's `anonymous` is None. The absence of an anonymous_space_access
    # finding must NOT read as "no public access" — emit a capability_gap so the
    # reader knows the public-access check could not run (review Bug 2: this was
    # a silent false clean).
    snap = _snap(space_permissions={"by_space": {
        "ENG": {"principal_types": ["group"], "operations": ["read"],
                "has_admin": True, "anonymous": None},
        "OPS": {"principal_types": ["group"], "operations": ["read"],
                "has_admin": True, "anonymous": None}}})
    fs = run_checks_confluence(snap)
    kinds = {f["kind"] for f in fs}
    assert "anonymous_space_access" not in kinds        # correctly silent...
    assert any(f["kind"] == "capability_gap"            # ...but NOT a false clean
               and f["name"] == "space_permissions" for f in fs)


def test_dc_anonymous_false_does_not_emit_capability_gap():
    # On DC the anonymous dimension IS evaluable (concrete False) -> no gap; a
    # real False must not be mistaken for "couldn't evaluate".
    snap = _snap(space_permissions={"by_space": {
        "ENG": {"principal_types": ["group"], "operations": ["read"],
                "has_admin": True, "anonymous": False}}})
    gaps = [f for f in run_checks_confluence(snap)
            if f["kind"] == "capability_gap" and f["name"] == "space_permissions"]
    assert not gaps


def test_anonymous_space_access_skipped_guard():
    snap = _snap(space_permissions={"skipped": True})
    assert "anonymous_space_access" not in _kinds(snap)


# --- anonymous_write_grant (Security, high, human; REUSED Jira kind) --------

def test_anonymous_write_grant_fired_on_create_op():
    snap = _snap(space_permissions={"by_space": {
        "PUB": {"principal_types": ["anonymous"],
                "operations": ["read", "create"],
                "has_admin": True, "anonymous": True},
    }})
    hits = [f for f in run_checks_confluence(snap)
            if f["kind"] == "anonymous_write_grant"]
    assert hits and hits[0]["severity"] == "high"
    assert hits[0]["name"] == "PUB"


def test_anonymous_write_grant_not_fired_read_only():
    snap = _snap(space_permissions={"by_space": {
        "PUB": {"principal_types": ["anonymous"], "operations": ["read"],
                "has_admin": True, "anonymous": True},
    }})
    assert "anonymous_write_grant" not in _kinds(snap)


def test_anonymous_write_grant_not_fired_when_not_anonymous():
    # A group with create rights is normal -> never an anonymous-write finding.
    snap = _snap(space_permissions={"by_space": {
        "DOCS": {"principal_types": ["group"], "operations": ["create"],
                 "has_admin": True, "anonymous": False},
    }})
    assert "anonymous_write_grant" not in _kinds(snap)


def test_anonymous_write_grant_skipped_guard():
    snap = _snap(space_permissions={"skipped": True})
    assert "anonymous_write_grant" not in _kinds(snap)


# --- space_permission_to_anyone (Security, medium, human) -------------------

def test_space_permission_to_anyone_fired_on_broad_principal():
    snap = _snap(space_permissions={"by_space": {
        "DOCS": {"principal_types": ["all-logged-in"], "operations": ["create"],
                 "has_admin": True, "anonymous": False},
    }})
    hits = [f for f in run_checks_confluence(snap)
            if f["kind"] == "space_permission_to_anyone"]
    assert hits and hits[0]["severity"] == "medium"
    assert hits[0]["name"] == "DOCS"


def test_space_permission_to_anyone_fired_on_access_class():
    snap = _snap(space_permissions={"by_space": {
        "DOCS": {"principal_types": ["access-class"], "operations": ["read"],
                 "has_admin": True, "anonymous": False},
    }})
    assert "space_permission_to_anyone" in _kinds(snap)


def test_space_permission_to_anyone_not_fired_for_group_user():
    snap = _snap(space_permissions={"by_space": {
        "DOCS": {"principal_types": ["group", "user"], "operations": ["read"],
                 "has_admin": True, "anonymous": False},
    }})
    assert "space_permission_to_anyone" not in _kinds(snap)


def test_space_permission_to_anyone_skipped_guard():
    snap = _snap(space_permissions={"skipped": True})
    assert "space_permission_to_anyone" not in _kinds(snap)


# --- permission_grant_to_empty_group (Security, medium, human) --------------
# A space permission granted to a 0-member group is a latent escalation hole:
# the grant looks harmless today, but anyone added to that group later silently
# inherits the space access. Cross-references the per-space group GRANT names
# (DC, where the v1 list names the group) against the groups area's member
# counts. On Cloud the v2 principal exposes only a group id, not a name, so the
# join is impossible there -> DISCLOSE a capability_gap, never a silent clean.

def test_permission_grant_to_empty_group_fired():
    snap = _snap(
        space_permissions={"by_space": {
            "DOCS": {"principal_types": ["group"], "operations": ["read"],
                     "has_admin": True, "anonymous": False,
                     "group_grants": ["ghost"]}}},
        groups={"member_counts": {"team": 5, "ghost": 0}})
    hits = [f for f in run_checks_confluence(snap)
            if f["kind"] == "permission_grant_to_empty_group"]
    assert hits and hits[0]["severity"] == "medium"
    assert hits[0]["name"] == "DOCS"
    assert hits[0]["detail"]["group"] == "ghost"  # group NAME is config, OK


def test_permission_grant_to_empty_group_not_fired_when_group_populated():
    snap = _snap(
        space_permissions={"by_space": {
            "DOCS": {"principal_types": ["group"], "operations": ["read"],
                     "has_admin": True, "anonymous": False,
                     "group_grants": ["team"]}}},
        groups={"member_counts": {"team": 5}})
    assert "permission_grant_to_empty_group" not in _kinds(snap)


def _empty_group_gaps(snap):
    """capability_gaps on space_permissions that are about the empty-group
    cross-reference (reason mentions groups)."""
    return [f for f in run_checks_confluence(snap)
            if f["kind"] == "capability_gap"
            and f["name"] == "space_permissions"
            and "group" in (f["detail"].get("reason", "")
                            + f["detail"].get("note", "")).lower()]


def test_permission_grant_to_empty_group_not_fired_when_group_unknown():
    # A granted group beyond the member-count cap (or whose count probe failed)
    # is ABSENT from member_counts, never 0 -> UNKNOWN is not EMPTY: must not
    # fire. But the gap is real, so it must be DISCLOSED (the groups directory
    # was capped, so the cross-reference is incomplete) — never a silent clean.
    snap = _snap(
        space_permissions={"by_space": {
            "DOCS": {"principal_types": ["group"], "operations": ["read"],
                     "has_admin": True, "anonymous": False,
                     "group_grants": ["beyond-cap"]}}},
        groups={"member_counts": {"team": 5}, "capped": True})
    assert "permission_grant_to_empty_group" not in _kinds(snap)
    assert _empty_group_gaps(snap), "a capped group directory must disclose a gap"


def test_permission_grant_to_empty_group_discloses_gap_when_groups_errored():
    # groups area errored -> member_counts is {} -> every granted group name is
    # unresolved -> must DISCLOSE (the area_error alone does not connect the
    # groups failure to the lost empty-group coverage), never a silent clean.
    snap = _snap(
        space_permissions={"by_space": {
            "DOCS": {"principal_types": ["group"], "operations": ["read"],
                     "has_admin": True, "anonymous": False,
                     "group_grants": ["ghost"]}}},
        groups={"error": "ERR503"})
    assert "permission_grant_to_empty_group" not in _kinds(snap)
    assert _empty_group_gaps(snap)


def test_permission_grant_to_empty_group_discloses_gap_when_groups_skipped():
    # groups area skipped -> no member_counts -> unresolved -> disclose.
    snap = _snap(
        space_permissions={"by_space": {
            "DOCS": {"principal_types": ["group"], "operations": ["read"],
                     "has_admin": True, "anonymous": False,
                     "group_grants": ["ghost"]}}},
        groups={"skipped": True})
    assert "permission_grant_to_empty_group" not in _kinds(snap)
    assert _empty_group_gaps(snap)


def test_permission_grant_to_empty_group_no_gap_when_all_grants_resolved():
    # Every granted group name resolves in member_counts (and the directory is
    # not capped) -> the cross-reference is complete -> NO disclosure noise.
    snap = _snap(
        space_permissions={"by_space": {
            "DOCS": {"principal_types": ["group"], "operations": ["read"],
                     "has_admin": True, "anonymous": False,
                     "group_grants": ["team"]}}},
        groups={"member_counts": {"team": 5}, "capped": False})
    assert "permission_grant_to_empty_group" not in _kinds(snap)
    assert not _empty_group_gaps(snap)


def test_permission_grant_to_empty_group_cloud_discloses_capability_gap():
    # group_grants is None == "this space HAS group grants but the granted group
    # NAMES are unavailable" (Cloud v2 principal gives a group id, not a name).
    # The join is impossible -> disclose a capability_gap mentioning groups, and
    # never silently emit (or suppress) the empty-group finding.
    snap = _snap(
        space_permissions={"by_space": {
            "ENG": {"principal_types": ["group"], "operations": ["read"],
                    "has_admin": True, "anonymous": False,
                    "group_grants": None}}},
        groups={"member_counts": {"team": 5}})
    fs = run_checks_confluence(snap)
    assert "permission_grant_to_empty_group" not in {f["kind"] for f in fs}
    gaps = [f for f in fs
            if f["kind"] == "capability_gap"
            and f["name"] == "space_permissions"
            and "group" in (f["detail"].get("reason", "")
                            + f["detail"].get("note", "")).lower()]
    assert gaps, "a Cloud group-grant must disclose an empty-group capability_gap"


def test_permission_grant_to_empty_group_no_gap_when_no_group_grants():
    # group_grants == [] (a space with no group grants at all) -> nothing to
    # cross-reference and NO capability_gap from this rule (anonymous False so
    # the anon dimension is evaluable and emits no gap either).
    snap = _snap(
        space_permissions={"by_space": {
            "DOCS": {"principal_types": ["user"], "operations": ["read"],
                     "has_admin": True, "anonymous": False,
                     "group_grants": []}}},
        groups={"member_counts": {"team": 5}})
    fs = run_checks_confluence(snap)
    assert "permission_grant_to_empty_group" not in {f["kind"] for f in fs}
    assert not [f for f in fs if f["kind"] == "capability_gap"
                and f["name"] == "space_permissions"]


def test_permission_grant_to_empty_group_skipped_guard():
    snap = _snap(space_permissions={"skipped": True},
                 groups={"member_counts": {"ghost": 0}})
    assert "permission_grant_to_empty_group" not in _kinds(snap)


# ===========================================================================
# CONTENT & DATA QUALITY
# ===========================================================================

# --- stale_page_ratio_high (DataQuality, medium, human) ---------------------

def test_stale_page_ratio_high_fired():
    snap = _snap(content_quality={"pages_total": 100,
                                  "stale_pages": int(STALE_RATIO_WARN * 100) + 5,
                                  "drafts": 0, "orphaned_pages": 0})
    hits = [f for f in run_checks_confluence(snap)
            if f["kind"] == "stale_page_ratio_high"]
    assert hits and hits[0]["severity"] == "medium"


def test_stale_page_ratio_high_not_fired_below_ratio():
    snap = _snap(content_quality={"pages_total": 100, "stale_pages": 10,
                                  "drafts": 0, "orphaned_pages": 0})
    assert "stale_page_ratio_high" not in _kinds(snap)


def test_stale_page_ratio_high_no_divide_by_zero():
    # pages_total == 0 must NOT raise and must NOT fire.
    snap = _snap(content_quality={"pages_total": 0, "stale_pages": 0,
                                  "drafts": 0, "orphaned_pages": 0})
    assert "stale_page_ratio_high" not in _kinds(snap)


def test_stale_page_ratio_high_none_metrics_unevaluable():
    # None metrics (DC / not gathered) -> never fire, never raise.
    snap = _snap(content_quality={"pages_total": None, "stale_pages": None,
                                  "drafts": None, "orphaned_pages": None})
    assert "stale_page_ratio_high" not in _kinds(snap)


def test_stale_page_ratio_high_skipped_guard():
    snap = _snap(content_quality={"skipped": True})
    assert "stale_page_ratio_high" not in _kinds(snap)


def test_stale_page_ratio_high_error_guard():
    snap = _snap(content_quality={"error": "ERR"})
    assert "stale_page_ratio_high" not in _kinds(snap)


# --- drafts_pileup (Hygiene, low, human) ------------------------------------

def test_drafts_pileup_fired():
    snap = _snap(content_quality={"pages_total": 100, "stale_pages": 0,
                                  "drafts": DRAFTS_WARN + 1, "orphaned_pages": 0})
    hits = [f for f in run_checks_confluence(snap)
            if f["kind"] == "drafts_pileup"]
    assert hits and hits[0]["severity"] == "low"


def test_drafts_pileup_not_fired_at_threshold():
    snap = _snap(content_quality={"pages_total": 100, "stale_pages": 0,
                                  "drafts": DRAFTS_WARN, "orphaned_pages": 0})
    assert "drafts_pileup" not in _kinds(snap)


def test_drafts_pileup_none_unevaluable():
    snap = _snap(content_quality={"pages_total": 100, "stale_pages": 0,
                                  "drafts": None, "orphaned_pages": 0})
    assert "drafts_pileup" not in _kinds(snap)


def test_drafts_pileup_skipped_guard():
    snap = _snap(content_quality={"skipped": True})
    assert "drafts_pileup" not in _kinds(snap)


# ===========================================================================
# TEMPLATES / LABELS / CONFIG
# ===========================================================================

# --- label_sprawl (Hygiene, low, human) -------------------------------------

def test_label_sprawl_fired():
    snap = _snap(labels={"global_count": LABEL_WARN + 1})
    hits = [f for f in run_checks_confluence(snap) if f["kind"] == "label_sprawl"]
    assert hits and hits[0]["severity"] == "low"


def test_label_sprawl_not_fired_at_threshold():
    snap = _snap(labels={"global_count": LABEL_WARN})
    assert "label_sprawl" not in _kinds(snap)


def test_label_sprawl_skipped_guard():
    snap = _snap(labels={"skipped": True})
    assert "label_sprawl" not in _kinds(snap)


def test_label_sprawl_error_guard():
    snap = _snap(labels={"error": "ERR"})
    assert "label_sprawl" not in _kinds(snap)


# --- template_sprawl (Hygiene, low, human) ----------------------------------
# A very large global-template population fragments template discovery (the
# create-from-template picker becomes a wall of near-duplicate templates).
# Reads the already-gathered templates.global_count — no new API surface.

def test_template_sprawl_fired():
    snap = _snap(templates={"global_count": TEMPLATE_WARN + 1,
                            "blueprint_count": 0})
    hits = [f for f in run_checks_confluence(snap)
            if f["kind"] == "template_sprawl"]
    assert hits and hits[0]["severity"] == "low"
    assert hits[0]["detail"]["count"] == TEMPLATE_WARN + 1


def test_template_sprawl_not_fired_at_threshold():
    snap = _snap(templates={"global_count": TEMPLATE_WARN, "blueprint_count": 0})
    assert "template_sprawl" not in _kinds(snap)


def test_template_sprawl_not_fired_when_count_none():
    # DC / unknowable global_count -> UNEVALUABLE, never fires, never raises.
    snap = _snap(templates={"global_count": None, "blueprint_count": None})
    assert "template_sprawl" not in _kinds(snap)


def test_template_sprawl_skipped_guard():
    snap = _snap(templates={"skipped": True})
    assert "template_sprawl" not in _kinds(snap)


def test_template_sprawl_error_guard():
    snap = _snap(templates={"error": "ERR"})
    assert "template_sprawl" not in _kinds(snap)


# --- confluence_empty_group (Hygiene, low, app) -----------------------------

def test_confluence_empty_group_fired():
    snap = _snap(groups={"names": ["a", "b"],
                         "member_counts": {"team": 5, "ghost": 0},
                         "capped": False})
    hits = [f for f in run_checks_confluence(snap)
            if f["kind"] == "confluence_empty_group"]
    assert hits and hits[0]["severity"] == "low"
    assert hits[0]["name"] == "ghost"
    assert not any(f["name"] == "team" for f in hits)


def test_confluence_empty_group_distinct_from_jira_empty_group():
    # Must emit confluence_empty_group, never the Jira empty_group kind.
    snap = _snap(groups={"member_counts": {"ghost": 0}})
    kinds = _kinds(snap)
    assert "confluence_empty_group" in kinds
    assert "empty_group" not in kinds


def test_confluence_empty_group_not_fired_when_populated():
    snap = _snap(groups={"member_counts": {"team": 5}})
    assert "confluence_empty_group" not in _kinds(snap)


def test_confluence_empty_group_skipped_guard():
    snap = _snap(groups={"skipped": True})
    assert "confluence_empty_group" not in _kinds(snap)


# ===========================================================================
# COVERAGE — capability_gap for skipped / unknowable areas
# ===========================================================================

def test_skipped_area_yields_capability_gap():
    snap = _snap(space_permissions={"skipped": True,
                                    "reason": "Cloud anonymous unknowable"})
    fs = run_checks_confluence(snap)
    assert any(f["kind"] == "capability_gap"
               and f["name"] == "space_permissions" for f in fs)


def test_partial_probe_failure_still_evaluates_read_spaces_and_discloses():
    # A space_permissions area that read SOME spaces but failed on others
    # (probe_error set, error None) must STILL evaluate the spaces it read —
    # never drop a real anonymous_write_grant — AND disclose the partial failure
    # as a capability_gap so the unread spaces are not implied clean.
    snap = _snap(space_permissions={
        "by_space": {"PUB": {"principal_types": ["anonymous"],
                             "operations": ["read", "create"],
                             "has_admin": True, "anonymous": True}},
        "error": None, "probe_error": "ERR503:boom"})
    fs = run_checks_confluence(snap)
    kinds = {f["kind"] for f in fs}
    assert "anonymous_write_grant" in kinds, "read spaces must still be evaluated"
    assert any(f["kind"] == "capability_gap" and f["name"] == "space_permissions"
               and "probe" in (f["detail"].get("note", "")).lower()
               for f in fs), "the partial probe failure must be disclosed"


def test_errored_area_yields_area_error():
    snap = _snap(spaces={"error": "ERR500:boom"})
    fs = run_checks_confluence(snap)
    assert any(f["kind"] == "area_error" and f["name"] == "spaces" for f in fs)


def test_content_quality_all_none_emits_capability_gap():
    # All content metrics None (unknowable) -> coverage gap so the reader knows
    # content checks could not run, and NO data-quality finding fires.
    snap = _snap(content_quality={"pages_total": None, "stale_pages": None,
                                  "drafts": None, "orphaned_pages": None})
    fs = run_checks_confluence(snap)
    kinds = {f["kind"] for f in fs}
    assert "capability_gap" in kinds
    assert "stale_page_ratio_high" not in kinds
    assert "drafts_pileup" not in kinds


# ===========================================================================
# MIGRATION: page hierarchy + render visibility
# ===========================================================================

# --- orphaned_pages (Structure) — pages outside the homepage subtree ---------

def test_orphaned_pages_fired_over_threshold():
    snap = _snap(spaces={"by_space": {
        "DOCS": {"name": "Docs", "type": "global", "status": "current",
                 "has_homepage": True, "page_count": 200, "orphan_pages": 40},
        "TEAM": {"name": "Team", "type": "global", "status": "current",
                 "has_homepage": True, "page_count": 200, "orphan_pages": 2},
    }, "count": 2, "personal_count": 0, "archived_count": 0})
    hits = [f for f in run_checks_confluence(snap)
            if f["kind"] == "orphaned_pages"]
    assert hits and hits[0]["name"] == "DOCS"
    assert hits[0]["detail"]["orphans"] == 40
    assert not any(f["name"] == "TEAM" for f in hits)   # 2 is under threshold


def test_orphaned_pages_unevaluable_when_none():
    snap = _snap(spaces={"by_space": {
        "DOCS": {"name": "Docs", "type": "global", "status": "current",
                 "has_homepage": True, "page_count": 200, "orphan_pages": None},
    }, "count": 1, "personal_count": 0, "archived_count": 0})
    assert "orphaned_pages" not in _kinds(snap)


def test_orphaned_pages_not_evaluated_on_archived_space():
    snap = _snap(spaces={"by_space": {
        "OLD": {"name": "Old", "type": "global", "status": "archived",
                "has_homepage": True, "page_count": 200, "orphan_pages": 99},
    }, "count": 1, "personal_count": 0, "archived_count": 1})
    assert "orphaned_pages" not in _kinds(snap)


# --- unsupported_macro_usage (DataQuality) — broken/blank render -------------

def test_unsupported_macro_usage_fired():
    snap = _snap(macros={"by_macro": {"gliffy": 12, "chart": 3, "drawio": 0},
                         "error": None})
    hits = [f for f in run_checks_confluence(snap)
            if f["kind"] == "unsupported_macro_usage"]
    names = {f["name"] for f in hits}
    assert "gliffy" in names and "chart" in names
    assert "drawio" not in names                       # zero usage → no finding
    g = next(f for f in hits if f["name"] == "gliffy")
    assert g["severity"] == "high"                     # third-party app macro
    assert g["detail"]["pages"] == 12
    c = next(f for f in hits if f["name"] == "chart")
    assert c["severity"] == "medium"                   # removed built-in


def test_unsupported_macro_usage_clean_when_zero():
    snap = _snap(macros={"by_macro": {"gliffy": 0, "chart": 0}, "error": None})
    assert "unsupported_macro_usage" not in _kinds(snap)


def test_unsupported_macro_usage_unevaluable_when_errored():
    snap = _snap(macros={"by_macro": {}, "error": "macro CQL surface unavailable"})
    kinds = _kinds(snap)
    assert "unsupported_macro_usage" not in kinds       # no false clean
    assert "area_error" in kinds                        # loud coverage signal


# --- cross_space_include_risk — content that silently goes blank ------------

def test_cross_space_include_risk_fired_for_include_macros():
    """Include / Excerpt-Include macros are SUPPORTED on Cloud but break when
    the referenced page migrates in a different batch — the consumer page then
    renders blank (invisible content). Distinct from unsupported_macro_usage:
    the macro is not unsupported, the cross-space REFERENCE is the risk."""
    snap = _snap(macros={"by_macro": {"include": 9, "excerpt-include": 4,
                                      "gliffy": 2}, "error": None})
    findings = run_checks_confluence(snap)
    inc = [f for f in findings if f["kind"] == "cross_space_include_risk"]
    names = {f["name"] for f in inc}
    assert names == {"include", "excerpt-include"}
    assert all(f["severity"] == "medium" for f in inc)
    assert next(f for f in inc if f["name"] == "include")["detail"]["pages"] == 9
    # an include macro must NOT also be reported as unsupported (wrong framing)
    unsupported = {f["name"] for f in findings
                   if f["kind"] == "unsupported_macro_usage"}
    assert "include" not in unsupported and "gliffy" in unsupported


def test_cross_space_include_risk_clean_when_zero():
    snap = _snap(macros={"by_macro": {"include": 0, "excerpt-include": 0},
                         "error": None})
    assert "cross_space_include_risk" not in _kinds(snap)


# ===========================================================================
# WHOLE-SNAPSHOT sanity
# ===========================================================================

def test_empty_snapshot_no_findings_no_raise():
    assert run_checks_confluence({}) == []
    assert run_checks_confluence({"areas": {}}) == []


def test_findings_have_shared_shape():
    snap = _snap(spaces={"by_space": {
        "DOCS": {"name": "Docs", "type": "global", "status": "current",
                 "has_homepage": False, "page_count": 0},
    }, "count": 1, "personal_count": 0, "archived_count": 0})
    for f in run_checks_confluence(snap):
        assert set(f) == {"area", "name", "kind", "severity", "detail"}
        assert isinstance(f["detail"], dict)


# --- page restrictions (migration access risk) ------------------------------

def _restr_area(by_space, capped=False):
    a = {"by_space": by_space}
    if capped:
        a["capped"] = True
    return a


def test_restricted_pages_fires_when_evaluable_and_restricted():
    snap = _snap(page_restrictions=_restr_area({
        "ENG": {"restricted": 4, "probed": 100, "evaluable": True,
                "page_capped": False}}))
    hits = [f for f in run_checks_confluence(snap)
            if f["kind"] == "restricted_pages"]
    assert hits and hits[0]["severity"] == "medium"
    assert hits[0]["name"] == "ENG" and hits[0]["detail"]["restricted"] == 4


def test_no_restricted_pages_finding_when_clean():
    snap = _snap(page_restrictions=_restr_area({
        "ENG": {"restricted": 0, "probed": 30, "evaluable": True,
                "page_capped": False}}))
    assert "restricted_pages" not in _kinds(snap)


def test_restrictions_unreadable_discloses_capability_gap():
    # API returned no restriction data on any probed space -> NOT evaluable ->
    # disclose, never read as "no restricted pages".
    snap = _snap(page_restrictions=_restr_area({
        "ENG": {"restricted": 0, "probed": 5, "evaluable": False,
                "page_capped": False}}))
    gaps = [f for f in run_checks_confluence(snap)
            if f["kind"] == "capability_gap"
            and f["area"] == "page_restrictions"]
    assert gaps and "could not be read" in gaps[0]["detail"]["note"]
    assert "restricted_pages" not in _kinds(snap)


def test_restrictions_sampled_discloses_capability_gap():
    snap = _snap(page_restrictions=_restr_area({
        "ENG": {"restricted": 1, "probed": 100, "evaluable": True,
                "page_capped": True}}))
    gaps = [f for f in run_checks_confluence(snap)
            if f["kind"] == "capability_gap"
            and f["area"] == "page_restrictions"
            and "sampled" in f["detail"].get("note", "")]
    assert gaps


def test_restrictions_space_cap_discloses_capability_gap():
    snap = _snap(page_restrictions=_restr_area({}, capped=True))
    gaps = [f for f in run_checks_confluence(snap)
            if f["kind"] == "capability_gap"
            and f["area"] == "page_restrictions"
            and "first batch" in f["detail"].get("note", "")]
    assert gaps
