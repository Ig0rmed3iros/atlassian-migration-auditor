"""Admin deep-links for findings.

A finding tells an admin *what* is wrong; a deep-link takes them to the exact
admin screen where they fix it. The link is **deployment-aware** because DC and
Cloud expose the same admin function at different paths (e.g. group admin), and
Confluence Cloud serves spaces under a ``/wiki`` context path that DC omits.

The table is keyed by finding kind. Each entry is ``(label, cloud_path,
dc_path)``. A path containing ``{name}`` is *space-scoped*: the finding name is
the object key (e.g. a Confluence space key) and is substituted, URL-encoded.

This module is pure (no I/O) — it only formats a base ``site_url`` plus a known
path, so it can run at render time without touching the network.
"""
from urllib.parse import quote

# kind -> (label, cloud_path, dc_path)
#
# Most Jira admin screens share a path across DC and Cloud (the classic
# /secure/admin/* JSPs still resolve on Cloud). Where Cloud moved a screen to
# the modern /jira/settings/* route, the two columns diverge — that divergence
# is the whole reason the link is deployment-aware.
_LINKS: dict[str, tuple[str, str, str]] = {
    # --- Jira: global admin screens ---
    "unsupported_custom_field_type": (
        "Manage custom fields",
        "/secure/admin/ViewCustomFields.jspa",
        "/secure/admin/ViewCustomFields.jspa"),
    "near_field_limit": (        # matches the kind the check actually emits
        "Manage custom fields",
        "/secure/admin/ViewCustomFields.jspa",
        "/secure/admin/ViewCustomFields.jspa"),
    "near_issue_type_limit": (
        "Manage issue types",
        "/secure/admin/ViewIssueTypes!default.jspa",
        "/secure/admin/ViewIssueTypes!default.jspa"),
    "near_priority_limit": (
        "Manage priorities",
        "/secure/admin/ViewPriorities.jspa",
        "/secure/admin/ViewPriorities.jspa"),
    "near_workflow_limit": (
        "Manage workflows",
        "/secure/admin/workflows/ListWorkflows.jspa",
        "/secure/admin/workflows/ListWorkflows.jspa"),
    # Group admin: Cloud relocated it to /jira/settings/people/groups; DC keeps
    # the classic GroupBrowser. A Cloud link to the DC path 404s and vice-versa.
    "group_name_collision_reserved": (
        "Manage groups",
        "/jira/settings/people/groups",
        "/secure/admin/user/GroupBrowser.jspa"),
    # Apps: the Universal Plugin Manager servlet is the same on both and is the
    # canonical place to assess/uninstall Marketplace apps.
    "apps_to_assess_for_cloud": (
        "Manage apps (UPM)",
        "/plugins/servlet/upm",
        "/plugins/servlet/upm"),
    "script_app_present": (
        "Manage apps (UPM)",
        "/plugins/servlet/upm",
        "/plugins/servlet/upm"),

    # --- Confluence: space-scoped ({name} is the space key) ---
    # Cloud serves spaces under /wiki; DC serves them at the bare root via the
    # /display short-URL. Both land on the space overview where homepage and
    # page-tree problems are visible.
    "space_no_homepage": (
        "Open space",
        "/wiki/spaces/{name}",
        "/display/{name}"),
    "orphaned_pages": (
        "Open space",
        "/wiki/spaces/{name}",
        "/display/{name}"),
}


def deep_link(kind: str, deployment: str | None, site_url: str | None,
              name: str | None = None) -> dict | None:
    """Return ``{"url", "label"}`` for a finding kind, or None when no link
    applies (unknown kind, missing site_url, or a space-scoped link with no
    name). Never raises — a deep-link is a convenience, never load-bearing."""
    entry = _LINKS.get(kind)
    if entry is None or not site_url:
        return None
    label, cloud_path, dc_path = entry
    path = dc_path if deployment == "dc" else cloud_path  # cloud is the default
    if "{name}" in path:
        if not name:
            return None
        path = path.replace("{name}", quote(str(name), safe=""))
    return {"url": site_url.rstrip("/") + path, "label": label}
