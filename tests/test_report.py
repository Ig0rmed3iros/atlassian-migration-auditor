"""Executive-summary PDF/HTML export for an environment audit.

A one-click, human-friendly report (verdict + grade + KPIs + AI executive summary
+ prioritized remediation roadmap + findings-by-problem-type + what-the-rules-miss
gaps), rendered server-side to PDF via WeasyPrint. Local-only: generated on the
box and downloaded by the operator, never transmitted.
"""
from __future__ import annotations

import copy

import httpx
import pytest
from fastapi.testclient import TestClient

# WeasyPrint is an OPTIONAL extra ([pdf]) needing system libs (pango/cairo), so
# it is absent in the minimal CI install. Tests that render a real PDF call
# pytest.importorskip("weasyprint") to skip there (they run locally where it is
# installed); the HTML report + graceful-503 paths run unconditionally.

from webapp.config import Config
from webapp.main import create_app
from webapp.report import build_report_context


def _app(tmp_path):
    cfg = Config(data_dir=str(tmp_path), bind_host="127.0.0.1", bind_port=8486,
                 public_base_url="http://localhost:8486", secret_key=None)
    return create_app(cfg, http=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(404))))


def _env_finding(kind, name, area="schemes", severity="low"):
    from auditor.envaudit.fixes import _FIXES, category_for
    fix = copy.copy(_FIXES.get(kind, {"tier": "human", "title": kind}))
    return {"area": area, "name": name, "kind": kind, "severity": severity,
            "detail": {"fix": fix, "category": category_for(kind),
                       "severity": severity}}


def _seed(tmp_path):
    app = _app(tmp_path)
    store = app.state.store
    mid = store.create_migration("Prod Jira", product="jira",
                                 audit_type="environment")
    run = store.create_run(mid, {}, kind="env_audit")
    findings = [
        _env_finding("scheme_unused", "S1"),
        _env_finding("scheme_unused", "S2"),
        _env_finding("component_no_lead", "C1", "components", "medium"),
        _env_finding("public_shared_dashboard", "D1", "dashboards", "high"),
    ]
    store.insert_findings_config(run, findings)
    store.update_run(run, status="done", verdict="CRITICAL", stats={
        "grade": "D", "health_score": 58, "findings": 4,
        "high": 1, "medium": 1, "low": 2,
        "headlines": ["1 high-severity issue needs attention."],
        "ai": {
            "skipped": False,
            "summary": "One unfinished migration is the common root cause.",
            "roadmap": [{"step": "Archive the dormant projects", "effort": "L",
                         "rationale": "most findings live inside them and "
                                      "collapse together"}],
            "top_risks": ["A public dashboard owned by a deactivated user."],
            "gaps": ["No automation/SLA reference map exists."],
            "themes": ["Migration debris across every layer"],
            "ai_findings": [{"title": "Unfinished migration root cause",
                             "severity": "high", "area": "multiple",
                             "priority": 1,
                             "root_cause": "An additive JCMA lift-and-shift "
                                           "never reconciled.",
                             "risk": "Reporting and SLA corruption.",
                             "remediation_steps": ["Stand up a cleanup workstream"]}],
            "model": "claude-cli"}})
    return app, store, mid, run


# ---------------------------------------------------------------------------
# build_report_context
# ---------------------------------------------------------------------------

def test_env_report_surfaces_ai_advisory_when_it_diverges(tmp_path):
    # No-bias review (Phase 2 P5): ai_health_score/ai_grade were computed and
    # shipped in the API but never displayed. Surface the AI's INDEPENDENT read
    # as an advisory cross-check when it diverges from the deterministic grade.
    import json
    app, store, mid, run = _seed(tmp_path)        # deterministic grade D / 58
    stats = json.loads(store.get_run(run)["stats_json"])
    stats["ai_grade"] = "B"; stats["ai_health_score"] = 80
    store.update_run(run, stats=stats)
    ctx = build_report_context(store, run)
    assert ctx["ai_grade"] == "B" and ctx["ai_health_score"] == 80
    t = TestClient(app).get(f"/runs/{run}/report").text
    assert "AI's read" in t


def test_build_report_context_assembles_executive_summary(tmp_path):
    app, store, mid, run = _seed(tmp_path)
    ctx = build_report_context(store, run)
    assert ctx["env_name"] == "Prod Jira"
    assert ctx["verdict"] == "CRITICAL"
    assert ctx["grade"] == "D" and ctx["health_score"] == 58
    assert ctx["total_findings"] == 4
    assert ctx["ai"]["summary"].startswith("One unfinished migration")
    assert ctx["ai"]["roadmap"][0]["step"] == "Archive the dormant projects"
    assert ctx["ai"]["gaps"] == ["No automation/SLA reference map exists."]
    # findings grouped by problem type, worst-severity first
    groups = {g["kind"]: g for g in ctx["finding_groups"]}
    assert groups["scheme_unused"]["count"] == 2
    assert ctx["finding_groups"][0]["severity"] == "high"   # dashboard leads
    # scheme_unused is app-tier → counted as app-fixable
    assert ctx["app_fixable"] >= 2


def test_build_report_context_none_for_non_env_audit(tmp_path):
    app, store, mid, run = _seed(tmp_path)
    other = store.create_run(mid, {}, kind="audit")
    assert build_report_context(store, other) is None
    assert build_report_context(store, 999999) is None


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

def test_report_pdf_route_returns_a_pdf_download(tmp_path):
    pytest.importorskip("weasyprint")
    app, store, mid, run = _seed(tmp_path)
    r = TestClient(app).get(f"/runs/{run}/report.pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert "attachment" in r.headers.get("content-disposition", "")
    assert r.content[:4] == b"%PDF", "body must be a real PDF"


def test_report_html_preview_has_the_executive_sections(tmp_path):
    app, store, mid, run = _seed(tmp_path)
    t = TestClient(app).get(f"/runs/{run}/report").text
    assert "Prod Jira" in t
    assert "CRITICAL" in t and ">D<" in t                  # verdict + grade
    assert "One unfinished migration" in t                 # AI summary
    assert "Archive the dormant projects" in t             # roadmap
    assert "deactivated user" in t                         # top risk
    assert "No automation/SLA reference map" in t          # gaps
    assert "Unfinished migration root cause" in t          # ai finding


def test_report_route_redirects_for_a_run_with_no_report(tmp_path):
    # env_audit + migration audit get reports; a FIX run (env_fix) does not.
    app, store, mid, run = _seed(tmp_path)
    other = store.create_run(mid, {}, kind="env_fix")
    r = TestClient(app).get(f"/runs/{other}/report.pdf", follow_redirects=False)
    assert r.status_code in (302, 303)


def test_report_pdf_fetcher_blocks_external_and_file_urls():
    """The PDF render must refuse every external/file resource (SSRF / local-file
    read defense), since the report has no legitimate remote assets."""
    import pytest
    import webapp.report as rep
    for url in ("http://169.254.169.254/latest/meta-data/",
                "https://evil.example/x.png", "file:///etc/passwd",
                "ftp://host/x", "data:text/plain,hi"):
        with pytest.raises(Exception):
            rep._blocking_url_fetcher(url)


def test_report_pdf_renders_despite_an_injected_external_url(tmp_path):
    """Even if a value smuggled an external URL into the document, the PDF still
    renders to valid bytes (autoescape neutralises the tag; the fetcher refuses
    the fetch regardless) — no hang, no resource read."""
    pytest.importorskip("weasyprint")
    from webapp.report import render_report_pdf
    app, store, mid, run = _seed(tmp_path)
    ctx = build_report_context(store, run)
    ctx["env_name"] = '<img src="http://169.254.169.254/x.png">PWN'
    pdf = render_report_pdf(_templates_shim(), ctx)
    assert pdf[:4] == b"%PDF"


def test_report_pdf_degrades_gracefully_without_weasyprint(tmp_path, monkeypatch):
    app, store, mid, run = _seed(tmp_path)
    import webapp.report as rep

    def _boom(*a, **k):
        raise rep.ReportUnavailable("PDF export requires WeasyPrint")

    monkeypatch.setattr(rep, "render_report_pdf", _boom)
    r = TestClient(app).get(f"/runs/{run}/report.pdf")
    assert r.status_code == 503
    assert "WeasyPrint" in r.text


def test_analysis_page_links_to_the_report(tmp_path):
    """The env analysis page must expose an Export/Download button to the report."""
    app, store, mid, run = _seed(tmp_path)
    t = TestClient(app).get(f"/runs/{run}/analysis").text
    assert f"/runs/{run}/report.pdf" in t


# ---------------------------------------------------------------------------
# story-first structure + page bound
# ---------------------------------------------------------------------------

def _templates_shim():
    import os
    import webapp
    from jinja2 import Environment, FileSystemLoader
    t = type("T", (), {})()
    t.env = Environment(loader=FileSystemLoader(
        os.path.join(os.path.dirname(webapp.__file__), "templates")))
    return t


def test_report_leads_with_the_story_before_the_data(tmp_path):
    app, store, mid, run = _seed(tmp_path)
    ctx = build_report_context(store, run)
    # the cover carries a story hook drawn from the AI narrative's lead
    assert ctx["bottom_line"].startswith("One unfinished migration")
    t = TestClient(app).get(f"/runs/{run}/report").text
    # cover bottom-line, THEN the story section, THEN the data table
    assert t.index('class="bottom-line"') < t.index("The story")
    assert t.index("One unfinished migration") < t.index("Findings by problem type")


def test_report_is_bounded_to_10_pages_even_when_huge(tmp_path):
    """Grouping + per-section caps keep the executive summary to <=10 pages no
    matter how large the underlying audit is."""
    pytest.importorskip("weasyprint")
    from weasyprint import HTML
    from webapp.report import render_report_html
    app, store, mid, run = _seed(tmp_path)
    ctx = build_report_context(store, run)
    # inflate every list AND the free-text fields well past the template caps
    ctx["finding_groups"] = ctx["finding_groups"] * 40
    ctx["ai"]["summary"] = "An enormous narrative. " * 4000   # ~90 KB single node
    ctx["ai"]["roadmap"] = [{"step": "Action " * 200, "effort": "L",
                             "rationale": "a long rationale " * 80}
                            for i in range(25)]
    ctx["ai"]["gaps"] = ["A blind spot the rule engine cannot see. " * 30
                         for _ in range(15)]
    ctx["ai"]["ai_findings"] = ctx["ai"]["ai_findings"] * 12
    ctx["ai"]["top_risks"] = ["A material risk worth surfacing. " * 40
                              for _ in range(12)]
    doc = HTML(string=render_report_html(_templates_shim(), ctx)).render()
    assert len(doc.pages) <= 10, f"report rendered {len(doc.pages)} pages"


def test_report_skipped_ai_has_no_story_section_or_skip_hook(tmp_path):
    """When AI was skipped, the cover hook must be the factual fallback (not the
    'AI analysis skipped' notice), and the AI story section must not render."""
    app = _app(tmp_path)
    store = app.state.store
    mid = store.create_migration("Prod Jira", product="jira",
                                 audit_type="environment")
    run = store.create_run(mid, {}, kind="env_audit")
    store.insert_findings_config(run, [_env_finding("scheme_unused", "S1")])
    store.update_run(run, status="done", verdict="NEEDS_ATTENTION", stats={
        "grade": "C", "health_score": 70, "findings": 1, "high": 0,
        "medium": 0, "low": 1, "headlines": ["1 low-severity issue."],
        "ai": {"skipped": True, "summary": "AI analysis skipped (no provider).",
               "roadmap": [], "top_risks": [], "gaps": [], "themes": [],
               "ai_findings": [], "model": None}})
    ctx = build_report_context(store, run)
    assert "skipped" not in ctx["bottom_line"].lower()
    t = TestClient(app).get(f"/runs/{run}/report").text
    assert "The story" not in t                  # story section suppressed
    assert "AI analysis skipped" not in t        # the notice is not printed


# ---------------------------------------------------------------------------
# Migration-audit report
# ---------------------------------------------------------------------------

def _seed_migration(tmp_path):
    app = _app(tmp_path)
    store = app.state.store
    mid = store.create_migration("Acme Prod", product="jira",
                                 audit_type="migration")
    run = store.create_run(mid, {}, kind="audit")
    store.set_run_projects(run, [
        {"key": "ACME", "name": "Acme", "src_count": 100, "tgt_count": 92,
         "missing": 8, "tail_count": 0, "fidelity_pct": 92.0, "blind_spot": 0,
         "status": "audited"},
        {"key": "OPS", "name": "Ops", "src_count": 50, "tgt_count": 50,
         "missing": 0, "tail_count": 0, "fidelity_pct": 99.0, "blind_spot": 0,
         "status": "audited"}])
    store.insert_findings_issue(run, [
        {"project": "ACME", "kind": "missing", "src_key": "ACME-1",
         "tgt_key": "", "field": "", "summary": "missing on target"},
        {"project": "ACME", "kind": "value_mismatch", "src_key": "ACME-2",
         "tgt_key": "ACME-2", "field": "priority", "summary": "priority differs"}])
    store.update_run(run, status="done", verdict="GAPS_FOUND", stats={
        "project_stats": {
            "ACME": {"common": 92, "fidelity_pct": 92.0,
                     "issues_with_mismatches": 1},
            "OPS": {"common": 50, "fidelity_pct": 99.0,
                    "issues_with_mismatches": 0}},
        "issues_src_total": 150, "headlines": ["8 issues missing on target."]})
    return app, store, run


def test_build_migration_report_context(tmp_path):
    from webapp.report import build_migration_report_context
    app, store, run = _seed_migration(tmp_path)
    ctx = build_migration_report_context(store, run)
    assert ctx["env_name"] == "Acme Prod" and ctx["verdict"] == "GAPS_FOUND"
    assert ctx["projects_count"] == 2 and ctx["missing_total"] == 8
    assert ctx["fidelity"] is not None
    assert ctx["project_rows"][0]["key"] == "ACME"      # worst fidelity leads
    kinds = {f["kind"] for f in ctx["findings_by_kind"]}
    assert "missing" in kinds and "value_mismatch" in kinds


def test_build_migration_report_context_none_for_env_audit(tmp_path):
    from webapp.report import build_migration_report_context
    app, store, mid, run = _seed(tmp_path)   # an env_audit run
    assert build_migration_report_context(store, run) is None


def test_migration_report_pdf_route_downloads(tmp_path):
    pytest.importorskip("weasyprint")
    app, store, run = _seed_migration(tmp_path)
    r = TestClient(app).get(f"/runs/{run}/report.pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content[:4] == b"%PDF"


def test_migration_report_html_has_sections(tmp_path):
    app, store, run = _seed_migration(tmp_path)
    t = TestClient(app).get(f"/runs/{run}/report").text
    assert "Acme Prod" in t and "Migration Audit" in t
    assert "Per-project fidelity" in t and "Findings by type" in t
    assert "ACME" in t


def test_migration_report_surfaces_cross_dialect_note(tmp_path):
    # No-bias review: content/comment mismatches in a DC->Cloud (wiki->ADF)
    # comparison carry a cross_dialect flag that was computed + stored but NEVER
    # surfaced — admins misread expected representation drift as data loss.
    from webapp.report import build_migration_report_context
    app, store, run = _seed_migration(tmp_path)
    store.insert_findings_issue(run, [
        {"project": "ACME", "kind": "content_mismatch", "src_key": "ACME-3",
         "tgt_key": "ACME-3", "field": "description",
         "summary": "description content differs",
         "detail": {"cross_dialect": True}}])
    ctx = build_migration_report_context(store, run)
    assert ctx["cross_dialect"] is True
    t = TestClient(app).get(f"/runs/{run}/report").text
    assert "representation" in t.lower()


def test_migration_report_no_cross_dialect_note_when_same_dialect(tmp_path):
    from webapp.report import build_migration_report_context
    app, store, run = _seed_migration(tmp_path)   # seed has no cross_dialect detail
    assert build_migration_report_context(store, run)["cross_dialect"] is False


def test_migration_report_discloses_custom_field_value_scope(tmp_path):
    # The fidelity score is computed over standard fields, description, comments
    # and attachments — NOT custom-field values. The report must disclose that
    # scope so a high score is never read as "custom fields migrated faithfully"
    # (review Bug 3: the score otherwise silently overstates fidelity).
    app, store, run = _seed_migration(tmp_path)
    t = TestClient(app).get(f"/runs/{run}/report").text.lower()
    assert "custom-field values" in t
    assert "verify-sensitive" in t


def test_migration_analysis_page_has_export_pdf(tmp_path):
    app, store, run = _seed_migration(tmp_path)
    t = TestClient(app).get(f"/runs/{run}/analysis").text
    assert f"/runs/{run}/report.pdf" in t


def test_migration_report_surfaces_uncheckable_coverage(tmp_path):
    # No-bias review: comment/attachment content beyond the inline cap is
    # uncheckable and does not dent fidelity, so a high % can hide unverified
    # content. Surface the uncheckable count next to the fidelity number.
    import json
    from webapp.report import build_migration_report_context
    app, store, run = _seed_migration(tmp_path)
    stats = json.loads(store.get_run(run)["stats_json"])
    stats["comments_uncheckable"] = 7
    store.update_run(run, stats=stats)
    ctx = build_migration_report_context(store, run)
    assert ctx["uncheckable_total"] == 7
    t = TestClient(app).get(f"/runs/{run}/report").text
    assert "could not be fully verified" in t


def test_no_template_has_a_bare_unscoped_table_header():
    # Static a11y guard (no-bias review: the scope fix initially missed several
    # templates): every <th> in every template must declare a scope so screen
    # readers can associate cells with headers.
    import pathlib
    tdir = pathlib.Path(__file__).resolve().parent.parent / "webapp" / "templates"
    offenders = [p.name for p in tdir.glob("*.html") if "<th>" in p.read_text()]
    assert not offenders, f"bare <th> (no scope) in: {offenders}"


def test_migration_report_tables_have_header_scope(tmp_path):
    # No-bias review (a11y): data tables need column-header semantics so screen
    # readers associate cells with headers. Every <th> in the report is a column
    # header -> scope="col".
    app, store, run = _seed_migration(tmp_path)
    t = TestClient(app).get(f"/runs/{run}/report").text
    assert "<th>" not in t                       # no bare, scope-less headers
    assert 'scope="col"' in t


def test_analysis_page_has_noscript_fallback_to_report(tmp_path):
    # No-bias review: the results view renders client-side into an empty div, so
    # with JS off / a screen reader the whole thing is invisible. A <noscript>
    # fallback must point at the server-rendered report.
    app, store, run = _seed_migration(tmp_path)
    t = TestClient(app).get(f"/runs/{run}/analysis").text
    assert "<noscript>" in t
    lo = t[t.index("<noscript>"):t.index("</noscript>")]
    assert f"/runs/{run}/report" in lo
