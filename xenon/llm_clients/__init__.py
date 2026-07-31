"""LLM client providers — pluggable per-provider modules.

Architecture note:
    The provider implementations currently live in xenon.utils.llm_client
    (marked with ``# ── Provider: <name> ──`` section comments).  They are
    designed to be extracted into separate files under this package as the
    provider surface grows.

    To add a new provider today:
    1. Add your implementation under a ``# ── Provider: my_provider ──``
       section in ``xenon/utils/llm_client.py``.
    2. Add an ``elif endpoint.provider == "my_provider":`` branch in
       ``chat_completion()``.

    When a provider grows beyond ~200 lines, extract it into its own
    ``_my_provider.py`` module here and wire it back via a local import.

Public types (re-exported from xenon.utils.llm_client):
"""

from xenon.utils.llm_client import (  # noqa: F401
    LLMResponse,
    LLMUsage,
    ModelEndpoint,
    ResponseTruncatedError,
    UsageTracker,
    build_endpoint,
    close_clients,
    parse_model_id,
    register_response_callback,
    register_usage_callback,
)
