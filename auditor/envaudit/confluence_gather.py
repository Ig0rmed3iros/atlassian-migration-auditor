"""Gather a single Confluence environment's configuration into a snapshot
(spec R1). Mirrors the Jira env gather (gather.py): the SAME outer shape
{deployment, projects, areas}, each area in its own guarded try/except so one
failure degrades to that area's `error` (never a whole-gather abort), Cloud-only
areas DC-skipped with {skipped:True, reason:...}, and per-object probes capped
with a `capped` flag.

ABSOLUTE PRIVACY (invariant I1). The snapshot stores ONLY:
  - counts and structural booleans,
  - principal / operation / space TYPES (group | user | anonymous;
    read | administer | ...; global | personal),
  - space STATUS (current | archived),
  - GLOBAL-space keys and names (config identifiers).
It NEVER stores: page titles/bodies/content, user identities, group MEMBER
identities, space-admin names, emails, accountIds, and NEVER a PERSONAL-space
key or name (personal-space keys/names embed usernames — personal spaces are
COUNTED only, via `personal_count`). Every reader below extracts the primitive
it needs (a bool / a type / a count) and discards the source object.
"""
from __future__ import annotations

from typing import Callable

from auditor.client import escape_query_key

from ._pool import map_results, worker_count

# Per-object probe caps (a probe past these sets the area's `capped` flag).
_GROUPS_PROBE_CAP = 60
_SPACE_PERM_CAP = 250      # max GLOBAL spaces we probe permissions for
_SPACE_PAGECOUNT_CAP = 250  # max GLOBAL spaces we count pages for
_RESTRICTION_PAGE_CAP = 100  # max pages sampled per space for restrictions

# Macro keys that commonly break (render as "Unknown macro" / blank) after a
# DC->Cloud migration. CCMA migrates the macro MARKUP, not the app or the macro
# definition, so a page using one of these can come out broken/invisible:
#   third_party_app — Marketplace-app macros with no installed Cloud renderer
#                     (Gliffy, draw.io, Team Calendars, ...).
#   removed_builtin — built-in macros Atlassian REMOVED from Cloud (Chart,
#                     Gallery, Page Index, ...) — render as legacy/unknown.
# Curated + extensible. Each is probed via `cql=macro="<key>"` (count only — the
# macro key is a type name, never page content). The `macro` CQL field is a
# documented Cloud field; an unsupported deployment yields a None count.
RISKY_MACROS: dict[str, str] = {
    "gliffy": "third_party_app",
    "drawio": "third_party_app",
    "drawioboard": "third_party_app",
    "team-calendar": "third_party_app",
    "chart": "removed_builtin",
    "gallery": "removed_builtin",
    "pageindex": "removed_builtin",
    # content_visibility — SUPPORTED macros whose CROSS-SPACE reference breaks
    # when the referenced page migrates in a different batch/space, leaving the
    # consumer page rendering blank (content silently becomes invisible). These
    # are not "unsupported" — the reference is the migration risk.
    "include": "content_visibility",          # Include Page
    "excerpt-include": "content_visibility",  # Excerpt Include
}

# Stale-page window: a page untouched for ~2 years (104 weeks) is stale.
_STALE_CQL = 'type=page and lastmodified < now("-104w")'

# Operation keys that mean "this principal can administer the space".
_ADMIN_OPS = {"administer", "setspacepermissions"}


def _as_int(v):
    """Coerce a CQL/count result to a real int, or None. cql_count returns an
    int on success or an "ERR.." string on failure (e.g. a 400 from a CQL field
    the deployment doesn't support) — a non-int becomes None so the metric reads
    as unevaluable, never a false 0."""
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _gather_spaces(client, dc, workers=1):
    """spaces area: per GLOBAL space {name,type,status,has_homepage,page_count}
    plus aggregate counts. Personal spaces are COUNTED only — their
    key/name/owner is NEVER stored (they embed usernames).

    Returns (area_dict, global_refs) where global_refs is [{key,id}, ...] for
    the GLOBAL spaces — the id-bearing list the permission probe needs (the
    public area shape is keyed by key and carries no id).

    The per-space page-count CQL probes are INDEPENDENT, so the first
    _SPACE_PAGECOUNT_CAP global spaces (in row order — identical to the
    sequential cap) are counted ~workers-wide; results merge by key, so output
    and the `capped` flag are completion-order-independent."""
    rows, err = client.spaces_detailed()
    global_rows: list = []   # GLOBAL space rows, in source order
    global_refs: list = []
    personal_count = 0
    archived_count = 0
    for r in (rows or []):
        stype = r.get("type")
        status = r.get("status")
        if status == "archived":
            archived_count += 1
        if stype == "personal":
            # COUNT ONLY — never the key/name (username-bearing).
            personal_count += 1
            continue
        # Only GLOBAL spaces are recorded in by_space (per spec target shape);
        # collaboration / other non-global non-personal types are aggregate-only.
        if stype != "global":
            continue
        key = r.get("key")
        if not key:
            continue
        global_refs.append({"key": key, "id": r.get("id")})
        global_rows.append(r)

    # The first _SPACE_PAGECOUNT_CAP global spaces are page-counted; the rest set
    # capped + page_count None — identical split to the sequential n_counted gate.
    counted_rows = global_rows[:_SPACE_PAGECOUNT_CAP]
    capped = len(global_rows) > _SPACE_PAGECOUNT_CAP

    def _count(r):
        # (key, page_count, orphan_pages) — counts only, never page titles/bodies.
        # orphan_pages = pages OUTSIDE the homepage subtree (total - descendants
        # of homepage - the homepage itself). These don't render in the Cloud
        # sidebar page tree — the classic DC->Cloud "page is here but invisible"
        # breakage. The homepage_id is used transiently here and never stored.
        key = r.get("key")
        total = _as_int(client.count_pages(key))
        orphan = None
        # The homepage id is a numeric content id — coerce to int so a non-numeric
        # value can never inject bare CQL; the key is escaped (defense in depth).
        try:
            hp_id = int(r.get("homepage_id"))
        except (TypeError, ValueError):
            hp_id = None
        if total is not None and hp_id is not None:
            under = _as_int(client.cql_count(
                f'space="{escape_query_key(key)}" and type=page '
                f'and ancestor={hp_id}'))
            if under is not None:
                orphan = max(total - under - 1, 0)
        return (key, total, orphan)

    page_counts: dict = {}
    orphan_counts: dict = {}
    for res in map_results(counted_rows, _count, workers):
        if isinstance(res, Exception):
            # Isolated: a probe crash leaves that space's counts absent (None
            # below), never aborts siblings.
            continue
        k, pc, orph = res
        page_counts[k] = pc
        orphan_counts[k] = orph

    by_space: dict = {}
    for r in global_rows:
        key = r.get("key")
        by_space[key] = {
            "name": r.get("name"),
            "type": r.get("type"),
            "status": r.get("status"),
            "has_homepage": bool(r.get("has_homepage")),
            "page_count": page_counts.get(key),
            "orphan_pages": orphan_counts.get(key),
        }
    out = {
        "by_space": by_space,
        "count": len(rows or []),
        "personal_count": personal_count,
        "archived_count": archived_count,
        "error": err,
    }
    if capped:
        out["capped"] = True
    return out, global_refs


def _gather_space_permissions(client, dc, global_spaces, workers=1):
    """space_permissions area: per GLOBAL space the set of principal/operation
    TYPES + has_admin + anonymous booleans. NEVER a principal value/name.

    On Cloud, anonymous access is NOT a v2 principal type (catalog §5), so the
    `anonymous` boolean is whatever the reduced perms expose (False unless a
    future surface adds it) — the checks layer treats Cloud anonymous as a
    capability gap. On DC the v1 permission list carries anonymousAccess, which
    space_permissions() folds in as an `anonymous` principal type.

    The per-space permission probes are INDEPENDENT — the first _SPACE_PERM_CAP
    spaces (in order, identical to the sequential cap) run ~workers-wide. Results
    merge by key and the error is the LAST errored space in order, so output is
    completion-order-independent."""
    probe_spaces = [sp for sp in global_spaces[:_SPACE_PERM_CAP] if sp.get("key")]
    capped = len(global_spaces) > _SPACE_PERM_CAP

    def _probe(sp):
        # (key, reduced-dict-or-None, err) — types/booleans only, never names.
        perms, perr = client.space_permissions(sp)
        if perr:
            return (sp.get("key"), None, perr)
        ptypes = sorted({p.get("principal_type") for p in perms
                         if p.get("principal_type")})
        ops = sorted({p.get("operation") for p in perms if p.get("operation")})
        has_admin = any(
            str(p.get("operation", "")).lower() in _ADMIN_OPS for p in perms)
        # On Cloud the v2 permissions API has no anonymous principal type, so
        # this dimension is genuinely UNEVALUABLE (None) — never a concrete
        # False, which downstream would read as "confirmed no public access"
        # and silently clean-bill a Cloud space (review Bug 2). DC's v1 list
        # folds anonymousAccess into an 'anonymous' principal type -> real bool.
        anonymous = (any(p.get("principal_type") == "anonymous" for p in perms)
                     if dc else None)
        # group_grants: the NAMES of groups holding a grant in this space (config
        # identifiers, never member identities) so the checks layer can flag a
        # grant to an empty group. DC's v1 list carries names; Cloud's v2
        # principal exposes only an opaque group id. The tri-state is load-
        # bearing: a list -> evaluable (cross-reference the names); None -> group
        # grants EXIST but their names are unavailable (Cloud) -> the checks layer
        # discloses a capability_gap rather than clean-billing; [] -> no group
        # grants -> nothing to check.
        gnames = sorted({n for p in perms for n in (p.get("group_names") or [])})
        has_group = any(p.get("principal_type") == "group" for p in perms)
        group_grants = gnames if gnames else (None if has_group else [])
        return (sp.get("key"), {
            "principal_types": ptypes,
            "operations": ops,
            "has_admin": has_admin,
            "anonymous": anonymous,
            "group_grants": group_grants,
        }, None)

    by_space: dict = {}
    err = None
    for res in map_results(probe_spaces, _probe, workers):
        if isinstance(res, Exception):
            # Isolated crash surfaces as the area error (last-writer-wins),
            # never aborts sibling probes.
            err = str(res)
            continue
        key, reduced, perr = res
        if perr:
            err = perr  # last-writer-wins; non-None marks partial coverage
            continue
        by_space[key] = reduced
    # A TOTAL failure (no space read) errors the area, loud. A PARTIAL failure
    # (some spaces read, some failed) must NOT error the area: that would gate
    # off the whole checks block via _evaluable and silently drop real findings
    # on the spaces we DID read (the by_space data). Keep the area evaluable and
    # record the partial failure for disclosure — mirrors the n_ok gating the
    # macros / content-quality areas already use.
    out = {"by_space": by_space, "error": err if not by_space else None}
    if err and by_space:
        out["probe_error"] = err
    if capped:
        out["capped"] = True
    return out


def _gather_restrictions(client, dc, global_spaces, workers=1):
    """page_restrictions area: per GLOBAL space a COUNT of pages carrying a
    view/edit restriction, sampled from the first _RESTRICTION_PAGE_CAP pages
    (one request per space, run ~workers-wide over the first _SPACE_PERM_CAP
    spaces). NEVER a restricting principal name/id (privacy I1).

    `evaluable` per space is False when the API returned no restriction data —
    the checks layer then DISCLOSES a coverage gap rather than reading absence
    as "no restrictions" (the dangerous false clean: a restricted page whose
    principal does not migrate becomes inaccessible post-cutover)."""
    probe_spaces = [sp for sp in global_spaces[:_SPACE_PERM_CAP] if sp.get("key")]
    capped = len(global_spaces) > _SPACE_PERM_CAP

    def _probe(sp):
        key = sp.get("key")
        probed, restricted, evaluable, truncated, err = \
            client.restricted_page_sample(key, cap=_RESTRICTION_PAGE_CAP)
        if err:
            return (key, None, err)
        # page_capped tracks whether the space was NOT fully drained (more pages
        # advertised via _links.next) — the truthful "sampled" signal, so a
        # restricted page beyond the first response is disclosed, not missed.
        return (key, {"restricted": restricted, "probed": probed,
                      "evaluable": evaluable, "page_capped": truncated}, None)

    by_space: dict = {}
    err = None
    for res in map_results(probe_spaces, _probe, workers):
        if isinstance(res, Exception):
            err = str(res)
            continue
        key, reduced, perr = res
        if perr:
            err = perr  # last-writer-wins; non-None marks partial coverage
            continue
        by_space[key] = reduced
    # A TOTAL failure (no space read) errors the area, loud. A PARTIAL failure
    # (some spaces read, some failed) must NOT error the area: that would gate
    # off the whole checks block via _evaluable and silently drop real findings
    # on the spaces we DID read (the by_space data). Keep the area evaluable and
    # record the partial failure for disclosure — mirrors the n_ok gating the
    # macros / content-quality areas already use.
    out = {"by_space": by_space, "error": err if not by_space else None}
    if err and by_space:
        out["probe_error"] = err
    if capped:
        out["capped"] = True
    return out


def _gather_macros(client, dc, workers=1):
    """macros area: per RISKY_MACROS key, the count of content using it
    (`cql=macro="<key>"` totalSize). COUNT ONLY — the macro key is a type name,
    never page content. A risky macro on >=1 page is a DC->Cloud render-fidelity
    risk (Unknown-macro / blank render). Per-key failure -> None for that key;
    the area error is set only when EVERY probe failed (never a false clean)."""
    def _count(key):
        return (key, _as_int(client.cql_count(f'macro="{key}"')))

    by_macro: dict = {}
    for res in map_results(list(RISKY_MACROS.keys()), _count, workers):
        if isinstance(res, Exception):
            continue
        k, c = res
        by_macro[k] = c
    n_ok = sum(1 for v in by_macro.values() if v is not None)
    error = None if n_ok > 0 else \
        "macro CQL surface unavailable (all macro probes failed)"
    return {"by_macro": by_macro, "error": error}


def _gather_groups(client, dc):
    """groups area: names + count + capped member-count probe. NEVER members."""
    names, member_counts, capped, err = client.groups_with_counts(
        cap=_GROUPS_PROBE_CAP)
    return {"names": names, "count": len(names),
            "member_counts": member_counts, "capped": capped, "error": err}


def _gather_templates(client, dc):
    """templates area: global page-template count + blueprint count."""
    gt, gt_err = client.global_templates()
    bp, bp_err = client.blueprints()
    err = gt_err or bp_err
    return {"global_count": gt, "blueprint_count": bp, "error": err}


def _gather_labels(client, dc):
    """labels area: global-label count."""
    cnt, err = client.global_labels()
    return {"global_count": cnt, "error": err}


def _gather_content_quality(client, dc, workers=1):
    """content_quality area: instance-wide CQL totalSize COUNTS only — never a
    page title/body.

      pages_total    = type=page
      stale_pages    = type=page and lastmodified < now("-104w")   (~2y)
      drafts         = type=page and status=draft   (guard: may 400 -> None)
      orphaned_pages = no clean instance-wide CQL -> None (best-effort)

    Per-metric failure (an ERR.. count / a 400 on a dialect-specific field)
    yields None for THAT metric; the area error is set only when EVERY count
    failed (so a fully-None area reads as unevaluable, not a false clean).

    The three CQL probes are INDEPENDENT — they run ~workers-wide and the
    integer-or-None results merge by metric, so the output and the n_ok-gated
    area error are completion-order-independent."""
    cqls = [("pages_total", "type=page"),
            ("stale_pages", _STALE_CQL),
            ("drafts", "type=page and status=draft")]

    def _count(item):
        # (metric, int|None) — count only, never a page title/body.
        _metric, cql = item
        return (_metric, _as_int(client.cql_count(cql)))

    values: dict = {}
    for res in map_results(cqls, _count, workers):
        if isinstance(res, Exception):
            # Isolated: a crash leaves that metric absent (None below), area
            # error still gated on whether ANY metric resolved.
            continue
        metric, v = res
        values[metric] = v

    pages_total = values.get("pages_total")
    stale_pages = values.get("stale_pages")
    drafts = values.get("drafts")
    n_ok = sum(1 for v in (pages_total, stale_pages, drafts) if v is not None)
    # No clean instance-wide CQL for true orphans (needs per-space homepage id
    # + the link-orphan signal isn't CQL-expressible) -> best-effort None.
    orphaned_pages = None
    error = None if n_ok > 0 else \
        "CQL count surface unavailable (all content-quality probes failed)"
    return {"pages_total": pages_total, "stale_pages": stale_pages,
            "drafts": drafts, "orphaned_pages": orphaned_pages,
            "error": error}


def _guard(areas, name, fn, say, fallback=None):
    """Run one area reader under its own try/except. A crash records an `error`
    (never aborts the gather) and continues; the partial shape is best-effort.
    Returns the area dict (so the caller can read derived data on success)."""
    try:
        areas[name] = fn()
        a = areas[name]
        say(f"[{name}] error={a.get('error')}")
        return a
    except Exception as exc:  # noqa: BLE001 — one area must never sink the run
        areas[name] = dict(fallback or {})
        areas[name]["error"] = str(exc)
        say(f"[{name}] FAILED: {exc}")
        return areas[name]


def gather_confluence(client, space_keys, progress: Callable[[str], None] | None = None):
    """Gather one Confluence environment into a privacy-safe snapshot.

    Returns the SAME outer shape as the Jira gather:
        {"deployment": "cloud"|"dc", "projects": [space keys], "areas": {...}}
    `space_keys` is the scoped space list (config identifiers); the gather reads
    instance-wide config and reduces everything to counts/booleans/types."""
    say = progress or (lambda m: None)
    dc = client.conn.deployment == "dc"
    # Bounded pool width for the independent per-space / per-area reads. httpx's
    # shared client is thread-safe and carries the per-call 429/5xx backoff, so
    # the same client is reused across threads and the pool stays modest.
    # MA_GATHER_WORKERS overrides the default (1 == forced sequential).
    workers = worker_count()
    areas: dict = {}

    # spaces FIRST: the GLOBAL-space {key,id} list it yields scopes the
    # per-space permission probes (and proves which keys are safe to name).
    # Its per-space page counts are themselves run ~workers-wide internally.
    global_spaces: list = []
    try:
        spaces_area, global_spaces = _gather_spaces(client, dc, workers)
        areas["spaces"] = spaces_area
        say(f"[spaces] count={spaces_area.get('count')} "
            f"error={spaces_area.get('error')}")
    except Exception as exc:  # noqa: BLE001
        areas["spaces"] = {
            "by_space": {}, "count": 0, "personal_count": 0,
            "archived_count": 0, "error": str(exc)}
        say(f"[spaces] FAILED: {exc}")

    # The remaining areas are INDEPENDENT of each other (space_permissions only
    # needs global_spaces, already in hand). Each runs under its own guard, and
    # the guards themselves run ~workers-wide — a per-area crash is isolated
    # into that area's error (never aborts siblings) by _guard, and results are
    # written to areas[name] keyed by name, so completion order is irrelevant.
    _independent = [
        ("space_permissions",
         lambda: _gather_space_permissions(client, dc, global_spaces, workers),
         {"by_space": {}}),
        ("page_restrictions",
         lambda: _gather_restrictions(client, dc, global_spaces, workers),
         {"by_space": {}}),
        ("groups", lambda: _gather_groups(client, dc),
         {"names": [], "count": 0, "member_counts": {}, "capped": False}),
        ("templates", lambda: _gather_templates(client, dc),
         {"global_count": None, "blueprint_count": None}),
        ("labels", lambda: _gather_labels(client, dc),
         {"global_count": None}),
        ("content_quality", lambda: _gather_content_quality(client, dc, workers),
         {"pages_total": None, "stale_pages": None,
          "drafts": None, "orphaned_pages": None}),
        ("macros", lambda: _gather_macros(client, dc, workers),
         {"by_macro": {}}),
    ]

    def _run_area(spec):
        # Returns (name, area_dict). _guard captures exceptions into the dict's
        # error and emits its say() event, so this never raises and the merge
        # below is a plain assignment.
        name, fn, fallback = spec
        local: dict = {}
        _guard(local, name, fn, say, fallback=fallback)
        return (name, local[name])

    for res in map_results(_independent, _run_area, workers):
        if isinstance(res, Exception):
            # Defensive: _guard already swallows task exceptions, so this is
            # unreachable in practice — kept so a future non-guarded task can
            # never sink the whole gather.
            continue
        name, area_dict = res
        areas[name] = area_dict

    return {"deployment": client.conn.deployment,
            "projects": list(space_keys), "areas": areas}
