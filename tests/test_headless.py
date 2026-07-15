"""Headless env-audit runner: drives the full pipeline against a live client
with no web server / DB / AI, returning a JSON-serializable gate result."""
import json

import httpx

from auditor.client import Connection, JiraClient
from auditor.connectors import get_connector
from auditor.envaudit.headless import run_env_audit, VERDICT_RANK


def _benign(req):
    # Every envelope empty/benign so the whole gather completes without error.
    return httpx.Response(200, json={"values": [], "results": [], "total": 0,
                                     "isLast": True, "count": 0})


def _jira_client(handler):
    conn = Connection(auth_type="pat", site_url="https://s.atlassian.net",
                      email="e", api_token="t")
    return JiraClient(
        conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda s: None)


def test_run_env_audit_completes_and_shapes_result():
    result = run_env_audit(_jira_client(_benign), get_connector("jira"))
    assert result["product"] == "jira"
    assert result["verdict"] in VERDICT_RANK
    assert isinstance(result["finding_total"], int)
    assert isinstance(result["findings"], list)
    assert result["grade"] and result["health_score"] is not None
    # The whole result must be JSON-serializable (it is the CLI's output).
    json.dumps(result, default=str)


def test_run_env_audit_surfaces_a_progress_trail():
    seen = []
    run_env_audit(_jira_client(_benign), get_connector("jira"),
                  progress=seen.append)
    assert any("scope" in m for m in seen) and any("checks" in m for m in seen)


def test_verdict_rank_orders_worst_last():
    assert VERDICT_RANK["CRITICAL"] > VERDICT_RANK["NEEDS_ATTENTION"] \
        > VERDICT_RANK["HEALTHY_WITH_NOTES"] > VERDICT_RANK["HEALTHY"]


# --- audit CLI control flow (exit codes, token, json) -----------------------
import argparse
from webapp.main import run_audit_cli

_RESULT = {"product": "jira", "deployment": "cloud", "verdict": "HEALTHY",
           "health_score": 95, "grade": "A", "finding_total": 0,
           "severity_counts": {"high": 0, "medium": 0, "low": 0},
           "capability_gaps": 0, "headlines": ["No configuration issues detected."],
           "findings": []}


def _args(**kw):
    base = dict(command="audit", product="jira", site="https://s.atlassian.net",
                deployment="cloud", email="e@acme.example", json=None,
                fail_on="CRITICAL")
    base.update(kw)
    return argparse.Namespace(**base)


def _stub_run(monkeypatch, verdict="HEALTHY"):
    r = dict(_RESULT, verdict=verdict)
    monkeypatch.setattr("auditor.envaudit.headless.run_env_audit",
                        lambda *a, **k: r)


def test_audit_cli_missing_token_returns_1(monkeypatch):
    monkeypatch.delenv("MA_AUDIT_TOKEN", raising=False)
    assert run_audit_cli(_args()) == 1


def test_audit_cli_healthy_returns_0(monkeypatch):
    monkeypatch.setenv("MA_AUDIT_TOKEN", "tok")
    _stub_run(monkeypatch, "HEALTHY")
    assert run_audit_cli(_args()) == 0


def test_audit_cli_critical_gates_to_2(monkeypatch):
    monkeypatch.setenv("MA_AUDIT_TOKEN", "tok")
    _stub_run(monkeypatch, "CRITICAL")
    assert run_audit_cli(_args(fail_on="CRITICAL")) == 2


def test_audit_cli_fail_on_threshold_respected(monkeypatch):
    # NEEDS_ATTENTION passes the default CRITICAL gate (0) but fails a stricter one.
    monkeypatch.setenv("MA_AUDIT_TOKEN", "tok")
    _stub_run(monkeypatch, "NEEDS_ATTENTION")
    assert run_audit_cli(_args(fail_on="CRITICAL")) == 0
    assert run_audit_cli(_args(fail_on="NEEDS_ATTENTION")) == 2


def test_audit_cli_writes_json(monkeypatch, tmp_path):
    monkeypatch.setenv("MA_AUDIT_TOKEN", "tok")
    _stub_run(monkeypatch, "HEALTHY")
    dest = str(tmp_path / "r.json")
    assert run_audit_cli(_args(json=dest)) == 0
    assert json.load(open(dest))["verdict"] == "HEALTHY"


def test_audit_cli_cloud_requires_email(monkeypatch):
    monkeypatch.setenv("MA_AUDIT_TOKEN", "tok")
    assert run_audit_cli(_args(deployment="cloud", email=None)) == 1


def test_audit_cli_dc_needs_no_email(monkeypatch):
    monkeypatch.setenv("MA_AUDIT_TOKEN", "tok")
    _stub_run(monkeypatch, "HEALTHY")
    assert run_audit_cli(_args(deployment="dc", email=None)) == 0


def test_run_env_audit_raises_on_enumeration_failure():
    # An auth/connection failure surfaces as RuntimeError (the CLI maps it to
    # exit 1, never a silent clean audit).
    import pytest
    client = _jira_client(lambda r: httpx.Response(401, text="nope"))
    with pytest.raises(RuntimeError):
        run_env_audit(client, get_connector("jira"))


def test_run_env_audit_confluence_product():
    conn = Connection(auth_type="pat", site_url="https://acme.atlassian.net",
                      email="e", api_token="t")
    from auditor.confluence.client import ConfluenceClient
    client = ConfluenceClient(
        conn, http=httpx.Client(transport=httpx.MockTransport(_benign)),
        sleeper=lambda s: None)
    result = run_env_audit(client, get_connector("confluence"))
    assert result["product"] == "confluence"
    assert result["verdict"] in VERDICT_RANK
    json.dumps(result, default=str)
