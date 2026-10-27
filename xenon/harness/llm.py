"""LLM action adapter for the headless harness (#220).

The model proposes exactly one capability-level action.  It never executes a
tool, writes the ledger, or bypasses the kernel: the catalog binds the
capability, the gateway executes it, and the controller verifies the receipt.

This module deliberately lives outside ``xenon.kernel`` so provider/network
concerns never leak into the kernel purity boundary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

from xenon.kernel.contracts import (
    Abort,
    Action,
    AskUser,
    CallTool,
    Capability,
    Claim,
    ClaimSource,
    Finish,
    ModelView,
    PropositionKind,
    Think,
    ToolIntent,
)
from xenon.kernel.strategies import ActFirst
from xenon.utils.llm_client import chat_completion


class ActionParseError(ValueError):
    """The model did not return one valid capability action."""


class ModelConfigError(ValueError):
    """No usable model id / credentials source was configured."""


@dataclass(frozen=True, slots=True)
class ModelConfig:
    model_id: str
    base_url: str = ""
    api_key: str = ""
    max_tokens: int = 8192
    temperature: float = 0.0
    reasoning_effort: str = ""


_DEFAULT_MAX_TOKENS = 8192
_DEFAULT_MODELS_PATH = Path.home() / ".xenon" / "models.yaml"
_DEFAULT_CREDENTIALS_PATH = Path.home() / ".xenon" / "credentials.yaml"

_ACTION_PROTOCOL = """\
ActFirst action protocol (overrides the generic protocol above):
Return exactly one JSON object, with no markdown fence and no prose outside it.

Supported actions:
1. Inspect or change the workspace:
   {"action":"call_tool","capability":"read","params":{"file_path":"path"}}
   {"action":"call_tool","capability":"search","params":{"pattern":"text","path":"optional-root"}}
   {"action":"call_tool","capability":"edit","params":{"files":[{"path":"path","content":"full content"}]}}
   {"action":"call_tool","capability":"verify","params":{"command":"python -m pytest tests/test_x.py -q"}}
   {"action":"call_tool","capability":"shell","params":{"command":"safe shell command"}}
2. Spend a turn reasoning without a tool:
   {"action":"think","text":"what you learned and what you will do next"}
3. Ask the user because the task is genuinely ambiguous:
   {"action":"ask_user","question":"one concrete question"}
4. Finish:
   {"action":"finish","answer":"what changed and how it was verified","claims":[
     {"id":"c1","kind":"file_changed","target":"path"},
     {"id":"c2","kind":"test_passed","target":"tests/test_x.py"}
   ]}
5. Abort when the task cannot be completed safely:
   {"action":"abort","reason":"why"}

Rules:
- Use capabilities, never concrete tool names. The catalog chooses the tool.
- ``edit`` params use full-file writes under ``files`` or exact replacements
  under ``edits``. ``verify`` is a shell command, usually pytest.
- Do not run git commit or git push. The harness finalizer stages only verified
  files and creates the delivery commit when you return finish.
- A claim is a factual assertion, not a status message. Only claim file_changed,
  test_passed, commit_created, etc. when the recent receipts show that fact.
- If a tool failed or a claim would be unverifiable, keep working or abort; do
  not paper over it with a finish claim.
"""


def _read_yaml(path: Path, *, required: bool = False) -> Mapping[str, Any]:
    if not path.exists():
        if required:
            raise ModelConfigError(f"model config not found: {path}")
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ModelConfigError(f"cannot read model config {path}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise ModelConfigError(f"model config must be an object: {path}")
    return data


def _model_from_entry(model_id: str, raw: Mapping[str, Any]) -> ModelConfig:
    max_tokens = raw.get("max_tokens", _DEFAULT_MAX_TOKENS)
    try:
        max_tokens = max(1, int(max_tokens))
    except (TypeError, ValueError):
        max_tokens = _DEFAULT_MAX_TOKENS
    try:
        temperature = float(raw.get("temperature", 0.0))
    except (TypeError, ValueError):
        temperature = 0.0
    return ModelConfig(
        model_id=str(model_id),
        base_url=str(raw.get("base_url") or ""),
        api_key=str(raw.get("api_key") or ""),
        max_tokens=max_tokens,
        temperature=temperature,
        reasoning_effort=str(raw.get("reasoning_effort") or ""),
    )


def _find_model(
    data: Mapping[str, Any], explicit: str | None
) -> tuple[str, Mapping[str, Any]] | None:
    models = data.get("models")
    if not isinstance(models, Mapping):
        return None

    def entry(alias: str) -> tuple[str, Mapping[str, Any]] | None:
        raw = models.get(alias)
        if not isinstance(raw, Mapping):
            return None
        return str(raw.get("model_id") or alias), raw

    if explicit:
        found = entry(explicit)
        if found is not None:
            return found
        wanted = explicit.lower()
        for alias, raw in models.items():
            if not isinstance(raw, Mapping):
                continue
            model_id = str(raw.get("model_id") or alias)
            if (
                model_id.lower() == wanted
                or model_id.rsplit("/", 1)[-1].lower() == wanted
                or alias.lower() == wanted
            ):
                return model_id, raw
        return None

    roles = data.get("roles")
    if isinstance(roles, Mapping):
        planner = roles.get("planner")
        if isinstance(planner, list):
            for alias in planner:
                found = entry(str(alias))
                if found is not None:
                    return found
    for alias in models:
        found = entry(str(alias))
        if found is not None:
            return found
    return None


def _credentials_fallback() -> ModelConfig | None:
    """Pick the first provider model when only credentials.yaml exists.

    The real REPL derives models from this file too.  Keeping the fallback here
    means ``xenon exec`` does not silently require users to duplicate a model
    list into models.yaml.
    """

    data = _read_yaml(_DEFAULT_CREDENTIALS_PATH)
    providers = data.get("providers")
    if not isinstance(providers, Mapping):
        return None
    for provider, raw in providers.items():
        if not isinstance(raw, Mapping):
            continue
        models = raw.get("models")
        if not isinstance(models, list) or not models:
            continue
        name = str(models[0])
        model_id = name if "/" in name else f"{provider}/{name}"
        return _model_from_entry(
            model_id,
            {
                "base_url": raw.get("base_url") or "",
                "api_key": raw.get("api_key") or "",
                "max_tokens": raw.get("max_tokens", _DEFAULT_MAX_TOKENS),
            },
        )
    return None


def resolve_model_config(
    explicit: Sequence[str] | None = None,
    *,
    config_path: str | Path | None = None,
) -> ModelConfig:
    """Resolve an explicit model or the first configured/default model.

    Resolution order:
    1. ``--model`` if it contains a provider/model id;
    2. ``--model`` alias or full id found in models.yaml;
    3. first planner / first model in models.yaml;
    4. first provider model in credentials.yaml;
    5. an actionable error.
    """

    wanted = next(
        (str(item).strip() for item in (explicit or ()) if str(item).strip()),
        None,
    )
    path = Path(config_path).expanduser() if config_path else _DEFAULT_MODELS_PATH
    data = _read_yaml(path, required=config_path is not None)
    found = _find_model(data, wanted)
    if found is not None:
        model_id, raw = found
        return _model_from_entry(model_id, raw)

    if wanted:
        if "/" in wanted:
            return ModelConfig(model_id=wanted)
        raise ModelConfigError(
            f"unknown model {wanted!r}; use provider/model or configure "
            f"{path}"
        )

    fallback = _credentials_fallback()
    if fallback is not None:
        return fallback
    raise ModelConfigError(
        "no model configured; pass --model provider/model or configure "
        f"{_DEFAULT_MODELS_PATH} / {_DEFAULT_CREDENTIALS_PATH}"
    )


def _extract_json(text: str) -> Mapping[str, Any]:
    value = (text or "").strip()
    if not value:
        raise ActionParseError("empty model response")
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    try:
        payload = json.loads(value)
    except (TypeError, ValueError) as exc:
        start = value.find("{")
        end = value.rfind("}")
        if start < 0 or end <= start:
            raise ActionParseError(f"model response is not JSON: {exc}") from exc
        try:
            payload = json.loads(value[start : end + 1])
        except (TypeError, ValueError) as inner:
            raise ActionParseError(
                f"model response is not JSON: {inner}"
            ) from inner
    if not isinstance(payload, Mapping):
        raise ActionParseError("model action must be an object")
    return payload


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ActionParseError(f"{where} must be an object")
    return value


def _strings(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, list):
        raise ActionParseError(f"{where} must be a list")
    return tuple(str(item) for item in value)


def _claim(value: Any, index: int) -> Claim:
    data = _mapping(value, f"claims[{index}]")
    kind = data.get("kind")
    if not kind:
        raise ActionParseError(f"claims[{index}].kind is required")
    try:
        proposition = PropositionKind(str(kind))
    except ValueError as exc:
        raise ActionParseError(f"unknown claim kind: {kind!r}") from exc
    return Claim(
        id=str(data.get("id") or f"claim-{index + 1}"),
        kind=proposition,
        target=str(data.get("target") or ""),
        expect=dict(_mapping(data.get("expect"), f"claims[{index}].expect")),
        source=ClaimSource.LLM,
    )


def parse_action(payload: str | Mapping[str, Any]) -> Action:
    """Parse a model action into the kernel's closed action vocabulary."""

    data = _extract_json(payload) if isinstance(payload, str) else payload
    action = str(data.get("action") or "").strip()
    if action == "think":
        text = str(data.get("text") or "").strip()
        if not text:
            raise ActionParseError("think.text is required")
        return Think(text)
    if action == "call_tool":
        raw_capability = data.get("capability")
        try:
            capability = Capability(str(raw_capability))
        except ValueError as exc:
            raise ActionParseError(
                f"unknown capability: {raw_capability!r}"
            ) from exc
        params = dict(_mapping(data.get("params"), "params"))
        if capability is Capability.VCS:
            raise ActionParseError(
                "vcs capability is unavailable in ActFirst; "
                "the deterministic finalizer owns commits"
            )
        command = str(params.get("command") or params.get("git_command") or "")
        if command.strip().casefold().startswith(("git commit", "git add")):
            raise ActionParseError(
                "git commit/add is unavailable in ActFirst; "
                "the deterministic finalizer owns staging and commits"
            )
        return CallTool(
            ToolIntent(
                capability=capability,
                params=params,
                targets=_strings(data.get("targets"), "targets"),
            )
        )
    if action == "ask_user":
        question = str(data.get("question") or "").strip()
        if not question:
            raise ActionParseError("ask_user.question is required")
        return AskUser(question)
    if action == "abort":
        return Abort(str(data.get("reason") or "model aborted"))
    if action == "finish":
        raw_claims = data.get("claims")
        if raw_claims is None:
            raw_claims = []
        if not isinstance(raw_claims, list):
            raise ActionParseError("finish.claims must be a list")
        return Finish(
            answer=str(data.get("answer") or ""),
            claims=tuple(
                _claim(item, index) for index, item in enumerate(raw_claims)
            ),
        )
    raise ActionParseError(f"unsupported action: {action!r}")


def build_messages(view: ModelView) -> list[dict[str, str]]:
    """Render one deterministic state snapshot plus the ActFirst protocol."""

    state = {
        "objective": view.task.objective,
        "workspace": view.task.workspace,
        "phase": view.phase.value,
        "budget": {
            "max_steps": view.budget.max_steps,
            "max_tokens": view.budget.max_tokens,
        },
        "debt": [
            {
                "id": item.obligation.id,
                "kind": item.obligation.kind.value,
                "target": item.obligation.target,
                "reason": item.reason,
            }
            for item in view.debt
        ],
        "recent_events": list(view.working_set),
        "available_tools": list(view.tools),
    }
    return [
        {"role": "system", "content": view.system + "\n" + _ACTION_PROTOCOL},
        {
            "role": "user",
            "content": (
                "Current deterministic state (ledger prefix, not chat memory):\n"
                + json.dumps(state, ensure_ascii=False, separators=(",", ":"))
                + "\nReturn exactly one JSON action."
            ),
        },
    ]


class LLMActionProposer:
    """Call a model once per runtime turn and parse one capability action."""

    def __init__(
        self,
        config: ModelConfig,
        *,
        client: Callable[..., str] | None = None,
    ) -> None:
        self.config = config
        self._client = client

    def _call(self, messages: list[dict[str, str]]) -> str:
        client = self._client or chat_completion
        kwargs: dict[str, Any] = {
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }
        if self.config.base_url:
            kwargs["base_url"] = self.config.base_url
        if self.config.reasoning_effort:
            kwargs["reasoning_effort"] = self.config.reasoning_effort
        if self.config.api_key:
            provider = self.config.model_id.split("/", 1)[0].lower()
            kwargs["credentials"] = {provider: self.config.api_key}
        return client(self.config.model_id, messages, **kwargs)

    def __call__(self, view: ModelView) -> Action:
        try:
            return parse_action(self._call(build_messages(view)))
        except ActionParseError as exc:
            # Do not crash the task on a malformed model turn.  A thought is
            # ledger-visible, the model sees it in the next working set, and
            # the runtime's max_steps budget prevents an infinite retry loop.
            return Think(
                f"Invalid ActFirst action: {str(exc)[:500]}. "
                "Return exactly one JSON action with a supported action field."
            )


def build_llm_strategy(
    config: ModelConfig,
    *,
    client: Callable[..., str] | None = None,
) -> ActFirst:
    return ActFirst(LLMActionProposer(config, client=client))


__all__ = [
    "ActionParseError",
    "LLMActionProposer",
    "ModelConfig",
    "ModelConfigError",
    "build_llm_strategy",
    "build_messages",
    "parse_action",
    "resolve_model_config",
]
