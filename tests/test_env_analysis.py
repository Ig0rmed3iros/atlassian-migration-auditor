import json
import subprocess
from auditor.envaudit.analysis import summarize_for_ai, analyze
from webapp.ai_provider import AnthropicProvider, ClaudeCLIProvider


class _Block:
    def __init__(self, type, **kw):
        self.type = type
        for k, v in kw.items(): setattr(self, k, v)
class _Resp:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content; self.stop_reason = stop_reason; self.stop_details = None
class _Msgs:
    def __init__(self, responses): self._r = list(responses); self.calls = []
    def create(self, **kw): self.calls.append(kw); return self._r.pop(0)
class _RawAnthropic:
    """A fake raw Anthropic client (messages.create)."""
    def __init__(self, responses): self.messages = _Msgs(responses)


def _Client(responses):
    """The 3rd arg to analyze() is now a PROVIDER, not a raw client. Wrap a fake
    Anthropic client in AnthropicProvider so the existing coverage is preserved."""
    return AnthropicProvider(_RawAnthropic(responses))


class _FakeProvider:
    """A minimal provider stub for analyze()'s provider-agnostic branches."""
    def __init__(self, result):
        self._result = result
        self.calls = []

    def complete(self, system, user_content, *, model=None, effort="medium"):
        self.calls.append({"system": system, "user_content": user_content,
                           "model": model, "effort": effort})
        return self._result


def test_privacy_boundary_holds_end_to_end_to_provider_call():
    # No-bias review: the PII guarantee was only tested at summarize_for_ai, not
    # through analyze() to the actual provider.complete(user_content). Inject PII
    # across vectors and assert NONE reaches the outbound payload.
    _PII_SENTINEL = "jane.doe@acme.example"
    _PII_MEMBER = "bob.member@corp.example"
    _ACC_CLASSIC = "557058:f6c30b2e-5f1a-4b2c-8d3e-1a2b3c4d5e6f"   # legacy colon form
    _ACC_MODERN = "5b10ac8d82e05b22cc7d4ef5"                       # 24-hex (dominant)
    _ACC_UUID = "712020a1-b2c3-4d5e-9f80-112233445566"            # UUID form
    prov = _FakeProvider({"text": '{"health_score": 80, "grade": "B"}',
                          "error": None, "refused": False, "model": "m"})
    snap = {"deployment": "cloud", "projects": ["T"], "areas": {"groups": {
        "names": ["developers", _PII_SENTINEL, _ACC_MODERN], "count": 3,
        "member_counts": {"developers": 5, _ACC_UUID: 1},
        "members": [_PII_MEMBER]}}}
    findings = [{"area": "groups", "name": _ACC_CLASSIC, "kind": "broad_group",
                 "severity": "high", "detail": {}}]
    analyze(snap, findings, prov)
    blob = prov.calls[0]["system"] + prov.calls[0]["user_content"]
    for pii in (_PII_SENTINEL, _PII_MEMBER, _ACC_CLASSIC, _ACC_MODERN, _ACC_UUID):
        assert pii not in blob, f"PII {pii!r} reached the provider payload"
    assert "developers" in blob          # legitimate object names still forwarded


def test_summary_is_metadata_only():
    snap = {"deployment": "cloud", "projects": ["ACME"], "areas": {
        "statuses": {"names": ["Open"], "count": 1},
        "custom_fields": {"names": ["Severity"], "count": 1,
                          "by_type": {"Severity": "select"},
                          "secret_values": ["SECRET customer value"]}}}
    findings = [{"area": "custom_fields", "name": "Severity",
                 "kind": "unused_custom_field", "severity": "low"}]
    s = summarize_for_ai(snap, findings)
    text = str(s)
    assert "Severity" in text and "unused_custom_field" in text
    assert "SECRET customer value" not in text   # PRIVACY: values never leak


def test_parse_validates_and_clamps_ai_score_and_grade():
    # No-bias review: the AI's score/grade reached the report unvalidated. Clamp
    # numeric scores to [0,100], coerce to int, normalize grade case, and reject
    # anything that isn't a clean A-F / number.
    from auditor.envaudit.analysis import _parse
    out = _parse('{"health_score": 150, "grade": "A+"}', "m")
    assert out["health_score"] == 100        # clamped to ceiling
    assert out["grade"] is None              # "A+" is not a clean A-F
    out2 = _parse('{"health_score": "great", "grade": "Excellent"}', "m")
    assert out2["health_score"] is None and out2["grade"] is None
    out3 = _parse('{"health_score": 82.6, "grade": "b"}', "m")
    assert out3["health_score"] == 83        # float -> rounded int
    assert out3["grade"] == "B"              # case-normalized
    out4 = _parse('{"health_score": -5, "grade": "F"}', "m")
    assert out4["health_score"] == 0         # clamped to floor


def test_ai_findings_with_fabricated_area_are_dropped():
    # No-bias review: ai_findings were never grounded — the model could invent an
    # area the audit never saw and it rendered as a real finding. Drop fabricated
    # areas; keep real areas and cross-area sentinels.
    js = ('{"health_score": 80, "grade": "B", "summary": "ok", "ai_findings": ['
          '{"title": "real", "area": "custom_fields"}, '
          '{"title": "halluc", "area": "boards"}, '
          '{"title": "cross", "area": "multiple"}]}')
    snap = {"areas": {"custom_fields": {"count": 1, "names": []}}}
    out = analyze(snap, [], _Client([_Resp([_Block("text", text=js)])]))
    titles = {f["title"] for f in out["ai_findings"]}
    assert titles == {"real", "cross"}      # the fabricated 'boards' area is gone


def test_ai_payload_redacts_emails_in_object_names():
    # No-bias review: object names are sent to the AI verbatim and routinely
    # embed emails/PII (an admin names a group after a person). Redact emails in
    # the OUTBOUND payload (the local report still shows the real name).
    from auditor.envaudit.analysis import summarize_for_ai
    snap = {"areas": {"groups": {
        "count": 2,
        "names": ["jira-administrators", "john.smith@acme.com"],
        "member_counts": {"john.smith@acme.com": 1}}}}
    out = summarize_for_ai(snap, [])
    g = out["areas"]["groups"]
    assert "john.smith@acme.com" not in g["names"]
    assert any("redacted-email" in n for n in g["names"])
    assert "jira-administrators" in g["names"]            # non-PII names preserved
    assert all("@acme.com" not in k for k in g.get("member_counts", {}))


def test_parse_rejects_nonfinite_and_bool_scores():
    # No-bias review: json.loads accepts Infinity/NaN; int(round(inf)) raised
    # OverflowError and aborted the whole run. And bool is an int subclass, so
    # `true` became a score of 1. Both must be rejected, never raise.
    from auditor.envaudit.analysis import _parse
    for bad in ("Infinity", "-Infinity", "NaN"):
        assert _parse('{"health_score": %s}' % bad, "m")["health_score"] is None
    assert _parse('{"health_score": true, "grade": false}', "m")["health_score"] is None
    assert _parse('{"health_score": true}', "m")["grade"] is None


def test_parse_selects_assessment_object_over_decoy():
    # No-bias review: the parser returned the FIRST parseable object, so a decoy
    # before the real assessment won and the whole assessment was lost.
    from auditor.envaudit.analysis import _parse
    text = ('Example bad config: {"jql": "project = X"} is invalid.\n'
            'Assessment:\n{"health_score": 75, "grade": "C", "summary": "real"}')
    out = _parse(text, "m")
    assert out["health_score"] == 75 and out["summary"] == "real"


def test_json_extraction_is_linear_not_quadratic():
    # No-bias review: O(n^2) scan hung ~34s on tens of KB of '{'. Must be bounded.
    from auditor.envaudit.analysis import _extract_json
    import time
    t0 = time.time()
    assert _extract_json("{" * 60000) is None
    assert time.time() - t0 < 2.0, "extraction must be ~linear, not quadratic"


def test_parse_section_robust_to_prose_and_decoy():
    # No-bias review: _parse_section still used index/rindex -> sectioned/
    # map-reduce per-area findings silently dropped on any stray brace.
    from auditor.envaudit.analysis import _parse_section
    text = ('Context {"x": 1}. Findings:\n'
            '{"area_findings": [{"title": "t", "severity": "high"}]}')
    out = _parse_section(text)
    assert out and out[0]["title"] == "t"


def test_analysis_stage_never_fails_run_when_analyze_raises(monkeypatch):
    # No-bias review: stage_env_analysis did not wrap the analyze_fn call, so an
    # AI exception aborted the run BEFORE finalize, losing the deterministic
    # score. The AI step must always degrade to a skipped assessment.
    from webapp import env_stages
    monkeypatch.setattr(env_stages, "ai_provider", lambda store: object())
    monkeypatch.setattr(env_stages, "_say", lambda *a, **k: None)

    def boom(*a, **k):
        raise OverflowError("inf token")
    monkeypatch.setattr(env_stages, "analyze_sectioned", boom)

    class _Conn:
        product = "jira"
    ctx = {"store": object(), "snapshot": {}, "env_findings": [],
           "connector": _Conn(), "run_id": 1}
    env_stages.stage_env_analysis(ctx)        # must NOT raise
    assert ctx["ai"]["skipped"] is True


def test_parse_extracts_json_despite_prose_and_fences():
    # No-bias review: index('{')..rindex('}') silently dropped a valid assessment
    # when the model wrapped JSON in prose/code-fences with stray braces.
    from auditor.envaudit.analysis import _parse
    text = ('Here is my assessment {see note}.\n```json\n'
            '{"health_score": 77, "grade": "B", "summary": "ok"}\n```\nDone.')
    out = _parse(text, "m")
    assert out["health_score"] == 77 and out["grade"] == "B"
    assert out["summary"] == "ok"


def test_analyze_parses_assessment():
    js = ('{"health_score": 72, "grade": "B", "summary": "ok", '
          '"themes": [{"title": "Field sprawl", "why": "x", "severity": "medium", '
          '"recommendation": "merge", "related": ["custom_fields/Severity"]}], '
          '"top_risks": ["r"], "quick_wins": ["w"]}')
    out = analyze({"areas": {}}, [], _Client([_Resp([_Block("text", text=js)])]))
    assert out["error"] is None and out["health_score"] == 72
    assert out["themes"][0]["title"] == "Field sprawl"


def test_analyze_no_provider_returns_skipped():
    out = analyze({"areas": {}}, [], None)
    assert out["skipped"] is True and out["error"] is None
    # The skipped message is now provider-neutral (not Anthropic-specific).
    assert "anthropic" not in out["summary"].lower()
    assert "provider" in out["summary"].lower()


# ---------------------------------------------------------------------------
# analyze() driven by a fake PROVIDER (provider-agnostic branches).
# ---------------------------------------------------------------------------

def test_analyze_with_fake_provider_happy_parses():
    js = ('{"health_score": 88, "grade": "A", "summary": "great", '
          '"themes": [], "top_risks": [], "quick_wins": []}')
    prov = _FakeProvider({"text": js, "error": None, "refused": False,
                          "model": "albert-heavy"})
    out = analyze({"areas": {}}, [], prov)
    assert out["error"] is None
    assert out["health_score"] == 88 and out["grade"] == "A"
    assert out["model"] == "albert-heavy"
    # The provider received the system prompt + the JSON-serialized summary.
    assert prov.calls[0]["system"]
    assert "Audit this Jira environment configuration" in prov.calls[0]["user_content"]


def test_analyze_with_fake_provider_refused_is_error():
    prov = _FakeProvider({"text": None, "error": None, "refused": True,
                          "model": "albert-heavy"})
    out = analyze({"areas": {}}, [], prov)
    assert out["themes"] == [] and "declin" in (out["error"] or "").lower()
    assert out["health_score"] is None and out["grade"] is None


def test_analyze_with_fake_provider_error_is_surfaced():
    prov = _FakeProvider({"text": None, "error": "endpoint unreachable",
                          "refused": False, "model": "albert-heavy"})
    out = analyze({"areas": {}}, [], prov)
    assert out["error"] and "endpoint unreachable" in out["error"]
    assert out["health_score"] is None and out["grade"] is None


def test_analyze_refusal():
    out = analyze({"areas": {}}, [], _Client([_Resp([], stop_reason="refusal")]))
    assert out["themes"] == [] and "declin" in (out["error"] or "").lower()


def test_analyze_pause_turn_exhaustion_returns_error():
    # Five consecutive pause_turn responses exhaust the retry loop. The last
    # response carries no text blocks, so falling through to _parse would
    # produce a bogus "no assessment" success (grade None) and mask a failure.
    # The loop must instead surface an explicit error.
    resps = [_Resp([], stop_reason="pause_turn") for _ in range(5)]
    out = analyze({"areas": {}}, [], _Client(resps))
    assert out["error"] and out["grade"] is None and out["health_score"] is None
    assert out["themes"] == []


# ---------------------------------------------------------------------------
# Phase-A new-area leak tests (Task 6 / spec R4 / invariant I1)
# Each test injects a PII sentinel string into the snap, then serializes
# summarize_for_ai's output to JSON and asserts:
#   (a) the PII sentinel is NOT present (hard leak guard), AND
#   (b) the legitimate metadata key IS present (so the test is meaningful).
# ---------------------------------------------------------------------------

_PII_SENTINEL = "jane.doe@acme.example"          # must never leave the machine
_PII_ACCOUNT  = "557058:xxxx-accountid-secret"
_PII_MEMBER   = "bob.member@corp.example"
_PII_LEAD     = "Jane Doe Lead Name"
_PII_LEAD_AID = "557058:zzzz-lead-secret"
_PII_CREATED  = "carol@acme.example"
_PII_HOLDER   = "secret-group-id-holder"
_PII_PARAM    = "secret-parameter-value"


def _snap_with(**areas):
    return {"deployment": "cloud", "projects": ["TEST"], "areas": areas}


def _ser(s):
    """Full JSON serialization — catches any path that stringify leaks a secret."""
    return json.dumps(s, default=str)


# --- groups ---

def test_leak_groups_member_identities_never_forwarded():
    """member identities in groups.members must NOT appear; count MUST appear."""
    snap = _snap_with(groups={
        "names": ["developers", "admins"],
        "count": 2,
        "member_counts": {"developers": 5, "admins": 2},
        "capped": False,
        # PII injection: member identity list
        "members": [_PII_SENTINEL, _PII_MEMBER],
    })
    out = _ser(summarize_for_ai(snap, []))
    assert _PII_SENTINEL not in out, "groups.members identity leaked"
    assert _PII_MEMBER not in out, "groups.members identity leaked"
    # Legitimate metadata must be forwarded
    assert "developers" in out, "groups.names not forwarded"
    assert '"count": 2' in out or '"count":2' in out, "groups.count not forwarded"


def test_leak_groups_member_counts_are_integers_no_identities():
    """member_counts integers are fine; raw member list must be absent."""
    snap = _snap_with(groups={
        "names": ["readers"],
        "count": 1,
        "member_counts": {"readers": 12},
        "capped": True,
        "members": [_PII_SENTINEL],
    })
    out = _ser(summarize_for_ai(snap, []))
    assert _PII_SENTINEL not in out
    # The count integer 12 and capped flag must reach the AI
    assert "12" in out, "groups.member_counts value not forwarded"
    assert "capped" in out, "groups.capped not forwarded"


# --- components ---

def test_leak_components_lead_identity_never_forwarded():
    """component lead name/accountId must NOT appear, and neither must individual
    component NAMES (only aggregate counts are forwarded now)."""
    snap = _snap_with(components={
        "count": 3,
        "by_project": {
            "PROJ": [
                {"name": "Backend", "has_lead": True, "assignee_type": "PROJECT_LEAD",
                 "lead": _PII_LEAD, "leadAccountId": _PII_LEAD_AID},
                {"name": "Frontend", "has_lead": False, "assignee_type": "UNASSIGNED"},
            ]
        },
    })
    out = _ser(summarize_for_ai(snap, []))
    assert _PII_LEAD not in out, "component lead name leaked"
    assert _PII_LEAD_AID not in out, "component leadAccountId leaked"
    # AGGREGATE shape: no per-component object list / individual names forwarded.
    assert "by_project" not in out, "components.by_project must not be forwarded"
    assert "Backend" not in out, "individual component name leaked"
    assert "Frontend" not in out, "individual component name leaked"
    # Aggregate metadata must reach the AI.
    area = summarize_for_ai(snap, [])["areas"]["components"]
    assert area["count"] == 3
    agg = area["aggregate"]
    assert agg["total"] == 2          # 2 components across PROJ
    assert agg["leaderless"] == 1     # Frontend has_lead False
    assert agg["unassigned_default"] == 1   # Frontend UNASSIGNED
    assert agg["projects_with_components"] == 1


def test_components_aggregate_shape():
    """components forwards ONLY aggregate counts, never per-component objects."""
    snap = _snap_with(components={
        "count": 5,
        "by_project": {
            "A": [{"name": "x", "has_lead": True, "assignee_type": "PROJECT_LEAD"},
                  {"name": "y", "has_lead": False, "assignee_type": "UNASSIGNED"}],
            "B": [{"name": "z", "has_lead": False, "assignee_type": "PROJECT_DEFAULT"}],
            "C": [],   # a project queried but with zero components
        },
    })
    area = summarize_for_ai(snap, [])["areas"]["components"]
    assert "by_project" not in area
    assert area["aggregate"] == {
        "total": 3, "leaderless": 2, "unassigned_default": 1,
        "projects_with_components": 2}


# --- versions ---

def test_leak_versions_creator_never_forwarded():
    """version creator identity AND individual version NAMES must NOT appear; the
    aggregate counts MUST."""
    snap = _snap_with(versions={
        "count": 4,
        "by_project": {
            "PROJ": [
                {"name": "v1.0", "released": True, "archived": False, "overdue": False,
                 "createdBy": _PII_CREATED},
                {"name": "v2.0", "released": False, "archived": False, "overdue": True,
                 "releaser": _PII_CREATED},
            ]
        },
    })
    out = _ser(summarize_for_ai(snap, []))
    assert _PII_CREATED not in out, "version creator/releaser identity leaked"
    # AGGREGATE shape: no per-version object list / individual names forwarded.
    assert "by_project" not in out, "versions.by_project must not be forwarded"
    assert "v1.0" not in out, "individual version name leaked"
    assert "v2.0" not in out, "individual version name leaked"
    area = summarize_for_ai(snap, [])["areas"]["versions"]
    agg = area["aggregate"]
    assert agg["total"] == 2
    assert agg["overdue"] == 1            # v2.0 overdue + not released
    assert agg["archived_unreleased"] == 0
    assert agg["released"] == 1           # v1.0


def test_versions_aggregate_shape():
    """versions forwards ONLY aggregate counts, never per-version objects."""
    snap = _snap_with(versions={
        "count": 3,
        "by_project": {
            "P": [
                {"name": "a", "released": True, "archived": True, "overdue": False},
                {"name": "b", "released": False, "archived": True, "overdue": True},
                {"name": "c", "released": False, "archived": False, "overdue": True},
            ],
        },
    })
    area = summarize_for_ai(snap, [])["areas"]["versions"]
    assert "by_project" not in area
    assert area["aggregate"] == {
        "total": 3, "overdue": 2, "archived_unreleased": 1, "released": 1}


# --- permission_scheme_grants ---

def test_leak_permission_scheme_grants_holder_value_never_forwarded():
    """holder value/parameter must NOT appear; permission + holder_type MUST appear."""
    snap = _snap_with(permission_scheme_grants={
        "count": 2,
        "by_scheme": {
            "Default Permission Scheme": [
                {"permission": "ADMINISTER", "holder_type": "anyone",
                 "holder": {"type": "anyone", "value": _PII_HOLDER,
                            "parameter": _PII_PARAM}},
            ]
        },
    })
    out = _ser(summarize_for_ai(snap, []))
    assert _PII_HOLDER not in out, "permission grant holder value leaked"
    assert _PII_PARAM not in out, "permission grant holder parameter leaked"
    # Config-type metadata must reach the AI
    assert "ADMINISTER" in out, "permission name not forwarded"
    assert "holder_type" in out, "holder_type not forwarded"
    assert "anyone" in out, "holder_type value not forwarded"


def test_permission_scheme_grants_skipped_forwarded():
    """skipped:True must be forwarded so the AI knows coverage is partial."""
    snap = _snap_with(permission_scheme_grants={"skipped": True, "reason": "DC"})
    out = summarize_for_ai(snap, [])
    assert out["areas"]["permission_scheme_grants"]["skipped"] is True


# --- custom_field_options ---

def test_leak_custom_field_options_no_option_text_forwarded():
    """option text values must NOT appear; context/option counts MUST appear."""
    snap = _snap_with(custom_field_options={
        "by_field": {
            "Status Category": {"contexts": 2, "options": 15},
        },
        "capped": False,
        # PII injection: option text is not something gather would put here,
        # but we guard against it defensively
        "raw_option_values": [_PII_SENTINEL],
    })
    out = _ser(summarize_for_ai(snap, []))
    assert _PII_SENTINEL not in out, "custom_field_options raw values leaked"
    # Structural counts must reach the AI
    assert "contexts" in out, "custom_field_options.contexts not forwarded"
    assert "15" in out, "custom_field_options option count not forwarded"
    assert "capped" in out, "custom_field_options.capped not forwarded"


def test_custom_field_options_skipped_forwarded():
    snap = _snap_with(custom_field_options={"skipped": True})
    out = summarize_for_ai(snap, [])
    assert out["areas"]["custom_field_options"]["skipped"] is True


# --- boards ---

def test_boards_count_and_names_forwarded():
    snap = _snap_with(boards={"names": ["Sprint Board", "Kanban"], "count": 2, "capped": False})
    out = summarize_for_ai(snap, [])
    area = out["areas"]["boards"]
    assert area["count"] == 2
    assert "Sprint Board" in area["names"]
    assert area.get("capped") is False


# --- filters ---

def test_filters_count_forwarded():
    snap = _snap_with(filters={"count": 42, "capped": True})
    out = summarize_for_ai(snap, [])
    area = out["areas"]["filters"]
    assert area["count"] == 42
    assert area.get("capped") is True


# --- dashboards ---

def test_dashboards_count_forwarded():
    snap = _snap_with(dashboards={"count": 17, "capped": False})
    out = summarize_for_ai(snap, [])
    area = out["areas"]["dashboards"]
    assert area["count"] == 17


# --- issuetype_schemes ---

def test_issuetype_schemes_forwarded():
    snap = _snap_with(issuetype_schemes={
        "names": ["Default Issue Type Scheme", "Software Scheme"],
        "count": 2,
        "projects_using": {"Default Issue Type Scheme": ["PROJ1", "PROJ2"],
                           "Software Scheme": []},
    })
    out = summarize_for_ai(snap, [])
    area = out["areas"]["issuetype_schemes"]
    assert area["count"] == 2
    assert "Default Issue Type Scheme" in area["names"]
    # projects_using (IDs only, not PII) must be forwarded
    assert "projects_using" in area


def test_issuetype_screen_schemes_forwarded():
    snap = _snap_with(issuetype_screen_schemes={
        "names": ["Default Issue Type Screen Scheme"],
        "count": 1,
        "projects_using": {"Default Issue Type Screen Scheme": ["PROJ1"]},
    })
    out = summarize_for_ai(snap, [])
    area = out["areas"]["issuetype_screen_schemes"]
    assert area["count"] == 1
    assert "projects_using" in area


# --- finding fix_tier forwarded ---

def test_finding_fix_tier_forwarded():
    """fix.tier must be included in the forwarded findings; detail is stripped."""
    findings = [
        {"area": "groups", "name": "empty-group", "kind": "empty_group",
         "severity": "low",
         "detail": {"fix": {"tier": "app", "title": "Delete group",
                             "detail": "Deletes via REST"},
                    "secret_data": "should not appear"}},
        {"area": "workflows", "name": "My WF", "kind": "workflow_sprawl",
         "severity": "medium",
         # no detail key — graceful fallback
         },
    ]
    snap = _snap_with()
    out = summarize_for_ai(snap, findings)
    forwarded = out["finding_sample"]
    assert out["finding_total"] == 2
    assert len(forwarded) == 2
    # The medium finding sorts ahead of the low one in the prioritised sample.
    by_kind = {f["kind"]: f for f in forwarded}
    # First finding must carry fix_tier
    assert by_kind["empty_group"].get("fix_tier") == "app", "fix_tier not forwarded"
    # No raw detail / secret data must appear
    assert "should not appear" not in _ser(forwarded)
    # Second finding (no detail.fix) should gracefully have fix_tier=None or absent
    assert by_kind["workflow_sprawl"].get("fix_tier") is None or \
        "fix_tier" not in by_kind["workflow_sprawl"]


def test_finding_fix_tier_missing_graceful():
    """Findings without a detail.fix dict must not raise and must not forward None as a value."""
    findings = [
        {"area": "statuses", "name": "Backlog", "kind": "status_not_in_workflow",
         "severity": "low"},
    ]
    out = summarize_for_ai({"areas": {}}, findings)
    # Should not raise; fix_tier absent or None is acceptable
    assert out["finding_sample"][0].get("fix_tier") is None or \
           "fix_tier" not in out["finding_sample"][0]


# --- findings summary is bounded: counts complete + sample capped/prioritised ---

def test_findings_summary_bounded_and_severity_prioritised():
    """On a large finding set, summarize_for_ai must forward COMPLETE by-kind /
    by-severity counts + finding_total, but cap finding_sample at
    _AI_FINDING_CAP and prioritise it by severity so highs are never crowded
    out by low/info findings. Privacy (metadata only) still holds."""
    from auditor.envaudit.analysis import _AI_FINDING_CAP
    # 200 findings: 3 high (with a PII sentinel in detail to prove it is dropped),
    # then a flood of 197 low findings that must NOT crowd out the highs.
    findings = []
    for i in range(3):
        findings.append({
            "area": "issue_quality", "name": f"high-{i}",
            "kind": "done_but_unresolved", "severity": "high",
            "detail": {"fix": {"tier": "human"},
                       "secret_data": _PII_SENTINEL, "count": 999}})
    for i in range(197):
        findings.append({
            "area": "versions", "name": f"low-{i}",
            "kind": "version_overdue", "severity": "low",
            "detail": {"fix": {"tier": "human"}, "leaked": _PII_SENTINEL}})
    assert len(findings) == 200

    out = summarize_for_ai(_snap_with(), findings)

    # finding_total is complete.
    assert out["finding_total"] == 200

    # Counts are COMPLETE over ALL findings (not just the sample).
    assert out["finding_counts_by_kind"]["done_but_unresolved"] == 3
    assert out["finding_counts_by_kind"]["version_overdue"] == 197
    assert out["finding_counts_by_severity"]["high"] == 3
    assert out["finding_counts_by_severity"]["low"] == 197
    assert sum(out["finding_counts_by_kind"].values()) == 200
    assert sum(out["finding_counts_by_severity"].values()) == 200

    sample = out["finding_sample"]
    # Sample is capped (raised from 40: latency is size-independent, so starving
    # the model of context only made the analysis shallow — the cap is now 150).
    assert len(sample) == _AI_FINDING_CAP == 150
    # Every high-severity finding is present (highs are sampled first and never
    # crowded out by the 197 lows).
    high_in_sample = [f for f in sample if f["severity"] == "high"]
    assert len(high_in_sample) == 3
    # The first three sampled entries are the highs (severity-prioritised order).
    assert [f["severity"] for f in sample[:3]] == ["high", "high", "high"]

    # Privacy: metadata only — the PII/secret sentinel never leaves, in the
    # sample OR the counts.
    assert _PII_SENTINEL not in _ser(out)
    # Each sampled entry carries ONLY the allowlisted keys (no detail/secret).
    for f in sample:
        assert set(f.keys()) <= {"area", "name", "kind", "severity", "fix_tier"}


def test_findings_summary_empty():
    """No findings: counts are empty dicts, total 0, sample empty — never raises."""
    out = summarize_for_ai(_snap_with(), [])
    assert out["finding_total"] == 0
    assert out["finding_counts_by_kind"] == {}
    assert out["finding_counts_by_severity"] == {}
    assert out["finding_sample"] == []


# ---------------------------------------------------------------------------
# PAYLOAD SIZE BOUND (E2BIG fix): a large real-world-scale snapshot must produce
# a compact, bounded AI payload — well under any argv length limit — while still
# carrying COMPLETE aggregate/finding counts. The bug: a 187-project instance
# (1028 components, 3883 versions, 1600 findings) sent a per-OBJECT list for
# every component/version, which made the prompt argv exceed the OS limit and
# the claude-bridge CLI failed with `spawnSync claude E2BIG`.
# ---------------------------------------------------------------------------

def test_ai_payload_is_bounded_for_large_instance():
    from auditor.envaudit.analysis import _AI_NAME_CAP, _AI_FINDING_CAP
    # 200 projects, 2000 components (10 each), 4000 versions (20 each).
    comp_by_project = {}
    ver_by_project = {}
    leaderless_expected = 0
    for p in range(200):
        pkey = f"PROJ{p}"
        comps = []
        for c in range(10):
            has_lead = (c % 2 == 0)
            if not has_lead:
                leaderless_expected += 1
            comps.append({"name": f"Component-{p}-{c}-with-a-long-name",
                          "has_lead": has_lead,
                          "assignee_type": "UNASSIGNED" if c == 0 else "PROJECT_LEAD"})
        comp_by_project[pkey] = comps
        vers = []
        for v in range(20):
            vers.append({"name": f"version-{p}-{v}-2025.01.{v:02d}",
                         "released": (v % 3 == 0),
                         "archived": (v % 5 == 0),
                         "overdue": (v % 4 == 0)})
        ver_by_project[pkey] = vers

    # 500 custom fields (a `names` area) + a big group/status set.
    cf_names = [f"Custom Field Number {i} (migrated)" for i in range(500)]
    status_names = [f"Status-{i}" for i in range(300)]

    snap = {
        "deployment": "cloud",
        "projects": [f"PROJ{p}" for p in range(200)],
        "areas": {
            "components": {"count": 2000, "by_project": comp_by_project},
            "versions": {"count": 4000, "by_project": ver_by_project},
            "custom_fields": {"names": cf_names, "count": 500,
                              "by_type": {n: "select" for n in cf_names}},
            "statuses": {"names": status_names, "count": 300},
        },
    }
    # 1600 findings (mix of severities).
    findings = []
    for i in range(1600):
        sev = ("high" if i < 50 else "medium" if i < 300 else "low")
        findings.append({"area": "versions", "name": f"PROJ{i % 200} / version-{i}",
                         "kind": "version_overdue", "severity": sev,
                         "detail": {"fix": {"tier": "human"}}})

    out = summarize_for_ai(snap, findings)
    blob = json.dumps(out, default=str)

    # SANITY BOUND only. The old hard <25KB cap guarded a broken proxy that spawned
    # `claude` with the prompt as ARGV (E2BIG). The live path pipes on STDIN (no
    # argv limit) and latency is size-independent, so the aggressive cap was pure
    # shallowness. We keep a generous bound to catch accidental unbounded growth.
    assert len(blob) < 250000, f"AI payload unexpectedly huge: {len(blob)} bytes"

    # Completeness is preserved despite the shrink.
    assert out["finding_total"] == 1600
    assert sum(out["finding_counts_by_severity"].values()) == 1600
    assert len(out["finding_sample"]) == _AI_FINDING_CAP

    comp = out["areas"]["components"]
    assert comp["count"] == 2000
    assert comp["aggregate"]["total"] == 2000
    assert comp["aggregate"]["leaderless"] == leaderless_expected == 1000
    assert comp["aggregate"]["projects_with_components"] == 200
    # 1 UNASSIGNED component per project.
    assert comp["aggregate"]["unassigned_default"] == 200

    ver = out["areas"]["versions"]
    assert ver["count"] == 4000
    assert ver["aggregate"]["total"] == 4000

    # The `names` areas are capped but the full count is preserved.
    cf = out["areas"]["custom_fields"]
    assert cf["count"] == 500
    assert len(cf["names"]) == _AI_NAME_CAP == 80


def test_names_cap_with_full_count():
    """An area with 500 names forwards exactly _AI_NAME_CAP (80) names plus the
    complete count (so the AI still knows the true scale)."""
    from auditor.envaudit.analysis import _AI_NAME_CAP
    names = [f"thing-{i}" for i in range(500)]
    snap = _snap_with(custom_fields={"names": names, "count": 500})
    area = summarize_for_ai(snap, [])["areas"]["custom_fields"]
    assert len(area["names"]) == _AI_NAME_CAP == 80
    assert area["names"] == names[:80]      # first 80, in order
    assert area["count"] == 500             # full count preserved


def test_permission_scheme_grants_caps_lowered():
    """schemes capped at <=30, grants per scheme capped at <=50."""
    by_scheme = {}
    for s in range(60):
        by_scheme[f"Scheme {s}"] = [
            {"permission": "BROWSE_PROJECTS", "holder_type": "group"}
            for _ in range(100)]
    snap = _snap_with(permission_scheme_grants={"count": 60, "by_scheme": by_scheme})
    area = summarize_for_ai(snap, [])["areas"]["permission_scheme_grants"]
    assert len(area["by_scheme"]) <= 30
    for grants in area["by_scheme"].values():
        assert len(grants) <= 50


# --- analyze with fix_tier in findings ---

def test_analyze_with_fix_tier_in_findings_parses_correctly():
    """analyze must still parse a well-formed JSON response when findings carry fix_tier."""
    js = ('{"health_score": 55, "grade": "C", "summary": "needs work", '
          '"themes": [], "top_risks": ["empty groups"], "quick_wins": ["delete empty group"]}')
    findings = [{"area": "groups", "name": "empty-g", "kind": "empty_group",
                 "severity": "low",
                 "detail": {"fix": {"tier": "app", "title": "Delete"}}}]
    snap = _snap_with(groups={"names": ["empty-g"], "count": 1,
                               "member_counts": {"empty-g": 0}, "capped": False})
    out = analyze(snap, findings, _Client([_Resp([_Block("text", text=js)])]))
    assert out["error"] is None
    assert out["health_score"] == 55
    assert out["grade"] == "C"
    assert "delete empty group" in out["quick_wins"]


def test_by_type_forwards_only_string_type_labels():
    """by_type forwards only string type labels; a structured/secret value in a
    by_type key is DROPPED, never passed through (uniform allowlist)."""
    snap = {"deployment": "cloud", "projects": ["ACME"], "areas": {
        "custom_fields": {"names": ["Severity", "Team"], "count": 2,
                          "by_type": {"Severity": "select",
                                      "Team": {"nested": "SECRET leak"}}}}}
    out = summarize_for_ai(snap, [])
    assert "SECRET leak" not in str(out)             # structured value dropped
    assert out["areas"]["custom_fields"]["by_type"] == {"Severity": "select"}


# ---------------------------------------------------------------------------
# SECTION 2 new-area leak tests (project activity + shared-object ownership)
# Inject a fake owner/lead identity into the gathered raw data path and assert
# it reaches NEITHER the forwarded AI payload NOR any structural value, while
# the legitimate metadata (counts/booleans/KEYS) IS forwarded.
# ---------------------------------------------------------------------------

_PII_PROJ_LEAD = "Pippa ProjectLead"
_PII_PROJ_AID = "557058:proj-lead-secret"
_PII_FILTER_OWNER = "Fred FilterOwner"
_PII_FILTER_AID = "557058:filter-owner-secret"
_PII_DASH_OWNER = "dana.dashboard@acme.example"


# --- projects activity ---

def test_leak_projects_lead_identity_never_forwarded():
    """A project lead name/accountId injected into the projects area must NOT
    appear; issue_count + stale booleans + project KEY MUST appear."""
    snap = _snap_with(projects={
        "count": 2,
        "by_project": {
            "ACME": {"issue_count": 0, "stale": False,
                     # PII injection: gather never stores these, guard anyway
                     "lead": _PII_PROJ_LEAD, "leadAccountId": _PII_PROJ_AID},
            "OLD": {"issue_count": 9, "stale": True},
        },
    })
    out = _ser(summarize_for_ai(snap, []))
    assert _PII_PROJ_LEAD not in out, "project lead name leaked"
    assert _PII_PROJ_AID not in out, "project leadAccountId leaked"
    # Legitimate metadata must reach the AI
    assert "ACME" in out, "project KEY not forwarded"
    assert "issue_count" in out, "issue_count not forwarded"
    assert "stale" in out, "stale boolean not forwarded"


def test_projects_activity_forwarded_shape():
    snap = _snap_with(projects={
        "count": 1,
        "by_project": {"ACME": {"issue_count": 5, "stale": True}},
    })
    out = summarize_for_ai(snap, [])
    area = out["areas"]["projects"]
    assert area["count"] == 1
    assert area["by_project"]["ACME"] == {"issue_count": 5, "stale": True}


def test_projects_activity_skipped_forwarded():
    snap = _snap_with(projects={"skipped": True, "reason": "DC"})
    out = summarize_for_ai(snap, [])
    assert out["areas"]["projects"]["skipped"] is True


# --- filters shared-object ownership ---

def test_leak_filters_owner_identity_never_forwarded():
    """An owner name/accountId injected into a filter item must NOT appear;
    the aggregate inactive-owned / public counts MUST appear."""
    snap = _snap_with(filters={
        "count": 2,
        "capped": False,
        "items": [
            {"owner_active": False, "public": True,
             # PII injection: gather never stores these, guard anyway
             "owner_name": _PII_FILTER_OWNER, "ownerAccountId": _PII_FILTER_AID},
            {"owner_active": True, "public": False},
        ],
    })
    out = _ser(summarize_for_ai(snap, []))
    assert _PII_FILTER_OWNER not in out, "filter owner name leaked"
    assert _PII_FILTER_AID not in out, "filter ownerAccountId leaked"
    # Aggregate counts must reach the AI
    area = summarize_for_ai(snap, [])["areas"]["filters"]
    assert area["count"] == 2
    assert area["inactive_owned"] == 1
    assert area["public"] == 1


def test_filters_items_list_not_forwarded_verbatim():
    """The raw per-object items list must NOT be forwarded; only aggregates."""
    snap = _snap_with(filters={
        "count": 1, "capped": False,
        "items": [{"owner_active": False, "public": True}]})
    area = summarize_for_ai(snap, [])["areas"]["filters"]
    assert "items" not in area, "raw filter items list must not be forwarded"


def test_filters_dc_count_only_no_aggregates():
    """DC filters carry no items -> aggregates absent, count still forwarded."""
    snap = _snap_with(filters={"count": 7, "capped": False})
    area = summarize_for_ai(snap, [])["areas"]["filters"]
    assert area["count"] == 7
    assert "inactive_owned" not in area
    assert "public" not in area


# --- dashboards shared-object ownership ---

def test_leak_dashboards_owner_identity_never_forwarded():
    snap = _snap_with(dashboards={
        "count": 2,
        "capped": False,
        "items": [
            {"owner_active": False, "public": True,
             "owner_email": _PII_DASH_OWNER},
            {"owner_active": False, "public": False},
        ],
    })
    out = _ser(summarize_for_ai(snap, []))
    assert _PII_DASH_OWNER not in out, "dashboard owner email leaked"
    area = summarize_for_ai(snap, [])["areas"]["dashboards"]
    assert area["count"] == 2
    assert area["inactive_owned"] == 2
    assert area["public"] == 1


def test_dashboards_items_list_not_forwarded_verbatim():
    snap = _snap_with(dashboards={
        "count": 1, "capped": False,
        "items": [{"owner_active": False, "public": True}]})
    area = summarize_for_ai(snap, [])["areas"]["dashboards"]
    assert "items" not in area


# ---------------------------------------------------------------------------
# SECTION 3 (ISSUE-LEVEL / DATA QUALITY) allowlist + leak tests
# Only the integer count metrics flow to the AI. A crafted issue-content string
# injected into the area must be DROPPED (the allowlist forwards ints only).
# ---------------------------------------------------------------------------

_PII_ISSUE_SUMMARY = "CONFIDENTIAL customer outage details"
_PII_ISSUE_KEY = "SECRET-9001"


def test_issue_quality_integer_metrics_forwarded():
    snap = _snap_with(issue_quality={
        "done_unresolved": 4, "stale_open": 60,
        "unassigned_unresolved": 120, "resolved_but_open": 3,
        "total_unresolved": 200, "error": None})
    area = summarize_for_ai(snap, [])["areas"]["issue_quality"]
    assert area["done_unresolved"] == 4
    assert area["stale_open"] == 60
    assert area["unassigned_unresolved"] == 120
    assert area["resolved_but_open"] == 3
    assert area["total_unresolved"] == 200


def test_leak_issue_quality_only_integers_flow():
    """A crafted issue-content string injected into the issue_quality area must
    NOT reach the AI payload; the integer counts MUST. The allowlist forwards
    only int metric values."""
    snap = _snap_with(issue_quality={
        "done_unresolved": 7,
        "total_unresolved": 50,
        # Hostile injection: a non-int value carrying issue content.
        "leaked_summary": _PII_ISSUE_SUMMARY,
        "issue_keys": [_PII_ISSUE_KEY],
        # A metric key whose value is a dict trying to smuggle content through.
        "stale_open": {"nested": _PII_ISSUE_SUMMARY},
    })
    out = _ser(summarize_for_ai(snap, []))
    assert _PII_ISSUE_SUMMARY not in out, "issue content leaked to AI payload"
    assert _PII_ISSUE_KEY not in out, "issue key leaked to AI payload"
    area = summarize_for_ai(snap, [])["areas"]["issue_quality"]
    # The integer metrics survive.
    assert area["done_unresolved"] == 7
    assert area["total_unresolved"] == 50
    # The smuggled dict value is dropped (not forwarded as a metric).
    assert area.get("stale_open") is None or isinstance(area.get("stale_open"), int)
    # No non-metric key leaks through.
    assert "leaked_summary" not in area
    assert "issue_keys" not in area


def test_issue_quality_none_metric_forwarded_as_none():
    """A None metric (unevaluable) forwards as None, not dropped silently into
    a misleading absence — the AI should see the gap."""
    snap = _snap_with(issue_quality={
        "done_unresolved": None, "stale_open": 12,
        "unassigned_unresolved": None, "resolved_but_open": None,
        "total_unresolved": 100, "error": None})
    area = summarize_for_ai(snap, [])["areas"]["issue_quality"]
    assert area["done_unresolved"] is None
    assert area["stale_open"] == 12
    assert area["total_unresolved"] == 100


def test_issue_quality_skipped_forwarded():
    snap = _snap_with(issue_quality={"skipped": True, "reason": "x"})
    out = summarize_for_ai(snap, [])
    assert out["areas"]["issue_quality"]["skipped"] is True


# ---------------------------------------------------------------------------
# CONFLUENCE product-aware allowlist + leak tests (spec R4)
# summarize_for_ai(snap, findings, product="confluence") forwards ONLY metadata
# (counts/booleans/types/global-space keys). It must NEVER forward a page title,
# a space-admin name/accountId, a personal-space key, an email, or a member
# identity.
# ---------------------------------------------------------------------------

_PII_PAGE_TITLE = "CONFIDENTIAL Q3 Revenue Projections"
_PII_SPACE_ADMIN = "Wendy SpaceAdmin"
_PII_SPACE_ADMIN_AID = "557058:space-admin-secret"
_PII_PERSONAL_KEY = "~jane.doe.personal"
_PII_CONF_MEMBER = "conf.member@corp.example"


def _conf_spaces_area():
    return {
        "by_space": {
            "ENG": {"name": "Engineering", "type": "global", "status": "current",
                    "has_homepage": True, "page_count": 5000},
            "OLD": {"name": "Archived Stuff", "type": "global",
                    "status": "archived", "has_homepage": False,
                    "page_count": 0},
        },
        "count": 12,
        "personal_count": 7,
        "archived_count": 3,
    }


def test_confluence_spaces_metadata_forwarded():
    """Confluence spaces area forwards counts + per-global-space type/status/
    has_homepage/page_count metadata."""
    snap = _snap_with(spaces=_conf_spaces_area())
    out = summarize_for_ai(snap, [], product="confluence")
    area = out["areas"]["spaces"]
    assert area["count"] == 12
    assert area["personal_count"] == 7
    assert area["archived_count"] == 3
    eng = area["by_space"]["ENG"]
    assert eng["type"] == "global"
    assert eng["status"] == "current"
    assert eng["has_homepage"] is True
    assert eng["page_count"] == 5000


def test_confluence_space_permissions_types_forwarded():
    """space_permissions forwards principal/operation TYPES + has_admin +
    anonymous booleans per space."""
    snap = _snap_with(space_permissions={
        "by_space": {
            "ENG": {"principal_types": ["group", "user"],
                    "operations": ["read", "create"],
                    "has_admin": True, "anonymous": False},
            "PUB": {"principal_types": ["anonymous"],
                    "operations": ["read"],
                    "has_admin": False, "anonymous": True},
        },
    })
    out = summarize_for_ai(snap, [], product="confluence")
    area = out["areas"]["space_permissions"]
    eng = area["by_space"]["ENG"]
    assert eng["principal_types"] == ["group", "user"]
    assert eng["operations"] == ["read", "create"]
    assert eng["has_admin"] is True
    assert eng["anonymous"] is False
    pub = area["by_space"]["PUB"]
    assert pub["has_admin"] is False
    assert pub["anonymous"] is True


def test_confluence_content_quality_integer_metrics_forwarded():
    snap = _snap_with(content_quality={
        "pages_total": 4000, "stale_pages": 2500, "drafts": 120,
        "orphaned_pages": None})
    out = summarize_for_ai(snap, [], product="confluence")
    area = out["areas"]["content_quality"]
    assert area["pages_total"] == 4000
    assert area["stale_pages"] == 2500
    assert area["drafts"] == 120
    assert area["orphaned_pages"] is None


def test_confluence_templates_and_labels_counts_forwarded():
    snap = _snap_with(
        templates={"global_count": 14, "blueprint_count": 6},
        labels={"global_count": 720})
    out = summarize_for_ai(snap, [], product="confluence")
    assert out["areas"]["templates"]["global_count"] == 14
    assert out["areas"]["templates"]["blueprint_count"] == 6
    assert out["areas"]["labels"]["global_count"] == 720


def test_confluence_groups_names_and_member_counts_forwarded():
    """Confluence groups forward the same metadata as Jira: names + count +
    member_counts integers — never member identities."""
    snap = _snap_with(groups={
        "names": ["confluence-users", "site-admins"],
        "count": 2,
        "member_counts": {"confluence-users": 40, "site-admins": 3},
        "capped": False,
        "members": [_PII_CONF_MEMBER],
    })
    out = summarize_for_ai(snap, [], product="confluence")
    ser = _ser(out)
    assert _PII_CONF_MEMBER not in ser
    area = out["areas"]["groups"]
    assert area["count"] == 2
    assert "confluence-users" in area["names"]
    assert area["member_counts"]["confluence-users"] == 40


def test_leak_confluence_no_identities_or_content_reach_ai():
    """The headline Confluence privacy guard: inject a page title, a space-admin
    name + accountId, a personal-space key, and an email into EVERY Confluence
    area, then assert NONE of them reach the AI payload, while legitimate
    metadata IS forwarded."""
    spaces = _conf_spaces_area()
    # Smuggle a personal-space key, an admin identity, and a page title into the
    # per-space record + the aggregate.
    spaces["by_space"]["ENG"]["admin_name"] = _PII_SPACE_ADMIN
    spaces["by_space"]["ENG"]["adminAccountId"] = _PII_SPACE_ADMIN_AID
    spaces["by_space"]["ENG"]["homepage_title"] = _PII_PAGE_TITLE
    spaces["by_space"][_PII_PERSONAL_KEY] = {
        "name": "Jane Doe", "type": "personal", "status": "current",
        "has_homepage": True, "page_count": 3}
    snap = _snap_with(
        spaces=spaces,
        space_permissions={"by_space": {
            "ENG": {"principal_types": ["user"], "operations": ["administer"],
                    "has_admin": True, "anonymous": False,
                    "admin_principal": _PII_SPACE_ADMIN,
                    "adminAccountId": _PII_SPACE_ADMIN_AID}}},
        content_quality={"pages_total": 100, "stale_pages": 90, "drafts": 1,
                         "orphaned_pages": None,
                         "sample_title": _PII_PAGE_TITLE},
        groups={"names": ["g1"], "count": 1, "member_counts": {"g1": 2},
                "members": [_PII_CONF_MEMBER]},
        templates={"global_count": 5, "blueprint_count": 2},
        labels={"global_count": 10})
    out = _ser(summarize_for_ai(snap, [], product="confluence"))
    # PRIVACY: no identity / content / personal-space key reaches the AI.
    assert _PII_PAGE_TITLE not in out, "page title leaked to AI payload"
    assert _PII_SPACE_ADMIN not in out, "space-admin name leaked"
    assert _PII_SPACE_ADMIN_AID not in out, "space-admin accountId leaked"
    assert _PII_PERSONAL_KEY not in out, "personal-space key leaked"
    assert _PII_CONF_MEMBER not in out, "group member identity leaked"
    # Legitimate metadata IS forwarded.
    assert '"ENG"' in out            # global-space KEY (config identifier)
    assert "has_homepage" in out
    assert "principal_types" in out


def test_confluence_skipped_area_forwarded():
    snap = _snap_with(content_quality={"skipped": True, "reason": "DC"})
    out = summarize_for_ai(snap, [], product="confluence")
    assert out["areas"]["content_quality"]["skipped"] is True


# --- analyze() product-prompt selection ---

def test_analyze_confluence_selects_confluence_prompt_and_message():
    """product='confluence' selects _SYSTEM_CONFLUENCE + a Confluence user
    message; the Jira prompt/message must NOT be used."""
    from auditor.envaudit.analysis import _SYSTEM_CONFLUENCE
    js = ('{"health_score": 70, "grade": "B", "summary": "ok", '
          '"themes": [], "top_risks": [], "quick_wins": []}')
    prov = _FakeProvider({"text": js, "error": None, "refused": False,
                          "model": "albert-heavy"})
    out = analyze(_snap_with(spaces=_conf_spaces_area()), [], prov,
                  product="confluence")
    assert out["error"] is None and out["health_score"] == 70
    call = prov.calls[0]
    assert call["system"] == _SYSTEM_CONFLUENCE
    assert "Confluence" in call["user_content"]
    assert "Audit this Jira environment" not in call["user_content"]


def test_analyze_jira_default_unchanged():
    """product defaults to jira: the Jira prompt + Jira user message are used."""
    from auditor.envaudit.analysis import _SYSTEM as _SYSTEM_JIRA
    js = ('{"health_score": 80, "grade": "A", "summary": "ok", '
          '"themes": [], "top_risks": [], "quick_wins": []}')
    prov = _FakeProvider({"text": js, "error": None, "refused": False,
                          "model": "m"})
    out = analyze({"areas": {}}, [], prov)
    assert out["error"] is None
    call = prov.calls[0]
    assert call["system"] == _SYSTEM_JIRA
    assert "Audit this Jira environment configuration" in call["user_content"]


def test_confluence_system_prompt_covers_lenses():
    """_SYSTEM_CONFLUENCE frames a Confluence administrator across the four
    lenses (space hygiene, permissions/security, content/data quality,
    configuration) and keeps the JSON-only output contract."""
    from auditor.envaudit.analysis import _SYSTEM_CONFLUENCE
    low = _SYSTEM_CONFLUENCE.lower()
    assert "confluence" in low
    assert "space" in low
    assert "permission" in low or "security" in low
    assert "health_score" in _SYSTEM_CONFLUENCE
    assert "JSON" in _SYSTEM_CONFLUENCE or "json" in low


# ---------------------------------------------------------------------------
# ai_findings: the AI acts as a SECOND, complementary auditor and emits
# additional issues the deterministic rule engine did NOT catch. Each item is
# {title, area, severity, observation, recommendation}. analyze() must forward
# the list when present and default to [] when absent/unparseable, never
# crashing. The system prompts must instruct the model to behave as a second
# auditor and document the ai_findings schema (source-guard). Privacy is
# UNCHANGED: the payload to the AI carries no new data.
# ---------------------------------------------------------------------------

_AI_FINDINGS_KEYS = {"title", "area", "severity", "observation", "recommendation"}


def test_analyze_forwards_ai_findings_when_present():
    """A provider returning JSON WITH an ai_findings array -> analyze() returns
    it parsed as a list of dicts each carrying the 5 keys."""
    js = ('{"health_score": 64, "grade": "C", "summary": "mixed", '
          '"themes": [], "top_risks": [], "quick_wins": [], '
          '"ai_findings": ['
          '{"title": "Inconsistent project key casing", '
          '"area": "projects", "severity": "low", '
          '"observation": "Some keys are lowercase while most are upper.", '
          '"recommendation": "Standardise project key casing."}, '
          '{"title": "Admin granted to all-logged-in", '
          '"area": "permission_scheme_grants", "severity": "high", '
          '"observation": "ADMINISTER is held by a logged-in access class.", '
          '"recommendation": "Restrict ADMINISTER to a named admin group."}]}')
    prov = _FakeProvider({"text": js, "error": None, "refused": False,
                          "model": "albert-heavy"})
    out = analyze({"areas": {}}, [], prov)
    assert out["error"] is None
    fnd = out["ai_findings"]
    assert isinstance(fnd, list) and len(fnd) == 2
    for item in fnd:
        assert isinstance(item, dict)
        assert _AI_FINDINGS_KEYS <= set(item.keys())
    assert fnd[1]["severity"] == "high"
    assert fnd[0]["area"] == "projects"


def test_analyze_defaults_ai_findings_to_empty_list_when_absent():
    """A well-formed response WITHOUT ai_findings -> analyze() defaults the key
    to [] and never crashes."""
    js = ('{"health_score": 90, "grade": "A", "summary": "great", '
          '"themes": [], "top_risks": [], "quick_wins": []}')
    prov = _FakeProvider({"text": js, "error": None, "refused": False,
                          "model": "m"})
    out = analyze({"areas": {}}, [], prov)
    assert out["error"] is None
    assert out["ai_findings"] == []


def test_analyze_ai_findings_default_on_unparseable():
    """Non-JSON / unparseable text -> ai_findings still defaults to [] (no crash,
    same graceful path as the other keys)."""
    prov = _FakeProvider({"text": "the model rambled with no json",
                          "error": None, "refused": False, "model": "m"})
    out = analyze({"areas": {}}, [], prov)
    assert out["ai_findings"] == []


def test_analyze_no_provider_includes_empty_ai_findings():
    """The skipped (no-provider) return shape carries ai_findings == []."""
    out = analyze({"areas": {}}, [], None)
    assert out["skipped"] is True
    assert out["ai_findings"] == []


def test_analyze_refused_includes_empty_ai_findings():
    prov = _FakeProvider({"text": None, "error": None, "refused": True,
                          "model": "m"})
    out = analyze({"areas": {}}, [], prov)
    assert out["ai_findings"] == []


def test_analyze_error_includes_empty_ai_findings():
    prov = _FakeProvider({"text": None, "error": "endpoint down",
                          "refused": False, "model": "m"})
    out = analyze({"areas": {}}, [], prov)
    assert out["ai_findings"] == []


def test_ai_findings_non_list_response_defaults_to_empty():
    """A hostile/garbled ai_findings value that is not a list must be coerced to
    [] so the renderer always receives a list."""
    js = ('{"health_score": 50, "grade": "C", "summary": "x", '
          '"themes": [], "top_risks": [], "quick_wins": [], '
          '"ai_findings": "not a list"}')
    prov = _FakeProvider({"text": js, "error": None, "refused": False,
                          "model": "m"})
    out = analyze({"areas": {}}, [], prov)
    assert out["ai_findings"] == []


def test_system_prompts_instruct_second_auditor_and_ai_findings_schema():
    """Source-guard: BOTH system prompts frame the model as a SECOND auditor
    working alongside the deterministic rule engine, instruct it to surface
    ADDITIONAL issues, and document the ai_findings schema (the five keys) in
    the requested JSON object."""
    from auditor.envaudit.analysis import _SYSTEM, _SYSTEM_CONFLUENCE
    for prompt in (_SYSTEM, _SYSTEM_CONFLUENCE):
        low = prompt.lower()
        # second-auditor framing
        assert "second" in low and "auditor" in low
        assert "rule engine" in low or "rule-based" in low or "deterministic" in low
        assert "additional" in low
        # the ai_findings schema appears in the requested JSON object, with all
        # five field names documented.
        assert "ai_findings" in prompt
        for key in ("title", "area", "severity", "observation",
                    "recommendation"):
            assert key in prompt, f"{key} missing from ai_findings schema"
        # JSON-only output contract preserved.
        assert "JSON" in prompt or "json" in low


def test_ai_findings_privacy_payload_unchanged():
    """Privacy: enabling ai_findings sends NO new data. The user_content the
    provider receives is exactly the JSON-serialised summarize_for_ai payload
    (counts/booleans/types/capped names) — a secret in the snapshot never
    leaves, exactly as before."""
    snap = {"deployment": "cloud", "projects": ["ACME"], "areas": {
        "custom_fields": {"names": ["Severity"], "count": 1,
                          "by_type": {"Severity": "select"},
                          "secret_values": ["SECRET customer value"]}}}
    js = ('{"health_score": 70, "grade": "B", "summary": "ok", "themes": [], '
          '"top_risks": [], "quick_wins": [], "ai_findings": []}')
    prov = _FakeProvider({"text": js, "error": None, "refused": False,
                          "model": "m"})
    analyze(snap, [], prov)
    sent = prov.calls[0]["user_content"]
    # The exact metadata-only payload is what gets sent, no more.
    expected = "Audit this Jira environment configuration:\n" + json.dumps(
        summarize_for_ai(snap, [], product="jira"), default=str)
    assert sent == expected
    assert "SECRET customer value" not in sent   # privacy boundary intact


def test_analyze_through_claude_cli_provider(monkeypatch):
    """End-to-end: analyze() drives a ClaudeCLIProvider whose `claude -p` call is
    faked to emit clean JSON on stdout. The prompt rides on STDIN (input=), never
    argv (no E2BIG), and analyze()/_parse extract a real grade + health_score."""
    js = ('{"health_score": 88, "grade": "A", "summary": "ok", '
          '"themes": [], "top_risks": [], "quick_wins": []}')
    captured = {}

    class _Proc:
        stdout = js
        stderr = ""
        returncode = 0

    def _fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["input"] = kw.get("input")
        captured["shell"] = kw.get("shell")
        return _Proc()

    monkeypatch.setattr(subprocess, "run", _fake_run)

    snap = {"deployment": "cloud", "projects": ["ACME"], "areas": {}}
    out = analyze(snap, [], ClaudeCLIProvider(), product="jira")
    assert out["error"] is None and out["skipped"] is False
    assert out["health_score"] == 88 and out["grade"] == "A"
    # The whole audit prompt went through STDIN, not argv (the E2BIG fix).
    assert captured["cmd"] == ["claude", "-p", "--effort", "medium"]
    assert captured["input"] and "Audit this Jira environment" in captured["input"]
    assert captured["shell"] in (None, False)
