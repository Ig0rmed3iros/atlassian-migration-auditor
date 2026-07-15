"""Tests for the pluggable AI provider abstraction (ai_provider.py).

Covers config storage + encryption-at-rest, provider-choice get/set, the
ai_provider(store) factory, and both provider classes' .complete() behavior.
No test makes a live network call — fake clients are injected, mirroring how
test_env_analysis injects a fake Anthropic client today."""
import anthropic

import subprocess

import pytest

from webapp.store import Store
from webapp.ai_provider import (
    save_openai_config, load_openai_config,
    save_claude_cli_config, load_claude_cli_config,
    get_provider_choice, set_provider_choice,
    ai_provider, AnthropicProvider, OpenAIProvider, ClaudeCLIProvider,
)


# ---------------------------------------------------------------------------
# Fakes — Anthropic message-shaped + OpenAI chat.completions-shaped.
# Placeholders only; NO real base_url / api_key anywhere (synthetic-scrubbed).
# ---------------------------------------------------------------------------

class _Block:
    def __init__(self, type, **kw):
        self.type = type
        for k, v in kw.items():
            setattr(self, k, v)


class _Resp:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason
        self.stop_details = None


class _Msgs:
    def __init__(self, responses):
        self._r = list(responses)
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        return self._r.pop(0)


class _AnthropicClient:
    def __init__(self, responses):
        self.messages = _Msgs(responses)


# --- fake OpenAI client (chat.completions.create) ---

class _OAChoiceMessage:
    def __init__(self, content):
        self.content = content


class _OAChoice:
    def __init__(self, content):
        self.message = _OAChoiceMessage(content)


class _OAResp:
    def __init__(self, content, model="albert-heavy"):
        self.choices = [_OAChoice(content)]
        self.model = model


class _OACompletions:
    def __init__(self, *, content=None, error=None):
        self._content = content
        self._error = error
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        if self._error is not None:
            raise self._error
        return _OAResp(self._content)


class _OAChat:
    def __init__(self, completions):
        self.completions = completions


class _OpenAIClient:
    def __init__(self, *, content=None, error=None):
        self.chat = _OAChat(_OACompletions(content=content, error=error))


# ---------------------------------------------------------------------------
# Config roundtrip + encryption-at-rest
# ---------------------------------------------------------------------------

def test_openai_config_roundtrip(tmp_path):
    s = Store(str(tmp_path / "a.db"), str(tmp_path / "a.key"))
    assert load_openai_config(s) is None
    save_openai_config(s, "https://example.test/v1", "albert-heavy", "sk-test-xxx")
    cfg = load_openai_config(s)
    assert cfg["base_url"] == "https://example.test/v1"
    assert cfg["model"] == "albert-heavy"
    assert cfg["api_key"] == "sk-test-xxx"


def test_openai_api_key_encrypted_at_rest(tmp_path):
    s = Store(str(tmp_path / "b.db"), str(tmp_path / "b.key"))
    save_openai_config(s, "https://example.test/v1", "albert-heavy", "sk-test-xxx")
    # The api key must be stored Fernet-encrypted, never in plaintext.
    raw = s.settings_get("openai_api_key_enc")
    assert raw and "sk-test-xxx" not in raw
    # base_url / model are non-secret config, but the key must not appear there.
    assert "sk-test-xxx" not in (s.settings_get("openai_base_url") or "")
    assert "sk-test-xxx" not in (s.settings_get("openai_model") or "")


def test_openai_config_blank_key_keeps_current(tmp_path):
    s = Store(str(tmp_path / "c.db"), str(tmp_path / "c.key"))
    save_openai_config(s, "https://example.test/v1", "albert-heavy", "sk-test-xxx")
    # Re-save with a blank key (write-only field left empty) -> key unchanged,
    # base_url / model still updated.
    save_openai_config(s, "https://example.test/v2", "albert-light", "")
    cfg = load_openai_config(s)
    assert cfg["base_url"] == "https://example.test/v2"
    assert cfg["model"] == "albert-light"
    assert cfg["api_key"] == "sk-test-xxx"


# ---------------------------------------------------------------------------
# Provider choice get/set
# ---------------------------------------------------------------------------

def test_provider_choice_defaults_to_anthropic(tmp_path):
    s = Store(str(tmp_path / "d.db"), str(tmp_path / "d.key"))
    assert get_provider_choice(s) == "anthropic"


def test_provider_choice_set_and_get(tmp_path):
    s = Store(str(tmp_path / "e.db"), str(tmp_path / "e.key"))
    set_provider_choice(s, "openai")
    assert get_provider_choice(s) == "openai"
    set_provider_choice(s, "anthropic")
    assert get_provider_choice(s) == "anthropic"


def test_provider_choice_rejects_unknown_keeps_default(tmp_path):
    s = Store(str(tmp_path / "f.db"), str(tmp_path / "f.key"))
    set_provider_choice(s, "bogus")
    # An unknown choice must not be persisted as the active provider.
    assert get_provider_choice(s) == "anthropic"


# ---------------------------------------------------------------------------
# ai_provider(store) factory
# ---------------------------------------------------------------------------

def test_ai_provider_none_when_anthropic_unconfigured(tmp_path):
    s = Store(str(tmp_path / "g.db"), str(tmp_path / "g.key"))
    # Default provider is anthropic; no key -> None (unconfigured).
    assert ai_provider(s) is None


def test_ai_provider_anthropic_when_key_set(tmp_path):
    from webapp.anthropic_key import save_key
    s = Store(str(tmp_path / "h.db"), str(tmp_path / "h.key"))
    save_key(s, "sk-ant-test-xyz")
    p = ai_provider(s)
    assert isinstance(p, AnthropicProvider)


def test_ai_provider_none_when_openai_selected_but_unconfigured(tmp_path):
    s = Store(str(tmp_path / "i.db"), str(tmp_path / "i.key"))
    set_provider_choice(s, "openai")
    # Selected openai but no config saved -> None.
    assert ai_provider(s) is None


def test_openai_base_url_rejects_metadata_target(tmp_path, monkeypatch):
    # Review: the OpenAI-compatible base_url is operator-supplied and the whole
    # metadata payload goes there — a metadata/link-local target must be blocked.
    s = Store(str(tmp_path / "m.db"), str(tmp_path / "m.key"))
    set_provider_choice(s, "openai")
    save_openai_config(s, "http://169.254.169.254/v1", "x", "sk-x")
    import webapp.ai_provider as ap

    class _FakeMod:
        class OpenAI:
            def __init__(self, **kw): pass
    monkeypatch.setattr(ap, "_import_openai", lambda: _FakeMod)
    with pytest.raises(ValueError):
        ai_provider(s)
    # A legit local proxy is allowed.
    save_openai_config(s, "http://localhost:4000/v1", "x", "sk-x")
    assert ai_provider(s) is not None


def test_ai_provider_openai_when_configured(tmp_path, monkeypatch):
    s = Store(str(tmp_path / "j.db"), str(tmp_path / "j.key"))
    set_provider_choice(s, "openai")
    save_openai_config(s, "https://example.test/v1", "albert-heavy", "sk-test-xxx")

    # Stub the lazy openai.OpenAI so no network/import is required in the test.
    built = {}

    class _FakeOpenAIModule:
        class OpenAI:
            def __init__(self, *, base_url, api_key, **kw):
                built["base_url"] = base_url
                built["api_key"] = api_key
                built["timeout"] = kw.get("timeout")
                built["max_retries"] = kw.get("max_retries")

    import webapp.ai_provider as ap
    monkeypatch.setattr(ap, "_import_openai", lambda: _FakeOpenAIModule)

    p = ai_provider(s)
    assert isinstance(p, OpenAIProvider)
    assert p.model == "albert-heavy"
    assert built["base_url"] == "https://example.test/v1"
    assert built["api_key"] == "sk-test-xxx"
    # Hardened client: a bounded timeout (cold-loading proxy models can hang)
    # and reduced retries (the SDK + a LiteLLM proxy both retry, which can burn
    # a rate-limited key on one logical call).
    assert built["timeout"] == 120.0
    assert built["max_retries"] == 1


# ---------------------------------------------------------------------------
# OpenAIProvider.complete — call shape + error handling
# ---------------------------------------------------------------------------

def test_openai_provider_builds_correct_call():
    js = '{"health_score": 80, "grade": "A"}'
    client = _OpenAIClient(content=js)
    prov = OpenAIProvider(client, "albert-heavy")
    r = prov.complete("SYS", "USER CONTENT", model=None, effort="medium")
    assert r["error"] is None
    assert r["refused"] is False
    assert r["text"] == js
    assert r["model"] == "albert-heavy"
    # Exactly one chat.completions.create call with the expected shape.
    call = client.chat.completions.calls[0]
    assert call["model"] == "albert-heavy"
    assert call["user"] == "igor"
    assert call["max_tokens"] == 4000          # bounded response
    assert call["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "USER CONTENT"},
    ]
    # No thinking / effort / web-search params leak into an OpenAI-compatible call.
    assert "thinking" not in call
    assert "output_config" not in call
    assert "tools" not in call
    # JSON output IS enforced so the structured assessment parses reliably.
    assert call["response_format"] == {"type": "json_object"}


def test_openai_provider_model_override():
    client = _OpenAIClient(content='{"grade": "B"}')
    prov = OpenAIProvider(client, "albert-heavy")
    prov.complete("SYS", "U", model="custom-model", effort="medium")
    assert client.chat.completions.calls[0]["model"] == "custom-model"


def test_openai_provider_surfaces_openai_error(monkeypatch):
    # Build a fake openai.OpenAIError subclass to inject (no real `openai`).
    import webapp.ai_provider as ap

    class _FakeOpenAIError(Exception):
        pass

    class _FakeOpenAIModule:
        OpenAIError = _FakeOpenAIError

    # Patch the lazy import so the provider classifies our fake error type as
    # an OpenAI error and returns the normalized error shape.
    monkeypatch.setattr(ap, "_import_openai", lambda: _FakeOpenAIModule)
    client = _OpenAIClient(error=_FakeOpenAIError("boom"))
    prov = OpenAIProvider(client, "albert-heavy")
    r = prov.complete("SYS", "U", model=None, effort="medium")
    assert r["text"] is None
    assert r["refused"] is False
    assert r["error"] and "boom" in r["error"]


# ---------------------------------------------------------------------------
# AnthropicProvider.complete — ports the existing analyze() coverage
# ---------------------------------------------------------------------------

def test_anthropic_provider_happy_returns_text():
    js = '{"health_score": 72, "grade": "B"}'
    prov = AnthropicProvider(_AnthropicClient([_Resp([_Block("text", text=js)])]))
    r = prov.complete("SYS", "U", model=None, effort="medium")
    assert r["error"] is None and r["refused"] is False
    assert r["text"] == js
    # Anthropic keeps the existing default model when none is supplied.
    assert r["model"] == "claude-opus-4-8"


def test_anthropic_default_model_is_real_pinned_id():
    # Regression (review Bug 6): the shipped default must be a model ID that
    # actually exists, or every Anthropic user's AI step 404s and silently
    # disables. Opus 4.8's only API ID is the DATELESS "claude-opus-4-8" — for
    # the 4.6+ generation the dateless form IS the pinned snapshot (no dated
    # variant exists). A fabricated dated string like "...-20251101" (which is
    # actually Opus 4.5's date) is the bug.
    from webapp import ai_provider as ap
    from auditor import solutions
    assert ap._ANTHROPIC_DEFAULT_MODEL == "claude-opus-4-8"
    # solutions.find_solutions falls back to the same pinned ID.
    src = solutions.find_solutions.__code__.co_consts
    assert "claude-opus-4-8" in src
    assert not any(isinstance(c, str) and c.startswith("claude-opus-4-8-")
                   for c in src), "no fabricated dated Opus 4.8 snapshot"


def test_anthropic_provider_refusal():
    prov = AnthropicProvider(_AnthropicClient([_Resp([], stop_reason="refusal")]))
    r = prov.complete("SYS", "U", model=None, effort="medium")
    assert r["refused"] is True
    assert r["text"] is None


def test_anthropic_provider_pause_turn_exhaustion_is_error():
    resps = [_Resp([], stop_reason="pause_turn") for _ in range(5)]
    prov = AnthropicProvider(_AnthropicClient(resps))
    r = prov.complete("SYS", "U", model=None, effort="medium")
    assert r["error"] and r["refused"] is False and r["text"] is None


def test_anthropic_provider_api_error_is_error():
    class _BoomMsgs:
        def create(self, **kw):
            raise anthropic.APIError("rate limited", request=None, body=None)

    class _BoomClient:
        messages = _BoomMsgs()

    prov = AnthropicProvider(_BoomClient())
    r = prov.complete("SYS", "U", model=None, effort="medium")
    assert r["error"] and r["refused"] is False and r["text"] is None


def test_anthropic_provider_passes_thinking_and_effort():
    js = '{"grade": "A"}'
    client = _AnthropicClient([_Resp([_Block("text", text=js)])])
    prov = AnthropicProvider(client)
    prov.complete("SYS", "U", model="claude-opus-4-8", effort="high")
    call = client.messages.calls[0]
    assert call["system"] == "SYS"
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"] == {"effort": "high"}
    assert call["messages"] == [{"role": "user", "content": "U"}]


# ---------------------------------------------------------------------------
# ClaudeCLIProvider.complete — runs `claude -p` with the prompt on STDIN.
#
# The prompt is piped to STDIN (never an argv argument) so a very large audit
# prompt cannot hit the OS argv size limit (E2BIG) — the exact failure mode of
# the OpenAI-compatible claude-bridge proxy, which spawns claude with the prompt
# as an argv arg. NO real CLI is ever invoked: subprocess.run is monkeypatched.
# ---------------------------------------------------------------------------

class _FakeProc:
    def __init__(self, *, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _capture_run(captured, *, stdout="", stderr="", returncode=0, raise_exc=None):
    """Build a fake subprocess.run that records its call and returns a fake proc
    (or raises raise_exc). Used to assert command shape + that the prompt rides
    on STDIN (input=) and shell=True is never used."""
    def _run(cmd, **kw):
        captured["cmd"] = cmd
        captured["kwargs"] = kw
        if raise_exc is not None:
            raise raise_exc
        return _FakeProc(stdout=stdout, stderr=stderr, returncode=returncode)
    return _run


def test_claude_cli_builds_command_and_pipes_prompt_on_stdin(monkeypatch):
    js = '{"health_score": 91, "grade": "A"}'
    captured = {}
    monkeypatch.setattr(subprocess, "run", _capture_run(captured, stdout=js))
    prov = ClaudeCLIProvider()
    r = prov.complete("SYS", "USER CONTENT", model=None, effort="medium")

    assert r["error"] is None and r["refused"] is False
    assert r["text"] == js
    # No model set -> the placeholder model label, not a real id.
    assert r["model"] == "claude-cli"

    # Command shape: ["claude", "-p"] (+ --effort) with NO --model when unset.
    assert captured["cmd"] == ["claude", "-p", "--effort", "medium"]
    kw = captured["kwargs"]
    # The full system+user prompt rides on STDIN (input=), NEVER as an argv arg.
    assert kw["input"] == "SYS\n\nUSER CONTENT"
    assert "SYS" not in captured["cmd"] and "USER CONTENT" not in captured["cmd"]
    # No shell-injection surface: a list command, shell never enabled.
    assert kw.get("shell") in (None, False)
    assert isinstance(captured["cmd"], list)
    # Decoded text I/O and captured output.
    assert kw["text"] is True
    assert kw["capture_output"] is True


def test_claude_cli_appends_model_when_set(monkeypatch):
    captured = {}
    monkeypatch.setattr(subprocess, "run",
                        _capture_run(captured, stdout='{"grade": "B"}'))
    prov = ClaudeCLIProvider(model="claude-opus-4-8")
    r = prov.complete("SYS", "U", model=None, effort="medium")
    assert captured["cmd"] == ["claude", "-p", "--model",
                               "claude-opus-4-8", "--effort", "medium"]
    # The configured model is echoed back as the result model.
    assert r["model"] == "claude-opus-4-8"


def test_claude_cli_default_timeout_is_generous():
    """A full env-audit analysis can take minutes on a local model; the old
    180s default timed out real runs. The default must be much larger."""
    assert ClaudeCLIProvider().timeout >= 600


def test_claude_cli_passes_timeout_to_subprocess(monkeypatch):
    captured = {}
    monkeypatch.setattr(subprocess, "run",
                        _capture_run(captured, stdout='{"grade": "A"}'))
    ClaudeCLIProvider(timeout=900).complete("SYS", "U", model=None,
                                             effort="medium")
    assert captured["kwargs"]["timeout"] == 900


def test_claude_cli_config_roundtrips_timeout(tmp_path):
    s = Store(str(tmp_path / "c.db"), str(tmp_path / "c.key"))
    # default present even before anything saved, and generous.
    assert load_claude_cli_config(s)["timeout"] >= 600
    save_claude_cli_config(s, "m", "claude", timeout=720)
    assert load_claude_cli_config(s)["timeout"] == 720


def test_claude_cli_config_ignores_bad_timeout(tmp_path):
    s = Store(str(tmp_path / "d.db"), str(tmp_path / "d.key"))
    save_claude_cli_config(s, "m", "claude", timeout=0)      # non-positive
    assert load_claude_cli_config(s)["timeout"] >= 600       # kept default
    save_claude_cli_config(s, "m", "claude", timeout=-5)
    assert load_claude_cli_config(s)["timeout"] >= 600


def test_ai_provider_claude_cli_uses_configured_timeout(tmp_path):
    s = Store(str(tmp_path / "e.db"), str(tmp_path / "e.key"))
    set_provider_choice(s, "claude_cli")
    save_claude_cli_config(s, "", "claude", timeout=840)
    prov = ai_provider(s)
    assert isinstance(prov, ClaudeCLIProvider) and prov.timeout == 840


def test_claude_cli_passes_effort_flag(monkeypatch):
    """The CLI exposes --effort (low|medium|high|xhigh|max); the provider must
    forward it so the audit runs with deep reasoning, not default."""
    captured = {}
    monkeypatch.setattr(subprocess, "run",
                        _capture_run(captured, stdout='{"grade": "A"}'))
    ClaudeCLIProvider().complete("S", "U", model=None, effort="high")
    assert "--effort" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--effort") + 1] == "high"


def test_claude_cli_passes_max_effort(monkeypatch):
    captured = {}
    monkeypatch.setattr(subprocess, "run",
                        _capture_run(captured, stdout='{"grade": "A"}'))
    ClaudeCLIProvider().complete("S", "U", model=None, effort="max")
    assert captured["cmd"][captured["cmd"].index("--effort") + 1] == "max"


def test_claude_cli_omits_invalid_effort(monkeypatch):
    captured = {}
    monkeypatch.setattr(subprocess, "run",
                        _capture_run(captured, stdout='{"grade": "A"}'))
    ClaudeCLIProvider().complete("S", "U", model=None, effort="bogus")
    assert "--effort" not in captured["cmd"]


def test_claude_cli_per_call_model_override(monkeypatch):
    captured = {}
    monkeypatch.setattr(subprocess, "run",
                        _capture_run(captured, stdout='{"grade": "C"}'))
    prov = ClaudeCLIProvider(model="default-model")
    r = prov.complete("SYS", "U", model="override-model", effort="medium")
    assert captured["cmd"] == ["claude", "-p", "--model", "override-model",
                               "--effort", "medium"]
    assert r["model"] == "override-model"


def test_claude_cli_custom_binary(monkeypatch):
    captured = {}
    monkeypatch.setattr(subprocess, "run",
                        _capture_run(captured, stdout='{"grade": "A"}'))
    prov = ClaudeCLIProvider(binary="/home/igor/.npm-global/bin/claude")
    prov.complete("SYS", "U", model=None, effort="medium")
    assert captured["cmd"][0] == "/home/igor/.npm-global/bin/claude"


def test_claude_cli_nonzero_returncode_is_error(monkeypatch):
    captured = {}
    monkeypatch.setattr(subprocess, "run", _capture_run(
        captured, stdout="", stderr="boom on the cli", returncode=2))
    prov = ClaudeCLIProvider()
    r = prov.complete("SYS", "U", model=None, effort="medium")
    assert r["text"] is None and r["refused"] is False
    assert r["error"] and "boom on the cli" in r["error"]
    assert "2" in r["error"]


def test_claude_cli_file_not_found_is_error(monkeypatch):
    captured = {}
    monkeypatch.setattr(subprocess, "run", _capture_run(
        captured, raise_exc=FileNotFoundError("no claude")))
    prov = ClaudeCLIProvider()
    r = prov.complete("SYS", "U", model=None, effort="medium")
    assert r["text"] is None and r["refused"] is False
    assert r["error"] and "not found" in r["error"].lower()


def test_claude_cli_timeout_is_error(monkeypatch):
    captured = {}
    monkeypatch.setattr(subprocess, "run", _capture_run(
        captured, raise_exc=subprocess.TimeoutExpired(cmd="claude", timeout=5)))
    prov = ClaudeCLIProvider(timeout=5)
    r = prov.complete("SYS", "U", model=None, effort="medium")
    assert r["text"] is None and r["refused"] is False
    assert r["error"] and "timed out" in r["error"].lower()
    # passes the configured timeout to subprocess.run.
    assert captured["kwargs"]["timeout"] == 5


def test_claude_cli_other_exception_is_error(monkeypatch):
    captured = {}
    monkeypatch.setattr(subprocess, "run", _capture_run(
        captured, raise_exc=RuntimeError("unexpected")))
    prov = ClaudeCLIProvider()
    r = prov.complete("SYS", "U", model=None, effort="medium")
    assert r["text"] is None and r["refused"] is False
    assert r["error"] and "unexpected" in r["error"]


# ---------------------------------------------------------------------------
# Provider choice + factory for the claude_cli provider.
# ---------------------------------------------------------------------------

def test_provider_choice_accepts_claude_cli(tmp_path):
    s = Store(str(tmp_path / "cli1.db"), str(tmp_path / "cli1.key"))
    set_provider_choice(s, "claude_cli")
    assert get_provider_choice(s) == "claude_cli"


def test_ai_provider_claude_cli_built_without_api_key(tmp_path):
    # The CLI uses LOCAL auth, so this provider is ALWAYS configured — the
    # factory returns it even with no Anthropic / OpenAI key stored at all.
    s = Store(str(tmp_path / "cli2.db"), str(tmp_path / "cli2.key"))
    set_provider_choice(s, "claude_cli")
    p = ai_provider(s)
    assert p is not None
    assert isinstance(p, ClaudeCLIProvider)


def test_ai_provider_claude_cli_uses_stored_model_and_binary(tmp_path):
    s = Store(str(tmp_path / "cli3.db"), str(tmp_path / "cli3.key"))
    set_provider_choice(s, "claude_cli")
    s.settings_set("claude_cli_model", "claude-opus-4-8")
    s.settings_set("claude_cli_binary", "/home/igor/.npm-global/bin/claude")
    p = ai_provider(s)
    assert isinstance(p, ClaudeCLIProvider)
    assert p.model == "claude-opus-4-8"
    assert p.binary == "/home/igor/.npm-global/bin/claude"


def test_ai_provider_claude_cli_defaults_binary_to_claude(tmp_path):
    s = Store(str(tmp_path / "cli4.db"), str(tmp_path / "cli4.key"))
    set_provider_choice(s, "claude_cli")
    p = ai_provider(s)
    assert isinstance(p, ClaudeCLIProvider)
    assert p.binary == "claude"
    assert p.model is None
