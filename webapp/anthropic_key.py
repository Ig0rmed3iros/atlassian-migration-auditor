"""Anthropic API key storage (Fernet-encrypted in settings) + client factory.

Mirrors the oauth_client_secret_enc pattern: the key never sits in the DB in
plaintext, and anthropic_client() returns None when unset so callers can show
an actionable 'add a key in Settings' prompt instead of crashing."""
from __future__ import annotations

_SETTING = "anthropic_api_key_enc"


def save_key(store, key: str) -> None:
    if key and key.strip():
        store.settings_set(_SETTING, store.encrypt({"key": key.strip()}).decode())


def load_key(store) -> str | None:
    enc = store.settings_get(_SETTING)
    if not enc:
        return None
    try:
        return store.decrypt(enc.encode()).get("key")
    except Exception:
        return None


def anthropic_client(store):
    """Build an anthropic.Anthropic from the stored key, or None when unset.
    Imported lazily so the dependency is only needed when the feature is used."""
    key = load_key(store)
    if not key:
        return None
    import anthropic
    return anthropic.Anthropic(api_key=key)
