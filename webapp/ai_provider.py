"""Pluggable AI provider abstraction for the environment-audit AI analysis.

The environment audit can run its AI assessment against EITHER Anthropic (the
default) OR an OpenAI-compatible endpoint (a custom base_url + the official
`openai` SDK), selected in Settings. Both providers expose a uniform
``complete(system, user_content, *, model, effort) -> dict`` returning a
normalized ``{"text", "error", "refused", "model"}`` shape, so analysis.py is
provider-agnostic.

Privacy boundary (unchanged): summarize_for_ai runs BEFORE any provider call,
so the same allowlisted metadata is sent to whichever provider is configured —
swapping the provider never widens the outbound surface.

The OpenAI dependency is LAZILY imported (only when an OpenAI provider is built
or used), so the app imports and runs fine without the `openai` package
installed."""
from __future__ import annotations

import os
import subprocess

from .anthropic_key import anthropic_client

# Settings keys (stored in the same encrypted settings table as anthropic_key /
# oauth_client_secret). The api key is Fernet-encrypted via store.encrypt; the
# base_url and model are non-secret config.
_PROVIDER_KEY = "ai_provider"
_OPENAI_BASE_URL = "openai_base_url"
_OPENAI_MODEL = "openai_model"
_OPENAI_KEY_ENC = "openai_api_key_enc"

# Claude-CLI provider config (both NON-SECRET — the CLI uses the user's own local
# Claude auth, so there is no api key to store): an optional model override and
# an optional binary path (default "claude", resolved on PATH).
_CLAUDE_CLI_MODEL = "claude_cli_model"
_CLAUDE_CLI_BINARY = "claude_cli_binary"
_CLAUDE_CLI_TIMEOUT = "claude_cli_timeout"

# A full env-audit analysis is a single large local inference; the old 180s cap
# timed out real runs. Default to 10 min, overridable per-instance in Settings
# (claude_cli_timeout) or via MA_CLAUDE_CLI_TIMEOUT.
_DEFAULT_CLI_TIMEOUT = 600

_VALID_PROVIDERS = ("anthropic", "openai", "claude_cli")

# Default Anthropic model: the pinned Opus 4.8 snapshot. From the Claude 4.6
# generation on, the DATELESS id is itself a pinned snapshot (not an evergreen
# alias), so "claude-opus-4-8" is the correct pin — there is no dated variant.
# (A fabricated dated string 404s and silently disables the AI step.)
_ANTHROPIC_DEFAULT_MODEL = "claude-opus-4-8"


def _import_openai():
    """Lazy import of the `openai` SDK. Isolated in a tiny helper so tests can
    monkeypatch it and so the app never imports `openai` at module load.

    Raises a friendly, actionable ImportError when the optional package is
    absent — callers (stage_env_analysis) catch it and degrade the optional AI
    step to 'skipped' rather than failing the whole audit run."""
    try:
        import openai
    except ImportError as exc:
        raise ImportError(
            "The 'openai' package is required for the OpenAI-compatible AI "
            "provider but is not installed. Install it with: pip install openai "
            "— or switch the AI provider back to Anthropic in Settings."
        ) from exc
    return openai


def _is_openai_error(exc: BaseException) -> bool:
    """True if `exc` is an openai.OpenAIError. The `openai` import is lazy and
    happens only when an exception is being classified, so a SUCCESSFUL OpenAI
    completion never needs `openai` importable — and a missing package surfaces
    as 'not an OpenAIError' (the original exception re-raises) rather than
    masking the real error."""
    try:
        openai = _import_openai()
    except Exception:
        return False
    return isinstance(exc, openai.OpenAIError)


# --------------------------------------------------------------- config store

def save_openai_config(store, base_url: str, model: str, api_key: str) -> None:
    """Persist the OpenAI-compatible endpoint config. base_url + model are
    written when non-blank; the api key is Fernet-encrypted and a BLANK key
    leaves the previously-stored key unchanged (write-only password field)."""
    if base_url and base_url.strip():
        store.settings_set(_OPENAI_BASE_URL, base_url.strip())
    if model and model.strip():
        store.settings_set(_OPENAI_MODEL, model.strip())
    if api_key and api_key.strip():
        store.settings_set(
            _OPENAI_KEY_ENC, store.encrypt({"key": api_key.strip()}).decode())


def load_openai_config(store) -> dict | None:
    """Return {"base_url", "model", "api_key"} when fully configured, else None.

    All three pieces are required to build a client; if any is missing the
    config is incomplete and the factory treats the provider as unconfigured."""
    base_url = store.settings_get(_OPENAI_BASE_URL)
    model = store.settings_get(_OPENAI_MODEL)
    enc = store.settings_get(_OPENAI_KEY_ENC)
    if not (base_url and model and enc):
        return None
    try:
        api_key = store.decrypt(enc.encode()).get("key")
    except Exception:
        return None
    if not api_key:
        return None
    return {"base_url": base_url, "model": model, "api_key": api_key}


def save_claude_cli_config(store, model: str, binary: str,
                           timeout=None) -> None:
    """Persist the local Claude-CLI config. All values are NON-SECRET (the CLI
    uses local auth, so there is no api key): an optional model override, an
    optional binary path, and an optional per-call timeout (seconds). Each is
    written only when meaningfully set, so a stray POST that omits one never
    wipes a previously-saved value. A non-positive/garbage timeout is ignored."""
    if model and model.strip():
        store.settings_set(_CLAUDE_CLI_MODEL, model.strip())
    if binary and binary.strip():
        store.settings_set(_CLAUDE_CLI_BINARY, binary.strip())
    if timeout is not None:
        try:
            n = int(timeout)
        except (TypeError, ValueError):
            n = 0
        if n > 0:
            store.settings_set(_CLAUDE_CLI_TIMEOUT, str(n))


def _resolve_cli_timeout(store) -> int:
    """Resolved CLI timeout (seconds): saved setting → MA_CLAUDE_CLI_TIMEOUT env
    → _DEFAULT_CLI_TIMEOUT. A non-positive/garbage value at any layer falls
    through to the next, so the timeout is never zero/negative."""
    for raw in (store.settings_get(_CLAUDE_CLI_TIMEOUT),
                os.environ.get("MA_CLAUDE_CLI_TIMEOUT")):
        if raw:
            try:
                n = int(raw)
            except (TypeError, ValueError):
                continue
            if n > 0:
                return n
    return _DEFAULT_CLI_TIMEOUT


def load_claude_cli_config(store) -> dict:
    """Return {"model", "binary", "timeout"} for the local Claude-CLI provider.
    model may be None (= the CLI's default model); binary falls back to "claude"
    (resolved on PATH); timeout falls back to a generous default. This provider
    is ALWAYS considered configured — it needs no api key, since the CLI
    authenticates with the user's own local Claude session."""
    return {"model": store.settings_get(_CLAUDE_CLI_MODEL) or None,
            "binary": store.settings_get(_CLAUDE_CLI_BINARY) or "claude",
            "timeout": _resolve_cli_timeout(store)}


def get_provider_choice(store) -> str:
    """Return the selected provider ('anthropic' default | 'openai' |
    'claude_cli')."""
    choice = store.settings_get(_PROVIDER_KEY)
    return choice if choice in _VALID_PROVIDERS else "anthropic"


def set_provider_choice(store, choice: str) -> None:
    """Persist the active provider choice. An unknown value is ignored so the
    active provider can never be left in an invalid state."""
    if choice in _VALID_PROVIDERS:
        store.settings_set(_PROVIDER_KEY, choice)


# ------------------------------------------------------------------ providers

_SYSTEM_PLACEHOLDER = None  # (kept for symmetry; system is passed per-call)


def _normalized(text=None, error=None, refused=False, model=None) -> dict:
    return {"text": text, "error": error, "refused": refused, "model": model}


def _anthropic_text(resp) -> str:
    return "".join(b.text for b in resp.content
                   if getattr(b, "type", None) == "text"
                   and getattr(b, "text", None))


class AnthropicProvider:
    """Wraps an anthropic.Anthropic client. Holds the EXACT logic that used to
    live inline in analyze(): the 5-iteration pause_turn loop, refusal handling,
    pause-turn-exhaustion -> error, anthropic.APIError -> error, and text
    extraction. Behavior is preserved 1:1."""

    def __init__(self, client):
        self._client = client

    def complete(self, system: str, user_content: str, *, model=None,
                 effort: str = "medium") -> dict:
        import anthropic
        model = model or os.environ.get("MA_SOLUTIONS_MODEL",
                                        _ANTHROPIC_DEFAULT_MODEL)
        messages = [{"role": "user", "content": user_content}]
        try:
            for _ in range(5):
                resp = self._client.messages.create(
                    model=model, max_tokens=4000, system=system,
                    thinking={"type": "adaptive"},
                    output_config={"effort": effort}, messages=messages)
                if getattr(resp, "stop_reason", None) == "refusal":
                    return _normalized(refused=True, model=model)
                if getattr(resp, "stop_reason", None) == "pause_turn":
                    messages = messages + [
                        {"role": "assistant", "content": resp.content}]
                    continue
                break
            # If the loop exhausted on pause_turn, resp is still a paused
            # response with no final text — surface the exhaustion as an
            # explicit error instead of masking it as a bogus empty success.
            if getattr(resp, "stop_reason", None) == "pause_turn":
                return _normalized(
                    error="AI analysis did not complete: the model kept "
                          "pausing (server tool loop did not finish)",
                    model=model)
            return _normalized(text=_anthropic_text(resp), model=model)
        except anthropic.APIError as exc:
            return _normalized(error=f"AI analysis failed: {exc}", model=model)


class OpenAIProvider:
    """Wraps an OpenAI-compatible client. Calls chat.completions.create with a
    system + user message pair and user='igor'. No thinking / effort / web-search
    params — OpenAI-compatible endpoints may not support them."""

    def __init__(self, client, model: str):
        self._client = client
        self.model = model

    def complete(self, system: str, user_content: str, *, model=None,
                 effort: str = "medium") -> dict:
        use_model = model or self.model
        # Enforce a JSON object response so the structured assessment parses
        # reliably (the analysis prompts already require JSON). Supported by the
        # common OpenAI-compatible endpoints (OpenAI, LiteLLM, vLLM, Ollama); a
        # rare endpoint that rejects it surfaces a normalized error rather than
        # silently degrading. (effort/thinking are Anthropic/CLI-only.)
        try:
            resp = self._client.chat.completions.create(
                model=use_model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user_content}],
                user="igor", max_tokens=4000,
                response_format={"type": "json_object"})
            text = resp.choices[0].message.content
            return _normalized(text=text, model=use_model)
        except Exception as exc:
            # Surface an openai.OpenAIError (or any client-side failure) as a
            # normalized error. The lazy import here keeps `openai` out of the
            # happy path entirely — the module need not be importable for a
            # successful completion (only when classifying a raised error).
            if _is_openai_error(exc):
                return _normalized(error=f"AI analysis failed: {exc}",
                                   model=use_model)
            raise


class ClaudeCLIProvider:
    """Runs the LOCAL `claude` CLI in print mode with the prompt piped on STDIN.

    Why this exists: an OpenAI-compatible 'claude-bridge' proxy spawns its own
    claude with the prompt as an ARGV argument, which fails with E2BIG on a large
    audit (the argv exceeds the OS limit). Running `claude -p` directly with the
    prompt on STDIN has no argv size limit — and it uses the user's OWN local
    Claude auth, so no api key is configured or stored for this provider.

    The command is always a LIST (never shell=True), so there is no
    shell-injection surface, and the prompt only ever travels via input= (STDIN),
    never as a command argument. All failure modes are normalized to an error
    dict — complete() never raises."""

    def __init__(self, model=None, binary="claude", timeout=_DEFAULT_CLI_TIMEOUT):
        self.model = model
        self.binary = binary or "claude"
        self.timeout = timeout

    def complete(self, system: str, user_content: str, *, model=None,
                 effort: str = "medium") -> dict:
        use_model = model or self.model
        prompt = f"{system}\n\n{user_content}"
        cmd = [self.binary, "-p"]
        if use_model:
            cmd += ["--model", use_model]
        # The CLI exposes --effort (low|medium|high|xhigh|max); forwarding it is
        # what makes the audit reason DEEPLY instead of at default effort. An
        # unrecognised value is dropped (the CLI would reject it) rather than
        # passed through.
        if effort in ("low", "medium", "high", "xhigh", "max"):
            cmd += ["--effort", effort]
        result_model = use_model or "claude-cli"
        try:
            # Prompt goes on STDIN (input=), NEVER argv — so it cannot hit the OS
            # argv size limit (E2BIG) the way the proxy's argv-arg spawn does.
            # shell is left at its default (False): no shell-injection surface.
            proc = subprocess.run(cmd, input=prompt, capture_output=True,
                                  text=True, timeout=self.timeout)
        except FileNotFoundError:
            return _normalized(
                error=f"claude CLI not found on PATH (looked for "
                      f"{self.binary!r}). Install the Claude CLI or set the "
                      f"binary path in Settings.",
                model=result_model)
        except subprocess.TimeoutExpired:
            return _normalized(
                error=f"claude CLI timed out after {self.timeout}s",
                model=result_model)
        except Exception as exc:
            return _normalized(error=f"claude CLI failed: {exc}",
                               model=result_model)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:300]
            return _normalized(
                error=f"claude CLI exited {proc.returncode}: {detail}",
                model=result_model)
        return _normalized(text=proc.stdout, model=result_model)


# ------------------------------------------------------------------- factory

def ai_provider(store):
    """Return the configured provider, or None when the selected provider is
    unconfigured (mirrors anthropic_client returning None on a missing key, so
    analyze() treats it as 'AI skipped' and never blocks the run)."""
    choice = get_provider_choice(store)
    if choice == "claude_cli":
        # No api key: the local CLI uses the user's own Claude auth, so this
        # provider is ALWAYS configured (never None for a missing key).
        cfg = load_claude_cli_config(store)
        return ClaudeCLIProvider(model=cfg["model"], binary=cfg["binary"],
                                 timeout=cfg["timeout"])
    if choice == "openai":
        cfg = load_openai_config(store)
        if cfg is None:
            return None
        openai = _import_openai()
        # timeout: a cold-loading model on an OpenAI-compatible proxy (e.g.
        # LiteLLM->Ollama) can hang; cap it so the optional AI step fails fast
        # instead of stalling the audit for the SDK default (~10 min).
        # max_retries=1: the SDK retries 429/5xx, and proxies like LiteLLM ALSO
        # retry internally — stacking both can burn a rate-limited key (e.g. a
        # 60 req/min cap) on a single logical call. One retry is enough.
        # The base_url is operator-supplied and the whole metadata payload goes
        # there — apply the same SSRF guard as the audit clients so a misconfig/
        # hostile endpoint can't redirect the payload at cloud metadata or a
        # link-local address (a legit local proxy on http://localhost is allowed).
        from auditor.client import assert_safe_target
        assert_safe_target(cfg["base_url"])
        client = openai.OpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"],
                               timeout=120.0, max_retries=1)
        return OpenAIProvider(client, cfg["model"])
    # Default: anthropic.
    client = anthropic_client(store)
    if client is None:
        return None
    return AnthropicProvider(client)
