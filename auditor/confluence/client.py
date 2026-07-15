"""Confluence client — Cloud and Data Center, PAT only (no OAuth in v1).

The deployment axis is wider here than in Jira because Atlassian REMOVED the
classic v1 enumeration endpoints from Cloud (410 Gone since the 2024-25
deprecations): /rest/api/content, /rest/api/space and /content/{id}/child are
DC-only now. What survives on Cloud is CQL search (/rest/api/content/search
for full expanded bodies, /rest/api/search for the count-bearing totalSize
envelope) and the v2 cursor APIs (/api/v2/spaces). So:

  - cloud: api_base = site + /wiki; spaces via v2 cursor; pages via
           content/search (CQL) which still returns v1-shaped expanded rows.
  - dc:    api_base = bare site (no /wiki context path by convention);
           classic v1 /rest/api/space + /rest/api/content with Bearer PAT
           (first-class on Confluence 7.9+).

Both page sources return the same {results, _links.next} envelope with the
same expanded row shape, so extract stays deployment-blind. _links.next is a
RELATIVE url and Atlassian is inconsistent about whether it is context- or
site-relative; _follow() dedups a /wiki prefix so a cloud client never
requests /wiki/wiki/... Mid-iteration failures raise ClientError — extraction
count verification depends on enumeration never silently truncating.
"""
from __future__ import annotations

from typing import Iterator
from urllib.parse import parse_qsl, urlsplit

from ..client import BaseClient, ClientError, escape_query_key

# One expansion list for both deployments: everything slim_page() needs in a
# single request (body fingerprint, lineage, labels, inline comment +
# attachment children with their cap markers).
_EXPAND = ("body.storage,version,history,ancestors,metadata.labels,"
           "children.comment,children.attachment")

# Expand path for the restriction probe — read + update restrictions, both
# principal kinds. Reduced to a COUNT of restricted pages; identities discarded.
_RESTRICTION_EXPAND = ("restrictions.read.restrictions.user,"
                       "restrictions.read.restrictions.group,"
                       "restrictions.update.restrictions.user,"
                       "restrictions.update.restrictions.group")


def _page_is_restricted(block: dict) -> bool:
    """True if a content row's expanded restrictions block carries any read or
    update restriction. Inspects only the PRESENCE of user/group entries —
    never the principal names/ids (privacy invariant I1)."""
    for op in ("read", "update"):
        rr = ((block.get(op) or {}).get("restrictions")) or {}
        users = ((rr.get("user") or {}).get("results")) or []
        groups = ((rr.get("group") or {}).get("results")) or []
        if users or groups:
            return True
    return False


class ConfluenceClient(BaseClient):
    """Confluence REST surface: v2 spaces + CQL search on Cloud, classic v1
    on Data Center. Same public methods either way."""

    @property
    def api_base(self) -> str:
        # Cloud serves Confluence under the /wiki context path; DC serves it
        # at the site root (operators put any context path in site_url).
        base = self.conn.site_url.rstrip("/")
        return base + "/wiki" if self.conn.deployment == "cloud" else base

    # ------------------------------------------------------------ plumbing
    def _follow(self, next_link: str) -> tuple[int, dict | list]:
        """Request a _links.next URL. The link is relative but its anchor is
        ambiguous (context- vs site-relative); when it carries the /wiki
        prefix our api_base already ends with, strip it. req() composes
        api_base + path, so the query string must travel as params."""
        s = urlsplit(next_link)
        path = s.path
        if path.startswith("/wiki/") and self.api_base.endswith("/wiki"):
            path = path[len("/wiki"):]
        return self.req(path, params=dict(parse_qsl(s.query)))

    # ------------------------------------------------------------- surface
    def myself(self) -> dict:
        """Identity check, normalized to the cross-product verify shape.

        /rest/api/user/current works on DC and (per the current spec) Cloud,
        but Cloud's v1 surface is shrinking release by release — so a Cloud
        404/410 falls back to a v2 spaces probe purely as an AUTH check and
        returns a placeholder identity. 401/403 always raises: the fallback
        must never read bad credentials as a verified connection."""
        st, d = self.req("/rest/api/user/current")
        if st == 200:
            # DC carries no accountId (email optional on both) — .get keeps
            # the identity shape stable so callers never branch.
            return {"display_name": d.get("displayName"),
                    "email": d.get("email"),
                    "account_id": d.get("accountId")}
        if st in (404, 410) and self.conn.deployment == "cloud":
            st2, d2 = self.req("/api/v2/spaces", params={"limit": 1})
            if st2 == 200:
                return {"display_name": "verified (identity API unavailable)",
                        "email": None, "account_id": None}
            raise ClientError(
                f"/user/current gone ({st}) and v2 auth check failed: "
                f"{st2} {d2.get('_error', '')}", st2)
        raise ClientError(
            f"/rest/api/user/current failed: {st} {d.get('_error', '')}", st)

    def all_spaces(self) -> tuple[list, str | None]:
        """All spaces as normalized {key,name,id} rows, (rows, err) contract
        like all_projects. First-page failure → ([], ERR...); mid-loop
        failure → (partial, ERR...:truncated) so an unreachable side can
        never render as a short clean list."""
        rows: list[dict] = []
        if self.conn.deployment == "cloud":
            st, d = self.req("/api/v2/spaces", params={"limit": 250})
            while True:
                if st != 200:
                    if not rows:
                        return [], f"ERR{st}:{str(d.get('_error', ''))[:60]}"
                    return rows, f"ERR{st}:truncated"
                rows += [{"key": r.get("key"), "name": r.get("name"),
                          "id": r.get("id")} for r in d.get("results", [])]
                nxt = (d.get("_links") or {}).get("next")
                if not nxt:
                    return rows, None
                st, d = self._follow(nxt)
        start, limit = 0, 50
        while True:
            st, d = self.req("/rest/api/space",
                             params={"start": start, "limit": limit})
            if st != 200:
                if not rows:
                    return [], f"ERR{st}:{str(d.get('_error', ''))[:60]}"
                return rows, f"ERR{st}:truncated"
            results = d.get("results", [])
            rows += [{"key": r.get("key"), "name": r.get("name"),
                      "id": r.get("id")} for r in results]
            # Short page or no next link = done (belt and braces: some DC
            # versions omit _links.next on the exact-fit final page).
            if len(results) < limit or not (d.get("_links") or {}).get("next"):
                return rows, None
            start += len(results)

    def cql_count(self, cql: str) -> int | str:
        """Result count for an arbitrary CQL query — the ONLY count source that
        works on both deployments: /rest/api/search carries totalSize in its
        envelope (content/search does not). limit=1 not 0: limit=0 is
        schema-valid but runtime-unverified. Errors return ERR<status> so a
        caller can treat an unsupported CQL field (400) as unevaluable instead
        of comparing against 0."""
        st, d = self.req("/rest/api/search",
                         params={"cql": cql, "limit": 1})
        return d.get("totalSize") if st == 200 else f"ERR{st}"

    def count_pages(self, space_key: str) -> int | str:
        """Current-page count for a space (delegates to cql_count). Errors
        return ERR<status> so the extraction verification gate fails loudly
        instead of comparing against 0."""
        return self.cql_count(self._space_cql(space_key))

    @staticmethod
    def _space_cql(space_key: str) -> str:
        # Keys are server-derived; escaping is defense in depth so a key
        # carrying a quote can never break out of the CQL literal.
        return f'space="{escape_query_key(space_key)}" and type=page'

    @staticmethod
    def _space_content_cql(space_key: str) -> str:
        # Migration fidelity covers BOTH content types — a dropped/broken blog
        # post is real data loss, not invisible. (The env audit deliberately
        # stays page-only via _space_cql, so its homepage-subtree orphan calc
        # isn't confused by date-organized blogs.)
        return f'space="{escape_query_key(space_key)}" and type in (page, blogpost)'

    def count_content(self, space_key: str) -> int | str:
        """Current page + blog-post count for a space — the migration extract's
        count-verification unit. ERR<status> on failure (fail loud)."""
        return self.cql_count(self._space_content_cql(space_key))

    def space_content(self, space_key: str, page_size: int = 50) -> Iterator[dict]:
        """All current PAGES and BLOG POSTS of a space with the full _EXPAND
        payload — the migration fidelity unit. Cloud enumerates both types in
        one CQL pass (it lost /rest/api/content); DC's v1 content endpoint takes
        a single `type`, so it enumerates page then blogpost. Both envelopes
        paginate via _links.next."""
        if self.conn.deployment == "cloud":
            st, d = self.req("/rest/api/content/search",
                             params={"cql": self._space_content_cql(space_key),
                                     "expand": _EXPAND, "limit": page_size})
            yield from self._paginate_content(st, d)
        else:
            for ctype in ("page", "blogpost"):
                st, d = self.req("/rest/api/content",
                                 params={"spaceKey": space_key, "type": ctype,
                                         "status": "current", "expand": _EXPAND,
                                         "limit": page_size})
                yield from self._paginate_content(st, d)

    def _paginate_content(self, st: int, d) -> Iterator[dict]:
        while True:
            if st != 200:
                raise ClientError(
                    f"content enumeration {st}: {d.get('_error', '')}", st)
            yield from d.get("results", [])
            nxt = (d.get("_links") or {}).get("next")
            if not nxt:
                return
            st, d = self._follow(nxt)

    def restricted_page_sample(
            self, space_key: str,
            cap: int = 100) -> tuple[int, int, bool, bool, str | None]:
        """Sample the first page (limit `cap`) of a space with view/edit
        RESTRICTIONS expanded, reduced to COUNTS only:
        (probed, restricted, evaluable, truncated, err).

        - probed:     pages inspected in this one request (<= cap).
        - restricted: pages carrying any read OR update restriction.
        - evaluable:  the API returned a restrictions block on >=1 row. False
                      means restrictions could NOT be read here, so the caller
                      must DISCLOSE the gap, never read absence as "no
                      restrictions" (the dangerous false clean: a restricted page
                      whose principal does not migrate becomes inaccessible).
        - truncated:  the response advertised MORE pages (`_links.next`), so the
                      space was NOT fully drained — the caller discloses that the
                      count is a sampled floor. This follows the next-link, not
                      "did we hit cap": a SHORT page that still has `next` is
                      truncated, so a restricted page on a later page is never
                      silently missed.
        Cloud lost /rest/api/content, so pages come via CQL content/search; DC
        uses classic v1 content. Restriction identities are NEVER stored."""
        if self.conn.deployment == "cloud":
            path = "/rest/api/content/search"
            params = {"cql": self._space_cql(space_key),
                      "expand": _RESTRICTION_EXPAND, "limit": cap}
        else:
            path = "/rest/api/content"
            params = {"spaceKey": space_key, "type": "page", "status": "current",
                      "expand": _RESTRICTION_EXPAND, "limit": cap}
        st, d = self.req(path, params=params)
        if st != 200:
            err = d.get("_error", "") if isinstance(d, dict) else ""
            return 0, 0, False, False, f"ERR{st}:{str(err)[:60]}"
        probed = restricted = 0
        evaluable = False
        for r in (d.get("results") or []):
            if not isinstance(r, dict):
                continue
            probed += 1
            block = r.get("restrictions")
            if isinstance(block, dict):
                evaluable = True
                if _page_is_restricted(block):
                    restricted += 1
        truncated = bool((d.get("_links") or {}).get("next"))
        return probed, restricted, evaluable, truncated, None

    def add_page_label(self, page_id: str, label: str):
        # /rest/api/content/{id}/label survives on Cloud (it was NOT removed in
        # the 2024-25 deprecations; only enumeration endpoints like
        # /rest/api/content and /rest/api/space are 410 Gone on Cloud).
        # No deployment branch needed — both Cloud and DC use the same path.
        return self.req(f"/rest/api/content/{page_id}/label", "POST",
                        [{"prefix": "global", "name": label}])

    # --------------------------------------------------- env-audit surface
    # These methods back the Confluence ENVIRONMENT audit (gather). They keep
    # the (rows|count, err) contract and reduce every response to counts /
    # booleans / TYPES — never identities — so the gather can never leak.

    def _cursor_collect(self, path, params, key="results", cap=20000):
        """Collect cursor-paginated v2 / classic results into one list.

        Returns (rows, err). First-page failure -> ([], ERR..); a mid-loop
        failure -> (partial, ERR..:truncated) so an unreachable side never
        renders as a short clean list. Both the v2 cursor (_links.next) and the
        classic size/start envelopes terminate via the absence of _links.next
        — callers that need start/limit pagination use a dedicated loop."""
        rows: list = []
        st, d = self.req(path, params=params)
        while True:
            if st != 200 or not isinstance(d, dict):
                if not rows:
                    err = d.get("_error", "") if isinstance(d, dict) else ""
                    return [], f"ERR{st}:{str(err)[:60]}"
                return rows, f"ERR{st}:truncated"
            rows += list(d.get(key, []))
            nxt = (d.get("_links") or {}).get("next")
            if not nxt or len(rows) >= cap:
                return rows, None
            st, d = self._follow(nxt)

    def spaces_detailed(self) -> tuple[list, str | None]:
        """Like all_spaces but KEEPS type (global|personal|collaboration),
        status (current|archived) and homepage presence (a boolean derived from
        homepageId / the expanded homepage object — never the homepage id/title).

        Cloud: /api/v2/spaces?limit=250 (cursor). DC:
        /rest/api/space?limit=50&expand=metadata (start/limit). Per-row shape:
        {key, name, id, type, status, has_homepage}."""
        def _row(r: dict) -> dict:
            hp = r.get("homepageId")
            if hp is None:
                hp = r.get("homepage")  # DC expand=homepage shape (object)
            # homepage_id: the content id ONLY (Cloud homepageId string, or the
            # DC homepage object's id). A content id is not a title/name/identity
            # — it is exposed so the gather can count orphaned pages via an
            # `ancestor=` CQL, and the gather uses it TRANSIENTLY and never writes
            # it into the snapshot.
            hid = hp.get("id") if isinstance(hp, dict) else hp
            return {"key": r.get("key"), "name": r.get("name"),
                    "id": r.get("id"), "type": r.get("type"),
                    "status": r.get("status"),
                    "has_homepage": bool(hp),
                    "homepage_id": str(hid) if hid not in (None, "") else None}

        if self.conn.deployment == "cloud":
            rows, err = self._cursor_collect(
                "/api/v2/spaces", {"limit": 250})
            return [_row(r) for r in rows], err
        # DC: classic start/limit envelope.
        out: list = []
        start, limit = 0, 50
        while True:
            st, d = self.req("/rest/api/space",
                             params={"start": start, "limit": limit,
                                     "expand": "metadata,homepage"})
            if st != 200:
                if not out:
                    return [], f"ERR{st}:{str(d.get('_error', ''))[:60]}"
                return out, f"ERR{st}:truncated"
            results = d.get("results", [])
            out += [_row(r) for r in results]
            if len(results) < limit or not (d.get("_links") or {}).get("next"):
                return out, None
            start += len(results)

    def space_permissions(self, space: dict) -> tuple[list, str | None]:
        """Permission TYPES for a space, reduced to principal/operation types
        ONLY (never the principal value/name/id).

        Cloud: /api/v2/spaces/{id}/permissions (cursor) ->
        {principal:{type,id}, operation:{key,targetType}} reduced to
        {principal_type, operation}. DC: /rest/api/space/{key}/permission ->
        a list of {operation, subjects:{user,group}, anonymousAccess}, expanded
        to one reduced row per (operation, principal-type) plus an anonymous
        principal type wherever anonymousAccess is True. (rows, err)."""
        if self.conn.deployment == "cloud":
            sid = space.get("id")
            rows, err = self._cursor_collect(
                f"/api/v2/spaces/{sid}/permissions", {"limit": 250})
            out = []
            for r in rows:
                principal = r.get("principal") or {}
                op = r.get("operation") or {}
                out.append({"principal_type": principal.get("type"),
                            "operation": op.get("key")})
            return out, err
        # DC: classic v1 permission list — a plain array.
        key = space.get("key")
        st, d = self.req(f"/rest/api/space/{key}/permission")
        if st != 200:
            err = d.get("_error", "") if isinstance(d, dict) else ""
            return [], f"ERR{st}:{str(err)[:60]}"
        if not isinstance(d, list):
            return [], None
        out = []
        for grant in d:
            if not isinstance(grant, dict):
                continue
            op = grant.get("operation")
            subjects = grant.get("subjects") or {}
            for ptype in ("user", "group"):
                block = subjects.get(ptype) or {}
                results = block.get("results") or []
                if not results:
                    continue
                row = {"principal_type": ptype, "operation": op}
                if ptype == "group":
                    # Group NAMES are config identifiers (not member identities),
                    # so they are safe to carry — the empty-group cross-reference
                    # needs them to look up member counts. USER subjects stay
                    # reduced to the type only (an accountId/name must never
                    # leak). Cloud's v2 principal exposes only a group id, so this
                    # name capture is a DC-only capability.
                    gnames = [g.get("name") for g in results
                              if isinstance(g, dict) and g.get("name")]
                    if gnames:
                        row["group_names"] = gnames
                out.append(row)
            if grant.get("anonymousAccess"):
                out.append({"principal_type": "anonymous", "operation": op})
        return out, None

    def groups_with_counts(self, cap: int = 60) -> tuple[list, dict, bool, str | None]:
        """All group NAMES + a capped member-count probe.

        Reads /rest/api/group (cursor envelope on both deployments), then for up
        to `cap` groups issues a member probe and stores the count ONLY — never
        a member identity. Returns (names, member_counts, capped, err)."""
        rows, err = self._cursor_collect("/rest/api/group", {"limit": 200})
        names = [g.get("name") for g in rows if g.get("name")]
        capped = len(rows) > cap
        member_counts: dict = {}
        for g in rows[:cap]:
            gname = g.get("name")
            gid = g.get("id")
            if not gname:
                continue
            try:
                cnt = self._group_member_count(gname, gid)
            except Exception:
                cnt = None
            if cnt is not None:
                member_counts[gname] = cnt
        return names, member_counts, capped, err

    def _group_member_count(self, name: str, gid) -> int | None:
        """Member count for one group via a limit=1 probe. v2-style envelopes
        carry no total, so we page only enough to know whether the group is
        empty vs non-empty when a total is absent; when the envelope reports a
        total/size we use it. Reads the COUNT only — never a member identity."""
        # Prefer the by-id endpoint when an id is available (Cloud v2).
        if gid:
            st, d = self.req(f"/rest/api/group/{gid}/membersByGroupId",
                             params={"limit": 1})
            if st != 200:
                st, d = self.req("/rest/api/group/member",
                                 params={"name": name, "limit": 1})
        else:
            st, d = self.req("/rest/api/group/member",
                             params={"name": name, "limit": 1})
        if st != 200 or not isinstance(d, dict):
            return None
        for total_key in ("total", "size", "totalSize"):
            if isinstance(d.get(total_key), int):
                return d[total_key]
        # No total in the envelope: report presence/absence of members so the
        # empty-group check can still fire (1 = at least one, 0 = none).
        return len(d.get("results") or [])

    def global_templates(self) -> tuple[int | None, str | None]:
        """Count of global page templates (/rest/api/template/page).
        (count, err); count None on failure (unevaluable, never a false 0)."""
        return self._count_envelope("/rest/api/template/page")

    def blueprints(self) -> tuple[int | None, str | None]:
        """Count of global blueprint templates (/rest/api/template/blueprint)."""
        return self._count_envelope("/rest/api/template/blueprint")

    def global_labels(self) -> tuple[int | None, str | None]:
        """Count of global labels (/rest/api/label?type=global)."""
        return self._count_envelope("/rest/api/label", {"type": "global"})

    def _count_envelope(self, path, params=None) -> tuple[int | None, str | None]:
        """Collect a cursor/size envelope and return (len, err). On any failure
        the count is None so a caller treats the area as unevaluable."""
        rows, err = self._cursor_collect(path, {**(params or {}), "limit": 200})
        if err and not rows:
            return None, err
        return len(rows), err

    # ----------------------------------------------------- env-fix WRITES
    # The ONLY mutating methods on this client. They back the two app-tier
    # Confluence env-fix kinds (empty_space → archive, confluence_empty_group →
    # delete). Both return (status, payload) like req() and NEVER raise on a
    # 4xx — the apply caller logs the status and decides the verdict. Writes
    # only ever flow through the audited instance (the apply path guards the
    # client's api_base before calling these).

    def archive_space(self, space_key_or_id: str) -> tuple[int, dict | list]:
        """Archive a space (reversible — an archived space can be restored).

        Cloud: PUT /api/v2/spaces/{id} with {"status": "archived"} (v2 is the
        only spaces API on Cloud; the SEGMENT is the numeric space id, not the
        key — the apply path resolves the id from spaces_detailed first).

        DC: PUT /rest/api/space/{key} with the space status set to archived,
        the classic v1 space-update endpoint (DC has no v2 spaces API).

        CAVEAT: Atlassian's v2 space-status update surface has been in flux
        (some tenants expose a dedicated /spaces/{id}/archive action). If a
        tenant rejects the status-update body, the documented archive action
        endpoint is the fallback. We use the status-update form here because it
        is the one shape documented for both deployments and mirrors the
        _FIXES api_hint ("PUT /wiki/api/v2/spaces/{id} status=archived").
        """
        if self.conn.deployment == "cloud":
            return self.req(f"/api/v2/spaces/{space_key_or_id}", method="PUT",
                            body={"status": "archived"})
        # DC: classic v1 space update by KEY.
        return self.req(f"/rest/api/space/{space_key_or_id}", method="PUT",
                        body={"status": "archived"})

    def delete_group(self, group_name: str) -> tuple[int, dict | list]:
        """Delete a directory group by NAME (reversible — a group can be
        recreated and repopulated).

        DELETE /rest/api/group?name=... on both deployments (Cloud serves it
        under the /wiki context path via api_base; DC at the bare root). The
        by-name form is the one documented on both Cloud and DC; the listing
        side (groups_with_counts) reads /rest/api/group too, so the name is the
        stable cross-deployment handle.
        """
        return self.req("/rest/api/group", method="DELETE",
                        params={"name": group_name})
