"""
Skill Manager — 自定义技能管理器。

Skill 是更复杂的能力，支持 LLM 调用 + 工具执行的多步骤组合。
存储位置: ~/.xenon/skills/<name>.yaml
"""

from __future__ import annotations

import logging
import shlex
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


def _shell_quote(value) -> str:
    """对模板替换值做 shell 转义，防止参数注入（A4，§8.10.3）。

    POSIX 用 shlex.quote；Windows PowerShell 用单引号 doubling。
    """
    text = str(value)
    if sys.platform == "win32":
        return "'" + text.replace("'", "''") + "'"
    return shlex.quote(text)


_SKILLS_DIR = Path.home() / ".xenon" / "skills"


@dataclass
class SkillStep:
    """Skill 的一个步骤。"""
    type: str                    # "llm" | "command" | "write_file" | "read_file" | "echo"
    prompt: str = ""             # LLM 步骤的提示词
    action: str = ""             # command 步骤的命令
    file_path: str = ""          # 文件路径
    content: str = ""            # 文件内容
    output_var: str = ""         # 输出变量名（存入 context）


@dataclass
class Skill:
    """一个技能。"""
    name: str
    description: str = ""
    system_prompt: str = ""      # LLM 步骤的系统提示
    steps: list[SkillStep] = field(default_factory=list)
    params: list[dict[str, str]] = field(default_factory=list)


class SkillManager:
    """技能管理器。"""

    def __init__(self, skills_dir: Path | None = None) -> None:
        self.skills_dir = skills_dir or _SKILLS_DIR
        self.skills: dict[str, Skill] = {}
        self.load()

    def load(self) -> None:
        """从磁盘加载所有技能。"""
        if not self.skills_dir.exists():
            self.skills = {}
            return

        try:
            for f in self.skills_dir.glob("*.yaml"):
                data = yaml.safe_load(f.read_text(encoding="utf-8"))
                if data and "name" in data:
                    steps = [SkillStep(**s) for s in data.get("steps", [])]
                    skill = Skill(
                        name=data["name"],
                        description=data.get("description", ""),
                        system_prompt=data.get("system_prompt", ""),
                        steps=steps,
                        params=data.get("params", []),
                    )
                    self.skills[skill.name] = skill
            logger.info(f"加载了 {len(self.skills)} 个技能")
        except Exception as e:
            logger.warning(f"加载技能失败: {e}")
            self.skills = {}

    def save(self) -> None:
        """保存所有技能到磁盘。"""
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        for name, skill in self.skills.items():
            data = asdict(skill)
            path = self.skills_dir / f"{name}.yaml"
            path.write_text(
                yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False),
                encoding="utf-8",
            )

    def save_one(self, skill: Skill) -> None:
        """保存单个技能。"""
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        data = asdict(skill)
        path = self.skills_dir / f"{skill.name}.yaml"
        path.write_text(
            yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )

    def create(
        self,
        name: str,
        description: str,
        steps: list[dict[str, str]],
        system_prompt: str = "",
        params: list[dict[str, str]] | None = None,
    ) -> Skill:
        """创建一个技能。"""
        name = name.lstrip("/").lower().replace(" ", "_")

        skill_steps = [SkillStep(**s) for s in steps]
        skill = Skill(
            name=name,
            description=description,
            system_prompt=system_prompt,
            steps=skill_steps,
            params=params or [],
        )
        self.skills[name] = skill
        self.save_one(skill)
        logger.info(f"创建技能: /{name}")
        return skill

    def remove(self, name: str) -> bool:
        """删除一个技能。"""
        name = name.lstrip("/").lower()
        if name in self.skills:
            del self.skills[name]
            path = self.skills_dir / f"{name}.yaml"
            if path.exists():
                path.unlink()
            return True
        return False

    def list_all(self) -> list[Skill]:
        """列出所有技能。"""
        return list(self.skills.values())

    def get(self, name: str) -> Skill | None:
        """获取一个技能。"""
        return self.skills.get(name.lstrip("/").lower())

    def execute(self, name: str, args: str, model_priority: list[str] | None = None) -> str:
        """执行一个技能。"""
        skill = self.get(name)
        if not skill:
            return f"❌ 技能 /{name} 不存在"

        # 参数替换
        param_values = self._parse_args(args, skill.params)

        context: dict[str, str] = dict(param_values)
        results = []

        for i, step in enumerate(skill.steps):
            try:
                if step.type == "llm":
                    output = self._execute_llm_step(step, context, skill.system_prompt, model_priority)
                elif step.type == "command":
                    output = self._execute_command_step(step, context)
                elif step.type == "echo":
                    output = self._resolve_template(step.prompt or step.action, context)
                elif step.type == "write_file":
                    output = self._execute_write_file(step, context)
                elif step.type == "read_file":
                    output = self._execute_read_file(step, context)
                else:
                    output = f"未知步骤类型: {step.type}"

                if step.output_var:
                    context[step.output_var] = output

                results.append(f"[步骤 {i + 1}] {output[:500]}")

            except Exception as e:
                error_msg = f"[步骤 {i + 1} 错误] {e}"
                results.append(error_msg)
                break

        return "\n\n".join(results)

    def _execute_llm_step(
        self, step: SkillStep, context: dict, system_prompt: str,
        model_priority: list[str] | None,
    ) -> str:
        """执行 LLM 步骤。"""
        from xenon.utils.llm_client import chat_completion

        prompt = self._resolve_template(step.prompt, context)
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        if not model_priority:
            return "错误: 未配置模型"

        last_error = None
        for model_id in model_priority:
            try:
                return chat_completion(model_id, messages, max_tokens=4096)
            except Exception as e:
                last_error = e

        return f"LLM 调用失败: {last_error}"

    def _execute_command_step(self, step: SkillStep, context: dict) -> str:
        """执行命令步骤。"""
        import subprocess

        # A4: 命令模板的替换值做 shell 转义，防止参数注入
        cmd = self._resolve_template(step.action, context, quote=True)

        try:
            if sys.platform == "win32":
                proc = subprocess.run(
                    ["powershell", "-Command", cmd],
                    capture_output=True, text=True, timeout=60,
                )
            else:
                proc = subprocess.run(
                    ["/bin/bash", "-c", cmd],
                    capture_output=True, text=True, timeout=60,
                )

            output = proc.stdout.strip()
            if proc.stderr.strip():
                output += f"\n[stderr] {proc.stderr.strip()}"
            return output or "(无输出)"

        except subprocess.TimeoutExpired:
            return f"命令超时: {cmd}"
        except Exception as e:
            return f"命令执行失败: {e}"

    def _execute_write_file(self, step: SkillStep, context: dict) -> str:
        """执行写文件步骤。"""
        path = self._resolve_template(step.file_path, context)
        content = self._resolve_template(step.content, context)

        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"已写入文件: {path}"

    def _execute_read_file(self, step: SkillStep, context: dict) -> str:
        """执行读文件步骤。"""
        path = self._resolve_template(step.file_path, context)
        p = Path(path)

        if not p.exists():
            return f"文件不存在: {path}"

        content = p.read_text(encoding="utf-8")
        return content[:5000]  # 限制长度

    def _parse_args(self, args: str, params: list[dict[str, str]]) -> dict[str, str]:
        """解析用户参数。"""
        result = {}

        for p in params:
            if "default" in p:
                result[p["name"]] = p["default"]

        if args.strip():
            parts = args.split()
            for i, p in enumerate(params):
                if i < len(parts):
                    result[p["name"]] = parts[i]

        return result

    @staticmethod
    def _resolve_template(template: str, context: dict, *, quote: bool = False) -> str:
        """模板变量替换（安全版，只替换 {word} 不支持属性访问）。

        quote=True 时对替换值做 shell 转义（用于命令模板，防注入，A4 §8.10.3）。
        """
        import re
        def _replace(m: re.Match) -> str:
            key = m.group(1)
            val = context.get(key)
            if val is None:
                return m.group(0)
            return _shell_quote(val) if quote else str(val)
        return re.sub(r"\{(\w+)\}", _replace, template)
