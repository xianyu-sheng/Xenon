export const meta = {
  name: 'optimize-startup-performance',
  description: '优化 Xenon 启动性能：缓存模型列表 + 并行请求',
  phases: [
    { title: '分析', detail: '定位启动慢的根本原因，分析现有代码结构' },
    { title: '实现', detail: '添加缓存机制 + 并行请求优化' },
    { title: '审查', detail: '代码审查，确保优化正确且无副作用' },
    { title: '测试', detail: '运行测试，验证优化效果' },
    { title: '提交', detail: '创建 PR 并推送到远程' },
    { title: '监控', detail: '监控 CI 状态，如有失败则回滚' },
  ],
}

// ── 结构化输出 schema ──────────────────────────────────────

const ANALYSIS_SCHEMA = {
  type: 'object',
  properties: {
    root_cause: { type: 'string', description: '启动慢的根本原因' },
    affected_files: {
      type: 'array',
      items: { type: 'string' },
      description: '需要修改的文件列表'
    },
    optimization_plan: {
      type: 'object',
      properties: {
        cache_strategy: { type: 'string', description: '缓存策略描述' },
        parallel_strategy: { type: 'string', description: '并行请求策略描述' },
        cache_location: { type: 'string', description: '缓存文件位置' },
        ttl_seconds: { type: 'number', description: '缓存有效期（秒）' },
      },
      required: ['cache_strategy', 'parallel_strategy', 'cache_location', 'ttl_seconds']
    },
  },
  required: ['root_cause', 'affected_files', 'optimization_plan']
}

const IMPLEMENTATION_SCHEMA = {
  type: 'object',
  properties: {
    modified_files: {
      type: 'array',
      items: { type: 'string' },
      description: '已修改的文件列表'
    },
    key_changes: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          file: { type: 'string' },
          change: { type: 'string' },
          line_range: { type: 'string' },
        }
      },
      description: '关键修改点'
    },
    estimated_speedup: { type: 'string', description: '预期提速效果（如 "从8秒降至1秒"）' },
  },
  required: ['modified_files', 'key_changes', 'estimated_speedup']
}

const REVIEW_SCHEMA = {
  type: 'object',
  properties: {
    approved: { type: 'boolean', description: '是否通过审查' },
    issues: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          severity: { type: 'string', enum: ['critical', 'major', 'minor'] },
          file: { type: 'string' },
          description: { type: 'string' },
        }
      },
      description: '发现的问题列表'
    },
    suggestions: {
      type: 'array',
      items: { type: 'string' },
      description: '改进建议'
    },
  },
  required: ['approved', 'issues', 'suggestions']
}

const TEST_SCHEMA = {
  type: 'object',
  properties: {
    all_passed: { type: 'boolean', description: '所有测试是否通过' },
    startup_time_before: { type: 'string', description: '优化前启动时间' },
    startup_time_after: { type: 'string', description: '优化后启动时间' },
    test_results: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          test_name: { type: 'string' },
          passed: { type: 'boolean' },
          error: { type: 'string' },
        }
      }
    },
  },
  required: ['all_passed', 'test_results']
}

const COMMIT_SCHEMA = {
  type: 'object',
  properties: {
    branch_name: { type: 'string', description: '创建的分支名' },
    commit_sha: { type: 'string', description: '提交的 SHA' },
    pr_number: { type: 'number', description: 'PR 编号' },
    pr_url: { type: 'string', description: 'PR 链接' },
  },
  required: ['branch_name', 'commit_sha', 'pr_url']
}

const MONITOR_SCHEMA = {
  type: 'object',
  properties: {
    ci_status: { type: 'string', enum: ['pending', 'success', 'failure'] },
    checks: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          name: { type: 'string' },
          status: { type: 'string' },
          conclusion: { type: 'string' },
        }
      }
    },
    action_taken: { type: 'string', description: '采取的行动（如"继续"、"回滚"）' },
    rollback_details: { type: 'string', description: '如果回滚，说明回滚详情' },
  },
  required: ['ci_status', 'action_taken']
}

// ── 工作流执行 ────────────────────────────────────────────

phase('分析')
log('🔍 分析启动性能瓶颈...')

const analysis = await agent(
  `分析 Xenon 启动慢的问题：

已知信息：
1. 启动时会调用 get_configured_providers(refresh_models=True)
2. 该函数会对每个配置的 provider 串行发送网络请求获取模型列表
3. 用户配置了 8 个 provider（deepseek, openai, heyroute 等）
4. 每个请求约 500ms-1s，总计 4-8 秒

任务：
1. 阅读 xenon/repl/provider_registry.py 中的 get_configured_providers 和 fetch_provider_models 函数
2. 阅读 xenon/repl/repl.py 中的 _check_first_run 函数
3. 设计缓存机制：
   - 缓存位置：~/.xenon/cache/provider_models.json
   - TTL: 1小时（3600秒）
   - 缓存内容：provider_key -> {models: [...], fetched_at: timestamp}
4. 设计并行请求机制：使用 ThreadPoolExecutor 或 asyncio 并行获取
5. 提供详细的实现计划

返回结构化分析结果。`,
  {
    schema: ANALYSIS_SCHEMA,
    label: '分析性能瓶颈',
    phase: '分析',
    model: 'claude-opus-5',
    effort: 'high'
  }
)

if (!analysis) {
  throw new Error('分析阶段失败')
}

log(`✓ 根本原因: ${analysis.root_cause}`)
log(`✓ 需修改文件: ${analysis.affected_files.join(', ')}`)
log(`✓ 缓存策略: ${analysis.optimization_plan.cache_strategy}`)

// ── 实现阶段 ──────────────────────────────────────────────

phase('实现')
log('⚙️  实现优化方案...')

const implementation = await agent(
  `实现启动性能优化：

分析结果：
${JSON.stringify(analysis, null, 2)}

具体要求：
1. 在 xenon/repl/provider_registry.py 中：
   - 添加缓存读写函数（使用 ~/.xenon/cache/provider_models.json）
   - 修改 fetch_provider_models 支持缓存
   - 修改 get_configured_providers 支持并行请求
   - 使用 concurrent.futures.ThreadPoolExecutor 实现并行

2. 缓存格式：
   {
     "provider_key": {
       "models": ["model1", "model2"],
       "fetched_at": 1234567890,
       "api_key_hash": "sha256_hash",
       "base_url_hash": "sha256_hash"  // 【必须】检测 base_url 变化
     }
   }

3. 缓存逻辑：
   - 优先读取缓存，如果缓存有效（未过期且 API key/base_url 未变）则使用
   - 在后台线程异步刷新过期的缓存
   - 首次启动或缓存无效时，并行请求所有 provider

4. 【关键】线程安全：
   - 使用 threading.Lock 保护缓存的读-改-写操作
   - _save_model_cache 必须先加锁，再读取完整缓存，更新单个条目，最后写回
   - 示例代码：
     \`\`\`python
     _cache_lock = threading.Lock()

     def _save_model_cache(provider_key, data):
         with _cache_lock:
             cache = _load_model_cache()
             cache[provider_key] = data
             atomic_write_text(cache_path, json.dumps(cache))
     \`\`\`

5. 并发度优化：
   - 使用 min(len(providers), (os.cpu_count() or 4) * 2) 而非硬编码 8
   - I/O 密集型任务用 2x CPU 核心数

6. 缓存清理：
   - _load_model_cache 时过滤掉过期 2 倍 TTL 的条目
   - 防止缓存文件无限膨胀

7. 确保向后兼容，不影响现有功能

实现完成后返回修改摘要。`,
  {
    schema: IMPLEMENTATION_SCHEMA,
    label: '实现优化代码',
    phase: '实现',
    model: 'claude-opus-5',
    effort: 'high'
  }
)

if (!implementation) {
  throw new Error('实现阶段失败')
}

log(`✓ 已修改文件: ${implementation.modified_files.join(', ')}`)
log(`✓ 预期提速: ${implementation.estimated_speedup}`)

// ── 审查阶段 ──────────────────────────────────────────────

phase('审查')
log('👀 代码审查...')

const review = await agent(
  `审查启动性能优化代码：

修改摘要：
${JSON.stringify(implementation, null, 2)}

审查要点：
1. 线程安全：并行请求是否会导致竞态条件？
2. 错误处理：网络失败、缓存损坏等异常是否正确处理？
3. 缓存失效：API key 变化时是否正确清除缓存？
4. 向后兼容：是否影响现有用户的配置？
5. 性能提升：是否真正解决了串行请求的问题？
6. 代码质量：是否符合项目规范？

重点检查：
- 缓存目录创建（mkdir -p）
- JSON 序列化/反序列化的异常处理
- ThreadPoolExecutor 的资源清理
- 空 provider 列表的边界情况
- 缓存过期判断的时区问题

返回审查结果（包括是否批准、问题列表、改进建议）。`,
  {
    schema: REVIEW_SCHEMA,
    label: '代码审查',
    phase: '审查',
    model: 'claude-opus-5',
    effort: 'high'
  }
)

if (!review) {
  throw new Error('审查阶段失败')
}

if (!review.approved) {
  const critical = review.issues.filter(i => i.severity === 'critical')
  if (critical.length > 0) {
    log('❌ 发现严重问题，工作流终止')
    critical.forEach(issue => {
      log(`  - [CRITICAL] ${issue.file}: ${issue.description}`)
    })
    throw new Error('代码审查未通过：存在严重问题')
  }
}

log(`✓ 审查${review.approved ? '通过' : '有建议'}`)
if (review.issues.length > 0) {
  log(`  发现 ${review.issues.length} 个问题`)
}

// ── 测试阶段 ──────────────────────────────────────────────

phase('测试')
log('🧪 运行测试...')

const testResult = await agent(
  `测试启动性能优化：

1. 运行现有测试套件：
   cd /home/xianyu-sheng/Xenon
   /usr/bin/python3 -m pytest tests/ -v -k "provider" --tb=short

2. 创建性能测试：
   - 测试缓存读写正确性
   - 测试并行请求正确性
   - 测试缓存过期逻辑
   - 测试 API key 变化时的缓存失效

3. 测量启动时间：
   - 优化前：time xenon --version（冷启动，删除缓存）
   - 优化后：time xenon --version（冷启动）
   - 优化后：time xenon --version（热启动，有缓存）

4. 如果测试失败，提供详细错误信息

返回测试结果。`,
  {
    schema: TEST_SCHEMA,
    label: '运行测试',
    phase: '测试',
    model: 'claude-opus-5',
    effort: 'medium'
  }
)

if (!testResult) {
  throw new Error('测试阶段失败')
}

if (!testResult.all_passed) {
  log('❌ 测试失败，终止工作流')
  testResult.test_results.filter(t => !t.passed).forEach(t => {
    log(`  - ${t.test_name}: ${t.error}`)
  })
  throw new Error('测试未通过')
}

log(`✓ 所有测试通过`)
if (testResult.startup_time_before && testResult.startup_time_after) {
  log(`  优化前: ${testResult.startup_time_before}`)
  log(`  优化后: ${testResult.startup_time_after}`)
}

// ── 提交阶段 ──────────────────────────────────────────────

phase('提交')
log('📦 提交代码...')

const commit = await agent(
  `提交启动性能优化：

修改文件：
${implementation.modified_files.join('\n')}

提交流程：
1. 确保在 main 分支
2. 创建新分支：perf/startup-optimization
3. 暂存所有修改：git add <modified_files>
4. 提交：git commit -m "perf: 优化启动性能 - 缓存模型列表 + 并行请求

- 添加 provider 模型列表缓存（TTL 1小时）
- 使用 ThreadPoolExecutor 并行请求多个 provider
- 预期提速：${implementation.estimated_speedup}
- 缓存位置：~/.xenon/cache/provider_models.json

Co-Authored-By: Claude <noreply@anthropic.com>"

5. 推送分支（禁用沙箱）：git push -u origin perf/startup-optimization
6. 创建 PR：gh pr create --title "perf: 优化启动性能 - 缓存模型列表 + 并行请求" --body "## 问题

启动时对每个 provider 串行发送网络请求获取模型列表，导致启动慢（8个 provider × 500ms = 4秒）。

## 解决方案

1. **缓存机制**：将模型列表缓存到 ~/.xenon/cache/provider_models.json，TTL 1小时
2. **并行请求**：使用 ThreadPoolExecutor 并行获取多个 provider 的模型列表
3. **智能失效**：API key 变化时自动清除缓存

## 效果

${implementation.estimated_speedup}

## 测试

- [ ] 现有测试全部通过
- [ ] 缓存读写正确
- [ ] 并行请求正确
- [ ] 启动时间显著降低

Co-Authored-By: Claude <noreply@anthropic.com>
🤖 Generated with [Claude Code](https://claude.com/claude-code)"

返回提交信息（分支名、commit SHA、PR 链接）。`,
  {
    schema: COMMIT_SCHEMA,
    label: '提交代码',
    phase: '提交',
    model: 'claude-opus-5',
    effort: 'low'
  }
)

if (!commit) {
  throw new Error('提交阶段失败')
}

log(`✓ 已创建分支: ${commit.branch_name}`)
log(`✓ 提交 SHA: ${commit.commit_sha}`)
log(`✓ PR: ${commit.pr_url}`)

// ── 监控阶段 ──────────────────────────────────────────────

phase('监控')
log('📊 监控 CI 状态...')

const monitor = await agent(
  `监控 CI 状态并处理失败：

PR 信息：
- 分支: ${commit.branch_name}
- PR: ${commit.pr_url}

监控任务：
1. 等待 CI 开始（最多等待 1 分钟）
2. 持续检查 CI 状态（每 30 秒检查一次）：
   gh pr checks ${commit.pr_number}

3. 如果 CI 失败：
   a. 获取失败日志：gh run view <run-id> --log-failed
   b. 分析失败原因
   c. 决定是否回滚：
      - 如果是严重错误（破坏现有功能），立即回滚
      - 如果是测试环境问题，标注 PR 并通知
   d. 回滚步骤：
      git checkout main
      git branch -D ${commit.branch_name}
      git push origin --delete ${commit.branch_name}
      gh pr close ${commit.pr_number} --comment "CI 失败，已回滚。失败原因：<原因>"

4. 如果 CI 成功：
   - 标注 PR 为 "ready for review"
   - 总结优化效果

返回监控结果。`,
  {
    schema: MONITOR_SCHEMA,
    label: '监控 CI',
    phase: '监控',
    model: 'claude-opus-5',
    effort: 'medium'
  }
)

if (!monitor) {
  throw new Error('监控阶段失败')
}

log(`✓ CI 状态: ${monitor.ci_status}`)
log(`✓ 行动: ${monitor.action_taken}`)

if (monitor.ci_status === 'failure' && monitor.rollback_details) {
  log(`⚠️  已回滚: ${monitor.rollback_details}`)
}

// ── 工作流总结 ────────────────────────────────────────────

return {
  success: monitor.ci_status === 'success',
  analysis,
  implementation,
  review,
  test: testResult,
  commit,
  monitor,
  summary: `启动性能优化${monitor.ci_status === 'success' ? '成功' : '失败'}。${implementation.estimated_speedup}`
}
