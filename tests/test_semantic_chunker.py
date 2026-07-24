"""v0.5.0: 语义分块器测试。"""
from xenon.repl.semantic_chunker import SemanticChunker, _extract_tool_name_from_turn
from xenon.repl.context_manager import ConversationTurn


def _make(role="user", content="", turn_type="general", tier=3, idx=0):
    return ConversationTurn(
        role=role, content=content, turn_type=turn_type, task_tier=tier, turn_index=idx,
    )


class TestSemanticChunker:
    def test_empty_turns(self):
        chunks = SemanticChunker().group([])
        assert chunks == []

    def test_single_turn(self):
        turns = [_make("user", "hello", "user_input", tier=1, idx=0)]
        chunks = SemanticChunker().group(turns)
        assert len(chunks) == 1
        assert chunks[0].chunk_type == "single_turn"
        assert chunks[0].size == 1

    def test_simple_qa_merged(self):
        """简单问答合并为一个块。"""
        turns = [
            _make("user", "你好", "user_input", tier=1, idx=0),
            _make("assistant", "你好！", "assistant_output", tier=1, idx=1),
        ]
        chunks = SemanticChunker().group(turns)
        assert len(chunks) == 1
        assert chunks[0].chunk_type == "single_turn"
        assert chunks[0].size == 2

    def test_tool_chain_merged(self):
        """工具链合并为原子块。"""
        turns = [
            _make("user", "读取 app.py", "user_input", tier=3, idx=0),
            _make("assistant", "<function_call>", "tool_call", tier=3, idx=1),
            _make("tool", "Observation: ...", "tool_result", tier=3, idx=2),
            _make("assistant", "文件内容如下...", "assistant_output", tier=3, idx=3),
        ]
        chunks = SemanticChunker().group(turns)
        assert len(chunks) == 1
        assert chunks[0].chunk_type == "tool_chain"
        assert chunks[0].size == 4
        assert chunks[0].has_tool_calls is True
        assert chunks[0].is_atomic is True

    def test_multi_turn_qa_separate_chunks(self):
        """多轮独立问答各自成块。"""
        turns = [
            _make("user", "Q1", "user_input", tier=1, idx=0),
            _make("assistant", "A1", "assistant_output", tier=1, idx=1),
            _make("user", "Q2", "user_input", tier=3, idx=2),
            _make("assistant", "A2", "assistant_output", tier=3, idx=3),
        ]
        chunks = SemanticChunker().group(turns)
        assert len(chunks) == 2
        assert chunks[0].size == 2
        assert chunks[1].size == 2

    def test_system_messages_separate(self):
        """System 消息独立成块。"""
        turns = [
            _make("system", "You are helpful", "system", tier=3, idx=0),
            _make("user", "Hi", "user_input", tier=1, idx=1),
            _make("assistant", "Hello", "assistant_output", tier=1, idx=2),
        ]
        chunks = SemanticChunker().group(turns)
        assert len(chunks) == 2
        assert chunks[0].chunk_type == "system"
        assert chunks[1].chunk_type == "single_turn"

    def test_dominant_tier_is_max(self):
        """主导 tier 取块内最大值。"""
        turns = [
            _make("user", "简单任务", "user_input", tier=1, idx=0),
            _make("assistant", "需要工具", "tool_call", tier=3, idx=1),
            _make("tool", "结果", "tool_result", tier=3, idx=2),
            _make("assistant", "复杂分析", "assistant_output", tier=5, idx=3),
        ]
        chunks = SemanticChunker().group(turns)
        assert chunks[0].dominant_tier == 5

    def test_compress_simple_chunk(self):
        """简单块的压缩。"""
        turns = [
            _make("user", "Q1", "user_input", tier=1, idx=0),
            _make("assistant", "A1", "assistant_output", tier=1, idx=1),
        ]
        chunks = SemanticChunker().group(turns)
        # 小于等于 2 轮不压缩
        result = SemanticChunker().compress_chunk(chunks[0])
        assert result is None

    def test_compress_tool_chain(self):
        """工具链块的压缩。"""
        turns = [
            _make("user", "读取文件", "user_input", tier=3, idx=0),
            _make("assistant", "<read_file>", "tool_call", tier=3, idx=1),
            _make("tool", "结果...", "tool_result", tier=3, idx=2),
            _make("assistant", "分析...", "assistant_output", tier=3, idx=3),
        ]
        chunks = SemanticChunker().group(turns)
        result = SemanticChunker().compress_chunk(chunks[0])
        assert result is not None
        assert "工具链块" in result.content
        assert "工具" in result.content

    def test_block_map(self):
        """group_id → 索引映射正确。"""
        turns = [
            _make("system", "prompt", "system", idx=0),
            _make("user", "Q1", "user_input", idx=1),
            _make("assistant", "A1", "assistant_output", idx=2),
            _make("user", "Q2", "user_input", idx=3),
            _make("assistant", "A2", "assistant_output", idx=4),
        ]
        mapper = SemanticChunker().build_block_map(turns)
        assert len(mapper) == 3  # system + 2 QA blocks
        # 索引覆盖所有 turns
        all_indices = []
        for indices in mapper.values():
            all_indices.extend(indices)
        assert sorted(all_indices) == [0, 1, 2, 3, 4]

    def test_consecutive_assistant_merged(self):
        """连续的 assistant 消息合并到同一个块。"""
        turns = [
            _make("user", "分析", "user_input", tier=3, idx=0),
            _make("assistant", "第一步分析", "assistant_output", tier=3, idx=1),
            _make("assistant", "第二步分析", "assistant_output", tier=3, idx=2),
        ]
        chunks = SemanticChunker().group(turns)
        assert len(chunks) == 1
        assert chunks[0].size == 3


class TestExtractToolName:
    def test_from_metadata(self):
        turn = _make(content="x")
        turn.metadata["tool_name"] = "read_file"
        assert _extract_tool_name_from_turn(turn) == "read_file"

    def test_from_turn_type(self):
        turn = _make("assistant", "x", "tool_call")
        assert _extract_tool_name_from_turn(turn) == "tool_call"

    def test_unknown(self):
        turn = _make(content="no tool name here")
        assert _extract_tool_name_from_turn(turn) == "unknown"
