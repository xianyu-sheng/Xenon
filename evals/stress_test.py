"""
v0.5.0 真实场景压力测试。

模拟真实 xenon 会话：多轮工具调用、混合优先级、真实格式的工具输出。
目标：压缩后，后续任务能否正确使用早期上下文中的关键信息。
"""

import json, sys, time, re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xenon.repl.context_manager import ContextManager, ConversationTurn
from xenon.repl.context_strategies import TieredStrategySelector, SpaceBudget
from xenon.utils.llm_client import chat_completion


def realistic_read_file(path: str) -> str:
    """模拟 read_file 工具的真实输出格式。"""
    files = {
        "/app/main.py": (
            '"""Main application entry point."""\n'
            'import asyncio\n'
            'from crawler import WebCrawler\n'
            'from storage import ResultStorage\n'
            'from config import load_config\n\n'
            'async def main():\n'
            '    config = load_config("config.yaml")\n'
            '    crawler = WebCrawler(\n'
            '        concurrency=config["crawler"]["concurrency"],\n'
            '        timeout=config["crawler"]["timeout"],\n'
            '    )\n'
            '    storage = ResultStorage(config["storage"]["output_dir"])\n'
            '    urls = ["https://example.com", "https://httpbin.org"]\n'
            '    results = await crawler.crawl(urls)\n'
            '    storage.save(results)\n'
            '    print(f"Crawled {len(results)} pages")\n\n'
            'if __name__ == "__main__":\n'
            '    asyncio.run(main())\n'
        ),
        "/app/crawler.py": (
            '"""Async web crawler with concurrency control."""\n'
            'import asyncio\n'
            'import aiohttp\n'
            'from bs4 import BeautifulSoup\n\n'
            'class WebCrawler:\n'
            '    def __init__(self, concurrency=5, timeout=30):\n'
            '        self.concurrency = concurrency\n'
            '        self.timeout = timeout\n'
            '        self.session = None\n'
            '        self.semaphore = asyncio.Semaphore(concurrency)\n'
            '        self.results = []\n\n'
            '    async def fetch(self, url):\n'
            '        async with self.semaphore:\n'
            '            async with self.session.get(url, timeout=self.timeout) as resp:\n'
            '                html = await resp.text()\n'
            '                return self.parse(html, url)\n\n'
            '    def parse(self, html, url):\n'
            '        soup = BeautifulSoup(html, "html.parser")\n'
            '        return {\n'
            '            "url": url,\n'
            '            "title": soup.title.string if soup.title else "",\n'
            '            "links": [a.get("href") for a in soup.find_all("a")[:10]],\n'
            '        }\n'
        ),
        "/app/storage.py": (
            '"""JSON-based result storage with dedup."""\n'
            'import json, os\n'
            'from pathlib import Path\n\n'
            'class ResultStorage:\n'
            '    def __init__(self, output_dir="./data"):\n'
            '        self.output_dir = Path(output_dir)\n'
            '        self.output_dir.mkdir(parents=True, exist_ok=True)\n'
            '        self.output_file = self.output_dir / "results.json"\n'
            '        self.seen_urls = set()\n\n'
            '    def save(self, results):\n'
            '        existing = self._load()\n'
            '        for r in results:\n'
            '            if r["url"] not in self.seen_urls:\n'
            '                existing.append(r)\n'
            '                self.seen_urls.add(r["url"])\n'
            '        with open(self.output_file, "w") as f:\n'
            '            json.dump(existing, f, indent=2, ensure_ascii=False)\n'
        ),
    }
    content = files.get(path, f"# File: {path}\n# (empty)")
    return f"Observation: [read_file] {path}\n\n{content}"


def realistic_search_results(query: str) -> str:
    """模拟 search_files 的输出。"""
    return (
        f"Observation: [search_files] pattern={query}\n"
        f"Found 8 files:\n"
        f"  /app/main.py (32 lines)\n"
        f"  /app/crawler.py (28 lines)\n"
        f"  /app/storage.py (22 lines)\n"
        f"  /app/config.yaml (15 lines)\n"
        f"  /app/tests/test_crawler.py (45 lines)\n"
        f"  /app/tests/test_storage.py (38 lines)\n"
        f"  /app/requirements.txt (5 lines)\n"
        f"  /app/README.md (60 lines)"
    )


def realistic_command_output(cmd: str) -> str:
    """模拟 command 工具的输出。"""
    if "test" in cmd:
        return (
            f"Observation: [command] {cmd}\n"
            f"============================= test session starts ==============================\n"
            f"collected 12 items\n\n"
            f"tests/test_crawler.py::test_fetch PASSED [  8%]\n"
            f"tests/test_crawler.py::test_parse PASSED [ 16%]\n"
            f"tests/test_crawler.py::test_concurrency PASSED [ 25%]\n"
            f"tests/test_crawler.py::test_timeout FAILED [ 33%]\n"
            f"tests/test_crawler.py::test_retry PASSED [ 41%]\n"
            f"tests/test_storage.py::test_save PASSED [ 50%]\n"
            f"tests/test_storage.py::test_dedup PASSED [ 58%]\n"
            f"tests/test_storage.py::test_incremental PASSED [ 66%]\n"
            f"tests/test_storage.py::test_empty_save PASSED [ 75%]\n"
            f"tests/test_config.py::test_load PASSED [ 83%]\n"
            f"tests/test_config.py::test_defaults PASSED [ 91%]\n"
            f"tests/test_config.py::test_invalid PASSED [100%]\n\n"
            f"======================== 1 failed, 11 passed in 2.34s ========================="
        )
    return f"Observation: [command] {cmd}\nExit code: 0\n(output omitted)"


def build_realistic_session() -> ContextManager:
    """构建真实 xenon 会话。

    模式：多轮工具调用 → 分析 → 修改 → 测试，混合 Q1 闲聊。
    """
    cm = ContextManager(max_tokens=128000)

    # === Phase 1: Q4 爬虫开发 (主任务) ===
    cm.set_active_tier(4)
    cm.add_user_message(
        "我需要为一个爬虫项目添加错误处理和重试机制。"
        "请先看看项目结构，理解现有代码，然后实现改进。"
    )
    cm.add_assistant_message(
        "好的，我先了解一下项目结构。",
        model_used="deepseek/deepseek-v4-pro",
    )
    # 工具调用：搜索文件
    cm.add_user_message("[助手调用了 search_files *.py]")
    cm.add_assistant_message(
        realistic_search_results("*.py"),
        model_used="deepseek/deepseek-v4-pro",
    )

    # 读取核心文件
    cm.add_user_message("[助手调用了 read_file /app/crawler.py]")
    cm.add_assistant_message(
        realistic_read_file("/app/crawler.py"),
        model_used="deepseek/deepseek-v4-pro",
    )

    cm.add_user_message("[助手调用了 read_file /app/storage.py]")
    cm.add_assistant_message(
        realistic_read_file("/app/storage.py"),
        model_used="deepseek/deepseek-v4-pro",
    )

    # 分析后给出方案
    cm.add_user_message("[助手分析完成，给出方案]")
    cm.add_assistant_message(
        "项目分析完成。现状：\n\n"
        "**crawler.py 问题：**\n"
        "1. `fetch` 方法没有 try/except，网络异常会直接崩溃\n"
        "2. 没有重试机制，临时故障导致整个抓取失败\n"
        "3. 超时 30 秒是硬编码，无法按 URL 级别定制\n\n"
        "**storage.py 问题：**\n"
        "1. `save` 方法没有处理磁盘满或写入权限异常\n"
        "2. `seen_urls` 只在内存中，重启后去重失效\n\n"
        "**改进方案：**\n"
        "1. crawler.py 添加 `RetryHandler` 类：指数退避，max_retries=3\n"
        "2. storage.py 添加文件锁 + 异常处理\n"
        "3. 添加 `error_log.json` 记录失败 URL 及原因\n\n"
        "要我开始实现吗？",
        model_used="deepseek/deepseek-v4-pro",
    )

    # 实现改进
    cm.add_user_message("开始实现，先改 crawler.py 添加重试机制")
    cm.add_assistant_message(
        "已修改 crawler.py，新增 RetryHandler 类：\n\n"
        "```python\n"
        "class RetryHandler:\n"
        "    def __init__(self, max_retries=3, base_delay=1.0, max_delay=60.0):\n"
        "        self.max_retries = max_retries\n"
        "        self.base_delay = base_delay\n"
        "        self.max_delay = max_delay\n"
        "    \n"
        "    async def execute(self, coro, url):\n"
        "        last_error = None\n"
        "        for attempt in range(self.max_retries + 1):\n"
        "            try:\n"
        "                return await coro\n"
        "            except Exception as e:\n"
        "                last_error = e\n"
        "                if attempt < self.max_retries:\n"
        "                    delay = min(self.base_delay * (2**attempt), self.max_delay)\n"
        "                    await asyncio.sleep(delay)\n"
        "        raise last_error\n"
        "```\n\n"
        "关键设计：base_delay=1s, max_retries=3, 指数退避倍率 2x, max_delay=60s。",
        model_used="deepseek/deepseek-v4-pro",
    )

    # 运行测试
    cm.add_user_message("运行测试看看有没有问题")
    cm.add_assistant_message(
        realistic_command_output("pytest tests/ -v"),
        model_used="deepseek/deepseek-v4-pro",
    )

    # Q1 闲聊打断
    cm.set_active_tier(1)
    cm.add_user_message("好的谢谢")
    cm.add_assistant_message("不客气！还有其他需要修改的吗？", model_used="deepseek/deepseek-v4-flash")

    # === Phase 2: Q5 架构重构 (升级) ===
    cm.set_active_tier(5)
    cm.add_user_message(
        "现在考虑更大的架构问题：我想把这个单机爬虫升级为分布式爬虫。"
        "需要支持：多节点调度、URL 去重共享、故障转移。"
        "请给我一个完整的架构方案，基于当前的代码结构。"
    )
    cm.add_assistant_message(
        "## 分布式爬虫架构方案\n\n"
        "### 1. 整体架构\n"
        "```\n"
        "                 ┌─────────────┐\n"
        "                 │  API Gateway │\n"
        "                 └──────┬──────┘\n"
        "                        │\n"
        "           ┌────────────┼────────────┐\n"
        "           ▼            ▼            ▼\n"
        "    ┌──────────┐ ┌──────────┐ ┌──────────┐\n"
        "    │ Scheduler │ │  Worker 1│ │  Worker N│\n"
        "    │  (Master) │ │  (Node)  │ │  (Node)  │\n"
        "    └─────┬─────┘ └────┬─────┘ └────┬─────┘\n"
        "          │            │            │\n"
        "          └────────────┼────────────┘\n"
        "                       │\n"
        "              ┌────────┴────────┐\n"
        "              │   Redis Cluster │\n"
        "              │  (Queue + Dedup)│\n"
        "              └─────────────────┘\n"
        "```\n\n"
        "### 2. 核心组件\n"
        "**Scheduler (Master):**\n"
        "- URL 种子管理 + 调度策略（BFS/DFS/优先级）\n"
        "- 健康检查：心跳间隔 5s，超时 15s 标记节点失活\n"
        "- 任务重分配：失活节点的待处理 URL 重新入队\n\n"
        "**Worker (Node):**\n"
        "- 复用现有 WebCrawler + RetryHandler\n"
        "- 从 Redis 队列拉取 URL（BLPOP 阻塞读取）\n"
        "- 结果写回 Redis + 定期刷盘到 JSON\n\n"
        "**Redis Cluster:**\n"
        "- URL 队列：Redis List（FIFO）\n"
        "- 去重集合：Redis Set（跨节点共享 seen_urls）\n"
        "- 结果缓存：Redis Hash（临时存储，批量刷盘）\n\n"
        "### 3. 关键参数\n"
        "- Worker 节点数: 推荐 5-10 节点\n"
        "- 心跳间隔: 5s, 超时: 15s\n"
        "- Redis 连接池: 每 Worker 20 连接\n"
        "- 任务批量大小: 10 URLs/batch\n"
        "- 故障转移窗口: 30s\n\n"
        "### 4. 容错设计\n"
        "- Worker 崩溃 → Master 检测心跳超时 → 重分配 URL → 新 Worker 接管\n"
        "- Master 崩溃 → Redis Sentinel 自动故障转移 → 新 Master 从 Redis 恢复状态\n"
        "- Redis 崩溃 → 本地 SQLite 兜底队列 + 定期同步\n\n"
        "要我开始实现调度器原型吗？",
        model_used="deepseek/deepseek-v4-pro",
    )

    # 读取现有文件验证兼容性
    cm.add_user_message("先看一下 storage.py 是否需要大改才能支持分布式")
    cm.add_assistant_message(
        realistic_read_file("/app/storage.py"),
        model_used="deepseek/deepseek-v4-pro",
    )

    cm.add_assistant_message(
        "分析：storage.py 需要以下改动以支持分布式：\n\n"
        "1. `seen_urls` 从内存 set → Redis Set（跨节点共享去重）\n"
        "2. `save` 方法加分布式锁（Redis SETNX），防止多节点同时写文件\n"
        "3. 新增 `flush_from_redis` 方法：从 Redis 批量读取结果并写入 JSON\n"
        "4. 保持本地 JSON 文件作为最终持久化（Redis 只是中间层）\n\n"
        "改动量约 40 行，不影响现有 API 兼容性。",
        model_used="deepseek/deepseek-v4-pro",
    )

    return cm


def run_stress_test(model_id="deepseek/deepseek-v4-pro"):
    """运行真实场景压力测试。"""
    print("=" * 60)
    print("Xenon v0.5.0 真实场景压力测试")
    print("=" * 60)

    cm = build_realistic_session()
    tok_before = cm.current_token_usage()
    turns_before = len(cm.history)
    print(f"会话规模: {tok_before} tokens, {turns_before} 轮")
    print(f"Q1 轮: {sum(1 for t in cm.history if t.task_tier == 1)}")
    print(f"Q4 轮: {sum(1 for t in cm.history if t.task_tier == 4)}")
    print(f"Q5 轮: {sum(1 for t in cm.history if t.task_tier == 5)}")

    # 自适应 max_tokens 触发压缩
    strategy4 = TieredStrategySelector().get_preset(4)
    target_max = int(tok_before / (strategy4.trigger_threshold + 0.05))

    cm2 = ContextManager(max_tokens=target_max)
    cm2.set_active_tier(4)  # 用 Q4 — 爬虫开发是主要场景
    for t in cm.history:
        cm2.add_message(t.role, t.content, task_tier=t.task_tier)

    ratio_before = cm2.usage_ratio()
    space = SpaceBudget.evaluate(ratio_before)
    print(f"\n压缩前: ratio={ratio_before:.0%}, space={space}")
    print(f"策略: Q4 trigger={strategy4.trigger_threshold}, keep_recent={strategy4.keep_recent_rounds}")

    # 压缩
    print("\n触发压缩...")
    t0 = time.time()
    result = cm2.compact(model_priority=[model_id, "deepseek/deepseek-v4-flash"])
    elapsed = (time.time() - t0) * 1000

    turns_after = len(cm2.history)
    tok_after = cm2.current_token_usage()
    print(f"压缩完成: {elapsed:.0f}ms")
    print(f"轮数: {turns_before} → {turns_after} (-{turns_before - turns_after})")
    print(f"Token: {tok_before} → {tok_after}")
    print(f"\n摘要 ({len(result)} chars):")
    print(result[:600])
    if len(result) > 600:
        print(f"... (truncated, total {len(result)} chars)")

    # ── 验证阶段 ──
    print("\n" + "=" * 60)
    print("验证：压缩后能否正确回答需要早期上下文的问题")
    print("=" * 60)

    queries = [
        # Q1: 早期技术细节
        {
            "query": "RetryHandler 的 max_retries 参数默认值是多少？base_delay 呢？退避倍率是多少？",
            "expected": ["3", "1.0", "1", "2"],
            "context": "早期的 crawler.py 修改",
        },
        {
            "query": "最初分析 crawler.py 时发现了哪三个问题？",
            "expected": ["try", "except", "重试", "超时", "硬编码"],
            "context": "项目分析阶段",
        },
        {
            "query": "storage.py 的 seen_urls 去重有什么重启后的问题？",
            "expected": ["内存", "重启", "失效", "丢失"],
            "context": "storage.py 分析",
        },
        # Q2: 后续架构决策
        {
            "query": "分布式架构方案中，Scheduler 的心跳间隔和超时分别是多少？",
            "expected": ["5", "15"],
            "context": "Q5 架构设计",
        },
        {
            "query": "Redis Cluster 的三个用途分别是什么？",
            "expected": ["队列", "去重", "缓存", "List", "Set", "Hash"],
            "context": "Q5 架构设计",
        },
        # Q3: 跨阶段综合
        {
            "query": "storage.py 需要做哪四个改动来支持分布式？改动量大约多少行？",
            "expected": ["Redis", "锁", "flush", "40", "Set"],
            "context": "storage.py 分布式改动",
        },
        # Q4: 测试结果
        {
            "query": "测试运行结果如何？通过/失败多少？哪个测试失败了？",
            "expected": ["11", "1", "test_timeout"],
            "context": "pytest 运行结果",
        },
    ]

    all_scores = []
    for i, q in enumerate(queries):
        ctx = cm2.get_messages()
        prompt = (
            "你是上下文记忆验证助手。以下是对话压缩后的上下文。"
            "只根据以下信息回答问题。不确定则说'无法确定'。\n\n"
            "=== 上下文 ===\n"
        )
        for turn in ctx:
            role_label = {"user": "用户", "assistant": "助手", "system": "系统"}.get(turn["role"], turn["role"])
            prompt += f"[{role_label}] {turn['content'][:800]}\n\n"  # 800 字符（原 400）防止截断丢失细节
        prompt += f"=== 问题 ===\n{q['query']}\n\n简洁回答。"

        try:
            t0 = time.time()
            resp = chat_completion(model_id, [{"role": "user", "content": prompt}],
                                   temperature=0.0, max_tokens=200)
            lat = (time.time() - t0) * 1000
            answer = resp.strip()[:300]

            matched = [kw for kw in q["expected"] if kw.lower() in answer.lower()]
            score = len(matched) / len(q["expected"]) if q["expected"] else 1.0
            all_scores.append(score)

            status = "✅" if score >= 0.75 else ("⚠️" if score >= 0.5 else "❌")
            print(f"\n[{i+1}/{len(queries)}] {status} {q['context']}")
            print(f"  Q: {q['query'][:80]}...")
            print(f"  A: {answer[:150]}")
            print(f"  得分: {score:.0%} ({len(matched)}/{len(q['expected'])}) [{lat:.0f}ms]")
        except Exception as e:
            print(f"\n[{i+1}/{len(queries)}] ❌ API 失败: {e}")
            all_scores.append(0.0)

    # ── 汇总 ──
    avg = sum(all_scores) / len(all_scores)
    high = sum(1 for s in all_scores if s >= 0.75)
    mid = sum(1 for s in all_scores if 0.5 <= s < 0.75)
    low = sum(1 for s in all_scores if s < 0.5)

    print(f"\n{'='*60}")
    print(f"压力测试结果")
    print(f"{'='*60}")
    print(f"  压缩: {turns_before}→{turns_after} 轮, 耗时 {elapsed:.0f}ms")
    print(f"  查询: {len(queries)}, 综合得分: {avg:.0%}")
    print(f"  高召回: {high}/{len(queries)} | 中: {mid}/{len(queries)} | 低: {low}/{len(queries)}")

    if avg >= 0.85:
        print(f"\n  ✅ 真实压力测试通过 — 压缩后上下文有效保留了关键信息。")
    elif avg >= 0.70:
        print(f"\n  ⚠️ 基本可用，部分信息有损。")
    else:
        print(f"\n  ❌ 压缩导致严重信息丢失。")

    return {"avg_score": avg, "latency_ms": elapsed, "details": all_scores}


if __name__ == "__main__":
    model = sys.argv[1] if len(sys.argv) > 1 else "deepseek/deepseek-v4-pro"
    report = run_stress_test(model)
    Path("/tmp/xenon_stress_test.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str))
