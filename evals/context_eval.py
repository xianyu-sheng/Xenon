"""
v0.5.0 上下文压缩实效评测（自适应版）。

自动调整参数确保压缩真实触发，然后验证信息保留率。
"""
from __future__ import annotations

import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xenon.repl.context_manager import ContextManager, ConversationTurn
from xenon.repl.context_strategies import TieredStrategySelector, SpaceBudget
from xenon.utils.llm_client import chat_completion


def make_fill(char: str, tokens_target: int) -> str:
    """生成达到目标 token 数的填充文本。"""
    # 英文场景 ~0.75 token/char，中文 ~2 token/char
    return char * int(tokens_target * 1.3)


def build_and_compact(
    tier: int,
    rounds: list[tuple[str, str]],  # [(user_msg, assistant_msg), ...]
    label: str,
    model_id: str,
) -> tuple[ContextManager, str, bool]:
    """构建对话 → 自动调整 max_tokens → 触发压缩。

    Returns:
        (ContextManager, 结果描述, 是否真正压缩了)
    """
    # 先用大窗口构建
    cm = ContextManager(max_tokens=128000)
    cm.set_active_tier(tier)

    for user_msg, asst_msg in rounds:
        cm.add_user_message(user_msg)
        cm.add_assistant_message(
            asst_msg,
            model_used="deepseek/deepseek-v4-pro" if tier >= 3 else "deepseek/deepseek-v4-flash",
        )

    actual_tokens = cm.current_token_usage()
    strategy = TieredStrategySelector().get_preset(tier)
    trigger = strategy.trigger_threshold

    # 自动设置 max_tokens 使 ratio 刚好超过 trigger
    # 目标: ratio = trigger + 0.05 (确保触发但不至于太临界)
    target_ratio = min(trigger + 0.08, 0.95)
    target_max_tokens = int(actual_tokens / target_ratio)

    # 确保有 older 可压缩：需要至少 strategy.keep_recent_rounds + 1 轮用户消息
    user_count = sum(1 for t in cm.history if t.role == "user")
    if user_count <= strategy.keep_recent_rounds:
        return cm, f"用户轮数 {user_count} <= keep_recent {strategy.keep_recent_rounds}，无 older 可压缩", False

    # 重建 ContextManager with calculated max_tokens
    cm2 = ContextManager(max_tokens=target_max_tokens)
    cm2.set_active_tier(tier)
    for t in cm.history:
        cm2.add_message(t.role, t.content, task_tier=getattr(t, "task_tier", tier))

    ratio = cm2.usage_ratio()
    space = SpaceBudget.evaluate(ratio)

    print(f"  [{label}] tokens={actual_tokens}, max_tokens={target_max_tokens}, "
          f"ratio={ratio:.1%}, space={space}, tier=Q{tier}")
    print(f"    策略: trigger={trigger}, keep_recent={strategy.keep_recent_rounds}, "
          f"crisis={strategy.crisis_action}")

    # 触发压缩
    t0 = time.time()
    result = cm2.compact(model_priority=[model_id])
    elapsed = (time.time() - t0) * 1000

    n_before = len(cm.history)
    n_after = len(cm2.history)
    compressed = n_after < n_before

    print(f"    压缩: {n_before}→{n_after} 轮, 耗时={elapsed:.0f}ms, "
          f"摘要前100字: {result[:100]}...")

    if not compressed:
        print(f"    ⚠️ 压缩未触发！原因可能是 older 为空。")

    return cm2, result, compressed


def verify(cm: ContextManager, queries: list[dict], model_id: str) -> list[dict]:
    """验证压缩后上下文的信息保留。"""
    results = []
    context_turns = cm.get_messages()

    for i, q in enumerate(queries):
        prompt = (
            "你是上下文记忆验证助手。以下是对话的压缩摘要和历史。"
            "只根据以下信息回答问题。信息不充分则回答'无法确定'。\n\n"
            "=== 对话上下文 ===\n"
        )
        for turn in context_turns:
            role_label = {"user": "用户", "assistant": "助手", "system": "系统"}.get(turn["role"], turn["role"])
            prompt += f"[{role_label}] {turn['content'][:400]}\n\n"
        prompt += f"=== 问题 ===\n{q['query']}\n\n请用一句话简洁回答。"

        try:
            t0 = time.time()
            resp = chat_completion(model_id, [{"role": "user", "content": prompt}],
                                   temperature=0.0, max_tokens=150)
            elapsed = (time.time() - t0) * 1000
            answer = resp.strip()[:300]
            expected = q["expected_keywords"]
            matched = [kw for kw in expected if kw.lower() in answer.lower()]
            missing = [kw for kw in expected if kw.lower() not in answer.lower()]
            score = len(matched) / len(expected) if expected else 1.0

            print(f"    [{i+1}/{len(queries)}] {q['query'][:50]}...")
            print(f"      答: {answer[:120]}")
            print(f"      得分: {score:.0%} (匹配 {matched}, 缺失 {missing}) [{elapsed:.0f}ms]")

            results.append({"query": q["query"], "answer": answer,
                           "matched": matched, "missing": missing,
                           "score": score, "latency_ms": elapsed})
        except Exception as e:
            print(f"    [{i+1}] ❌ API失败: {e}")
            results.append({"query": q["query"], "answer": "",
                           "matched": [], "missing": q["expected_keywords"],
                           "score": 0.0, "error": str(e)})
    return results


def main(model_id="deepseek/deepseek-v4-pro"):
    print("=" * 60)
    print("Xenon v0.5.0 上下文压缩实效评测")
    print("=" * 60)
    print(f"模型: {model_id}\n")

    all_results = []
    compression_triggered = 0

    # ── 场景 1: Q4 编程 — 正常 LLM 6 段压缩 ──
    print("─" * 40)
    print("场景 1: Q4 多轮编程 → 正常 LLM 6段摘要路径")
    print("─" * 40)

    # 6 轮用户消息（> Q4 keep_recent=4），带大量填充确保 token 充足
    rounds1 = [
        ("我想用 Python 实现网页爬虫，需要并发抓取和 JSON 存储结果。",
         "好的。方案：aiohttp + Semaphore + BeautifulSoup + JSON 存储。" + make_fill("x", 400)),
        ("请创建 crawler.py 主文件",
         "已创建。WebCrawler 类：默认并发数 5，超时 30 秒，用 Semaphore 控并发。" + make_fill("y", 400)),
        ("创建 storage.py，需支持增量追加和 URL 去重",
         "已创建。ResultStorage 类：基于 URL 的 seen_urls 集合去重，输出 results.json。" + make_fill("z", 400)),
        ("创建 config.yaml 配置文件",
         "已创建。max_retries=3, retry_delay=2, output_dir=./data, format=json。" + make_fill("w", 400)),
        ("分析性能瓶颈",
         "网络 I/O 占 80%，HTML 解析 10%，JSON 序列化 5%。建议连接池复用。" + make_fill("v", 300)),
        ("确认所有文件已创建",
         "已确认：crawler.py, storage.py, config.yaml 均已创建。" + make_fill("u", 200)),
    ]

    cm1, _, triggered1 = build_and_compact(4, rounds1, "Q4编程", model_id)
    compression_triggered += 1 if triggered1 else 0

    queries1 = [
        {"query": "用户最初要求爬虫支持哪两个核心功能？", "expected_keywords": ["并发", "JSON"]},
        {"query": "crawler.py 的默认并发数是多少？超时多少秒？", "expected_keywords": ["5", "30"]},
        {"query": "storage.py 的去重策略基于什么？输出文件名是什么？", "expected_keywords": ["URL", "results.json"]},
        {"query": "config.yaml 中 max_retries 和 retry_delay 的值？", "expected_keywords": ["3", "2"]},
        {"query": "性能瓶颈中占比最大的是什么？", "expected_keywords": ["网络", "I/O"]},
    ]
    print("\n  验证压缩后信息有效性:")
    all_results.extend(verify(cm1, queries1, model_id))

    # ── 场景 2: Q1 简单任务 → 3 段简化摘要 ──
    print("\n" + "─" * 40)
    print("场景 2: Q1 琐碎任务 → 3 段简化摘要路径")
    print("─" * 40)

    rounds2 = [
        ("你好", "你好！有什么可以帮助你的？"),
        ("今天星期几", "今天是星期三。"),
        ("帮我翻译 hello world 到中文", "你好世界。" + make_fill("t", 300)),
        ("谢谢", "不客气！"),
        ("再见", "再见！" + make_fill("s", 200)),
        ("确认一下刚才说的", "刚才说了翻译和问候。" + make_fill("r", 150)),
    ]

    cm2, _, triggered2 = build_and_compact(1, rounds2, "Q1闲聊", model_id)
    compression_triggered += 1 if triggered2 else 0

    queries2 = [
        {"query": "用户最初说了什么？", "expected_keywords": ["你好"]},
        {"query": "用户要求翻译什么内容？翻译结果是什么？", "expected_keywords": ["hello", "你好"]},
    ]
    print("\n  验证压缩后信息有效性:")
    all_results.extend(verify(cm2, queries2, model_id))

    # ── 场景 3: Q5 架构设计 — 跨 tier 驱逐 ──
    print("\n" + "─" * 40)
    print("场景 3: Q5 混合优先级 → 跨 tier 驱逐路径")
    print("─" * 40)

    # 先有 Q1/Q3 低优先级对话，然后才是 Q5 核心任务
    rounds3 = [
        ("你好", "你好！"),
        ("今天天气如何", "抱歉无法查询天气。"),
        ("写一个斐波那契函数",
         "def fib(n): return n if n<=1 else fib(n-1)+fib(n-2)"),
        ("请设计分布式任务调度系统。要求：优先级队列、工作窃取、故障恢复、水平扩展。",
         "## 架构\n- TaskRouter 任务路由\n- PriorityQueue 五级 P0-P4\n"
         "- WorkerPool 推荐 10 节点\n- WorkStealingScheduler\n"
         "- FaultTolerance: SWIM 协议\n- 单节点 1000 tasks/s\n"
         "- 集群 10000 tasks/s\n- 消息队列: Redis Streams\n"
         "- 水平扩展: K8s HPA\n" + make_fill("架构", 500)),
        ("水平扩展的具体实现方案？",
         "K8s HPA + 自定义指标:\n- 最小 3 副本, 最大 20 副本\n"
         "- 扩容冷却 60s, 缩容冷却 300s\n"
         "- 自定义 Metrics Adapter 采集队列深度\n" + make_fill("扩展", 300)),
        ("故障恢复机制再详细说明一下",
         "SWIM 协议:\n- 心跳间隔 1s, 超时 3s\n- 怀疑机制 + 多播传播\n"
         "- 故障检测后自动重分配任务\n- 任务幂等性设计\n" + make_fill("故障", 250)),
    ]

    cm3, _, triggered3 = build_and_compact(5, rounds3, "Q5混合", model_id)
    compression_triggered += 1 if triggered3 else 0

    queries3 = [
        {"query": "分布式调度系统有哪四个核心要求？", "expected_keywords": ["优先级队列", "工作窃取", "故障恢复", "水平扩展"]},
        {"query": "WorkerPool 推荐多少节点？集群目标处理能力是多少 tasks/s？", "expected_keywords": ["10", "10000"]},
        {"query": "消息队列使用什么技术？", "expected_keywords": ["Redis"]},
        {"query": "K8s HPA 的最小和最大副本数是多少？冷却时间呢？", "expected_keywords": ["3", "20", "60", "300"]},
    ]
    print("\n  验证压缩后信息有效性:")
    all_results.extend(verify(cm3, queries3, model_id))

    # ── 汇总 ──
    print("\n" + "=" * 60)
    print("评测汇总")
    print("=" * 60)

    triggered_count = compression_triggered
    total = len(all_results)
    scores = [r["score"] for r in all_results]
    avg = sum(scores) / len(scores) if scores else 0
    high = sum(1 for s in scores if s >= 0.75)
    mid = sum(1 for s in scores if 0.5 <= s < 0.75)
    low = sum(1 for s in scores if s < 0.5)

    print(f"  压缩触发场景: {triggered_count}/3")
    print(f"  验证查询数: {total}")
    print(f"  综合得分: {avg:.1%}")
    print(f"  高召回(≥75%): {high}/{total} | 中召回: {mid}/{total} | 低召回: {low}/{total}")
    print(f"  平均延迟: {sum(r.get('latency_ms', 0) for r in all_results) / max(total, 1):.0f}ms")

    if triggered_count == 0:
        print("\n  ❌ 压缩未在任何场景触发，无法评测压缩效果。")
    elif avg >= 0.85:
        print(f"\n  ✅ 压缩有效！{triggered_count}/3 场景真实触发压缩，信息保留率 {avg:.0%}。")
    elif avg >= 0.70:
        print(f"\n  ✅ 基本可用。{triggered_count}/3 场景触发压缩，信息保留率 {avg:.0%}。")
    elif avg >= 0.50:
        print(f"\n  ⚠️ 有信息丢失。需优化策略参数。")
    else:
        print(f"\n  ❌ 严重信息丢失。策略需要重新设计。")

    return {"model": model_id, "avg_score": avg, "total": total,
            "compression_triggered": triggered_count,
            "high": high, "mid": mid, "low": low, "details": all_results}


if __name__ == "__main__":
    model = sys.argv[1] if len(sys.argv) > 1 else "deepseek/deepseek-v4-pro"
    report = main(model)
    Path("/tmp/xenon_context_eval.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str))
    print(f"\n报告: /tmp/xenon_context_eval.json")
