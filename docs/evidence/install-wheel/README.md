# 安装即用（#110）：wheel 干净安装 + 三个入口验证

## 根因（从 CHANGELOG 定位）

0.9.3 wheel 不含 `python -m xenon` 入口（`CHANGELOG.md` 0.9.4 节明确记载），
用户 `pip install` 后执行 `xenon` 失败。0.9.4 起新增 `python -m xenon` 入口并
补充 Windows PATH 排障文档；0.9.5 延续。

## 验证（0.9.5 wheel，真实用户路径）

```text
$ python -m venv /tmp/xenon-venv
$ /tmp/xenon-venv/Scripts/python.exe -m pip install dist/xenon_harness-0.9.5-py3-none-any.whl
$ /tmp/xenon-venv/Scripts/xenon.exe --version   -> xenon 0.9.5
$ /tmp/xenon-venv/Scripts/xenon.exe --help      -> usage 正常
$ /tmp/xenon-venv/Scripts/python.exe -m xenon --version  -> xenon 0.9.5（0.9.4 起的修复入口）
$ /tmp/xenon-venv/Scripts/xenon.exe models      -> 用法正常
$ /tmp/xenon-venv/Scripts/xenon.exe integrations -> skill 扫描正常
```

- console script：`[console_scripts] xenon = xenon.main:cli`（wheel METADATA 确认）；
- 依赖：`pip install` 带依赖安装后 `rich` 等齐全，无 `ModuleNotFoundError`。

## 结论

#110 在 0.9.4 已修复；0.9.5 干净环境安装后「一键安装即可使用」成立。
