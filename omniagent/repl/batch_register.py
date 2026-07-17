"""批量模型注册管线:解析 -> 校验 -> discover -> probe -> 事务注册。

P1-A: 统一 models.yaml schema,融合 ModelRegistry(别名/角色)与 ModelPool(权重/凭证/能力)。
一个文件同时描述模型列表 + 角色优先级 + 性能 profile,注册后 AutoRouter 的池立即生效。

设计取舍(见桌面《整合方案》§4):
- YAML 一等,JSON 兼容(yaml.safe_load 通吃 JSON,不另立解析路径)。
- 轻量 stdlib 校验,不引入 pydantic。
- discover/probe 均可选,默认 probe 开、discover 关。
- 幂等:重复 alias 视为更新;逐个注册,单条失败记 failed 继续(不回滚已成功的已校验条目)。
"""
from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# ${ENV_VAR} 引用语法
_ENV_REF = re.compile(r"^\$\{([A-Z0-9_]+)\}$")
# alias 仅允许字母/数字/下划线/连字符/点(与现有 alias 生成规则一致)
_ALIAS_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

PROBE_TIMEOUT = 12.0
PROBE_MAX_WORKERS = 8
DISCOVER_TIMEOUT = 10.0


@dataclass
class ModelSpec:
    """单个模型的注册规格(解析 + 校验后的中间结构)。"""
    alias: str
    model_id: str
    api_key: str = ""
    base_url: str = ""
    weight: float = 1.0
    context_window: int = 0      # 0 = 不覆盖(用 _infer_capability 推断)
    max_tokens: int = 0          # 0 = 用默认 4096
    temperature: float = 0.0     # 0 = 用默认 0.7
    tags: list[str] = field(default_factory=list)
    tier: int = 0                # 0 = 不覆盖
    discover: bool = False
    probe: bool = True
    source: str = "file"         # "file" | "discover:<parent>"


@dataclass
class BatchResult:
    """批量注册结果。"""
    registered: list[str] = field(default_factory=list)          # 成功注册的 alias
    updated: list[str] = field(default_factory=list)             # 已存在被更新
    failed: list[tuple[str, str]] = field(default_factory=list)  # (alias, reason)
    probed_ok: list[str] = field(default_factory=list)
    probed_fail: list[tuple[str, str]] = field(default_factory=list)  # (alias, err)
    discovered: list[tuple[str, str]] = field(default_factory=list)   # (parent_alias, child_model_id)
    roles: dict[str, list[str]] = field(default_factory=dict)
    profile: str = "balanced"

    @property
    def ok(self) -> bool:
        return not self.failed and not self.probed_fail

    def summary(self) -> str:
        lines = [f"✅ 已注册 {len(self.registered)} / 更新 {len(self.updated)} 个模型"]
        if self.discovered:
            lines.append(f"🔍 discover 展开子模型 {len(self.discovered)} 个")
        if self.probed_ok:
            lines.append(f"🧪 probe 通过 {len(self.probed_ok)} 个")
        if self.probed_fail:
            lines.append(f"⚠️  probe 失败 {len(self.probed_fail)} 个(已跳过注册):")
            for alias, err in self.probed_fail:
                lines.append(f"     - {alias}: {err}")
        if self.failed:
            lines.append(f"❌ 注册失败 {len(self.failed)} 个:")
            for alias, reason in self.failed:
                lines.append(f"     - {alias}: {reason}")
        return "\n".join(lines)


# ── 解析 ────────────────────────────────────────────────────

def parse_file(path: str | Path) -> tuple[list[ModelSpec], dict[str, list[str]], str, list[str]]:
    """解析 models.yaml/JSON 文件。

    Returns:
        (specs, roles, profile, errors)。errors 非空表示文件级错误(无法继续)。
    """
    path = Path(path)
    errors: list[str] = []
    if not path.exists():
        return [], {}, "balanced", [f"文件不存在: {path}"]

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        return [], {}, "balanced", [f"YAML/JSON 解析失败: {e}"]

    if not isinstance(data, dict):
        return [], {}, "balanced", [f"文件顶层应为映射(dict),实为 {type(data).__name__}"]

    profile = str(data.get("profile", "balanced")).strip() or "balanced"
    raw_models = data.get("models", [])
    if isinstance(raw_models, dict):
        # 兼容 {alias: {fields}} 形式(registry.export_config 风格)
        raw_models = [{"alias": a, **(v if isinstance(v, dict) else {})} for a, v in raw_models.items()]
    if not isinstance(raw_models, list):
        return [], {}, profile, [f"'models' 应为列表,实为 {type(raw_models).__name__}"]

    specs: list[ModelSpec] = []
    for idx, item in enumerate(raw_models):
        if not isinstance(item, dict):
            errors.append(f"models[{idx}] 应为映射,实为 {type(item).__name__}")
            continue
        specs.append(_spec_from_dict(item, idx))

    roles_raw = data.get("roles", {})
    roles = {str(k): list(v) for k, v in roles_raw.items()} if isinstance(roles_raw, dict) else {}

    return specs, roles, profile, errors


def _spec_from_dict(d: dict[str, Any], idx: int) -> ModelSpec:
    """从 dict 构造 ModelSpec,字段缺失/类型问题留给 validate 报告。"""
    return ModelSpec(
        alias=str(d.get("alias", "")).strip(),
        model_id=str(d.get("model_id", "")).strip(),
        api_key=_resolve_env(d.get("api_key", "")),
        base_url=str(d.get("base_url", "")).strip(),
        weight=float(d.get("weight", 1.0) or 1.0),
        context_window=int(d.get("context_window", 0) or 0),
        max_tokens=int(d.get("max_tokens", 0) or 0),
        temperature=float(d.get("temperature", 0.0) or 0.0),
        tags=list(d.get("tags", []) or []),
        tier=int(d.get("tier", 0) or 0),
        discover=bool(d.get("discover", False)),
        probe=bool(d.get("probe", True)),
    )


def _resolve_env(value: Any) -> str:
    """解析 ${ENV_VAR} 引用;非字符串或无匹配则原样返回(空字符串留作 fallback)。"""
    if not isinstance(value, str) or not value:
        return value if isinstance(value, str) else ""
    m = _ENV_REF.match(value.strip())
    if m:
        return os.environ.get(m.group(1), "")
    return value


# ── 校验 ────────────────────────────────────────────────────

def validate(specs: list[ModelSpec]) -> list[str]:
    """字段校验,返回错误列表(每条对应一个坏 spec,含 alias/序号)。"""
    errors: list[str] = []
    seen_aliases: dict[str, int] = {}

    for i, s in enumerate(specs):
        label = s.alias or f"models[{i}]"
        if not s.alias:
            errors.append(f"{label}: alias 不能为空")
        elif not _ALIAS_RE.match(s.alias):
            errors.append(f"{label}: alias 含非法字符(仅允许字母/数字/_.-)")
        if not s.model_id:
            errors.append(f"{label}: model_id 不能为空")
        elif "/" not in s.model_id:
            errors.append(f"{label}: model_id 应为 'provider/model_name' 格式(缺少 '/')")
        if s.weight <= 0:
            errors.append(f"{label}: weight 必须大于 0(当前 {s.weight})")
        if s.tier != 0 and not (1 <= s.tier <= 5):
            errors.append(f"{label}: tier 应在 1-5 之间(当前 {s.tier})")
        if s.context_window < 0:
            errors.append(f"{label}: context_window 不能为负")
        # alias 唯一性
        if s.alias:
            if s.alias in seen_aliases:
                errors.append(f"{label}: alias 重复(与 models[{seen_aliases[s.alias]}] 冲突)")
            else:
                seen_aliases[s.alias] = i

    return errors


# ── discover(上游 /v1/models 自动发现)──────────────────────

def discover_models(base_url: str, api_key: str, *, timeout: float = DISCOVER_TIMEOUT) -> list[str]:
    """GET {base_url}/v1/models 拉取上游可用模型列表(OpenAI 兼容)。

    用于 Ollama/vLLM/本地兼容端点;闭源商默认不开 discover。
    """
    import httpx
    url = base_url.rstrip("/") + "/v1/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    with httpx.Client(timeout=timeout) as client:
        r = client.get(url, headers=headers)
        r.raise_for_status()
        data = r.json()
    items = data.get("data", []) if isinstance(data, dict) else []
    return [str(m.get("id")) for m in items if isinstance(m, dict) and m.get("id")]


def _expand_discover(specs: list[ModelSpec], result: BatchResult) -> list[ModelSpec]:
    """对 discover=True 的 spec,拉取子模型并展开为独立 ModelSpec。"""
    expanded: list[ModelSpec] = []
    for s in specs:
        if not s.discover:
            expanded.append(s)
            continue
        if not s.base_url:
            result.failed.append((s.alias, "discover=True 但未提供 base_url"))
            expanded.append(s)
            continue
        try:
            children = discover_models(s.base_url, s.api_key)
        except Exception as e:
            result.failed.append((s.alias, f"discover 失败: {e}"))
            expanded.append(s)
            continue
        expanded.append(s)  # 父模型本身也保留注册(discover 是"额外发现子模型",非替代)
        provider = s.model_id.split("/")[0] if "/" in s.model_id else s.model_id
        for child in children:
            child_alias = child.replace("/", "-").replace(".", "-").replace(":", "-")
            # 避免与父 alias 冲突:若 child_alias == s.alias 则跳过
            if child_alias == s.alias:
                continue
            result.discovered.append((s.alias, f"{provider}/{child}"))
            expanded.append(ModelSpec(
                alias=child_alias,
                model_id=f"{provider}/{child}",
                api_key=s.api_key,
                base_url=s.base_url,
                weight=s.weight,
                tags=list(s.tags),
                tier=s.tier,
                probe=s.probe,
                source=f"discover:{s.alias}",
            ))
    return expanded


# ── probe(单 token 验活)────────────────────────────────────

def probe_model(spec: ModelSpec, *, timeout: float = PROBE_TIMEOUT) -> tuple[bool, str]:
    """对单个模型发最小请求验活(捕获失效 key/网络/限流)。

    复用 llm_client.chat_completion 以复用 provider 适配(Anthropic 原生 vs OpenAI 兼容)。
    """
    from omniagent.utils.llm_client import chat_completion
    provider = spec.model_id.split("/")[0] if "/" in spec.model_id else ""
    creds = {provider: spec.api_key} if spec.api_key else None
    try:
        chat_completion(
            spec.model_id,
            [{"role": "user", "content": "ping"}],
            credentials=creds,
            base_url=spec.base_url or None,
            max_tokens=1,
            temperature=0.0,
            timeout=timeout,
            max_retries=0,
        )
        return True, ""
    except Exception as e:
        return False, str(e)[:200]


def _run_probes(specs: list[ModelSpec], result: BatchResult, *, max_workers: int = PROBE_MAX_WORKERS) -> list[ModelSpec]:
    """并发 probe;返回通过验证的 specs(probe fail 的记 probed_fail 并排除)。"""
    to_probe = [s for s in specs if s.probe]
    skip_probe = [s for s in specs if not s.probe]
    if not to_probe:
        return specs

    passed: list[ModelSpec] = list(skip_probe)
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(probe_model, s): s for s in to_probe}
        for fut in as_completed(futures):
            s = futures[fut]
            try:
                ok, err = fut.result()
            except Exception as e:
                ok, err = False, f"probe 异常: {e}"
            if ok:
                result.probed_ok.append(s.alias)
                passed.append(s)
            else:
                result.probed_fail.append((s.alias, err))
    return passed


# ── 主流程 ──────────────────────────────────────────────────

def batch_register(
    path: str | Path,
    registry: Any,
    pool: Any,
    *,
    probe: bool = True,
    dry_run: bool = False,
    max_workers: int = PROBE_MAX_WORKERS,
) -> BatchResult:
    """批量注册模型到 ModelRegistry + ModelPool。

    Args:
        path: models.yaml/JSON 文件路径。
        registry: ModelRegistry 实例。
        pool: ModelPool 实例。
        probe: 是否启用验活(文件内 per-model 的 probe 字段仍可单独关闭)。
        dry_run: 仅校验+报告,不注册。
        max_workers: probe 并发数。
    """
    result = BatchResult()
    specs, roles, profile, errors = parse_file(path)
    result.roles = roles
    result.profile = profile

    if errors:
        result.failed.append(("__file__", "; ".join(errors)))
        return result

    errors = validate(specs)
    if errors:
        for e in errors:
            result.failed.append(("__validate__", e))
        # 校验失败的条目整体跳过;仅注册通过校验的(理论上 validate 全过才注册,此处保守:有错则不注册任何)
        return result

    # discover 展开
    specs = _expand_discover(specs, result)
    # 展开后子模型可能引入 alias 冲突,再校验一次
    post_errors = validate(specs)
    if post_errors:
        for e in post_errors:
            result.failed.append(("__validate__", e))

    if dry_run:
        return result

    # probe
    if probe:
        specs = _run_probes(specs, result, max_workers=max_workers)

    # 事务式注册:逐个 add_model + register;单条失败记 failed 继续(已校验+probe 通过,失败属罕见)
    for s in specs:
        try:
            existed_in_registry = registry.get_model(s.alias) is not None
            existed_in_pool = pool.get(s.alias) is not None

            cap_overrides: dict[str, Any] = {}
            if s.tier:
                cap_overrides["tier"] = s.tier
            if s.context_window:
                cap_overrides["context_window"] = s.context_window

            registry.add_model(
                s.model_id, s.alias,
                api_key=s.api_key, base_url=s.base_url,
                max_tokens=s.max_tokens or 4096,
                temperature=s.temperature or 0.7,
                context_window=s.context_window or 128000,
                weight=s.weight,
            )
            pool.register(
                s.model_id, alias=s.alias, weight=s.weight,
                api_key=s.api_key, base_url=s.base_url, **cap_overrides,
            )

            if existed_in_registry or existed_in_pool:
                result.updated.append(s.alias)
            else:
                result.registered.append(s.alias)
        except Exception as e:
            result.failed.append((s.alias, f"注册异常: {e}"))

    # 角色(注册后才生效)
    for role, aliases in roles.items():
        try:
            registry.assign_role(role, aliases)
        except Exception as e:
            result.failed.append((f"role:{role}", str(e)))

    return result
