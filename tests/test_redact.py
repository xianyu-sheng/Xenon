"""The single redaction contract every writer shares (§11.7 / §13.2-3)."""

from __future__ import annotations

from xenon.utils.redact import REDACTED, contains_secret, redact_text


def test_env_style_assignments_keep_the_name() -> None:
    text = redact_text("OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz\nDB_PASSWORD=hunter2")
    assert "OPENAI_API_KEY=" + REDACTED in text
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in text
    assert "DB_PASSWORD=" + REDACTED in text
    assert "hunter2" not in text


def test_quoted_value_keeps_its_quotes() -> None:
    assert redact_text('api_key="abc123"') == f'api_key="{REDACTED}"'


def test_auth_scheme_and_provider_shapes() -> None:
    assert "abc.def.ghi" not in redact_text("Authorization: Bearer abc.def.ghi")
    assert "AIza" not in redact_text("key AIza" + "a" * 35)
    assert "ghp_" + "a" * 30 not in redact_text("token ghp_" + "a" * 30)
    assert "sk-ant-" not in redact_text("sk-ant-api03-" + "a" * 30)


def test_private_key_block_is_removed_whole() -> None:
    text = "before\n-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----\nafter"
    redacted = redact_text(text)
    assert "MIIabc" not in redacted
    assert redacted.startswith("before")
    assert redacted.endswith("after")


def test_query_values_only_when_the_name_is_sensitive() -> None:
    text = redact_text("https://h/p?token=xyz&page=2&api_key=k1")
    assert "token=" + REDACTED in text
    assert "api_key=" + REDACTED in text
    assert "page=2" in text


def test_chinese_labels() -> None:
    assert "s3cr3t" not in redact_text("密钥=s3cr3t")


def test_ordinary_text_is_untouched() -> None:
    for text in (
        "修复 calc.py 里的 bug，让 test_calc.py 通过",
        "def add(a, b): return a + b",
        "Reviewer asked for a token budget discussion",
        "https://example.com/page?id=7",
    ):
        assert redact_text(text) == text
        assert not contains_secret(text)


def test_contains_secret_answers_without_rewriting() -> None:
    assert contains_secret("OPENAI_API_KEY=abc")
    assert not contains_secret("nothing to see")


def test_short_provider_keys_are_still_credentials() -> None:
    """The merged list must not be weaker than the one it replaced.

    The memory guard refuses to persist a secret it can see; a 16-char
    threshold let 12–15-char ``sk-``/``ak-`` keys through into stored
    memories, which the previous per-module list caught.
    """

    assert contains_secret("ak-abcdefghijkl")
    assert contains_secret("sk-abcdefghijkl")
    assert not contains_secret("sk-learn")
