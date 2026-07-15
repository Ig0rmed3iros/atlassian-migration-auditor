"""Admin deep-links: per-finding-kind, deployment-aware URLs that take an
admin straight to the screen where the issue is fixed."""
from auditor.envaudit.deeplinks import deep_link


SITE = "https://acme.atlassian.net"


def test_jira_custom_field_link_same_on_both_deployments():
    cloud = deep_link("unsupported_custom_field_type", "cloud", SITE)
    dc = deep_link("unsupported_custom_field_type", "dc", SITE)
    assert cloud["url"] == SITE + "/secure/admin/ViewCustomFields.jspa"
    assert dc["url"] == cloud["url"]
    assert cloud["label"]


def test_near_field_limit_has_a_deep_link():
    # Review (low): the deep-link key was 'near_custom_field_limit' but the check
    # emits 'near_field_limit', so the finding never got an admin link.
    link = deep_link("near_field_limit", "cloud", SITE)
    assert link and link["url"] == SITE + "/secure/admin/ViewCustomFields.jspa"


def test_jira_groups_link_differs_by_deployment():
    """Cloud and DC expose group admin at different paths — the link must
    respect the deployment so it doesn't 404."""
    cloud = deep_link("group_name_collision_reserved", "cloud", SITE)
    dc = deep_link("group_name_collision_reserved", "dc", SITE)
    assert cloud["url"] == SITE + "/jira/settings/people/groups"
    assert dc["url"] == SITE + "/secure/admin/user/GroupBrowser.jspa"


def test_apps_link_points_at_upm():
    link = deep_link("apps_to_assess_for_cloud", "dc", SITE)
    assert link["url"] == SITE + "/plugins/servlet/upm"


def test_confluence_space_link_uses_wiki_prefix_on_cloud():
    """Confluence Cloud serves spaces under /wiki; DC serves them at the bare
    root. The finding name is the space key and must be URL-encoded."""
    cloud = deep_link("space_no_homepage", "cloud", SITE, name="DEV TEAM")
    dc = deep_link("space_no_homepage", "dc", SITE, name="DEV TEAM")
    assert cloud["url"] == SITE + "/wiki/spaces/DEV%20TEAM"
    assert dc["url"] == SITE + "/display/DEV%20TEAM"


def test_space_scoped_link_needs_a_name():
    """A space-scoped template with no name can't build a useful link — return
    None rather than a link to a malformed path."""
    assert deep_link("orphaned_pages", "cloud", SITE, name=None) is None
    assert deep_link("orphaned_pages", "cloud", SITE, name="DEV")["url"] == \
        SITE + "/wiki/spaces/DEV"


def test_unknown_kind_returns_none():
    assert deep_link("no_such_kind", "cloud", SITE) is None


def test_missing_site_url_returns_none():
    assert deep_link("apps_to_assess_for_cloud", "cloud", "") is None
    assert deep_link("apps_to_assess_for_cloud", "cloud", None) is None


def test_unknown_deployment_falls_back_to_cloud_path():
    """deployment None/unknown shouldn't crash — fall back to the Cloud path."""
    link = deep_link("near_workflow_limit", None, SITE)
    assert link["url"] == SITE + "/secure/admin/workflows/ListWorkflows.jspa"
