"""Jira client used by every audit stage — Cloud and Data Center.

One Connection abstraction, two auth modes and two deployments:
  - pat + cloud: Basic email:api_token against https://<site>.atlassian.net
  - pat + dc:    Bearer api_token (DC PATs are first-class bearer tokens;
                 no email exists server-side) against the bare site_url
  - oauth:       Bearer access token against https://api.atlassian.com/ex/jira/{cloudId}
                 with proactive + on-401 refresh. Atlassian uses ROTATING refresh
                 tokens: every refresh returns a NEW refresh_token which MUST be
                 persisted (on_tokens_refreshed) or the connection dies.

The deployment axis lives entirely in here (auth header, /rest/api/2 vs /3,
pagination envelopes, keyset search) so extract/compare/config callers stay
deployment-blind. BaseClient owns the HTTP plumbing; product clients
(JiraClient here, ConfluenceClient elsewhere) own their REST surface.

Retry posture (ported from the reference pipeline's lib.py): 429 honors
Retry-After (capped 30s); 5xx/transport retried with linear backoff; other
4xx return immediately. `sleeper` is injectable so tests never sleep.
"""
from __future__ import annotations

import base64
import ipaddress
import random
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator
from urllib.parse import urlparse

import httpx


# Instance-metadata hostnames that are NEVER a legitimate Atlassian target but
# would, if reached, return cloud credentials to an attacker who tricked the
# user into a hostile site_url.
_METADATA_HOSTS = frozenset((
    "metadata.google.internal", "metadata.goog", "metadata", "instance-data"))


def _host_to_ip(host: str):
    """The IP a host LITERAL denotes, normalizing every encoding the OS resolver
    accepts (decimal/hex/octal/short-form IPv4 via inet_aton, canonical IPv6),
    or None if the host is a DNS name. ipaddress.ip_address alone only accepts
    canonical dotted-quad, which is exactly how encoded metadata-IP literals
    slipped past the guard while still routing to 169.254.169.254."""
    h = host.strip("[]").split("%")[0]   # drop IPv6 brackets + zone id
    if ":" in h:
        try:
            return ipaddress.ip_address(h)
        except ValueError:
            return None
    try:
        return ipaddress.ip_address(socket.inet_aton(h))
    except (OSError, ValueError):
        return None


def assert_safe_target(site_url: str) -> None:
    """Reject obviously-dangerous request targets before any credential is sent.

    Blocks a non-http(s) scheme, the cloud instance-metadata hostnames (incl. a
    trailing-dot FQDN), and link-local / unspecified addresses — across ALL IPv4
    encodings (decimal/hex/octal/short) and IPv4-mapped IPv6 — since none is ever
    a real Atlassian Cloud or DC target and a misconfigured/hostile site_url
    would otherwise exfiltrate the Bearer PAT to 169.254.169.254. Private
    RFC-1918 / loopback hosts are deliberately ALLOWED: self-hosted Data Center
    legitimately runs on internal addresses. Residual, out of scope: DNS
    rebinding (a NAME that resolves to a blocked IP at connect time)."""
    raw = (site_url or "").strip()
    try:
        parts = urlparse(raw if "://" in raw else "https://" + raw)
        host = (parts.hostname or "").lower()
    except ValueError:
        # e.g. an IPv4 literal wrongly wrapped in brackets -> treat as hostile.
        raise ValueError(f"refusing malformed target URL {site_url!r}")
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"unsupported URL scheme {scheme!r}: site URL must be http/https")
    if not host:
        raise ValueError("site URL has no host")
    if host.rstrip(".") in _METADATA_HOSTS:
        raise ValueError(f"refusing to target instance-metadata host {host!r}")
    ip = _host_to_ip(host)
    if ip is not None:
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped          # ::ffff:169.254.169.254 -> the v4 target
        if ip.is_link_local or ip.is_unspecified:
            raise ValueError(f"refusing to target reserved address {host} ({ip})")

# Back-compat re-exports: adf_text/h16 grew up here, then moved to textnorm
# (which must not import client). Existing `from .client import ...` callers
# keep working unchanged.
from .textnorm import adf_text, h16  # noqa: F401

GATEWAY = "https://api.atlassian.com"

# Matched by _strip_order_by at positions OUTSIDE double-quoted segments
# only — a naive .sub() would truncate a query whose quoted literal contains
# the words (summary ~ "out of order by design").
_ORDER_BY_RE = re.compile(r"\s+order\s+by\s", re.I)


def _strip_order_by(jql: str) -> str:
    """Drop a trailing ORDER BY clause, quote-aware. JQL string literals are
    double-quoted with backslash escapes; the token only counts once the
    scanner is outside every literal."""
    in_quote = False
    i = 0
    while i < len(jql):
        ch = jql[i]
        if ch == '"':
            in_quote = not in_quote
        elif in_quote and ch == "\\":
            i += 1                       # skip the escaped char (e.g. \")
        elif not in_quote and _ORDER_BY_RE.match(jql, i):
            return jql[:i]
        i += 1
    return jql


def escape_query_key(s: str) -> str:
    """Backslash-escape \\ and \" for interpolation into a double-quoted
    JQL/CQL string literal. Container keys are server-derived today, so this
    is defense in depth: a key carrying a quote must never break out of the
    literal and rewrite the query."""
    return (s or "").replace("\\", "\\\\").replace('"', '\\"')


class ClientError(RuntimeError):
    def __init__(self, msg: str, status: int = -1):
        super().__init__(msg)
        self.status = status


@dataclass
class Connection:
    auth_type: str                      # "pat" | "oauth"
    site_url: str
    deployment: str = "cloud"           # "cloud" | "dc"
    email: str | None = None
    api_token: str | None = None
    cloud_id: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    expires_at: float = 0.0
    on_tokens_refreshed: Callable[["Connection"], None] | None = field(
        default=None, repr=False, compare=False)
    refresh_fn: Callable[[str], dict] | None = field(
        default=None, repr=False, compare=False)

    @property
    def api_base(self) -> str:
        if self.auth_type == "oauth":
            return f"{GATEWAY}/ex/jira/{self.cloud_id}"
        return self.site_url.rstrip("/")

    def browse_url(self, key: str) -> str:
        return f"{self.site_url.rstrip('/')}/browse/{key}"


class BaseClient:
    """HTTP plumbing shared by every product client: auth, retry, refresh,
    startAt pagination. Knows deployments, knows no product endpoints."""

    def __init__(self, conn: Connection, http: httpx.Client | None = None,
                 sleeper: Callable[[float], None] = time.sleep):
        # SSRF guard: refuse to build a client (and thus ever send the PAT) for a
        # link-local/metadata target. Runs once per client, before any request.
        assert_safe_target(conn.site_url)
        self.conn = conn
        self.http = http or httpx.Client(timeout=60.0)
        self.sleep = sleeper

    @property
    def api_base(self) -> str:
        """Client-level override point for the request base URL. Jira uses
        the Connection's base verbatim; Confluence Cloud must append /wiki
        (a product detail the deployment-agnostic Connection can't know)."""
        return self.conn.api_base

    # ---------------------------------------------------------------- auth
    def _refresh(self) -> bool:
        c = self.conn
        if c.auth_type != "oauth" or not c.refresh_fn or not c.refresh_token:
            return False
        tok = c.refresh_fn(c.refresh_token)
        c.access_token = tok["access_token"]
        # Rotating refresh tokens: persist the NEW one every time.
        c.refresh_token = tok.get("refresh_token", c.refresh_token)
        c.expires_at = time.time() + float(tok.get("expires_in", 3600))
        if c.on_tokens_refreshed:
            c.on_tokens_refreshed(c)
        return True

    def _refresh_safe(self) -> bool:
        """Wrap _refresh() so any exception degrades to the normal 401 path."""
        try:
            return self._refresh()
        except Exception:
            return False

    def _auth_header(self) -> str:
        """Pure header builder — no side effects."""
        c = self.conn
        if c.auth_type == "oauth":
            return f"Bearer {c.access_token}"
        if c.deployment == "dc":
            return f"Bearer {c.api_token}"
        raw = f"{c.email}:{c.api_token}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    # ----------------------------------------------------------------- req
    def req(self, path: str, method: str = "GET", body=None, params=None,
            tries: int = 6, headers: dict | None = None,
            retry_safe: bool = False) -> tuple[int, dict | list]:
        url = self.api_base + path
        refreshed_once = False
        last_status, last_err = -1, "exhausted retries"
        oauth = self.conn.auth_type == "oauth"
        # A 5xx / transport error after a NON-idempotent write (POST) may mean the
        # write actually landed but the response was lost — retrying risks a
        # duplicate create/action, so those are NOT retried (fail loud instead).
        # 429 is always retried: the request was rate-limited, never processed.
        # retry_safe lets a READ that is POST-only by API shape (JQL search /
        # approximate-count carry the query in the body) opt back into retries.
        idempotent = retry_safe or method.upper() in (
            "GET", "HEAD", "PUT", "DELETE", "OPTIONS")
        for attempt in range(tries):
            # Proactive refresh: at most once per req() call, never propagates.
            if oauth and not refreshed_once and self.conn.expires_at \
                    and time.time() > self.conn.expires_at - 60:
                refreshed_once = True
                self._refresh_safe()
            hdrs = {"Authorization": self._auth_header(),
                    "Accept": "application/json"}
            if headers:
                hdrs.update(headers)
            try:
                resp = self.http.request(method, url, params=params,
                                         json=body, headers=hdrs)
            except httpx.HTTPError as ex:
                last_status, last_err = -1, str(ex)
                if not idempotent:
                    return -1, {"_error": f"{last_err} (write not retried — "
                                          f"it may have already applied)"}
                self.sleep(3 * (attempt + 1) + random.uniform(0, 1.0))
                continue
            if resp.status_code == 429:
                try:
                    wait = int(resp.headers.get("Retry-After", "5"))
                except ValueError:
                    wait = 5
                self.sleep(min(wait + 1, 30))
                continue
            if resp.status_code == 401 and oauth and not refreshed_once:
                refreshed_once = True
                if self._refresh_safe():
                    continue
            if resp.status_code in (500, 502, 503, 504):
                last_status, last_err = resp.status_code, resp.text[:400]
                if not idempotent:
                    return resp.status_code, {"_error": resp.text[:400]}
                self.sleep(3 * (attempt + 1) + random.uniform(0, 1.0))
                continue
            if resp.status_code >= 400:
                return resp.status_code, {"_error": resp.text[:400]}
            if not resp.content or not resp.content.strip():
                return resp.status_code, {}
            return resp.status_code, resp.json()
        return last_status, {"_error": last_err}

    # ----------------------------------------------------------- paginators
    @staticmethod
    def _page_done(d: dict, fetched: int) -> bool:
        """Termination ladder: isLast is authoritative when present (the
        server-reported total can be stale/under-read); DC PageBean envelopes
        omit isLast, so fall back to total; a wrapper with NEITHER (e.g.
        /permissionscheme's {permissionSchemes:[...]}) is an unpaginated
        single page — re-requesting it only accumulates duplicate rows."""
        if "isLast" in d:
            return bool(d["isLast"])
        if "total" in d:
            return fetched >= int(d.get("total") or 0)
        return True

    def paginate_start_at(self, path: str, params=None, key=None,
                          cap: int = 20000) -> tuple[list, str | None]:
        st, d = self.req(path, params={**(params or {}),
                                       "startAt": 0, "maxResults": 50})
        if st != 200:
            return [], f"ERR{st}:{str(d.get('_error', ''))[:60]}"
        if isinstance(d, list):
            return d, None
        arrkey = key or ("values" if "values" in d else next(
            (k for k in ("permissionSchemes", "notificationSchemes") if k in d),
            None))
        if arrkey is None:
            return [], None
        out = list(d.get(arrkey, []))
        start = len(out)
        while not self._page_done(d, start) and start < cap and d.get(arrkey):
            st, d = self.req(path, params={**(params or {}),
                                           "startAt": start, "maxResults": 50})
            if st != 200:
                # Mid-loop failure: never silently truncate — fail loud so an
                # unreachable side can't be rendered as a short/clean list.
                return out, f"ERR{st}:truncated"
            chunk = d.get(arrkey, [])
            out += chunk
            start += len(chunk)
            if not chunk:
                break
        return out, None


class JiraClient(BaseClient):
    """Jira REST surface: v3 + cursor search on Cloud, v2 + keyset search
    on Data Center. Same public methods either way."""

    @property
    def api_prefix(self) -> str:
        return "/rest/api/2" if self.conn.deployment == "dc" else "/rest/api/3"

    # ----------------------------------------------------------- paginators
    def search_jql(self, jql: str, fields: list[str], expand=None,
                   page: int = 100) -> Iterator[dict]:
        if self.conn.deployment == "dc":
            yield from self._search_dc(jql, fields, expand, page)
            return
        token = None
        while True:
            body = {"jql": jql, "maxResults": page, "fields": fields}
            if expand:
                body["expand"] = expand
            if token:
                body["nextPageToken"] = token
            st, d = self.req("/rest/api/3/search/jql", "POST", body,
                             retry_safe=True)   # read: safe to retry on a blip
            if st != 200:
                raise ClientError(f"search/jql {st}: {d.get('_error', '')}", st)
            yield from d.get("issues", [])
            token = d.get("nextPageToken")
            if d.get("isLast") or not token:
                break

    def _search_dc(self, jql: str, fields: list[str], expand,
                   page: int) -> Iterator[dict]:
        """Keyset pagination by id. DC 10/11 with the OpenSearch backend
        enforces a 10,000-result window — startAt past it fails with HTTP 500
        'Search limit exceeded' (Atlassian KB workaround: AND id > lastId).
        Emission order differs from the caller's ORDER BY, which is fine:
        compare loads extracts into dicts, so order is irrelevant."""
        bare = _strip_order_by(jql).strip()
        last_id = None
        while True:
            clause = f" AND id > {last_id}" if last_id is not None else ""
            params = {"jql": f"({bare}){clause} ORDER BY id ASC",
                      "startAt": 0, "maxResults": page,
                      "fields": ",".join(fields)}
            if expand:
                params["expand"] = expand if isinstance(expand, str) \
                    else ",".join(expand)
            st, d = self.req(f"{self.api_prefix}/search", params=params)
            if st != 200:
                raise ClientError(f"search {st}: {d.get('_error', '')}", st)
            issues = d.get("issues", [])
            if not issues:
                break
            yield from issues
            page_max = max(int(i["id"]) for i in issues)
            if last_id is not None and page_max <= last_id:
                # A backend that ignores the keyset clause re-serves the same
                # page forever: abort loudly — the count-verification gate
                # would catch truncation, but a spin never returns to it.
                raise ClientError(
                    f"keyset search did not advance past id {last_id} "
                    f"(page max {page_max}) — aborting instead of looping")
            last_id = page_max

    def sd_list(self, path: str) -> list:
        """Servicedeskapi start/limit pagination.

        Raises ClientError on any failure — a JSM outage must never read as
        an empty queue list. X-ExperimentalApi is sent unconditionally: JSM DC
        5.x queue endpoints 403 without it (value is the string "true"), and
        graduated/Cloud endpoints ignore it.
        """
        out, start = [], 0
        while True:
            st, d = self.req(path, params={"start": start, "limit": 50},
                             headers={"X-ExperimentalApi": "true"})
            if st != 200:
                raise ClientError(
                    f"servicedeskapi {st}: {d.get('_error', '')}", st)
            vals = d.get("values", [])
            out += vals
            if d.get("isLastPage", True) or not vals:
                break
            start += len(vals)
            if start > 50000:
                break
        return out

    # ----------------------------------------------------------- shortcuts
    def approx_count(self, jql: str):
        if self.conn.deployment == "dc":
            # Exact under Lucene; OpenSearch-backed instances MAY cap counts
            # above 10k — acceptable: a capped count fails the extraction-
            # verification gate loudly rather than silently.
            st, d = self.req(f"{self.api_prefix}/search",
                             params={"jql": jql, "maxResults": 0})
            return d.get("total") if st == 200 else f"ERR{st}"
        st, d = self.req("/rest/api/3/search/approximate-count", "POST",
                         {"jql": jql}, retry_safe=True)   # read: safe to retry
        return d.get("count") if st == 200 else f"ERR{st}"

    def all_projects(self) -> tuple[list, str | None]:
        if self.conn.deployment == "dc":
            # DC /project is a plain unpaginated array (no `insight` expand).
            st, d = self.req(f"{self.api_prefix}/project",
                             params={"expand": "description,lead"})
            if st != 200:
                return [], f"ERR{st}:{str(d.get('_error', ''))[:60]}"
            return list(d), None
        return self.paginate_start_at(
            "/rest/api/3/project/search",
            params={"expand": "description,lead,insight"})

    def myself(self) -> dict:
        # Works on both deployments; DC returns no accountId — callers .get().
        st, d = self.req(f"{self.api_prefix}/myself")
        if st != 200:
            raise ClientError(f"/myself failed: {st} {d.get('_error', '')}", st)
        return d

    def installed_plugins(self) -> tuple[list, str | None]:
        """UPM installed-app list (Data Center / Server). GET /rest/plugins/1.0/
        -> plugins[] with key/userInstalled/enabled. Cloud has no equivalent UPM
        endpoint (the env gather DC-gates this). Returns (plugins, error); the
        gather reduces these to counts/booleans — no app-key list is stored."""
        st, d = self.req("/rest/plugins/1.0/")
        if st != 200 or not isinstance(d, dict):
            return [], f"ERR{st}"
        plugins = d.get("plugins")
        return (plugins if isinstance(plugins, list) else []), None

    # --------------------------------------------------------- target writes
    # All write methods below POST to /rest/api/3 (Cloud target) directly —
    # never through api_prefix — because the applier always runs against a
    # Cloud target, never a DC source.

    def create_field(self, name: str, ftype: str, searcher: str | None = None):
        body = {"name": name, "type": ftype}
        if searcher:
            body["searcherKey"] = searcher
        return self.req("/rest/api/3/field", "POST", body)

    def create_field_context(self, field_id: str, name: str,
                             project_ids=None, issue_type_ids=None):
        body = {"name": name}
        if project_ids:
            body["projectIds"] = project_ids
        if issue_type_ids:
            body["issueTypeIds"] = issue_type_ids
        return self.req(f"/rest/api/3/field/{field_id}/context", "POST", body)

    def add_field_options(self, field_id: str, context_id: str, values: list[str]):
        body = {"options": [{"value": v, "disabled": False} for v in values]}
        return self.req(
            f"/rest/api/3/field/{field_id}/context/{context_id}/option", "POST", body)

    def add_field_to_screen(self, screen_id: str, tab_id: str, field_id: str):
        return self.req(
            f"/rest/api/3/screens/{screen_id}/tabs/{tab_id}/fields", "POST",
            {"fieldId": field_id})

    def create_status(self, name: str, category: str, description: str = ""):
        # category ∈ {"TODO","IN_PROGRESS","DONE"} (statusCategory key).
        body = {"scope": {"type": "GLOBAL"},
                "statuses": [{"name": name, "statusCategory": category,
                              "description": description}]}
        return self.req("/rest/api/3/statuses", "POST", body)

    def create_priority(self, name: str, description: str = ""):
        return self.req("/rest/api/3/priority", "POST",
                        {"name": name, "description": description})

    def create_resolution(self, name: str, description: str = ""):
        return self.req("/rest/api/3/resolution", "POST",
                        {"name": name, "description": description})

    def create_issue_type(self, name: str, description: str = "",
                          hierarchy_level: int = 0):
        body = {"name": name, "description": description,
                "type": "subtask" if hierarchy_level < 0 else "standard"}
        return self.req("/rest/api/3/issuetype", "POST", body)

    def create_link_type(self, name: str, inward: str, outward: str):
        return self.req("/rest/api/3/issueLinkType", "POST",
                        {"name": name, "inward": inward, "outward": outward})

    def create_screen(self, name: str, description: str = ""):
        return self.req("/rest/api/3/screens", "POST",
                        {"name": name, "description": description})

    def add_screen_tab(self, screen_id: str, name: str):
        return self.req(f"/rest/api/3/screens/{screen_id}/tabs", "POST",
                        {"name": name})

    def set_issue_fields(self, issue_key: str, fields: dict, notify: bool = False):
        # notify=False suppresses the migration-noise email storm.
        return self.req(
            f"/rest/api/3/issue/{issue_key}", "PUT",
            {"fields": fields}, params={"notifyUsers": str(notify).lower()})

    def get_workflow(self, name: str):
        return self.req("/rest/api/3/workflows", "POST",
                        {"workflowNames": [name]},
                        params={"expand": "transitions,statuses"})

    def update_workflow(self, payload: dict):
        # The high-risk path: wiring a status+transition into an EXISTING workflow.
        return self.req("/rest/api/3/workflows/update", "POST", payload)

    # ----------------------------------------------------------- target deletes
    # App-tier env-fix cleanup deletes. Each returns (status, body) and NEVER
    # raises on a 4xx (req() already returns 4xx bodies verbatim) so the applier
    # can branch on the status and log it. All target the Cloud /rest/api/3
    # surface directly — the env-fix applier only ever runs against Cloud.

    def delete_screen(self, screen_id):
        return self.req(f"/rest/api/3/screens/{screen_id}", "DELETE")

    def delete_workflow(self, entity_id):
        # Cloud workflow delete is keyed by the workflow entityId (a UUID), not
        # the display name. The applier resolves the entityId from the live
        # /workflow/search row's id.entityId before calling this.
        return self.req(f"/rest/api/3/workflow/{entity_id}", "DELETE")

    def delete_field(self, field_id):
        # field_id is the customfield_NNNNN id. Deleting a custom field is
        # irreversible and destroys every stored value, so the applier only ever
        # calls this after the on-no-screen + zero-values preconditions hold.
        return self.req(f"/rest/api/3/field/{field_id}", "DELETE")

    def delete_project(self, project_key):
        # Cloud moves the project to the trash (recoverable ~60 days); it is not
        # a hard delete. The applier only calls this for a verified-empty project.
        return self.req(f"/rest/api/3/project/{project_key}", "DELETE")

    def delete_status(self, status_id):
        # Cloud status delete is keyed by id via the ?id= query param on the bulk
        # /statuses endpoint (the param repeats for a multi-delete; we delete one
        # at a time). The applier only calls this after the status is confirmed
        # to be in NO workflow AND to hold zero issues, and never for a built-in
        # status. Deleting a status that holds issues would lose their state.
        return self.req("/rest/api/3/statuses", "DELETE",
                        params={"id": status_id})

    # ----------------------------------------------------- access (groups/roles)
    def search_users(self, query: str, max_results: int = 10):
        st, d = self.req("/rest/api/3/user/search",
                         params={"query": query, "maxResults": max_results})
        if st != 200 or not isinstance(d, list):
            return [], (d.get("_error") if isinstance(d, dict) else f"status {st}")
        return d, None

    def user_groups(self, account_id: str):
        st, d = self.req("/rest/api/3/user/groups",
                         params={"accountId": account_id})
        if st != 200 or not isinstance(d, list):
            return [], (d.get("_error") if isinstance(d, dict) else f"status {st}")
        return d, None

    def add_user_to_group(self, group_id: str, account_id: str):
        return self.req("/rest/api/3/group/user", "POST",
                        {"accountId": account_id}, params={"groupId": group_id})

    def project_role_map(self, project_key: str):
        st, d = self.req(f"/rest/api/3/project/{project_key}/role")
        if st != 200 or not isinstance(d, dict):
            return {}, (d.get("_error") if isinstance(d, dict) else f"status {st}")
        # value is the role URL; the id is its last path segment.
        return {name: url.rstrip("/").rsplit("/", 1)[-1]
                for name, url in d.items()}, None

    def project_role_actors(self, project_key: str, role_id: str):
        st, d = self.req(f"/rest/api/3/project/{project_key}/role/{role_id}")
        if st != 200 or not isinstance(d, dict):
            return [], (d.get("_error") if isinstance(d, dict) else f"status {st}")
        return d.get("actors", []), None

    def add_user_to_project_role(self, project_key: str, role_id: str,
                                 account_id: str):
        return self.req(f"/rest/api/3/project/{project_key}/role/{role_id}",
                        "POST", {"user": [account_id]})
