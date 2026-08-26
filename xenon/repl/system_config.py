"""Unified System Configuration — ~/.xenon/config.yaml 统一收口所有环境变量。

设计原则:
1. **向后兼容**: 环境变量优先级高于配置文件（CI/Docker 场景可用环境变量覆盖）
2. **结构清晰**: 按功能分组（validation、engine、watch、limits、paths、interaction）
3. **默认值明确**: 代码中的 fallback 逻辑清晰，配置文件不存在时使用默认值
4. **热加载支持**: 可选使用 ConfigWatcher 监听配置文件变更

优先级（高到低）:
1. 环境变量（XENON_*）
2. ~/.xenon/config.yaml
3. 代码中的默认值

用法::

    from xenon.repl.system_config import get_config, reload_config

    config = get_config()
    if config.validation.strict:
        # 严格校验模式
        ...

    # 热加载（可选）
    reload_config()
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# 默认配置路径
_DEFAULT_CONFIG_PATH = Path.home() / ".xenon" / "config.yaml"
CONFIG_PATH = Path(
    os.environ.get("XENON_CONFIG_PATH", str(_DEFAULT_CONFIG_PATH)),
).expanduser()


@dataclass
class ValidationConfig:
    """参数校验配置。"""
    # XENON_STRICT_VALIDATION: 严格模式（≥2 命中即阻止）
    strict: bool = False


@dataclass
class EngineConfig:
    """引擎行为配置。"""
    # XENON_NO_AUTO_ENGINE: 禁用范式自动路由
    disable_auto_routing: bool = False


@dataclass
class WatchConfig:
    """配置文件热加载配置。"""
    # XENON_CONFIG_WATCH: 配置文件热加载开关（models.yaml）
    enabled: bool = True


@dataclass
class LimitsConfig:
    """资源限制配置。"""
    # XENON_MAX_MODELS_PER_PROVIDER: 每个 provider 实际注册进模型池的上限
    max_models_per_provider: int = 3
    # 配置向导里「发现了哪些模型」预览清单的显示条数。与上面那项分开，是因为
    # 两者约束的是不同的东西：上面决定真正注册多少个模型（影响路由和成本），
    # 这里只决定给用户看几行。向导历来显示 5 条，收口到一个键会让它悄悄变成 3。
    wizard_preview_models: int = 5


@dataclass
class PathsConfig:
    """路径配置。"""
    # XENON_CREDENTIALS_PATH: 凭证文件路径
    credentials: str = str(Path.home() / ".xenon" / "credentials.yaml")
    # XENON_CACHE_DIR: 缓存目录（主要用于测试）
    cache: str = ""
    # XENON_PROJECT_ROOT: 项目根目录
    project_root: str = ""


@dataclass
class InteractionConfig:
    """交互行为配置。"""
    # XENON_ASSUME_YES: 自动确认所有交互提示
    assume_yes: bool = False
    # XENON_TERMINAL_ASCII: 终端 ASCII 模式（禁用 Unicode 字符）
    terminal_ascii: bool = False


@dataclass
class DevelopmentConfig:
    """开发/调试配置。"""
    # XENON_REGISTER_MODULE_ALLOW: 允许注册的模块列表（逗号分隔）
    register_module_allow: str = ""
    # XENON_ALLOW_HOME_PROJECT: 允许在 home 目录使用项目
    allow_home_project: bool = False
    # XENON_NO_PT: 禁用 PTY（测试用）
    no_pty: bool = False


@dataclass
class SystemConfig:
    """系统配置根对象。"""
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    watch: WatchConfig = field(default_factory=WatchConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    interaction: InteractionConfig = field(default_factory=InteractionConfig)
    development: DevelopmentConfig = field(default_factory=DevelopmentConfig)


# 文件层缓存：只缓存 yaml 解析结果（按 路径+mtime 失效）。
# 环境变量**不缓存**——每次 get_config() 实时读取，保证：
#   1. 环境变量永远是最高优先级，不会被首次加载的快照冻结；
#   2. CI/Docker 运行中改 env、以及测试里 monkeypatch.setenv 立即生效；
#   3. 不同测试之间不会通过单例互相污染（如 strict 模式泄漏）。
_file_cache: dict[str, Any] | None = None
_file_cache_key: tuple[str, float] | None = None


def _load_from_file() -> dict[str, Any]:
    """从 config.yaml 加载配置。文件不存在时返回空字典（不报错）。

    解析结果按 (路径, mtime) 缓存，文件被改动后自动失效重读。
    """
    global _file_cache, _file_cache_key

    try:
        stat = CONFIG_PATH.stat()
    except OSError:
        # 文件不存在/不可读 → 用默认值，不报错
        _file_cache, _file_cache_key = {}, None
        return {}

    key = (str(CONFIG_PATH), stat.st_mtime)
    if _file_cache is not None and _file_cache_key == key:
        return _file_cache

    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            logger.warning("配置文件格式错误（应为字典/映射）: %s", CONFIG_PATH)
            data = {}
    except yaml.YAMLError as e:
        logger.warning("配置文件 YAML 解析失败，使用默认值: %s (%s)", CONFIG_PATH, e)
        data = {}
    except OSError as e:
        logger.warning("配置文件读取失败，使用默认值: %s (%s)", CONFIG_PATH, e)
        data = {}

    _file_cache, _file_cache_key = data, key
    return data


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    """取出一个配置分组；类型不对时降级为空字典并告警。"""
    section = data.get(name, {})
    if not isinstance(section, dict):
        logger.warning("配置分组 %r 格式错误（应为映射），已忽略", name)
        return {}
    return section


_TRUE_TOKENS = frozenset({"1", "true", "yes", "on"})
_FALSE_TOKENS = frozenset({"0", "false", "no", "off"})


def _coerce_bool(value: Any, fallback: bool, origin: str) -> bool:
    """把配置文件里的值强制成 bool；无法识别时告警并回退。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUE_TOKENS:
            return True
        if token in _FALSE_TOKENS:
            return False
    if value is None:
        return fallback
    logger.warning("配置项 %s 不是有效布尔值: %r，使用默认值 %r", origin, value, fallback)
    return fallback


def _coerce_int(value: Any, fallback: int, origin: str) -> int:
    """把配置文件里的值强制成 int；无法识别时告警并回退。"""
    if isinstance(value, bool):  # bool 是 int 子类，先挡掉避免 True→1
        logger.warning("配置项 %s 应为整数，收到布尔值 %r，使用默认值 %r", origin, value, fallback)
        return fallback
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value.strip())
        except ValueError:
            pass
    if value is None:
        return fallback
    logger.warning("配置项 %s 不是有效整数: %r，使用默认值 %r", origin, value, fallback)
    return fallback


def _at_least_one(value: int, fallback: int, origin: str) -> int:
    """把「取前 N 个」类的上限钳到 ≥1，越界时告警并回退。

    这类值最终落到 ``models[:n]`` 这样的切片上，0 会静默地一个模型都不注册，
    负数更糟——``models[:-2]`` 是「从末尾砍掉 2 个」，看起来正常工作但结果全错。
    宽松的类型转换不该放过这一类错误：它不会报错，只会让行为悄悄不对。
    """
    if value < 1:
        logger.warning(
            "配置项 %s 必须 ≥1，收到 %r，使用默认值 %r", origin, value, fallback
        )
        return fallback
    return value


def _coerce_str(value: Any, fallback: str, origin: str) -> str:
    """把配置文件里的值强制成 str；空值回退默认。"""
    if value is None:
        return fallback
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else fallback
    logger.warning("配置项 %s 不是字符串: %r，使用默认值 %r", origin, value, fallback)
    return fallback


def _get_bool_env(key: str, default: bool) -> bool:
    """环境变量 > 传入默认值。支持 1/0/true/false/yes/no/on/off（大小写不敏感）。

    环境变量每次实时读取，绝不缓存 —— 保证其优先级最高且改动立即生效。
    """
    raw = os.environ.get(key)
    if raw is None:
        return default
    token = raw.strip().lower()
    if token in _TRUE_TOKENS:
        return True
    if token in _FALSE_TOKENS:
        return False
    if token:
        logger.warning("环境变量 %s 不是有效布尔值: %r，回退到 %r", key, raw, default)
    return default


def _get_int_env(key: str, default: int) -> int:
    """环境变量 > 传入默认值。解析失败时告警并回退。"""
    raw = os.environ.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        logger.warning("环境变量 %s 不是有效整数: %r，回退到 %r", key, raw, default)
        return default


def _get_str_env(key: str, default: str) -> str:
    """环境变量 > 传入默认值。空字符串视为未设置。"""
    raw = os.environ.get(key, "").strip()
    return raw if raw else default


def _merge_config(file_data: dict[str, Any]) -> SystemConfig:
    """合并配置文件与环境变量。优先级：环境变量 > 文件 > 代码默认值。

    文件里的值先经 ``_coerce_*`` 校验类型（写错类型只告警并回退，不抛异常），
    再作为 ``_get_*_env`` 的 fallback —— 这样环境变量始终压过文件。
    """
    validation_data = _section(file_data, "validation")
    engine_data = _section(file_data, "engine")
    watch_data = _section(file_data, "watch")
    limits_data = _section(file_data, "limits")
    paths_data = _section(file_data, "paths")
    interaction_data = _section(file_data, "interaction")
    development_data = _section(file_data, "development")

    default_credentials = str(Path.home() / ".xenon" / "credentials.yaml")

    validation = ValidationConfig(
        strict=_get_bool_env(
            "XENON_STRICT_VALIDATION",
            _coerce_bool(validation_data.get("strict"), False, "validation.strict"),
        ),
    )

    engine = EngineConfig(
        disable_auto_routing=_get_bool_env(
            "XENON_NO_AUTO_ENGINE",
            _coerce_bool(
                engine_data.get("disable_auto_routing"), False,
                "engine.disable_auto_routing",
            ),
        ),
    )

    watch = WatchConfig(
        enabled=_get_bool_env(
            "XENON_CONFIG_WATCH",
            _coerce_bool(watch_data.get("enabled"), True, "watch.enabled"),
        ),
    )

    limits = LimitsConfig(
        max_models_per_provider=_at_least_one(
            _get_int_env(
                "XENON_MAX_MODELS_PER_PROVIDER",
                _coerce_int(
                    limits_data.get("max_models_per_provider"), 3,
                    "limits.max_models_per_provider",
                ),
            ),
            3, "limits.max_models_per_provider",
        ),
        wizard_preview_models=_at_least_one(
            _get_int_env(
                "XENON_WIZARD_PREVIEW_MODELS",
                _coerce_int(
                    limits_data.get("wizard_preview_models"), 5,
                    "limits.wizard_preview_models",
                ),
            ),
            5, "limits.wizard_preview_models",
        ),
    )

    paths = PathsConfig(
        credentials=_get_str_env(
            "XENON_CREDENTIALS_PATH",
            _coerce_str(
                paths_data.get("credentials"), default_credentials,
                "paths.credentials",
            ),
        ),
        cache=_get_str_env(
            "XENON_CACHE_DIR",
            _coerce_str(paths_data.get("cache"), "", "paths.cache"),
        ),
        project_root=_get_str_env(
            "XENON_PROJECT_ROOT",
            _coerce_str(paths_data.get("project_root"), "", "paths.project_root"),
        ),
    )

    interaction = InteractionConfig(
        assume_yes=_get_bool_env(
            "XENON_ASSUME_YES",
            _coerce_bool(
                interaction_data.get("assume_yes"), False,
                "interaction.assume_yes",
            ),
        ),
        terminal_ascii=_get_bool_env(
            "XENON_TERMINAL_ASCII",
            _coerce_bool(
                interaction_data.get("terminal_ascii"), False,
                "interaction.terminal_ascii",
            ),
        ),
    )

    development = DevelopmentConfig(
        register_module_allow=_get_str_env(
            "XENON_REGISTER_MODULE_ALLOW",
            _coerce_str(
                development_data.get("register_module_allow"), "",
                "development.register_module_allow",
            ),
        ),
        allow_home_project=_get_bool_env(
            "XENON_ALLOW_HOME_PROJECT",
            _coerce_bool(
                development_data.get("allow_home_project"), False,
                "development.allow_home_project",
            ),
        ),
        no_pty=_get_bool_env(
            "XENON_NO_PT",
            _coerce_bool(development_data.get("no_pty"), False, "development.no_pty"),
        ),
    )

    return SystemConfig(
        validation=validation,
        engine=engine,
        watch=watch,
        limits=limits,
        paths=paths,
        interaction=interaction,
        development=development,
    )


def load_config() -> SystemConfig:
    """加载系统配置。优先级：环境变量 > config.yaml > 代码默认值。

    配置文件不存在或格式错误时静默使用默认值，绝不抛异常。
    """
    return _merge_config(_load_from_file())


def get_config() -> SystemConfig:
    """获取当前生效配置。

    **每次调用都重新合并**：yaml 解析结果按 mtime 缓存（无 I/O 开销），
    环境变量实时读取。这样保证：

    - 环境变量优先级最高，且运行中改动/测试 monkeypatch 立即生效；
    - 编辑 config.yaml 后无需重启即自动生效（mtime 变化触发重读）；
    - 不会有跨调用方、跨测试的状态泄漏。
    """
    return _merge_config(_load_from_file())


def reload_config() -> SystemConfig:
    """强制丢弃文件缓存并重新加载（供 inotify 热加载回调调用）。"""
    global _file_cache, _file_cache_key
    _file_cache, _file_cache_key = None, None
    config = load_config()
    logger.info("配置已重新加载: %s", CONFIG_PATH)
    return config


def generate_default_config_file(path: Path | None = None) -> None:
    """生成默认配置文件模板（带注释）。"""
    if path is None:
        path = CONFIG_PATH

    path.parent.mkdir(parents=True, exist_ok=True)

    template = """\
# Xenon 系统配置文件
# 优先级: 环境变量 > 本文件 > 代码默认值

# ========================================
# 参数校验配置
# ========================================
validation:
  # 严格模式（≥2 条命中即阻止）
  # 环境变量: XENON_STRICT_VALIDATION
  strict: false

# ========================================
# 引擎行为配置
# ========================================
engine:
  # 禁用范式自动路由
  # 环境变量: XENON_NO_AUTO_ENGINE
  disable_auto_routing: false

# ========================================
# 配置文件热加载
# ========================================
watch:
  # 是否启用 models.yaml 热加载（inotify 监听）
  # 环境变量: XENON_CONFIG_WATCH
  enabled: true

# ========================================
# 资源限制配置
# ========================================
limits:
  # 每个 provider 实际注册进模型池的模型数上限（影响路由与成本）
  # 环境变量: XENON_MAX_MODELS_PER_PROVIDER
  max_models_per_provider: 3

  # 配置向导里「发现了哪些模型」预览清单的显示条数（只影响显示）
  # 环境变量: XENON_WIZARD_PREVIEW_MODELS
  wizard_preview_models: 5

# ========================================
# 路径配置
# ========================================
paths:
  # 凭证文件路径
  # 环境变量: XENON_CREDENTIALS_PATH
  credentials: ~/.xenon/credentials.yaml

  # 缓存目录（主要用于测试，留空使用默认）
  # 环境变量: XENON_CACHE_DIR
  cache: ""

  # 项目根目录（留空自动检测）
  # 环境变量: XENON_PROJECT_ROOT
  project_root: ""

# ========================================
# 交互行为配置
# ========================================
interaction:
  # 自动确认所有交互提示（非交互模式）
  # 环境变量: XENON_ASSUME_YES
  assume_yes: false

  # 终端 ASCII 模式（禁用 Unicode 字符）
  # 环境变量: XENON_TERMINAL_ASCII
  terminal_ascii: false

# ========================================
# 开发/调试配置
# ========================================
development:
  # 允许注册的模块列表（逗号分隔）
  # 环境变量: XENON_REGISTER_MODULE_ALLOW
  register_module_allow: ""

  # 允许在 home 目录使用项目
  # 环境变量: XENON_ALLOW_HOME_PROJECT
  allow_home_project: false

  # 禁用 PTY（测试用）
  # 环境变量: XENON_NO_PT
  no_pty: false
"""

    with path.open("w", encoding="utf-8") as f:
        f.write(template)

    logger.info("已生成默认配置文件: %s", path)
