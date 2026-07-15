import pytest
from auditor.connectors import (
    Connector, get_connector, known_products, supports_blind_spots,
)


class FakeJiraClient:
    """Stubs only what Connector.verify touches — no HTTP, no Connection."""

    def myself(self):
        return {"displayName": "Igor Medeiros",
                "emailAddress": "igor@acme.example",
                "accountId": "5d2e-acme-001"}


class FakeConfluenceClient:
    """ConfluenceClient.myself() already returns the normalized identity
    shape (it does the DC/Cloud branching internally), so the connector's
    verify must be a passthrough — no double-shaping."""

    def myself(self):
        return {"display_name": "Igor Medeiros", "email": None,
                "account_id": None}


def test_get_connector_jira_and_unknown():
    c = get_connector("jira")
    assert isinstance(c, Connector)
    assert c.product == "jira"
    assert c.container_label == "project"
    assert c.item_label == "issue"
    assert c.supports_elevation is True
    assert c.browse_url("https://acme.atlassian.net/", "AC", "AC-1") == \
        "https://acme.atlassian.net/browse/AC-1"
    # An unknown product must fail loudly, not fall back to jira.
    with pytest.raises(ValueError, match="bamboo"):
        get_connector("bamboo")


def test_confluence_connector_registered():
    c = get_connector("confluence")
    assert isinstance(c, Connector)
    assert c.product == "confluence"
    assert c.container_label == "space"
    assert c.item_label == "page"
    assert c.supports_elevation is False
    assert c.detect_blind_spots is None
    # Registration is what makes the product creatable (store validates
    # against known_products), so this assertion guards the whole flow.
    assert "confluence" in known_products()
    assert c.verify(FakeConfluenceClient()) == {
        "display_name": "Igor Medeiros", "email": None, "account_id": None}
    # The /wiki context path is a Cloud-only convention; DC serves Confluence
    # at the site root. Titles are URL-quoted.
    assert c.browse_url("https://acme.atlassian.net/", "ENG", "Acme Home") == \
        "https://acme.atlassian.net/wiki/display/ENG/Acme%20Home"
    assert c.browse_url("https://wiki.acme.example", "ENG", "Acme Home",
                        deployment="dc") == \
        "https://wiki.acme.example/display/ENG/Acme%20Home"


def test_jira_connector_verify_shapes_identity():
    me = get_connector("jira").verify(FakeJiraClient())
    assert me == {"display_name": "Igor Medeiros",
                  "email": "igor@acme.example",
                  "account_id": "5d2e-acme-001"}


def test_supports_blind_spots_matrix():
    jira = get_connector("jira")
    assert supports_blind_spots(jira, "cloud") is True
    assert supports_blind_spots(jira, "dc") is False
    # Confluence has no insight-count analog on ANY deployment: the product
    # flag is False, so the deployment AND can never flip it on.
    conf = get_connector("confluence")
    assert supports_blind_spots(conf, "cloud") is False
    assert supports_blind_spots(conf, "dc") is False


def test_jira_count_items_escapes_container_key():
    """Container keys are server-derived today, but a key carrying a double
    quote must never break out of the JQL literal (defense in depth)."""
    seen = {}

    class FakeJira:
        def approx_count(self, jql):
            seen["jql"] = jql
            return 0

    get_connector("jira").count_items(FakeJira(), 'AC" OR project != "X')
    assert seen["jql"] == 'project = "AC\\" OR project != \\"X"'
