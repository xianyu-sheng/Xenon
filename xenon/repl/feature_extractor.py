"""
任务特征提取器 - 从用户输入中提取任务特征

用于智能路由决策的第一步：分析用户输入，判断任务类型、复杂度、质量要求等特征。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xenon.repl.context_manager import ContextManager


@dataclass
class TaskFeatures:
    """任务特征数据类"""

    # 基础特征
    is_query: bool              # 是否为查询类任务
    is_coding: bool             # 是否涉及代码
    is_exploratory: bool        # 是否为探索性任务

    # 复杂度指标
    input_length: int           # 用户输入长度
    estimated_steps: int        # 预估步骤数
    estimated_tool_calls: int   # 预估工具调用次数
    complexity_score: float     # 综合复杂度 (0-1)

    # 上下文特征
    has_file_operations: bool   # 是否涉及文件操作
    has_git_operations: bool    # 是否涉及 Git 操作
    needs_web_access: bool      # 是否需要网络访问

    # 质量要求
    quality_critical: bool      # 是否对质量敏感


class FeatureExtractor:
    """
    任务特征提取器

    基于关键词匹配和启发式规则，从用户输入中提取任务特征。
    """

    # 关键词模式
    # 注意：中文不需要 \b 边界，使用简单的包含匹配
    CODING_KEYWORDS = [
        '写', '创建', '修改', '重构', '优化', '实现', '函数', '类', '模块', '代码',
        'bug', '调试', '测试', 'fix', 'implement', 'refactor', 'optimize',
        'function', 'class', 'module', 'debug', '生成', '编写', '构建'
    ]

    QUERY_KEYWORDS = [
        '什么', '为什么', '如何', '怎么', '解释', '说明', '查看', '显示', '列出', '介绍',
        'what', 'why', 'how', 'explain', 'show', 'list', 'describe', 'tell'
    ]

    TOOL_KEYWORDS = [
        '文件', '目录', 'git', '命令', '运行', '执行', '搜索', '查找', '读取', '写入',
        'file', 'directory', 'folder', 'command', 'run', 'execute', 'search',
        'find', 'read', 'write', '创建', '删除', '移动', '复制'
    ]

    QUALITY_KEYWORDS = [
        '生产', '发布', '重要', '关键', '严格', '审查', '仔细', '安全', '稳定',
        'production', 'release', 'critical', 'important', 'review', 'careful',
        'secure', 'stable', '认证', '权限'
    ]

    EXPLORATORY_KEYWORDS = [
        '探索', '尝试', '看看', '试试', '找找', '分析', '调查', '研究',
        'explore', 'try', 'investigate', 'analyze', 'research', 'study'
    ]

    def extract(
        self,
        user_input: str,
        context: ContextManager
    ) -> TaskFeatures:
        """
        提取任务特征

        Args:
            user_input: 用户输入文本
            context: 对话上下文（暂未使用，预留给 Phase 2）

        Returns:
            TaskFeatures: 提取的任务特征
        """
        text = user_input.lower()

        # 基础特征判断
        is_query = self._is_query(text, user_input)
        is_coding = self._is_coding(text)
        is_exploratory = self._is_exploratory(text)

        # 复杂度评估
        input_length = len(user_input)
        estimated_steps = self._estimate_steps(text)
        estimated_tool_calls = self._estimate_tool_calls(text)
        complexity_score = self._calculate_complexity(
            input_length, estimated_steps, estimated_tool_calls
        )

        # 上下文特征
        has_file_operations = self._has_file_operations(text)
        has_git_operations = 'git' in text
        needs_web_access = self._needs_web_access(text)

        # 质量要求
        quality_critical = self._is_quality_critical(text)

        return TaskFeatures(
            is_query=is_query,
            is_coding=is_coding,
            is_exploratory=is_exploratory,
            input_length=input_length,
            estimated_steps=estimated_steps,
            estimated_tool_calls=estimated_tool_calls,
            complexity_score=complexity_score,
            has_file_operations=has_file_operations,
            has_git_operations=has_git_operations,
            needs_web_access=needs_web_access,
            quality_critical=quality_critical,
        )

    def _is_query(self, text: str, original: str) -> bool:
        """
        判断是否为查询类任务

        查询类任务：包含疑问词，且不包含明显的动作指令
        """
        # 包含查询关键词
        has_query_kw = any(kw in text for kw in self.QUERY_KEYWORDS)

        # 包含问号
        has_question_mark = '?' in original or '？' in original

        # 包含动作关键词（创建、修改、删除等）
        action_keywords = [
            '创建', '修改', '删除', '写', '实现', '生成', '添加',
            'create', 'modify', 'delete', 'write', 'implement', 'generate', 'add'
        ]
        has_action_kw = any(kw in text for kw in action_keywords)

        # 查询类：有疑问词或问号，且无动作词
        return (has_query_kw or has_question_mark) and not has_action_kw

    def _is_coding(self, text: str) -> bool:
        """判断是否涉及代码"""
        return any(kw in text for kw in self.CODING_KEYWORDS)

    def _is_exploratory(self, text: str) -> bool:
        """判断是否为探索性任务"""
        return any(kw in text for kw in self.EXPLORATORY_KEYWORDS)

    def _estimate_steps(self, text: str) -> int:
        """
        预估任务步骤数

        基于连接词、分句、列举项等启发式估算
        """
        step_count = 1  # 至少一步

        # 连接词：然后、接着、之后、再等
        connectors = [
            '然后', '接着', '之后', '再', '最后', '并且',
            'then', 'next', 'after', 'and then', 'finally'
        ]
        for conn in connectors:
            step_count += text.count(conn)

        # 标点符号：分号、换行
        step_count += text.count(';')
        step_count += text.count('；')
        step_count += min(text.count('\n'), 5)  # 换行最多算5步

        # 列举：1. 2. 3. 或 - 项
        import re
        numbered_items = re.findall(r'\d+[.、]', text)
        step_count += len(numbered_items)

        bullet_items = re.findall(r'^[\-\*]\s', text, re.MULTILINE)
        step_count += len(bullet_items)

        return min(step_count, 10)  # 上限10步

    def _estimate_tool_calls(self, text: str) -> int:
        """
        预估工具调用次数

        基于工具相关关键词的出现次数
        """
        tool_count = sum(1 for kw in self.TOOL_KEYWORDS if kw in text)
        return min(tool_count, 10)  # 上限10次

    def _calculate_complexity(
        self,
        input_length: int,
        estimated_steps: int,
        estimated_tool_calls: int
    ) -> float:
        """
        计算综合复杂度 (0-1)

        综合考虑输入长度、步骤数、工具调用数
        """
        # 空输入特殊处理
        if input_length == 0 and estimated_steps == 1 and estimated_tool_calls == 0:
            return 0.0

        # 长度分数 (0-0.3)，500字符为基准
        length_score = min(input_length / 500, 1.0) * 0.3

        # 步骤分数 (0-0.4)，5步为基准
        steps_score = min(estimated_steps / 5, 1.0) * 0.4

        # 工具分数 (0-0.3)，5次调用为基准
        tools_score = min(estimated_tool_calls / 5, 1.0) * 0.3

        return min(length_score + steps_score + tools_score, 1.0)

    def _has_file_operations(self, text: str) -> bool:
        """判断是否涉及文件操作"""
        file_keywords = [
            '文件', '目录', '文件夹', '路径', '保存', '加载',
            'file', 'directory', 'folder', 'path', 'save', 'load',
            '.py', '.js', '.txt', '.json', '.yaml', '.md'  # 文件扩展名
        ]
        return any(kw in text for kw in file_keywords)

    def _needs_web_access(self, text: str) -> bool:
        """判断是否需要网络访问"""
        web_keywords = [
            '网页', '网站', '在线', '搜索', '下载', 'api',
            'url', 'http', 'https', 'web', 'website', 'online',
            'search', 'download', 'fetch'
        ]
        return any(kw in text for kw in web_keywords)

    def _is_quality_critical(self, text: str) -> bool:
        """判断是否对质量敏感"""
        return any(kw in text for kw in self.QUALITY_KEYWORDS)
