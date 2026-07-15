"""Product connector registry — the single product-dispatch point.

webapp/stages.py and webapp/main.py must never import jira/confluence modules
directly: they resolve one Connector from `migrations.product` and call
through it. The split of axes is deliberate — the connector is the PRODUCT
axis only; the DEPLOYMENT axis (auth header, api prefix, pagination
envelopes, body dialect) lives inside each product client, so a connector's
callables stay deployment-blind. The one place both axes meet is
supports_blind_spots(): blind-spot detection needs the `insight` issue
counts that only Jira Cloud's /project/search exposes, so the product
capability flag is ANDed with the side's deployment at the call site.

The jira connector wraps the existing modules verbatim; lambdas only adapt
signatures to the cross-product contracts documented on the dataclass.
Unknown products raise ValueError loudly — falling back to jira would run a
jira audit against a non-jira site and produce confidently wrong findings.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote

from . import compare as compare_mod
from . import config_audit as config_mod
from . import extract as extract_mod
from . import permissions as perm_mod
from .client import JiraClient, escape_query_key
from .confluence import compare as conf_compare_mod
from .confluence import extract as conf_extract_mod
from .confluence import macros as conf_macros_mod
from .confluence.client import ConfluenceClient

# Workspace-file contract, re-exported so stages stay product-blind: every
# product's extract callable writes .core.jsonl.gz files stamped with
# EXTRACT_FORMAT, and extract_format() reads the stamp back (1 = unstamped
# legacy, 0 = unreadable). The run engine refuses to reuse cached extracts
# whose stamp is not current.
EXTRACT_FORMAT = extract_mod.EXTRACT_FORMAT
extract_format = extract_mod.extract_format


@dataclass(frozen=True)
class Connector:
    product: str                 # "jira" | "confluence"
    container_label: str         # "project" | "space"
    item_label: str              # "issue" | "page"
    supports_blind_spots: bool   # product capability; AND deployment via supports_blind_spots()
    supports_elevation: bool
    make_client: Callable        # (Connection, http) -> client
    verify: Callable             # (client) -> {"display_name","email","account_id"}
    list_containers: Callable    # (client) -> (rows [{key,name,id}], err)
    count_items: Callable        # (client, key) -> int | "ERR..."
    extract: Callable            # (client, key, out_path, progress) -> {"extracted","approx","verified"}
    compare: Callable            # (key, src_path, tgt_path, cross_dialect=False) -> {"stats","findings"}
    audit_config: Callable       # (src_client, tgt_client, containers, workspace, progress) -> {"areas","findings"}
    browse_url: Callable         # (conn_row_site_url, container_key, item_key) -> str
    detect_blind_spots: Callable | None = None   # (client, keys) -> rows; only when supports_blind_spots


def _jira_verify(client) -> dict:
    # DC /myself carries no accountId (and emailAddress is optional) — .get()
    # keeps the identity shape stable so stages never branch on deployment.
    me = client.myself()
    return {"display_name": me.get("displayName"),
            "email": me.get("emailAddress"),
            "account_id": me.get("accountId")}


def _jira_list_containers(client) -> tuple[list, str | None]:
    # Normalize to the cross-product container shape. Jira-only extras
    # (insight, lead) are NOT forwarded: modules that need them
    # (detect_blind_spots) fetch their own project list.
    rows, err = client.all_projects()
    return ([{"key": p.get("key"), "name": p.get("name"), "id": p.get("id")}
             for p in rows], err)


JIRA = Connector(
    product="jira",
    container_label="project",
    item_label="issue",
    supports_blind_spots=True,
    supports_elevation=True,
    make_client=lambda conn, http: JiraClient(conn, http=http),
    verify=_jira_verify,
    list_containers=_jira_list_containers,
    count_items=lambda c, k: c.approx_count(
        f'project = "{escape_query_key(k)}"'),
    extract=extract_mod.extract_project,
    compare=compare_mod.compare_project,
    # workspace is unused for jira: its config audit reads live admin APIs.
    # Confluence's macro audit reads workspace extracts instead, which is why
    # the cross-product contract carries it.
    audit_config=lambda src, tgt, containers, workspace, progress:
        config_mod.audit_config(src, tgt, jsm_projects=containers,
                                progress=progress),
    detect_blind_spots=perm_mod.detect_blind_spots,
    browse_url=lambda site, container, item:
        f"{site.rstrip('/')}/browse/{item}",
)

def _confluence_browse_url(site: str, container: str, item: str,
                           deployment: str = "cloud") -> str:
    """Page link by space + title. The /wiki context path is a Cloud-only
    convention (DC serves Confluence at whatever root the operator put in
    site_url), so this is the one connector callable that takes the
    deployment — defaulted so the 3-arg cross-product contract still works."""
    prefix = "/wiki" if deployment == "cloud" else ""
    return f"{site.rstrip('/')}{prefix}/display/{container}/{quote(item)}"


CONFLUENCE = Connector(
    product="confluence",
    container_label="space",
    item_label="page",
    # No deployment of Confluence exposes an insight-count analog to verify
    # search totals against, and elevation is a Jira admin-API concept.
    supports_blind_spots=False,
    supports_elevation=False,
    make_client=lambda conn, http: ConfluenceClient(conn, http=http),
    # myself() already returns the normalized identity dict (it owns the
    # Cloud-v1-removal fallback), so verify is a passthrough.
    verify=lambda client: client.myself(),
    # all_spaces() already yields normalized {key,name,id} rows.
    list_containers=lambda client: client.all_spaces(),
    # Migration scope counts pages AND blog posts (the extract covers both).
    count_items=lambda c, k: c.count_content(k),
    extract=conf_extract_mod.extract_space,
    compare=conf_compare_mod.compare_space,
    # The macro inventory reads the run's own workspace extracts — there is
    # no live macro-usage API — so the live clients are unused here.
    audit_config=lambda src, tgt, containers, workspace, progress:
        conf_macros_mod.audit_macros(workspace, containers, progress),
    browse_url=_confluence_browse_url,
)

_REGISTRY: dict[str, Connector] = {"jira": JIRA, "confluence": CONFLUENCE}


def get_connector(product: str) -> Connector:
    try:
        return _REGISTRY[product]
    except KeyError:
        raise ValueError(f"unknown product {product!r}") from None


def known_products() -> tuple[str, ...]:
    """Products the registry can actually serve. The store validates
    migration creation against THIS list, so a migration row can never name
    a product whose every follow-up step (connections, scope, runs) would
    blow up in get_connector. Registering a connector (e.g. confluence in
    Task 13) makes the product creatable with no store change."""
    return tuple(sorted(_REGISTRY))


def supports_blind_spots(connector: Connector, deployment: str) -> bool:
    """Per-SIDE capability check: a jira DC side has no insight counts to
    compare search totals against, so detection is skipped (with a warn
    event) rather than reporting fake zero-blind-spot confidence."""
    return connector.supports_blind_spots and deployment == "cloud"
