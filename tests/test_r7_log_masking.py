"""R7 验收：敏感参数脱敏 + 日志级别归位。

- mask_sensitive_params：敏感键（api_key/token/content/python_function/
  command_template 等）值替换为 <masked len=N>，大小写不敏感；非敏感保留。
- ConsoleCallback.on_act verbose 输出不泄露敏感值。
"""
from xenon.engine.callbacks import ConsoleCallback, mask_sensitive_params


class TestMaskSensitiveParams:
    def test_masks_api_key_token_content(self):
        out = mask_sensitive_params({"api_key": "sk-x", "token": "tok", "content": "abc"})
        assert out["api_key"].startswith("<masked")
        assert out["token"].startswith("<masked")
        assert out["content"].startswith("<masked")
        assert "sk-x" not in out.values()
        assert "abc" not in out.values()

    def test_masks_python_function_command_template(self):
        out = mask_sensitive_params({
            "python_function": "def f(): pass",
            "command_template": "rm -rf /tmp",
        })
        assert out["python_function"].startswith("<masked")
        assert out["command_template"].startswith("<masked")
        assert "def f" not in str(out["python_function"])
        assert "rm -rf" not in str(out["command_template"])

    def test_masked_includes_length_for_strings(self):
        assert mask_sensitive_params({"api_key": "sk-x"})["api_key"] == "<masked len=4>"

    def test_non_sensitive_preserved(self):
        out = mask_sensitive_params({"file_path": "/a.py", "pattern": "foo"})
        assert out["file_path"] == "/a.py"
        assert out["pattern"] == "foo"

    def test_case_insensitive(self):
        out = mask_sensitive_params({"API_KEY": "sk", "Token": "t"})
        assert out["API_KEY"].startswith("<masked")
        assert out["Token"].startswith("<masked")

    def test_non_string_sensitive_value(self):
        assert mask_sensitive_params({"token": 12345})["token"] == "<masked>"

    def test_non_dict_returns_truncated_repr(self):
        out = mask_sensitive_params("not a dict")
        assert isinstance(out, str)
        assert "not a dict" in out

    def test_does_not_mutate_input(self):
        original = {"api_key": "sk-x", "file_path": "/a"}
        mask_sensitive_params(original)
        assert original == {"api_key": "sk-x", "file_path": "/a"}


class TestConsoleCallbackOnActMasking:
    def test_verbose_output_does_not_leak_sensitive(self, capsys):
        cb = ConsoleCallback(verbose=True)
        cb.on_act("write_file", {
            "file_path": "/tmp/x.py",
            "content": "SECRET_CODE_HERE",
            "api_key": "sk-leak",
        })
        out = capsys.readouterr().out
        assert "SECRET_CODE_HERE" not in out
        assert "sk-leak" not in out
        assert "<masked" in out
        assert "/tmp/x.py" in out  # 非敏感参数保留可见

    def test_non_verbose_does_not_print(self, capsys):
        cb = ConsoleCallback(verbose=False)
        cb.on_act("write_file", {"content": "secret", "api_key": "sk"})
        out = capsys.readouterr().out
        assert out == ""
