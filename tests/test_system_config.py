"""测试统一系统配置（system_config.py）。

验证：
1. 配置文件加载
2. 环境变量优先级
3. 默认值回退
4. 配置热加载
5. 文件不存在时的降级行为
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from xenon.repl.system_config import (
    load_config,
    get_config,
    reload_config,
    generate_default_config_file,
)


@pytest.fixture(autouse=True)
def reset_file_cache():
    """每个测试前后清空 yaml 文件缓存，避免跨测试串味。"""
    import xenon.repl.system_config as config_module
    config_module._file_cache = None
    config_module._file_cache_key = None
    yield
    config_module._file_cache = None
    config_module._file_cache_key = None


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    """创建临时配置文件路径。"""
    return tmp_path / "config.yaml"


@pytest.fixture
def mock_config_path(config_file: Path, monkeypatch):
    """将 CONFIG_PATH 替换为临时路径。"""
    import xenon.repl.system_config as config_module
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_file)
    return config_file


def test_load_config_with_defaults(mock_config_path: Path, monkeypatch):
    """配置文件不存在时，应返回所有默认值。"""
    # 清除 conftest.py 设置的环境变量
    monkeypatch.delenv("XENON_CACHE_DIR", raising=False)
    monkeypatch.delenv("XENON_ASSUME_YES", raising=False)

    config = load_config()

    assert config.validation.strict is False
    assert config.engine.disable_auto_routing is False
    assert config.watch.enabled is True
    assert config.limits.max_models_per_provider == 3
    assert config.paths.credentials == str(Path.home() / ".xenon" / "credentials.yaml")
    assert config.paths.cache == ""
    assert config.paths.project_root == ""
    assert config.interaction.assume_yes is False
    assert config.interaction.terminal_ascii is False
    assert config.development.register_module_allow == ""
    assert config.development.allow_home_project is False
    assert config.development.no_pty is False


def test_load_config_from_file(mock_config_path: Path, monkeypatch):
    """配置文件存在时，应正确加载所有字段。"""
    # 清除 conftest.py 设置的环境变量，测试纯文件加载
    monkeypatch.delenv("XENON_CACHE_DIR", raising=False)
    monkeypatch.delenv("XENON_ASSUME_YES", raising=False)

    config_data = {
        "validation": {"strict": True},
        "engine": {"disable_auto_routing": True},
        "watch": {"enabled": False},
        "limits": {"max_models_per_provider": 10},
        "paths": {
            "credentials": "~/.custom/creds.yaml",
            "cache": "/tmp/cache",
            "project_root": "/home/user/project",
        },
        "interaction": {
            "assume_yes": True,
            "terminal_ascii": True,
        },
        "development": {
            "register_module_allow": "mymodule,another",
            "allow_home_project": True,
            "no_pty": True,
        },
    }

    with open(mock_config_path, "w") as f:
        yaml.dump(config_data, f)

    config = load_config()

    assert config.validation.strict is True
    assert config.engine.disable_auto_routing is True
    assert config.watch.enabled is False
    assert config.limits.max_models_per_provider == 10
    assert config.paths.credentials == "~/.custom/creds.yaml"
    assert config.paths.cache == "/tmp/cache"
    assert config.paths.project_root == "/home/user/project"
    assert config.interaction.assume_yes is True
    assert config.interaction.terminal_ascii is True
    assert config.development.register_module_allow == "mymodule,another"
    assert config.development.allow_home_project is True
    assert config.development.no_pty is True


def test_env_overrides_file(mock_config_path: Path, monkeypatch):
    """环境变量应优先于配置文件。"""
    # 配置文件设置为 False
    config_data = {
        "validation": {"strict": False},
        "engine": {"disable_auto_routing": False},
        "limits": {"max_models_per_provider": 3},
    }
    with open(mock_config_path, "w") as f:
        yaml.dump(config_data, f)

    # 环境变量设置为 True/不同值
    monkeypatch.setenv("XENON_STRICT_VALIDATION", "1")
    monkeypatch.setenv("XENON_NO_AUTO_ENGINE", "true")
    monkeypatch.setenv("XENON_MAX_MODELS_PER_PROVIDER", "20")

    config = load_config()

    assert config.validation.strict is True
    assert config.engine.disable_auto_routing is True
    assert config.limits.max_models_per_provider == 20


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("yes", True),
        ("on", True),
        (" 1 ", True),
        ("0", False),
        ("false", False),
        ("False", False),
        ("no", False),
        ("off", False),
        ("", False),         # 空值 → 默认值 False
        ("invalid", False),  # 无效值 → 默认值 False
    ],
)
def test_env_bool_parsing(mock_config_path: Path, monkeypatch, value, expected):
    """布尔环境变量应支持常见写法，大小写与空白不敏感；无效值回退默认。"""
    monkeypatch.setenv("XENON_STRICT_VALIDATION", value)
    assert load_config().validation.strict is expected


def test_env_bool_off_overrides_file_true(mock_config_path: Path, monkeypatch):
    """文件开启、环境变量关闭时，config_watcher 的开关应为关闭。

    这条覆盖 test_config_watcher.py 里 XENON_CONFIG_WATCH=0/false/False 的契约。
    """
    with open(mock_config_path, "w") as f:
        yaml.dump({"watch": {"enabled": True}}, f)

    for token in ("0", "false", "False", "off", "no"):
        monkeypatch.setenv("XENON_CONFIG_WATCH", token)
        assert load_config().watch.enabled is False, f"Failed for token: {token}"


def test_env_int_parsing(mock_config_path: Path, monkeypatch):
    """测试整数环境变量的解析和回退。"""
    # 有效整数
    monkeypatch.setenv("XENON_MAX_MODELS_PER_PROVIDER", "15")
    config = load_config()
    assert config.limits.max_models_per_provider == 15

    # 无效整数（应回退到默认值）
    monkeypatch.setenv("XENON_MAX_MODELS_PER_PROVIDER", "invalid")
    config = load_config()
    assert config.limits.max_models_per_provider == 3


def test_env_str_parsing(mock_config_path: Path, monkeypatch):
    """测试字符串环境变量的解析。"""
    monkeypatch.setenv("XENON_CREDENTIALS_PATH", "/custom/path/creds.yaml")
    monkeypatch.setenv("XENON_REGISTER_MODULE_ALLOW", "mod1,mod2")

    config = load_config()

    assert config.paths.credentials == "/custom/path/creds.yaml"
    assert config.development.register_module_allow == "mod1,mod2"


def test_get_config_returns_equal_values(mock_config_path: Path):
    """连续调用 get_config() 在配置未变时应给出相同取值。"""
    config1 = get_config()
    config2 = get_config()

    assert config1 == config2


def test_env_change_takes_effect_without_reload(mock_config_path: Path, monkeypatch):
    """环境变量在运行中改动应立即生效（不缓存 env 快照）。

    这是「环境变量优先级最高」的核心保证：若首次 get_config() 把 env 冻结成
    快照，CI/Docker 里运行期改 env 就会失效。
    """
    monkeypatch.delenv("XENON_STRICT_VALIDATION", raising=False)
    assert get_config().validation.strict is False

    monkeypatch.setenv("XENON_STRICT_VALIDATION", "1")
    assert get_config().validation.strict is True

    monkeypatch.setenv("XENON_STRICT_VALIDATION", "0")
    assert get_config().validation.strict is False


def test_env_beats_file_even_when_file_says_true(mock_config_path: Path, monkeypatch):
    """文件为 true、环境变量为 0 时，必须以环境变量为准。"""
    with open(mock_config_path, "w") as f:
        yaml.dump({"watch": {"enabled": True}}, f)

    monkeypatch.setenv("XENON_CONFIG_WATCH", "0")
    assert get_config().watch.enabled is False


def test_file_edit_picked_up_via_mtime(mock_config_path: Path, monkeypatch):
    """编辑 config.yaml 后应自动生效（mtime 变化触发重读）。"""
    monkeypatch.delenv("XENON_MAX_MODELS_PER_PROVIDER", raising=False)

    with open(mock_config_path, "w") as f:
        yaml.dump({"limits": {"max_models_per_provider": 4}}, f)
    assert get_config().limits.max_models_per_provider == 4

    # 重写文件并显式推进 mtime，避免同秒粒度下的 stat 无变化
    with open(mock_config_path, "w") as f:
        yaml.dump({"limits": {"max_models_per_provider": 9}}, f)
    st = mock_config_path.stat()
    os.utime(mock_config_path, (st.st_atime + 10, st.st_mtime + 10))

    assert get_config().limits.max_models_per_provider == 9


def test_wrong_type_in_file_falls_back(mock_config_path: Path, monkeypatch):
    """文件里类型写错时应告警并回退默认值，而不是抛异常。"""
    monkeypatch.delenv("XENON_MAX_MODELS_PER_PROVIDER", raising=False)
    monkeypatch.delenv("XENON_STRICT_VALIDATION", raising=False)

    with open(mock_config_path, "w") as f:
        yaml.dump(
            {
                "validation": {"strict": ["not", "a", "bool"]},
                "limits": {"max_models_per_provider": "not-an-int"},
            },
            f,
        )

    config = load_config()
    assert config.validation.strict is False
    assert config.limits.max_models_per_provider == 3


def test_string_bools_in_file_accepted(mock_config_path: Path, monkeypatch):
    """文件里写 "true"/"off" 这类字符串布尔值应被正确识别。"""
    monkeypatch.delenv("XENON_STRICT_VALIDATION", raising=False)
    monkeypatch.delenv("XENON_CONFIG_WATCH", raising=False)

    with open(mock_config_path, "w") as f:
        yaml.dump(
            {"validation": {"strict": "true"}, "watch": {"enabled": "off"}},
            f,
        )

    config = load_config()
    assert config.validation.strict is True
    assert config.watch.enabled is False


def test_malformed_section_ignored(mock_config_path: Path, monkeypatch):
    """某个分组不是映射时，只忽略该分组，其余分组仍生效。"""
    monkeypatch.delenv("XENON_STRICT_VALIDATION", raising=False)
    monkeypatch.delenv("XENON_CONFIG_WATCH", raising=False)

    with open(mock_config_path, "w") as f:
        yaml.dump(
            {"validation": "should-be-a-mapping", "watch": {"enabled": False}},
            f,
        )

    config = load_config()
    assert config.validation.strict is False  # 坏分组回退默认
    assert config.watch.enabled is False      # 好分组照常生效


def test_reload_config(mock_config_path: Path, monkeypatch):
    """reload_config() 应丢弃缓存并重新读取配置文件。"""
    monkeypatch.delenv("XENON_STRICT_VALIDATION", raising=False)

    config_data: dict[str, Any] = {"validation": {"strict": False}}
    with open(mock_config_path, "w") as f:
        yaml.dump(config_data, f)

    assert get_config().validation.strict is False

    # 修改配置文件（不动 mtime，直接靠 reload 强制失效缓存）
    config_data["validation"]["strict"] = True
    with open(mock_config_path, "w") as f:
        yaml.dump(config_data, f)

    config2 = reload_config()
    assert config2.validation.strict is True

    # 后续 get_config() 也应看到新值
    assert get_config().validation.strict is True


def test_partial_config_file(mock_config_path: Path):
    """配置文件只包含部分字段时，其余字段应使用默认值。"""
    config_data = {
        "validation": {"strict": True},
        # 其他分组缺失
    }
    with open(mock_config_path, "w") as f:
        yaml.dump(config_data, f)

    config = load_config()

    assert config.validation.strict is True
    # 其他字段应为默认值
    assert config.engine.disable_auto_routing is False
    assert config.watch.enabled is True
    assert config.limits.max_models_per_provider == 3


def test_invalid_yaml_file(mock_config_path: Path):
    """配置文件格式错误时，应回退到默认值。"""
    with open(mock_config_path, "w") as f:
        f.write("invalid: yaml: content: ][")

    config = load_config()

    # 应使用默认值
    assert config.validation.strict is False
    assert config.engine.disable_auto_routing is False


def test_non_dict_yaml(mock_config_path: Path):
    """配置文件不是字典时，应回退到默认值。"""
    with open(mock_config_path, "w") as f:
        yaml.dump(["not", "a", "dict"], f)

    config = load_config()

    # 应使用默认值
    assert config.validation.strict is False


def test_generate_default_config_file(tmp_path: Path):
    """测试生成默认配置文件模板。"""
    config_path = tmp_path / "generated_config.yaml"

    generate_default_config_file(config_path)

    assert config_path.exists()

    # 验证生成的文件可以被 YAML 解析
    with open(config_path) as f:
        content = f.read()
        assert "validation:" in content
        assert "engine:" in content
        assert "watch:" in content
        assert "limits:" in content
        assert "paths:" in content
        assert "interaction:" in content
        assert "development:" in content

        # 验证注释存在
        assert "XENON_STRICT_VALIDATION" in content
        assert "环境变量" in content


def test_config_path_expansion(mock_config_path: Path, monkeypatch):
    """测试路径配置中的 ~ 扩展。"""
    config_data = {
        "paths": {
            "credentials": "~/my_credentials.yaml",
        }
    }
    with open(mock_config_path, "w") as f:
        yaml.dump(config_data, f)

    # 注意：配置加载时不会自动扩展 ~，由使用方扩展
    config = load_config()
    assert config.paths.credentials == "~/my_credentials.yaml"


def test_empty_string_env_uses_default(mock_config_path: Path, monkeypatch):
    """空字符串环境变量应使用默认值。"""
    monkeypatch.setenv("XENON_CREDENTIALS_PATH", "   ")  # 只有空格
    monkeypatch.setenv("XENON_REGISTER_MODULE_ALLOW", "")

    config = load_config()

    # 空字符串应使用默认值
    assert config.paths.credentials == str(Path.home() / ".xenon" / "credentials.yaml")
    assert config.development.register_module_allow == ""


def test_all_env_vars_respected(mock_config_path: Path, monkeypatch):
    """测试所有环境变量都被正确识别。"""
    env_vars = {
        "XENON_STRICT_VALIDATION": "1",
        "XENON_NO_AUTO_ENGINE": "1",
        "XENON_CONFIG_WATCH": "0",
        "XENON_MAX_MODELS_PER_PROVIDER": "7",
        "XENON_CREDENTIALS_PATH": "/custom/creds.yaml",
        "XENON_CACHE_DIR": "/tmp/cache",
        "XENON_PROJECT_ROOT": "/project",
        "XENON_ASSUME_YES": "1",
        "XENON_TERMINAL_ASCII": "1",
        "XENON_REGISTER_MODULE_ALLOW": "mod1,mod2",
        "XENON_ALLOW_HOME_PROJECT": "1",
        "XENON_NO_PT": "1",
    }

    for key, value in env_vars.items():
        monkeypatch.setenv(key, value)

    config = load_config()

    assert config.validation.strict is True
    assert config.engine.disable_auto_routing is True
    assert config.watch.enabled is False
    assert config.limits.max_models_per_provider == 7
    assert config.paths.credentials == "/custom/creds.yaml"
    assert config.paths.cache == "/tmp/cache"
    assert config.paths.project_root == "/project"
    assert config.interaction.assume_yes is True
    assert config.interaction.terminal_ascii is True
    assert config.development.register_module_allow == "mod1,mod2"
    assert config.development.allow_home_project is True
    assert config.development.no_pty is True
