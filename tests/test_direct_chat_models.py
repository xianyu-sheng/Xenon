"""``-m`` accepts a registry alias; the provider layer needs provider/model."""

from __future__ import annotations

import types

from xenon.repl.direct_chat import DirectChatMixin


class _Registry:
    def __init__(self) -> None:
        self.models = {
            "deepseek-v4-pro": types.SimpleNamespace(
                model_id="deepseek/deepseek-v4-pro"
            ),
        }

    def get_model(self, alias: str):
        return self.models.get(alias)


def test_an_alias_is_mapped_to_the_canonical_model_id() -> None:
    repl = types.SimpleNamespace(registry=_Registry())

    assert DirectChatMixin._canonical_model_ids(
        repl, ["deepseek-v4-pro"]
    ) == ["deepseek/deepseek-v4-pro"]


def test_unknown_ids_are_left_untouched() -> None:
    repl = types.SimpleNamespace(registry=_Registry())

    assert DirectChatMixin._canonical_model_ids(
        repl, ["openai/gpt-x", "bare-name"]
    ) == ["openai/gpt-x", "bare-name"]
