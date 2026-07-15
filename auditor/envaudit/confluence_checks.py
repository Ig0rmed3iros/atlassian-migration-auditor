"""Deterministic Confluence environment-audit rules (spec R2).

Mirrors auditor.envaudit.checks.run_checks (the Jira side) but reads the
Confluence snapshot produced by gather_confluence: spaces, space_permissions,
groups, templates, labels, content_quality.

Discipline (identical to the Jira checks):
  - Shared finding shape {area, name, kind, severity, detail}.
  - Every rule is `_evaluable`-gated: an area that is skipped (expected DC /
    Cloud API absence) or errored (unexpected fetch failure) is UNEVALUABLE and
    never produces a false clean — instead a coverage signal is emitted:
        skipped -> capability_gap / info
        error   -> area_error    / warning
  - A per-object metric of None (DC count-only / unknowable) is unevaluable for
    that object: the rule simply does not fire (no false positive).
  - No divide-by-zero: ratio rules require a positive denominator.

Privacy: only counts, booleans, principal/permission TYPES, space TYPE/STATUS,
and space KEYS (config identifiers) are read. Personal-space keys/names are
NEVER forwarded — personal_space_sprawl reads the aggregate count only. No page
content, titles, member lists, principal identities, or admin names.

`anonymous_write_grant` and `capability_gap` / `area_error` are REUSED from the
Jira registry (same category + tier + meaning), so they intentionally do not get
a Confluence-specific kind name. Every other kind below is Confluence-specific.
"""
from __future__ import annotations

# Re-use the tiny shared helpers from the Jira checks so the two modules cannot
# drift apart on the finding shape / evaluability rule.
from .checks import _area, _evaluable, _f
from .confluence_gather import RISKY_MACROS

# ---------------------------------------------------------------------------
# Named thresholds
# ---------------------------------------------------------------------------

# A single space with more than this many pages degrades page-tree navigation
# and space view/edit (Atlassian Portfolio Insights "spaces with too many
# pages"). Atlassian does not publish the exact number, so it is configurable.
LARGE_SPACE_WARN = 5000

# A large archived-space population signals deferred cleanup and ongoing storage
# cost (archiving does not free Cloud storage). Count of archived spaces.
ARCHIVED_CLUTTER_WARN = 50

# Personal spaces accumulate with the user base and count toward the space
# guardrail; an absolute count above this is sprawl worth a review. Count only.
PERSONAL_SPRAWL_WARN = 200

# Atlassian's space guardrail is 10,000 (optimal < 8,000); beyond it
# permission-check overhead and search degrade. Warn approaching, high at/over.
SPACE_WARN = 8000
SPACE_CRIT = 10000

# Per instance, a stale-page fraction above this erodes trust and clutters
# search (Better Content Archiving's canonical "not updated for N days" signal).
STALE_RATIO_WARN = 0.5

# Lingering never-published drafts clutter search and cannot be bulk-deleted
# natively; an absolute count above this is worth surfacing.
DRAFTS_WARN = 100

# A very large global-label population fragments discoverability (Confluence has
# no native UI to find/remove unused labels). Count of global labels.
LABEL_WARN = 500

# A very large global-template population fragments template discovery: the
# create-from-template picker becomes a wall of near-duplicate templates and
# authors stop trusting it. Atlassian publishes no guardrail, so it is a
# configurable warn on the count of global (space-independent) templates.
TEMPLATE_WARN = 100

# Pages that sit OUTSIDE a space's homepage subtree don't render in the Cloud
# sidebar page tree — a classic DC->Cloud breakage when a parent was trashed /
# not migrated / restricted, promoting its children to root. Flag a space whose
# count of such pages exceeds this; the exact count rides in the detail.
ORPHAN_WARN = 10

# Principal types that represent a broad "anyone / all logged-in" audience.
# A write/admin grant to one of these is over-exposure (least-privilege break).
_BROAD_PRINCIPAL_TYPES = {"all-logged-in", "all-users", "access-class",
                          "anyone", "confluence-users"}

# Operation tokens (substring match, case-insensitive) that imply the principal
# can create / update / delete content — i.e. a write capability, not read-only.
_WRITE_OP_TOKENS = ("create", "update", "edit", "delete", "restrict",
                    "administer", "archive", "export")


def _is_write_op(op: str) -> bool:
    low = (op or "").lower()
    return any(tok in low for tok in _WRITE_OP_TOKENS)


# Distinguishes a reduced perms dict that EXPLICITLY set group_grants=None (Cloud
# probed it, names unavailable -> disclose) from one that never carried the key
# at all (older snapshot / not probed -> no signal). `None` alone cannot tell
# these apart, and conflating them would emit a spurious capability_gap.
_UNSET = object()


def run_checks_confluence(snap: dict) -> list[dict]:
    out: list[dict] = []
    areas = snap.get("areas") or {}

    # ---- Coverage signals for every area that cannot be fully evaluated -----
    # Identical contract to run_checks: an expected skip -> capability_gap/info;
    # an unexpected fetch error -> area_error/warning (loud, distinct signal).
    for name, a in areas.items():
        if not isinstance(a, dict):
            continue
        if a.get("skipped"):
            out.append(_f(name, name, "capability_gap", "info",
                          reason=a.get("reason") or "skipped"))
        elif a.get("error"):
            out.append(_f(name, name, "area_error", "warning",
                          error=a.get("error")))

    # ======================================================================
    # SPACES & HYGIENE
    # ======================================================================
    spaces = _area(snap, "spaces")
    if _evaluable(spaces):
        by_space = spaces.get("by_space") or {}
        for key, s in by_space.items():
            if not isinstance(s, dict):
                continue
            status = s.get("status")
            page_count = s.get("page_count")
            has_homepage = s.get("has_homepage")

            # Only `current` spaces are evaluated for content/structure hygiene;
            # archived spaces are intentionally dormant.
            is_current = status == "current"

            # empty_space (low, app): a current space with zero pages. None
            # page_count (DC count-only / unknowable) is UNEVALUABLE -> skip.
            if is_current and isinstance(page_count, int) and page_count == 0:
                out.append(_f("spaces", key, "empty_space", "low",
                              space=key))

            # large_space (medium, human): a current space over the page warn.
            if is_current and isinstance(page_count, int) \
                    and page_count > LARGE_SPACE_WARN:
                out.append(_f("spaces", key, "large_space", "medium",
                              space=key, pages=page_count,
                              threshold=LARGE_SPACE_WARN))

            # space_no_homepage (medium, human): a current space whose homepage
            # is absent. has_homepage None (unknowable) -> UNEVALUABLE.
            if is_current and has_homepage is False:
                out.append(_f("spaces", key, "space_no_homepage", "medium",
                              space=key))

            # orphaned_pages (Structure, migration): pages OUTSIDE the homepage
            # subtree don't appear in the Cloud sidebar tree — reachable only by
            # search / direct link. A DC->Cloud breakage (a trashed / unmigrated
            # / restricted parent promotes its children to root). orphan_pages
            # None (unevaluable) -> skip; only on current spaces. High when most
            # of the space is orphaned, else medium.
            orphan_pages = s.get("orphan_pages")
            if is_current and isinstance(orphan_pages, int) \
                    and orphan_pages > ORPHAN_WARN:
                sev = "high" if (isinstance(page_count, int) and page_count > 0
                                 and orphan_pages / page_count > 0.5) \
                    else "medium"
                out.append(_f("spaces", key, "orphaned_pages", sev,
                              space=key, orphans=orphan_pages,
                              total=page_count, threshold=ORPHAN_WARN))

        # Aggregate space-population rules. count / personal_count /
        # archived_count are ints from the gather aggregate.
        count = spaces.get("count")
        personal_count = spaces.get("personal_count")
        archived_count = spaces.get("archived_count")

        # archived_space_clutter (low, human): too many archived spaces.
        if isinstance(archived_count, int) \
                and archived_count > ARCHIVED_CLUTTER_WARN:
            out.append(_f("spaces", "spaces", "archived_space_clutter", "low",
                          archived=archived_count,
                          threshold=ARCHIVED_CLUTTER_WARN))

        # personal_space_sprawl (low, human): too many personal spaces. The
        # finding NAME is the generic area ("spaces") and the detail carries the
        # COUNT only — never any personal-space key/name (privacy-critical).
        if isinstance(personal_count, int) \
                and personal_count > PERSONAL_SPRAWL_WARN:
            out.append(_f("spaces", "spaces", "personal_space_sprawl", "low",
                          personal=personal_count,
                          threshold=PERSONAL_SPRAWL_WARN))

        # space_count_near_guardrail (Performance): approaching Atlassian's
        # 10,000-space guardrail. High at/over CRIT, else medium over WARN.
        # Exactly ONE finding (high wins over medium).
        if isinstance(count, int):
            if count > SPACE_CRIT:
                out.append(_f("spaces", "spaces",
                              "space_count_near_guardrail", "high",
                              count=count, threshold=SPACE_CRIT))
            elif count > SPACE_WARN:
                out.append(_f("spaces", "spaces",
                              "space_count_near_guardrail", "medium",
                              count=count, threshold=SPACE_WARN))

    # ======================================================================
    # PERMISSIONS & SECURITY
    # ======================================================================
    perms = _area(snap, "space_permissions")
    if _evaluable(perms):
        by_space = perms.get("by_space") or {}
        anon_evaluable = False     # did ANY probed space yield a real anonymous bool?
        empty_group_gap = False    # did ANY space have group grants w/o names (Cloud)?
        grant_unresolved = False   # did ANY granted group name fail to resolve a count?
        # Member counts for the empty-group cross-reference. The groups area is
        # only consulted when it is itself evaluable; an unknown group (beyond
        # the count cap, or a failed probe) is simply ABSENT from member_counts,
        # never stored as 0 — so an UNKNOWN group is never mistaken for an EMPTY
        # one (no false positive), and its absence is DISCLOSED below.
        groups_area = _area(snap, "groups")
        member_counts = (groups_area.get("member_counts") or {}) \
            if _evaluable(groups_area) else {}
        for key, p in by_space.items():
            if not isinstance(p, dict):
                continue
            principal_types = [str(t).lower()
                               for t in (p.get("principal_types") or [])]
            operations = [str(o) for o in (p.get("operations") or [])]
            has_admin = p.get("has_admin")
            anonymous = p.get("anonymous")
            if anonymous is not None:
                anon_evaluable = True

            # permission_grant_to_empty_group (Security, medium, human): a space
            # grant to a 0-member group is a latent escalation hole — a future
            # add to that group silently inherits the space access. group_grants
            # is the list of granted group NAMES (config identifiers, DC-only);
            # None means "group grants exist but the names are unavailable"
            # (Cloud v2 principal gives a group id, not a name) -> the join is
            # impossible, so DISCLOSE rather than clean-bill. [] = no group
            # grants -> nothing to check.
            group_grants = p.get("group_grants", _UNSET)
            if group_grants is _UNSET:
                # The gather did not populate this dimension (an older snapshot,
                # or a probe that never reached the reduction). NOT a signal —
                # only an EXPLICIT None means "probed, names unavailable".
                pass
            elif group_grants is None:
                empty_group_gap = True
            else:
                for gname in group_grants:
                    # member_counts values are always ints (the client stores a
                    # count only when the probe succeeded), so an ABSENT name
                    # means the count is unknown: the groups area errored /
                    # skipped (member_counts == {}), the group directory was
                    # capped before reaching this group, or its count probe
                    # failed. An unknown count is NOT zero -> never fire, but the
                    # cross-reference is incomplete, so DISCLOSE rather than
                    # clean-bill (review: capped/errored groups were a silent
                    # false clean).
                    cnt = member_counts.get(gname, _UNSET)
                    if cnt is _UNSET:
                        grant_unresolved = True
                    elif cnt == 0:
                        out.append(_f("space_permissions", key,
                                      "permission_grant_to_empty_group",
                                      "medium", space=key, group=gname))

            # space_no_admin (high, human): no space-admin grant present. Only
            # fire on an explicit False — None (unknowable) is UNEVALUABLE.
            if has_admin is False:
                out.append(_f("space_permissions", key, "space_no_admin",
                              "high", space=key))

            # anonymous_space_access (high, human): the space is reachable
            # anonymously (DC anonymousAccess True). None -> UNEVALUABLE.
            if anonymous is True:
                out.append(_f("space_permissions", key,
                              "anonymous_space_access", "high", space=key))

                # anonymous_write_grant (high, human; REUSED Jira kind): the
                # anonymous principal also holds a write/create/delete op. A
                # spam/defacement vector beyond read-only public access.
                if any(_is_write_op(o) for o in operations):
                    out.append(_f("space_permissions", key,
                                  "anonymous_write_grant", "high", space=key))

            # space_permission_to_anyone (medium, human): a broad principal
            # class (all-logged-in / access-class / all-users) holds a grant in
            # this space. Anonymous is handled by the dedicated rules above, so
            # exclude it here to avoid double-reporting the same exposure.
            broad = [t for t in principal_types
                     if t in _BROAD_PRINCIPAL_TYPES and t != "anonymous"]
            if broad:
                out.append(_f("space_permissions", key,
                              "space_permission_to_anyone", "medium",
                              space=key))

        # We probed spaces but the anonymous/public-access dimension was
        # unevaluable for ALL of them (Cloud: not a v2 principal type). An
        # absent anonymous_space_access finding must NOT read as "no public
        # access" — emit a coverage gap so the reader verifies it manually
        # (review Bug 2: this path was a silent false clean).
        if by_space and not anon_evaluable:
            out.append(_f("space_permissions", "space_permissions",
                          "capability_gap", "info",
                          reason="anonymous/public-space access can't be read "
                                 "from the Cloud permissions API — verify public "
                                 "access in each space's settings manually"))

        # A space granted permission to a group whose NAME the API would not
        # surface (Cloud v2 exposes a group id, not a name) cannot be checked
        # for an empty-group escalation hole. An absent
        # permission_grant_to_empty_group finding must NOT read as "no empty
        # group is granted" — disclose so the reader cross-checks manually.
        if empty_group_gap:
            out.append(_f("space_permissions", "space_permissions",
                          "capability_gap", "info",
                          reason="space permissions are granted to groups but "
                                 "the Cloud permissions API exposes a group id, "
                                 "not a name — grants to empty groups can't be "
                                 "cross-checked; verify group membership in the "
                                 "space settings manually"))

        # A granted group whose member count we never read (the groups directory
        # was capped past it, the groups area errored / was skipped, or its count
        # probe failed) leaves the empty-group cross-reference INCOMPLETE. An
        # absent permission_grant_to_empty_group finding must not read as "no
        # empty-group grant" — disclose the blind spot.
        if grant_unresolved:
            out.append(_f("space_permissions", "space_permissions",
                          "capability_gap", "info",
                          reason="one or more space permission grants target a "
                                 "group whose member count could not be read "
                                 "(the group directory was capped, the groups "
                                 "area was unavailable, or a count probe failed) "
                                 "— grants to empty groups can't be fully "
                                 "cross-checked; verify group membership "
                                 "manually"))

    # ======================================================================
    # SAMPLING-CAP DISCLOSURE (coverage) — INDEPENDENT of _evaluable. gather
    # probes only the first N global spaces for per-space page counts and
    # permissions, setting `capped`. A single transient per-space probe error
    # poisons the area `error` (so _evaluable would be False and the whole area,
    # including any in-block cap note, would be silently dropped) — exactly the
    # large-instance case where coverage is most incomplete. `capped` is only
    # ever True when the probe actually saw more than the cap, so it is always
    # trustworthy; disclose regardless of error/evaluable state.
    # ======================================================================
    for area_name, what in (("spaces", "per-space page counts"),
                            ("space_permissions", "space permissions"),
                            ("page_restrictions", "page restrictions")):
        a = _area(snap, area_name)
        if isinstance(a, dict) and a.get("capped"):
            out.append(_f(area_name, area_name, "capability_gap", "info",
                          note=(f"{what} were probed for only the first batch of "
                                "global spaces; remaining spaces were not "
                                "evaluated")))
        # A PARTIAL probe failure (some spaces read, some errored) keeps the area
        # evaluable so the read spaces are still checked, but the unread spaces
        # must be DISCLOSED — never implied clean (review: a single transient
        # per-space failure used to error the whole area and silently drop every
        # finding on the spaces that WERE read).
        if isinstance(a, dict) and a.get("probe_error"):
            out.append(_f(area_name, area_name, "capability_gap", "info",
                          note=(f"{what} could not be read for some spaces "
                                "(transient probe failure); those spaces were "
                                "not evaluated and are not implied clean")))

    # ======================================================================
    # PAGE RESTRICTIONS (migration access risk) — per GLOBAL space the count of
    # pages with a view/edit restriction. A restricted page whose restricting
    # user/group does not migrate becomes inaccessible post-cutover; if the
    # restriction dimension can't be read it is DISCLOSED, never read as clean.
    # ======================================================================
    restr = _area(snap, "page_restrictions")
    if _evaluable(restr):
        by_space = restr.get("by_space") or {}
        any_evaluable = False
        any_sampled = False
        for key, p in by_space.items():
            if not isinstance(p, dict):
                continue
            if p.get("evaluable"):
                any_evaluable = True
                n = p.get("restricted")
                if isinstance(n, int) and n > 0:
                    out.append(_f("page_restrictions", key, "restricted_pages",
                                  "medium", space=key, restricted=n,
                                  probed=p.get("probed"),
                                  note="pages with view/edit restrictions — if a "
                                       "restricting user or group does not "
                                       "migrate, the page becomes inaccessible "
                                       "after cutover; verify the restriction "
                                       "principals migrate"))
            if p.get("page_capped"):
                any_sampled = True
        # If NO probed space yielded readable restriction data, the dimension is
        # unevaluable here -> disclose (never a silent clean on the most
        # dangerous Confluence migration gap).
        if by_space and not any_evaluable:
            out.append(_f("page_restrictions", "page_restrictions",
                          "capability_gap", "info",
                          note="page-level view/edit restrictions could not be "
                               "read from this instance — verify restricted "
                               "pages manually before cutover"))
        elif any_sampled:
            out.append(_f("page_restrictions", "page_restrictions",
                          "capability_gap", "info",
                          note="restrictions were sampled from the first pages "
                               "of each space; a space may hold restricted pages "
                               "beyond the sample"))

    # ======================================================================
    # CONTENT & DATA QUALITY
    # ======================================================================
    cq = _area(snap, "content_quality")
    if _evaluable(cq):
        pages_total = cq.get("pages_total")
        stale_pages = cq.get("stale_pages")
        drafts = cq.get("drafts")

        # stale_page_ratio_high (DataQuality, medium, human): a high fraction of
        # year-plus-stale pages. STRICTLY no divide-by-zero / None: require both
        # an int stale count AND a positive int total before computing a ratio.
        if isinstance(stale_pages, int) and isinstance(pages_total, int) \
                and pages_total > 0:
            ratio = stale_pages / pages_total
            if ratio > STALE_RATIO_WARN:
                out.append(_f("content_quality", "content_quality",
                              "stale_page_ratio_high", "medium",
                              stale=stale_pages, total=pages_total,
                              ratio=round(ratio, 3),
                              threshold=STALE_RATIO_WARN))

        # drafts_pileup (Hygiene, low, human): lingering never-published drafts.
        # None -> UNEVALUABLE.
        if isinstance(drafts, int) and drafts > DRAFTS_WARN:
            out.append(_f("content_quality", "content_quality",
                          "drafts_pileup", "low",
                          drafts=drafts, threshold=DRAFTS_WARN))

        # content_quality is gathered but every metric is None (DC / unknowable)
        # -> emit a capability_gap so the reader knows the content checks could
        # not run (and nothing above fired). This is NOT a false clean.
        if all(cq.get(k) is None for k in
               ("pages_total", "stale_pages", "drafts", "orphaned_pages")):
            out.append(_f("content_quality", "content_quality",
                          "capability_gap", "info",
                          reason="content metrics unavailable"))

    # ======================================================================
    # MACROS — render fidelity (DC->Cloud migration)
    # ======================================================================
    macros = _area(snap, "macros")
    if _evaluable(macros):
        # unsupported_macro_usage: a risky macro is present on >=1 page. CCMA
        # moves the markup, not the app/definition, so these render as "Unknown
        # macro" / blank in Cloud. One finding per macro key in use. third-party
        # app macros are HIGH (broken diagrams/panels); removed built-ins MEDIUM.
        # NAME is the macro key (a type name, never page content). A None / zero
        # count is UNEVALUABLE / clean and does not fire.
        for mkey, cnt in sorted((macros.get("by_macro") or {}).items()):
            if not isinstance(cnt, int) or cnt <= 0:
                continue
            cat = RISKY_MACROS.get(mkey, "third_party_app")
            # content_visibility macros (Include / Excerpt-Include) are SUPPORTED
            # on Cloud — the risk is the cross-space reference going blank, not an
            # unsupported macro. Report a distinct kind so the guidance is right.
            if cat == "content_visibility":
                out.append(_f("macros", mkey, "cross_space_include_risk",
                              "medium", macro=mkey, pages=cnt))
                continue
            sev = "high" if cat == "third_party_app" else "medium"
            out.append(_f("macros", mkey, "unsupported_macro_usage", sev,
                          macro=mkey, category=cat, pages=cnt))

    # ======================================================================
    # TEMPLATES / LABELS / CONFIG
    # ======================================================================
    labels = _area(snap, "labels")
    if _evaluable(labels):
        # label_sprawl (Hygiene, low, human): a very large global-label set.
        global_count = labels.get("global_count")
        if isinstance(global_count, int) and global_count > LABEL_WARN:
            out.append(_f("labels", "labels", "label_sprawl", "low",
                          count=global_count, threshold=LABEL_WARN))

    templates = _area(snap, "templates")
    if _evaluable(templates):
        # template_sprawl (Hygiene, low, human): a very large global-template
        # set fragments template discovery. global_count None (DC / unknowable)
        # is UNEVALUABLE -> never fires.
        tcount = templates.get("global_count")
        if isinstance(tcount, int) and tcount > TEMPLATE_WARN:
            out.append(_f("templates", "templates", "template_sprawl", "low",
                          count=tcount, threshold=TEMPLATE_WARN))

    groups = _area(snap, "groups")
    if _evaluable(groups):
        # confluence_empty_group (Hygiene, low, app): a Confluence directory
        # group with zero members. Distinct kind name from the Jira empty_group
        # so the shared fix registry stays unambiguous. Count only, no members.
        for gname, cnt in (groups.get("member_counts") or {}).items():
            if cnt == 0:
                out.append(_f("groups", gname, "confluence_empty_group", "low"))

    return out
