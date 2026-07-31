"""LLM client providers — replaceable per-provider modules.

To add a new provider, create a module following the pattern in
_openai.py or _anthropic.py, then wire it into
xenon.utils.llm_client.chat_completion().

Public API (import from here or from xenon.utils.llm_client):
    LLMUsage, LLMResponse, ModelEndpoint, ResponseTruncatedError,
    UsageTracker, build_endpoint, parse_model_id, close_clients,
    register_response_callback, register_usage_callback
"""

from xenon.llm_clients._base import (  # noqa: F401
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
