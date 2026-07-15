from webapp.store import Store
from webapp.anthropic_key import save_key, load_key, anthropic_client


def test_key_roundtrip_encrypted(tmp_path):
    s = Store(str(tmp_path / "a.db"), str(tmp_path / "a.key"))
    assert load_key(s) is None
    save_key(s, "sk-ant-test-123")
    assert load_key(s) == "sk-ant-test-123"
    # stored value is encrypted, not plaintext
    raw = s.settings_get("anthropic_api_key_enc")
    assert raw and "sk-ant-test-123" not in raw


def test_anthropic_client_none_without_key(tmp_path):
    s = Store(str(tmp_path / "b.db"), str(tmp_path / "b.key"))
    assert anthropic_client(s) is None


def test_anthropic_client_built_with_key(tmp_path):
    s = Store(str(tmp_path / "c.db"), str(tmp_path / "c.key"))
    save_key(s, "sk-ant-test-xyz")
    client = anthropic_client(s)
    assert client is not None and hasattr(client, "messages")
