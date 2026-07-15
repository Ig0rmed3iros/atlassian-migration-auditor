"""Deterministic health-check rules over a gathered config snapshot (spec R4).

Each rule reads only snapshot metadata. A rule whose source area is skipped or
errored emits the appropriate coverage signal (never a false clean):
  - skipped  → capability_gap / info    (expected DC skip, no API available)
  - error    → area_error  / warning    (unexpected fetch failure; loud signal)
"""
from __future__ import annotations
import re
from ..config_audit import _norm_name

# Atlassian instance-health guidance: high counts of custom fields, workflows,
# statuses, and screens degrade indexing and JQL query performance.
FIELD_WARN = 300
FIELD_CRIT = 800
OPT_WARN = 100
OPT_CRIT = 500
WORKFLOW_WARN = 100
STATUS_WARN = 100
SCREEN_WARN = 300
PERMSCHEME_WARN = 50

# Global config-object sprawl. Atlassian instance-health guidance recommends a
# lean set of shared resolutions, priorities, issue types, and link types;
# per-project copies (a common migration artefact) bloat issue forms and JQL
# autocomplete. The default Jira install ships ~5 resolutions, ~5 priorities,
# ~5-10 issue types, and ~4 link types, so these warn levels sit well above
# anything a healthy instance needs.
RESOLUTION_WARN = 30
PRIORITY_WARN = 15
ISSUETYPE_WARN = 40
LINKTYPE_WARN = 30

# Single-workflow complexity. Atlassian recommends keeping individual workflows
# small and readable; oversized workflows (too many statuses or transitions)
# are hard to maintain and slow the workflow editor and transition evaluation.
WF_STATUS_WARN = 20
WF_TRANSITION_WARN = 40
WF_STATUS_INFO = 12

# Atlassian distinguishes LIMITS (hard — cannot be exceeded) from GUARDRAILS
# (recommended thresholds; performance may degrade past them). Two hard limits
# are scoped PER company-managed PROJECT, NOT site-wide:
#   - 700 custom fields per project       (FIELD_LIMIT_CRIT)
#   - 150 issue (work) types per project  (ISSUE_TYPE_LIMIT)
# The env audit gathers SITE-WIDE counts, which cannot confirm a per-project
# breach. But a project's fields / issue types are a SUBSET of the site's, so a
# site-wide count <= the per-project limit PROVES no project can breach it. We
# therefore stay silent at/below the limit and, only above it, DISCLOSE that a
# per-project breach is possible (medium) — never assert a HIGH block we cannot
# confirm. (See the near_field_limit / near_issue_type_limit blocks below.)
FIELD_LIMIT_CRIT = 700
ISSUE_TYPE_LIMIT = 150

# Site-wide performance GUARDRAILS (recommended maxima, not hard blocks).
# priorities and workflows ARE global objects, so the site-wide count is the
# correct scope; we warn at ~80% and escalate to high at the guardrail. No
# project-count entry: Atlassian publishes no citable hard project limit, so a
# fixed threshold would be a fabricated number — omitted on purpose.
# (area, kind, warn, limit, label) — count read from areas[area]["count"].
_GUARDRAILS = [
    ("priorities", "near_priority_limit", 80, 100, "priorities"),
    ("workflows", "near_workflow_limit", 120, 150, "workflows"),
]

# Default-install catalogue sizes. A resolution or priority outside the
# canonical default set, or a priority list larger than the default, is a
# review candidate (migration artefact). Canonical sets are matched
# case/whitespace-insensitively via _norm_name.
_CANONICAL_RESOLUTIONS = {
    _norm_name(n) for n in (
        "Done", "Won't Do", "Duplicate", "Cannot Reproduce", "Won't Fix")}
PRIORITY_DEFAULT_COUNT = 5

# A project with this many overdue, unreleased versions signals an abandoned
# release calendar (distinct from the single-version version_overdue rule).
OVERDUE_VERSIONS_WARN = 5

# version_naming_inconsistent fires only on a SUBSTANTIAL semver/free-text mix
# (this many of EACH) — a couple of named versions among semver releases is
# normal, not inconsistent.
_VERSION_MIX_MIN = 3

# Admin-group bloat. A group whose name matches an admin pattern AND whose
# member count exceeds this threshold widens the blast radius of an admin
# compromise. Counts + name pattern only — never member identities.
ADMIN_GROUP_MEMBER_WARN = 20

# Board explosion: more than this multiple of the project count usually means
# ungoverned per-team duplicate boards.
BOARD_PER_PROJECT_RATIO = 3
# ...and an absolute floor, so a handful of boards on a small instance (e.g. 4
# boards / 1 project) is not flagged as a "board explosion".
_BOARD_MIN_ABS = 10

# Workflow global-transition anti-pattern. Over-broad global transitions (that
# apply from ANY status) defeat process control and confuse reporting; more
# than this many in one workflow is an anti-pattern worth flagging.
GLOBAL_TRANSITION_WARN = 3

# Section 3 — ISSUE-LEVEL / DATA QUALITY thresholds. These gate count-only
# data-quality metrics gathered via approx-count JQL (invariant I1: integers
# only — no issue content/keys/identities). A handful of stale or unassigned
# open issues is normal; a large absolute count, or a large fraction of all
# unresolved issues, signals neglected hygiene worth an admin's attention.
STALE_ISSUE_WARN = 50          # absolute stale-open count warn level
UNASSIGNED_WARN = 100          # absolute unassigned-unresolved count warn level

# --- DC->Cloud migration (JCMA) checks, gated to deployment == "dc" ---
# Group names Cloud reserves; a DC group sharing one of these is MERGED into the
# Cloud group on migration -> silent permission escalation / unexpected paid
# access. A mandatory JCMA pre-migration check.
_RESERVED_CLOUD_GROUPS = {
    "site-admins", "administrators", "jira-administrators",
    "jira-software-users", "jira-servicedesk-users", "jira-core-users",
    "atlassian-addons-admin", "system-administrators",
}
# (custom-field type classification now lives in gather._cf_type_key /
# app_provided_count — the check reads that verdict; review Bug 1.)
# Fraction-of-total triggers: fire even below the absolute warn when the metric
# is a high share of all unresolved issues (a small backlog that is mostly
# stale/unassigned is still unhealthy).
STALE_ISSUE_FRACTION = 0.5
UNASSIGNED_FRACTION = 0.5

# Status names that legitimately have no outbound transition (terminal states).
# A status whose name matches one of these patterns is NOT a dead-end finding;
# this keeps dead_end_status conservative (no false positives on real Done/
# Closed/Resolved/Cancelled statuses).
_TERMINAL_STATUS_RE = re.compile(
    r'\b(done|closed|resolved|cancel(?:l?ed)?|rejected|complete[d]?|'
    r'won\'?t\s*(?:do|fix)|abandoned|archived)\b', re.IGNORECASE)

_SENSITIVE_PERMS = {"ADMINISTER", "ADMINISTER_PROJECTS"}

# Anonymous write exposure: these permissions let an unauthenticated user
# create or mutate issue content, a higher-severity exposure than read-only
# browse.
_ANON_WRITE_PERMS = {"CREATE_ISSUES", "ADD_COMMENTS", "EDIT_ISSUES"}

# Holder types that represent "any logged-in user" (an application-access role
# or the built-in logged-in-user holder). Admin granted to these is admin-group
# bloat by another name.
_LOGGED_IN_HOLDER_TYPES = {"applicationRole", "loggedInUser"}

# Admin-group name patterns (lower-cased substring match).
_ADMIN_GROUP_PATTERNS = ("admin", "site-admins", "jira-administrators")

_SEMVER_RE = re.compile(r'^\d+\.\d+')

_MIGRATION_SUFFIX_RE = re.compile(
    r'\s*(\(migrated(?:\s+\d+)?\)|\s*-\s*copy)\s*$', re.IGNORECASE)


def _strip_migration_suffix(name):
    return _MIGRATION_SUFFIX_RE.sub('', name).strip()


def _has_migration_suffix(name):
    return bool(_MIGRATION_SUFFIX_RE.search(name))


def _duplicate_names(names):
    """Yield (name, first_seen) for each name that normalises to a value already
    seen earlier in the list. Case/whitespace-insensitive via _norm_name, but
    the verbatim names are returned so the report names the real objects."""
    seen = {}
    for nm in names:
        k = _norm_name(nm)
        if k in seen:
            yield nm, seen[k]
        else:
            seen[k] = nm


def _area(snap, name):
    return (snap.get("areas") or {}).get(name) or {}


def _evaluable(a):
    return bool(a) and not a.get("skipped") and not a.get("error")


def _f(area, name, kind, severity, **detail):
    return {"area": area, "name": name, "kind": kind, "severity": severity,
            "detail": detail}


def run_checks(snap: dict) -> list[dict]:
    out: list[dict] = []
    areas = snap.get("areas") or {}

    # Coverage signals for every area that cannot be fully evaluated.
    # Skipped areas (expected DC API absence) → capability_gap / info.
    # Errored areas (unexpected fetch failure) → area_error / warning so that
    # a live network failure on a Cloud environment is visibly distinct from an
    # expected DC skip in the findings output.
    for name, a in areas.items():
        if not isinstance(a, dict):
            continue
        if a.get("skipped"):
            out.append(_f(name, name, "capability_gap", "info",
                          reason=a.get("reason") or "skipped"))
        elif a.get("error"):
            out.append(_f(name, name, "area_error", "warning",
                          error=a.get("error")))

    cf = _area(snap, "custom_fields")
    if _evaluable(cf):
        # duplicate_field: detect collisions over the RAW names list (so EXACT
        # duplicate names — two custom fields with the identical name — are
        # reported too, not silently collapsed). Names are kept verbatim; the
        # collision key is the normalised name so near-duplicates (casing /
        # whitespace) are also caught.
        #
        # Overlap suppression (P3): a migration-suffix twin whose base name
        # collides is reported by the migration_artifact rule below — the more
        # specific, actionable finding (unfixable tier). Do NOT also emit a
        # duplicate_field for that same name pair.
        cf_names = cf.get("names", [])
        cf_base_set = {_norm_name(n) for n in cf_names
                       if not _has_migration_suffix(n)}
        seen = {}
        for nm in cf_names:
            # Skip names that the migration_artifact rule will claim: a
            # migration-suffix name whose stripped base collides with a
            # non-suffix custom field. (migration_artifact wins the pair.)
            if _has_migration_suffix(nm) and \
                    _norm_name(_strip_migration_suffix(nm)) in cf_base_set:
                continue
            k = _norm_name(nm)
            if k in seen:
                out.append(_f("custom_fields", nm, "duplicate_field", "medium",
                              collides_with=seen[k]))
            else:
                seen[k] = nm

        scr = _area(snap, "screens")
        if _evaluable(scr) and scr.get("fields"):
            on_screen = {x for fields in scr["fields"].values() for x in fields}
            for nm in cf.get("names", []):
                if nm not in on_screen:
                    out.append(_f("custom_fields", nm, "unused_custom_field",
                                  "low", note="on no screen"))

        # field_sprawl
        count = cf.get("count", 0)
        if count > FIELD_CRIT:
            out.append(_f("custom_fields", "custom_fields", "field_sprawl", "high",
                          count=count, threshold=FIELD_CRIT))
        elif count > FIELD_WARN:
            out.append(_f("custom_fields", "custom_fields", "field_sprawl", "medium",
                          count=count, threshold=FIELD_WARN))

        # near_field_limit: 700 fields is the hard limit PER company-managed
        # project, not site-wide. A project's fields are a subset of the site's,
        # so a site-wide count <= 700 proves no project can breach it. Only a
        # count > 700 makes a per-project breach POSSIBLE, and it is
        # unconfirmable from the site-wide total -> disclose (medium), never a
        # false HIGH. (field_sprawl above carries the site-wide hygiene signal.)
        if count > FIELD_LIMIT_CRIT:
            out.append(_f(
                "custom_fields", "custom_fields", "near_field_limit", "medium",
                count=count, limit=FIELD_LIMIT_CRIT,
                note=("700 fields is the hard limit per company-managed "
                      "project; this site-wide count only makes a per-project "
                      "breach possible — verify per project.")))

    scr = _area(snap, "screens")
    if _evaluable(scr) and scr.get("fields"):
        for nm, fields in scr["fields"].items():
            if not fields:
                out.append(_f("screens", nm, "empty_screen", "low"))

    # screen_sprawl
    if _evaluable(scr):
        sc = scr.get("count", 0)
        if sc > SCREEN_WARN:
            out.append(_f("screens", "screens", "screen_sprawl", "low",
                          count=sc, threshold=SCREEN_WARN))

    wf = _area(snap, "workflows")
    # On DC the workflow transition structure can't be introspected
    # (structure_checked False); the structure checks below silently skip. The
    # area is still evaluable (not skipped/errored), so the absence of a
    # workflow-structure finding must NOT read as a clean bill — disclose the
    # lost coverage (review: a broken DC workflow was a silent false clean).
    if _evaluable(wf) and not wf.get("structure_checked"):
        out.append(_f("workflows", "workflows", "capability_gap", "info",
                      reason="workflow transition structure could not be "
                             "introspected on this deployment; transition and "
                             "reachability checks were not run"))
    if _evaluable(wf) and wf.get("structure_checked") and wf.get("detail"):
        for nm, d in wf["detail"].items():
            if d.get("statuses") and not d.get("transitions"):
                out.append(_f("workflows", nm, "workflow_no_transitions",
                              "high", statuses=len(d["statuses"])))

    # workflow_sprawl
    if _evaluable(wf):
        count = len(wf.get("names", []))
        if count > WORKFLOW_WARN:
            out.append(_f("workflows", "workflows", "workflow_sprawl", "medium",
                          count=count, threshold=WORKFLOW_WARN))

    st = _area(snap, "statuses")
    if _evaluable(st) and _evaluable(wf) and wf.get("structure_checked") and wf.get("detail"):
        # Status name matching is case-sensitive: gather collects names verbatim
        # from /status, and workflow detail names come from /workflow/search with
        # the same casing. If both endpoints are consistent (they are in practice)
        # this is safe. A case mismatch would produce a false orphan finding.
        in_workflows = {s for d in wf["detail"].values() for s in (d.get("statuses") or [])}
        for nm in st.get("names", []):
            if nm not in in_workflows:
                out.append(_f("statuses", nm, "status_not_in_workflow", "medium"))

    # status_sprawl
    if _evaluable(st):
        count = st.get("count", 0)
        if count > STATUS_WARN:
            out.append(_f("statuses", "statuses", "status_sprawl", "low",
                          count=count, threshold=STATUS_WARN))

    # duplicate_status_name: two statuses whose names normalise to the same
    # value (case/whitespace-insensitive). Duplicate status names break board
    # mapping and JQL `status =` queries and are a frequent migration artefact.
    if _evaluable(st):
        for nm, first in _duplicate_names(st.get("names", [])):
            out.append(_f("statuses", nm, "duplicate_status_name", "medium",
                          collides_with=first))

    # NOTE: an "unused_status" rule was considered but dropped — in v1 (no
    # scheme→workflow→status mapping) it reduces to exactly the same condition
    # as status_not_in_workflow above, so it only double-reported the same
    # status. status_not_in_workflow is the single source of truth.
    wfs = _area(snap, "workflow_schemes")

    for scheme_area in ("workflow_schemes", "screen_schemes", "field_config_schemes"):
        sa = _area(snap, scheme_area)
        if _evaluable(sa) and sa.get("projects_using") is not None:
            for nm in sa.get("names", []):
                if not sa["projects_using"].get(nm):
                    out.append(_f(scheme_area, nm, "scheme_unused", "low",
                                  scheme_type=scheme_area))

    # NOTE: a "project_missing_scheme" rule once lived here. It was REMOVED
    # because it could not be made correct on the aggregate projects_using
    # shape: it compared snap["projects"] (project KEYS, e.g. "AC") against the
    # union of workflow_schemes.projects_using values, which gather populates
    # with project IDs (str(pid) from projectIds). Keys never equal IDs, so it
    # false-fired for EVERY project. Worse, even with correct id/key matching it
    # cannot see the system DEFAULT workflow scheme (projects on the default are
    # in no named scheme's projectIds), so it would still false-positive on every
    # default-scheme project. The aggregate projects_using approach cannot
    # support this check, so it is gone. (scheme_unused above is unaffected — it
    # only tests empty-vs-nonempty project lists, never id/key equality.)

    # large_option_set
    cfo = _area(snap, "custom_field_options")
    if _evaluable(cfo):
        for field_nm, data in cfo.get("by_field", {}).items():
            opts = data.get("options", 0)
            if opts > OPT_CRIT:
                out.append(_f("custom_field_options", field_nm, "large_option_set", "medium",
                              options=opts, threshold=OPT_CRIT))
            elif opts > OPT_WARN:
                out.append(_f("custom_field_options", field_nm, "large_option_set", "low",
                              options=opts, threshold=OPT_WARN))

    # permission_scheme_sprawl
    ps = _area(snap, "permission_schemes")
    if _evaluable(ps):
        count = ps.get("count", 0)
        if count > PERMSCHEME_WARN:
            out.append(_f("permission_schemes", "permission_schemes",
                          "permission_scheme_sprawl", "low",
                          count=count, threshold=PERMSCHEME_WARN))

    # Global config-object sprawl: resolutions / priorities / issue types /
    # link types. Each is a simple count-over-threshold rule (low severity); a
    # bloated global catalogue is usually a per-project migration artefact.
    for area_name, kind, threshold in (
        ("resolutions", "resolution_sprawl", RESOLUTION_WARN),
        ("priorities", "priority_sprawl", PRIORITY_WARN),
        ("issue_types", "issue_type_sprawl", ISSUETYPE_WARN),
        ("link_types", "link_type_sprawl", LINKTYPE_WARN),
    ):
        a = _area(snap, area_name)
        if _evaluable(a):
            count = a.get("count")
            # None (unevaluable) -> skip; never compare None > int (a crash).
            if isinstance(count, int) and count > threshold:
                out.append(_f(area_name, area_name, kind, "low",
                              count=count, threshold=threshold))

    # duplicate_issue_type_name: two issue types whose names collide. Confuses
    # issue-type schemes and reporting (mirror of duplicate_status_name).
    it = _area(snap, "issue_types")
    if _evaluable(it):
        for nm, first in _duplicate_names(it.get("names", [])):
            out.append(_f("issue_types", nm, "duplicate_issue_type_name",
                          "medium", collides_with=first))

    # unused_resolution: a resolution name outside the canonical default set.
    # Surplus resolutions are a top migration artefact; this names the
    # individual values an admin must inspect (resolution_sprawl only counts).
    res = _area(snap, "resolutions")
    if _evaluable(res):
        for nm in res.get("names", []):
            if _norm_name(nm) not in _CANONICAL_RESOLUTIONS:
                out.append(_f("resolutions", nm, "unused_resolution", "low",
                              note="not in the canonical default resolution set"))

    # redundant_priority_set: the priority list exceeds the default 5 AND
    # contains a normalised-collision near-duplicate. Names the suspect values
    # behind the count-only priority_sprawl rule.
    pri = _area(snap, "priorities")
    if _evaluable(pri):
        pri_names = pri.get("names", [])
        pri_count = pri.get("count", len(pri_names))
        if pri_count > PRIORITY_DEFAULT_COUNT:
            for nm, first in _duplicate_names(pri_names):
                out.append(_f("priorities", nm, "redundant_priority_set", "low",
                              collides_with=first, count=pri_count))

    # large_workflow: per-workflow complexity. Requires structural detail.
    if _evaluable(wf) and wf.get("structure_checked") and wf.get("detail"):
        for nm, d in wf["detail"].items():
            n_status = len(d.get("statuses") or [])
            n_trans = len(d.get("transitions") or [])
            if n_status > WF_STATUS_WARN or n_trans > WF_TRANSITION_WARN:
                out.append(_f("workflows", nm, "large_workflow", "medium",
                              statuses=n_status, transitions=n_trans,
                              status_threshold=WF_STATUS_WARN,
                              transition_threshold=WF_TRANSITION_WARN))
            elif n_status > WF_STATUS_INFO:
                out.append(_f("workflows", nm, "large_workflow", "low",
                              statuses=n_status, transitions=n_trans,
                              status_threshold=WF_STATUS_INFO))

    # ---- Workflow-structure graph checks ------------------------------------
    # STRICTLY no-false-positive: these fire ONLY when the transition graph is
    # fully known (structure_checked True AND per-workflow `edges` present). On
    # DC (structure_checked False) or an older snapshot without edges, the
    # workflow is unevaluable and nothing is emitted.
    if _evaluable(wf) and wf.get("structure_checked") and wf.get("detail"):
        for nm, d in wf["detail"].items():
            edges = d.get("edges")
            if not isinstance(edges, list):
                continue  # no graph for this workflow -> unevaluable
            statuses = [s for s in (d.get("statuses") or []) if s]
            # Sets derived purely from the edge graph.
            reached = {e.get("to") for e in edges if isinstance(e, dict)}
            # A status has an outbound transition if it is a `from` of some
            # directed edge, OR any global edge exists (global = outbound from
            # every status).
            has_global = any(isinstance(e, dict) and e.get("global")
                             for e in edges)
            outbound = set()
            for e in edges:
                if not isinstance(e, dict):
                    continue
                for src in (e.get("from") or []):
                    outbound.add(src)
            # Initial/create status: gathered explicitly, else the first listed.
            initial = d.get("initial_status")
            if initial is None and statuses:
                initial = statuses[0]

            # unreachable_status: a status that is never the `to` of any
            # transition and is not the initial/create status. A global
            # transition into a status makes it reachable (its `to` is in
            # `reached`). Medium severity.
            for s in statuses:
                if s == initial:
                    continue
                if s not in reached:
                    out.append(_f("workflows", f"{nm} / {s}",
                                  "unreachable_status", "medium",
                                  workflow=nm, status=s))

            # dead_end_status: a NON-terminal status with no outbound
            # transition. Conservative: a status whose name matches a
            # done/closed/resolved/cancelled pattern is a legitimate terminal
            # state and is NEVER flagged. A global transition gives every status
            # an outbound path. Low severity.
            if not has_global:
                for s in statuses:
                    if s in outbound:
                        continue
                    if _TERMINAL_STATUS_RE.search(s or ""):
                        continue  # legitimate terminal status
                    out.append(_f("workflows", f"{nm} / {s}",
                                  "dead_end_status", "low",
                                  workflow=nm, status=s))

            # global_transition_overuse: more than GLOBAL_TRANSITION_WARN global
            # transitions in one workflow is an anti-pattern. Low severity.
            global_n = sum(1 for e in edges
                           if isinstance(e, dict) and e.get("global"))
            if global_n > GLOBAL_TRANSITION_WARN:
                out.append(_f("workflows", nm, "global_transition_overuse",
                              "low", globals=global_n,
                              threshold=GLOBAL_TRANSITION_WARN))

    # workflow_unreferenced: a workflow whose name is in workflows.names but not
    # in workflow_schemes.workflows_used. Fires ONLY when workflows_used is
    # present (Cloud) — an absent set means unevaluable, never flag every
    # workflow. workflows area must itself be evaluable.
    if _evaluable(wf) and _evaluable(wfs) and wfs.get("workflows_used") is not None:
        used = set(wfs["workflows_used"])
        for nm in wf.get("names", []):
            if nm not in used:
                out.append(_f("workflows", nm, "workflow_unreferenced", "low",
                              note="in no workflow scheme"))

    # screen_not_in_scheme: a screen in screens.names not referenced by any
    # screen scheme. Fires ONLY when screen_schemes.screens_used is present.
    sss = _area(snap, "screen_schemes")
    if _evaluable(scr) and _evaluable(sss) and sss.get("screens_used") is not None:
        used_screens = set(sss["screens_used"])
        for nm in scr.get("names", []):
            if nm not in used_screens:
                out.append(_f("screens", nm, "screen_not_in_scheme", "low",
                              note="in no screen scheme"))

    # unused_issue_type_scheme / unused_issue_type_screen_scheme
    for scheme_area, kind in (("issuetype_schemes", "unused_issue_type_scheme"),
                               ("issuetype_screen_schemes", "unused_issue_type_screen_scheme")):
        sa = _area(snap, scheme_area)
        if _evaluable(sa) and sa.get("projects_using") is not None:
            for nm in sa.get("names", []):
                if not sa["projects_using"].get(nm):
                    out.append(_f(scheme_area, nm, kind, "low",
                                  scheme_type=scheme_area))

    # empty_group / large_group_admin_bloat
    grp = _area(snap, "groups")
    if _evaluable(grp):
        for gname, cnt in grp.get("member_counts", {}).items():
            if cnt == 0:
                out.append(_f("groups", gname, "empty_group", "low"))
            # Admin-group bloat: an admin-pattern group with an oversized
            # membership widens the blast radius. Counts + name pattern only.
            low_name = (gname or "").lower()
            if cnt > ADMIN_GROUP_MEMBER_WARN and \
                    any(p in low_name for p in _ADMIN_GROUP_PATTERNS):
                out.append(_f("groups", gname, "large_group_admin_bloat", "low",
                              members=cnt, threshold=ADMIN_GROUP_MEMBER_WARN))

    # version_overdue / version_archived_unreleased
    ver = _area(snap, "versions")
    if _evaluable(ver):
        for proj_key, vlist in ver.get("by_project", {}).items():
            for v in vlist:
                vname = f"{proj_key} / {v['name']}"
                if not v.get("released") and v.get("overdue"):
                    out.append(_f("versions", vname, "version_overdue", "low",
                                  project=proj_key))
                if v.get("archived") and not v.get("released"):
                    out.append(_f("versions", vname, "version_archived_unreleased", "low",
                                  project=proj_key))

            # many_overdue_versions_in_project: a project-level aggregate. One
            # overdue version is noise; many signal an abandoned release
            # calendar. Counts only unreleased + overdue versions.
            overdue_n = sum(1 for v in vlist
                            if v.get("overdue") and not v.get("released"))
            if overdue_n >= OVERDUE_VERSIONS_WARN:
                out.append(_f("versions", proj_key,
                              "many_overdue_versions_in_project", "medium",
                              project=proj_key, overdue=overdue_n,
                              threshold=OVERDUE_VERSIONS_WARN))

            # version_naming_inconsistent: within one project, names mix a
            # semver convention (\d+\.\d+...) with free text. Low-confidence
            # heuristic, so info severity. Require a SUBSTANTIAL mix (>= 3 of
            # EACH): nearly every project pairs semver releases with a couple of
            # named versions (Backlog / Future / TBD), which is normal usage,
            # not "inconsistent" — gating to a real mix kills that cry-wolf noise.
            vnames = [v.get("name", "") for v in vlist]
            semver = [n for n in vnames if _SEMVER_RE.match(n)]
            freetext = [n for n in vnames if n and not _SEMVER_RE.match(n)]
            if len(semver) >= _VERSION_MIX_MIN and len(freetext) >= _VERSION_MIX_MIN:
                out.append(_f("versions", proj_key,
                              "version_naming_inconsistent", "info",
                              project=proj_key, semver=len(semver),
                              freetext=len(freetext)))

    # component_no_lead / component_unassigned_default
    comp = _area(snap, "components")
    if _evaluable(comp):
        for proj_key, clist in comp.get("by_project", {}).items():
            for c in clist:
                cname = f"{proj_key} / {c['name']}"
                if not c.get("has_lead"):
                    out.append(_f("components", cname, "component_no_lead", "low",
                                  project=proj_key))
                # An UNASSIGNED default assignee means issues created against
                # this component get no assignee, so they can fall through the
                # cracks. A human must pick a sensible default.
                if c.get("assignee_type") == "UNASSIGNED":
                    out.append(_f("components", cname,
                                  "component_unassigned_default", "low",
                                  project=proj_key))

    # permission_grant_overly_broad / public_browse_grant / anonymous_write_grant
    # / admin_grant_to_logged_in
    psg = _area(snap, "permission_scheme_grants")
    if _evaluable(psg):
        for scheme_nm, grants in psg.get("by_scheme", {}).items():
            for g in grants:
                holder = g.get("holder_type")
                perm = g.get("permission")
                if holder == "anyone":
                    if perm in _SENSITIVE_PERMS:
                        out.append(_f("permission_scheme_grants", scheme_nm,
                                      "permission_grant_overly_broad", "medium",
                                      permission=perm))
                    elif perm == "BROWSE_PROJECTS":
                        # Browse granted to anyone exposes the project (and every
                        # issue in it) to anonymous access — flag for a security
                        # review, but lower severity than an admin grant.
                        out.append(_f("permission_scheme_grants", scheme_nm,
                                      "public_browse_grant", "low",
                                      permission=perm))
                    elif perm in _ANON_WRITE_PERMS:
                        # Anonymous create/comment/edit is a higher-severity
                        # write exposure than read-only browse.
                        out.append(_f("permission_scheme_grants", scheme_nm,
                                      "anonymous_write_grant", "medium",
                                      permission=perm))
                elif holder in _LOGGED_IN_HOLDER_TYPES and perm in _SENSITIVE_PERMS:
                    # Project/global admin granted to "any logged-in user" is
                    # admin-group bloat by another name and a real audit finding.
                    out.append(_f("permission_scheme_grants", scheme_nm,
                                  "admin_grant_to_logged_in", "medium",
                                  permission=perm, holder_type=holder))

    # board_count_exceeds_projects: a board explosion (often per-team duplicate
    # boards) relative to the project count. Pure count ratio; needs at least
    # one project as a denominator.
    bd = _area(snap, "boards")
    projects = snap.get("projects") or []
    if _evaluable(bd) and projects:
        bcount = bd.get("count", 0)
        if (bcount >= _BOARD_MIN_ABS
                and bcount > BOARD_PER_PROJECT_RATIO * len(projects)):
            out.append(_f("boards", "boards", "board_count_exceeds_projects",
                          "info", count=bcount, projects=len(projects),
                          ratio=BOARD_PER_PROJECT_RATIO))

    # dashboard_filter_volume_high: filters or dashboards at/near the gather cap
    # indicate a very large shared-object population (indexing + governance
    # cost). The capped flag is set when the gather hit its 500-row ceiling.
    for area_name in ("filters", "dashboards"):
        a = _area(snap, area_name)
        if _evaluable(a) and a.get("capped"):
            out.append(_f(area_name, area_name, "dashboard_filter_volume_high",
                          "info", count=a.get("count", 0)))

    # ---- Section 2: project activity ----------------------------------------
    # empty_project: a project with zero issues (dead config carrying scheme
    # and board overhead). inactive_project: a project that HAS issues but whose
    # last issue update is older than the stale threshold (abandoned). Both read
    # only the {issue_count, stale} booleans/counts gathered per project KEY —
    # no project lead, name, or timestamp is ever read here.
    proj = _area(snap, "projects")
    if _evaluable(proj):
        for pkey, info in proj.get("by_project", {}).items():
            if not isinstance(info, dict):
                continue
            ic = info.get("issue_count")
            if ic == 0:
                out.append(_f("projects", pkey, "empty_project", "low",
                              project=pkey))
            elif isinstance(ic, int) and ic > 0 and info.get("stale"):
                out.append(_f("projects", pkey, "inactive_project", "medium",
                              project=pkey))

    # ---- Section 2: shared-object ownership ---------------------------------
    # shared_object_owned_by_inactive: filters/dashboards owned by a deactivated
    # user keep running, can't be edited, and may leak data. We surface a single
    # GENERIC finding per object class with an aggregate count — NEVER an owner
    # name/accountId (the gather only ever stored the owner.active boolean).
    # public_shared_filter / public_shared_dashboard: objects shared with anyone
    # on the web or all logged-in users leak their JQL (and on public sites,
    # results). Read the share-type-derived `public` boolean only.
    for area_name, obj_label in (("filters", "filter"), ("dashboards", "dashboard")):
        a = _area(snap, area_name)
        # `items` is present only on Cloud (DC stays count-only); guard on it so
        # a count-only DC snapshot is treated as unevaluable, never a false clean.
        if not (_evaluable(a) and isinstance(a.get("items"), list)):
            continue
        items = a["items"]
        inactive_owned = sum(1 for it in items
                             if isinstance(it, dict) and it.get("owner_active") is False)
        if inactive_owned:
            out.append(_f(area_name, obj_label,
                          "shared_object_owned_by_inactive", "high",
                          count=inactive_owned))
        public_n = sum(1 for it in items
                       if isinstance(it, dict) and it.get("public") is True)
        if public_n:
            if area_name == "filters":
                out.append(_f("filters", "filter", "public_shared_filter",
                              "high", count=public_n))
            else:
                out.append(_f("dashboards", "dashboard",
                              "public_shared_dashboard", "medium",
                              count=public_n))

    # ---- Section 3: ISSUE-LEVEL / DATA QUALITY ------------------------------
    # Source area: issue_quality, populated by count-only approx-count JQL
    # probes (invariant I1 — integers only, never issue content/keys/identity).
    # Each metric is an int OR None. A None metric is UNEVALUABLE: the check
    # simply does not fire (never a false clean, never a false finding). The
    # area must itself be evaluable (not skipped, no whole-area error).
    iq = _area(snap, "issue_quality")
    if _evaluable(iq):
        done_unres = iq.get("done_unresolved")
        stale_open = iq.get("stale_open")
        unassigned = iq.get("unassigned_unresolved")
        resolved_open = iq.get("resolved_but_open")
        total_unres = iq.get("total_unresolved")

        # done_but_unresolved (high): Done-category issues with empty resolution
        # break Unresolved filters, release warnings, velocity, and burndown.
        if isinstance(done_unres, int) and done_unres > 0:
            out.append(_f("issue_quality", "done_but_unresolved",
                          "done_but_unresolved", "high", count=done_unres))

        # resolved_but_open (medium): the mirror defect — a resolution set while
        # the issue sits in a non-Done status. Also corrupts reporting/reopens.
        if isinstance(resolved_open, int) and resolved_open > 0:
            out.append(_f("issue_quality", "resolved_but_open",
                          "resolved_but_open", "medium", count=resolved_open))

        # stale_open_issues (medium): unresolved/open issues untouched for a
        # year. Fire on a large absolute count OR a high fraction of all
        # unresolved issues (a small backlog that is mostly stale is unhealthy).
        if isinstance(stale_open, int) and stale_open > 0:
            high_fraction = (isinstance(total_unres, int) and total_unres > 0
                             and stale_open >= STALE_ISSUE_FRACTION * total_unres)
            if stale_open > STALE_ISSUE_WARN or high_fraction:
                detail = {"count": stale_open, "threshold": STALE_ISSUE_WARN}
                if isinstance(total_unres, int):
                    detail["total_unresolved"] = total_unres
                out.append(_f("issue_quality", "stale_open_issues",
                              "stale_open_issues", "medium", **detail))

        # unassigned_unresolved_high (low): open work with no owner. Fire on a
        # large absolute count OR a high fraction of all unresolved issues.
        if isinstance(unassigned, int) and unassigned > 0:
            high_fraction = (isinstance(total_unres, int) and total_unres > 0
                             and unassigned >= UNASSIGNED_FRACTION * total_unres)
            if unassigned > UNASSIGNED_WARN or high_fraction:
                detail = {"count": unassigned, "threshold": UNASSIGNED_WARN}
                if isinstance(total_unres, int):
                    detail["total_unresolved"] = total_unres
                out.append(_f("issue_quality", "unassigned_unresolved_high",
                              "unassigned_unresolved_high", "low", **detail))

    # migration_artifact: a name with migration suffix whose base collides
    # with another name in the same area
    _ARTIFACT_AREAS = [
        ("custom_fields", "names"),
        ("statuses", "names"),
        ("workflows", "names"),
        ("workflow_schemes", "names"),
        ("screen_schemes", "names"),
        ("field_config_schemes", "names"),
        ("issuetype_schemes", "names"),
        ("issuetype_screen_schemes", "names"),
    ]
    for area_name, names_key in _ARTIFACT_AREAS:
        a = _area(snap, area_name)
        if not _evaluable(a):
            continue
        all_names = a.get(names_key, [])
        normed_set = {_norm_name(n) for n in all_names if not _has_migration_suffix(n)}
        for nm in all_names:
            if _has_migration_suffix(nm):
                base = _strip_migration_suffix(nm)
                if _norm_name(base) in normed_set:
                    out.append(_f(area_name, nm, "migration_artifact", "medium",
                                  source_area=area_name, base_name=base))

    # ======================================================================
    # SITE-WIDE PERFORMANCE GUARDRAILS — recommended maxima (both deployments)
    # ======================================================================
    for area_name, kind, warn, limit, _label in _GUARDRAILS:
        a = _area(snap, area_name)
        if not _evaluable(a):
            continue
        count = a.get("count")
        if not isinstance(count, int):
            continue          # unevaluable -> no false positive
        if count >= limit:
            out.append(_f(area_name, area_name, kind, "high",
                          count=count, limit=limit))
        elif count >= warn:
            out.append(_f(area_name, area_name, kind, "medium",
                          count=count, limit=limit))

    # near_issue_type_limit: 150 issue (work) types is the hard limit PER
    # company-managed project, not site-wide. Same subset logic as
    # near_field_limit — a site-wide count <= 150 proves no project can breach
    # it; only > 150 makes a per-project breach possible, unconfirmable from the
    # site-wide total -> disclose (medium).
    it = _area(snap, "issue_types")
    if _evaluable(it):
        c = it.get("count")
        if isinstance(c, int) and c > ISSUE_TYPE_LIMIT:
            out.append(_f(
                "issue_types", "issue_types", "near_issue_type_limit", "medium",
                count=c, limit=ISSUE_TYPE_LIMIT,
                note=("150 issue types is the hard limit per company-managed "
                      "project; this site-wide count only makes a per-project "
                      "breach possible — verify per project.")))

    # ======================================================================
    # DC -> CLOUD MIGRATION (JCMA) — only on a Data Center / Server SOURCE
    # ======================================================================
    if snap.get("deployment") == "dc":
        # group_name_collision_reserved (Security, high): a DC group whose name
        # collides with a reserved Cloud group. Cloud MERGES same-named groups on
        # migration -> silent permission escalation / unexpected paid access. A
        # mandatory JCMA pre-migration fix. Group names are config identifiers.
        groups = _area(snap, "groups")
        if _evaluable(groups):
            for gname in (groups.get("names") or []):
                if str(gname).lower() in _RESERVED_CLOUD_GROUPS:
                    out.append(_f("groups", gname,
                                  "group_name_collision_reserved", "high"))

        # unsupported_custom_field_type (DataQuality, high): custom fields whose
        # type key is app-provided / outside JCMA's supported namespace. JCMA
        # migrates the field shell but SILENTLY DROPS the values. The verdict is
        # precomputed at gather time (app_provided_count) from the FULL type key
        # — re-deriving it from the lossy by_type suffix flagged ~100% of fields
        # (review Bug 1). A missing count is unevaluable -> no false positive.
        cf = _area(snap, "custom_fields")
        if _evaluable(cf):
            unsupported = cf.get("app_provided_count")
            if isinstance(unsupported, int) and unsupported > 0:
                out.append(_f("custom_fields", "custom_fields",
                              "unsupported_custom_field_type", "high",
                              count=unsupported, total=cf.get("count")))

        # apps_to_assess_for_cloud (Structure, medium): every user-installed app
        # must be individually assessed for a Cloud equivalent — apps with no
        # Cloud version block/fragment the migration (the #1 JCMA blocker). The
        # DC API can't resolve Cloud availability, so we report the at-risk
        # population (count). None -> unevaluable.
        plugins = _area(snap, "plugins")
        if _evaluable(plugins):
            n_apps = plugins.get("user_installed_count")
            if isinstance(n_apps, int) and n_apps > 0:
                out.append(_f("plugins", "plugins", "apps_to_assess_for_cloud",
                              "medium", count=n_apps,
                              enabled=plugins.get("enabled_count")))
            # script_app_present (Structure, high): a script app (ScriptRunner /
            # JSU / JMWE) is installed; its scripted fields/listeners/behaviours
            # and non-native workflow rules do NOT migrate and must be rebuilt.
            if plugins.get("script_apps_present") is True:
                out.append(_f("plugins", "plugins", "script_app_present",
                              "high"))

    return out
