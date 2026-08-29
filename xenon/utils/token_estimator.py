"""
检查点辅助函数 - 用于 token 估算。

提供简单的 token 估算功能，用于检查点管理器判断保存时机。
"""


def estimate_tokens(text: str) -> int:
    """
    估算文本的 token 数量。

    使用简单的启发式规则：
    - 中文字符: ~2 字符/token
    - 英文字符: ~4 字符/token

    Args:
        text: 要估算的文本

    Returns:
        估算的 token 数
    """
    if not text:
        return 0

    # 统计中文字符
    chinese_chars = sum(1 for c in text if '一' <= c <= '鿿')
    english_chars = len(text) - chinese_chars

    # 中文约 2 字符/token，英文约 4 字符/token
    estimated = chinese_chars // 2 + english_chars // 4

    return max(estimated, 1)
