export const meta = {
  name: 'fix-reasoning-token-limit',
  description: '修复推理阶段 max_tokens 限制过严的问题',
  phases: [
    { title: '分析', detail: '分析当前问题和日志' },
    { title: '实现', detail: '修改 token 限制策略' },
    { title: '审查', detail: '代码审查和质量检查' },
    { title: '测试', detail: '验证修复效果' },
    { title: '提交', detail: '提交代码并创建PR' },
    { title: '监控', detail: '监控CI状态并处理失败' },
  ],
}

// ═══════════════════════════════════════════════════════════════════════════
// Phase 1: 分析问题
// ═══════════════════════════════════════════════════════════════════════════

phase('分析')
log('🔍 Phase 1: 分析推理阶段 token 限制问题...')

const ANALYSIS_SCHEMA = {
  type: 'object',
  required: ['issue_description', 'current_behavior', 'root_cause', 'files_to_modify', 'proposed_solution'],
  properties: {
    issue_description: { type: 'string' },
    current_behavior: {
      type: 'object',
      properties: {
        initial_max_tokens: { type: 'number' },
        retry_multiplier: { type: 'number' },
        max_retries: { type: 'number' },
      },
    },
    root_cause: { type: 'string' },
    files_to_modify: { type: 'array', items: { type: 'string' } },
    proposed_solution: {
      type: 'object',
      properties: {
        new_initial_max_tokens: { type: 'number' },
        rationale: { type: 'string' },
      },
    },
  },
}

const analysis = await agent(
  `分析推理阶段 max_tokens 限制过严的问题。

观察到的日志：
\`\`\`
19:58:35 [xenon.utils.llm_client] INFO: API 推理阶段在可见输出前被截断，扩大 max_tokens: 200 → 456 (1/3)
19:58:42 [xenon.utils.llm_client] INFO: API 推理阶段在可见输出前被截断，扩大 max_tokens: 456 → 912 (2/3)
\`\`\`

问题：
1. 初始 max_tokens 只有 200，对于推理阶段太小
2. 需要多次重试才能完成，影响用户体验
3. 每次重试都增加延迟和成本

任务：
1. 找到设置 max_tokens 的代码位置（应该在 xenon/utils/llm_client.py）
2. 分析当前的限制策略
3. 确定根本原因
4. 提出合理的解决方案（建议初始值 800-1000，避免频繁重试）

返回结构化的分析结果。`,
  { schema: ANALYSIS_SCHEMA, label: '分析器', model: 'claude-sonnet-4-20250514' }
)

if (!analysis) {
  throw new Error('分析阶段失败：无法解析问题')
}

log(`📋 问题: ${analysis.issue_description}`)
log(`📊 当前初始 max_tokens: ${analysis.current_behavior.initial_max_tokens}`)
log(`💡 建议新值: ${analysis.proposed_solution.new_initial_max_tokens}`)
log(`📁 需要修改: ${analysis.files_to_modify.join(', ')}`)

// ═══════════════════════════════════════════════════════════════════════════
// Phase 2: 实现修复
// ═══════════════════════════════════════════════════════════════════════════

phase('实现')
log('💻 Phase 2: 实现 token 限制修复...')

const IMPLEMENTATION_SCHEMA = {
  type: 'object',
  required: ['success', 'changes', 'summary'],
  properties: {
    success: { type: 'boolean' },
    changes: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          file: { type: 'string' },
          description: { type: 'string' },
          old_value: { type: 'string' },
          new_value: { type: 'string' },
        },
      },
    },
    summary: { type: 'string' },
    issues_encountered: { type: 'array', items: { type: 'string' } },
  },
}

const implementation = await agent(
  `根据分析结果实现 token 限制修复。

分析结果：
${JSON.stringify(analysis, null, 2)}

任务：
1. 找到并阅读 xenon/utils/llm_client.py 中设置 max_tokens 的代码
2. 修改初始 max_tokens 的值（建议从 200 改为 ${analysis.proposed_solution.new_initial_max_tokens}）
3. 如果有推理模式的特殊处理，也需要调整
4. 添加注释说明修改原因

要求：
- 保持代码风格一致
- 不破坏现有逻辑
- 添加清晰的注释

完成后返回修改摘要。`,
  { schema: IMPLEMENTATION_SCHEMA, label: '实现者', model: 'claude-sonnet-4-20250514' }
)

if (!implementation || !implementation.success) {
  throw new Error(`实现阶段失败: ${implementation?.issues_encountered?.join(', ') || '未知错误'}`)
}

log(`✅ 完成 ${implementation.changes.length} 处修改`)
for (const change of implementation.changes) {
  log(`  📝 ${change.file}: ${change.old_value} → ${change.new_value}`)
}

// ═══════════════════════════════════════════════════════════════════════════
// Phase 3: 代码审查
// ═══════════════════════════════════════════════════════════════════════════

phase('审查')
log('🔎 Phase 3: 代码审查...')

const REVIEW_SCHEMA = {
  type: 'object',
  required: ['approved', 'issues', 'recommendations'],
  properties: {
    approved: { type: 'boolean' },
    issues: {
      type: 'array',
      items: {
        type: 'object',
        required: ['severity', 'description'],
        properties: {
          severity: { type: 'string', enum: ['critical', 'major', 'minor'] },
          description: { type: 'string' },
          suggestion: { type: 'string' },
        },
      },
    },
    recommendations: { type: 'array', items: { type: 'string' } },
    review_summary: { type: 'string' },
  },
}

const review = await agent(
  `审查 token 限制修复的代码。

已修改的文件：
${implementation.changes.map(c => `- ${c.file}: ${c.description}`).join('\n')}

审查要点：
1. **值的合理性**: 新的 max_tokens 值是否合理？
   - 太小：仍会频繁截断
   - 太大：浪费资源，可能超出模型限制
   - 建议范围：800-1200

2. **向后兼容**: 是否影响现有功能？

3. **边界情况**: 是否处理了所有场景？
   - 推理模式
   - 非推理模式
   - 重试逻辑

4. **代码质量**:
   - 注释是否清晰
   - 逻辑是否正确
   - 是否有硬编码的魔数

如果发现 critical 或 major 问题，设置 approved=false。`,
  { schema: REVIEW_SCHEMA, label: '审查者', model: 'claude-sonnet-4-20250514' }
)

if (!review) {
  throw new Error('审查阶段失败：无法获取审查结果')
}

if (review.issues.length > 0) {
  log(`⚠️  发现 ${review.issues.length} 个问题：`)
  for (const issue of review.issues) {
    log(`  [${issue.severity}] ${issue.description}`)
    if (issue.suggestion) {
      log(`    💡 建议: ${issue.suggestion}`)
    }
  }
}

if (!review.approved) {
  throw new Error(`代码审查未通过:\n${review.review_summary}`)
}

log(`✅ 代码审查通过: ${review.review_summary}`)

// ═══════════════════════════════════════════════════════════════════════════
// Phase 4: 测试验证
// ═══════════════════════════════════════════════════════════════════════════

phase('测试')
log('🧪 Phase 4: 测试验证...')

const TEST_SCHEMA = {
  type: 'object',
  required: ['all_passed', 'unit_tests', 'integration_test'],
  properties: {
    all_passed: { type: 'boolean' },
    unit_tests: {
      type: 'object',
      required: ['passed', 'failed', 'total'],
      properties: {
        passed: { type: 'number' },
        failed: { type: 'number' },
        total: { type: 'number' },
        failures: { type: 'array', items: { type: 'string' } },
      },
    },
    integration_test: {
      type: 'object',
      required: ['tested', 'result'],
      properties: {
        tested: { type: 'boolean' },
        result: { type: 'string' },
        initial_tokens_used: { type: 'number' },
        retry_count: { type: 'number' },
      },
    },
    error_logs: { type: 'array', items: { type: 'string' } },
  },
}

const testResults = await agent(
  `测试 token 限制修复。

步骤：
1. 运行相关的单元测试（如果存在）
   pytest tests/ -k "llm_client or token" -v

2. 进行集成测试：
   - 创建一个简单的测试脚本，模拟推理阶段的调用
   - 验证初始 max_tokens 是否已更改
   - 检查是否减少了重试次数

3. 检查日志输出，确认：
   - 初始 max_tokens 使用了新值
   - 重试次数减少（理想情况是0次重试）

返回测试结果。`,
  { schema: TEST_SCHEMA, label: '测试者', model: 'claude-sonnet-4-20250514' }
)

if (!testResults) {
  throw new Error('测试阶段失败：无法运行测试')
}

log(`📊 单元测试: ${testResults.unit_tests.passed}/${testResults.unit_tests.total} 通过`)

if (testResults.integration_test.tested) {
  log(`📊 集成测试: ${testResults.integration_test.result}`)
  log(`  初始 tokens: ${testResults.integration_test.initial_tokens_used}`)
  log(`  重试次数: ${testResults.integration_test.retry_count}`)
}

if (!testResults.all_passed) {
  log('❌ 测试失败，记录失败信息...')
  const failureInfo = {
    unit_failures: testResults.unit_tests.failures,
    error_logs: testResults.error_logs,
  }
  throw new Error(`测试失败:\n${JSON.stringify(failureInfo, null, 2)}`)
}

log('✅ 所有测试通过')

// ═══════════════════════════════════════════════════════════════════════════
// Phase 5: 提交代码
// ═══════════════════════════════════════════════════════════════════════════

phase('提交')
log('📦 Phase 5: 提交代码并创建PR...')

const COMMIT_SCHEMA = {
  type: 'object',
  required: ['success', 'branch_name', 'commit_hash', 'pr_number'],
  properties: {
    success: { type: 'boolean' },
    branch_name: { type: 'string' },
    commit_hash: { type: 'string' },
    pr_number: { type: 'number' },
    pr_url: { type: 'string' },
    error: { type: 'string' },
  },
}

const commit = await agent(
  `提交 token 限制修复代码。

修改的文件：
${implementation.changes.map(c => c.file).join('\n')}

步骤：
1. 创建新分支: fix/reasoning-token-limit
2. 添加修改的文件
3. 提交，提交信息格式：

\`\`\`
fix: 提高推理阶段初始 max_tokens 限制

问题：
- 初始 max_tokens 仅 200，对推理阶段太小
- 频繁触发截断重试，影响用户体验
- 每次重试增加延迟和 API 成本

修复：
- 将初始 max_tokens 从 200 提高到 ${analysis.proposed_solution.new_initial_max_tokens}
- ${analysis.proposed_solution.rationale}

效果：
- 减少重试次数（${testResults.integration_test.retry_count} 次）
- 改善响应速度
- 降低不必要的 API 调用

Co-Authored-By: Claude <noreply@anthropic.com>
\`\`\`

4. 推送: git push -u origin fix/reasoning-token-limit
5. 创建PR，包含详细的问题描述、修复说明和测试结果

返回提交结果。`,
  { schema: COMMIT_SCHEMA, label: '提交者', model: 'claude-sonnet-4-20250514' }
)

if (!commit || !commit.success) {
  throw new Error(`提交失败: ${commit?.error || '未知错误'}`)
}

log(`✅ 已创建分支: ${commit.branch_name}`)
log(`✅ 提交哈希: ${commit.commit_hash}`)
log(`✅ PR #${commit.pr_number}: ${commit.pr_url}`)

// ═══════════════════════════════════════════════════════════════════════════
// Phase 6: 监控CI状态
// ═══════════════════════════════════════════════════════════════════════════

phase('监控')
log('👀 Phase 6: 监控CI状态...')

const MONITOR_SCHEMA = {
  type: 'object',
  required: ['ci_status', 'checks'],
  properties: {
    ci_status: { type: 'string', enum: ['pending', 'success', 'failure'] },
    checks: {
      type: 'array',
      items: {
        type: 'object',
        required: ['name', 'status', 'conclusion'],
        properties: {
          name: { type: 'string' },
          status: { type: 'string' },
          conclusion: { type: 'string' },
          details_url: { type: 'string' },
          failure_logs: { type: 'array', items: { type: 'string' } },
        },
      },
    },
    needs_rollback: { type: 'boolean' },
    rollback_reason: { type: 'string' },
  },
}

const monitor = await agent(
  `监控 PR #${commit.pr_number} 的CI状态。

步骤：
1. 等待CI开始运行（最多2分钟）
   gh run list --branch fix/reasoning-token-limit --limit 1

2. 轮询CI状态，每30秒检查一次（最多10分钟）
   gh run view <run-id> --json status,conclusion

3. 如果CI失败：
   - 获取失败日志: gh run view <run-id> --log-failed
   - 分析失败原因
   - 判断是否需要回滚

4. 如果CI成功：
   - 返回成功状态

返回CI监控结果。`,
  { schema: MONITOR_SCHEMA, label: '监控者', model: 'claude-sonnet-4-20250514' }
)

if (!monitor) {
  throw new Error('监控阶段失败：无法获取CI状态')
}

log(`🚦 CI状态: ${monitor.ci_status}`)

if (monitor.ci_status === 'failure') {
  log('❌ CI失败，分析失败原因...')

  for (const check of monitor.checks) {
    if (check.conclusion === 'failure') {
      log(`  ❌ ${check.name}: ${check.conclusion}`)
      if (check.failure_logs && check.failure_logs.length > 0) {
        log(`     失败日志:`)
        for (const logLine of check.failure_logs.slice(0, 5)) {
          log(`     ${logLine}`)
        }
      }
    }
  }

  if (monitor.needs_rollback) {
    log(`🔙 需要回滚: ${monitor.rollback_reason}`)

    const rollback = await agent(
      `执行回滚操作。

失败原因：
${monitor.rollback_reason}

失败日志：
${JSON.stringify(monitor.checks.filter(c => c.conclusion === 'failure'), null, 2)}

步骤：
1. 切换回main分支: git checkout main
2. 删除本地分支: git branch -D fix/reasoning-token-limit
3. 删除远程分支: git push origin --delete fix/reasoning-token-limit
4. 关闭PR: gh pr close ${commit.pr_number} --comment "CI失败，已回滚。失败原因：${monitor.rollback_reason}"

返回回滚结果的文本说明。`,
      { label: '回滚者', model: 'claude-sonnet-4-20250514' }
    )

    throw new Error(`CI失败，已回滚。\n${rollback}`)
  }
}

if (monitor.ci_status === 'success') {
  log('✅ CI通过！')
  log('🎉 Token 限制修复成功完成！')
  log(`📋 PR #${commit.pr_number} 已准备好合并`)
}

// ═══════════════════════════════════════════════════════════════════════════
// 返回最终结果
// ═══════════════════════════════════════════════════════════════════════════

const result = {
  phase: 'completed',
  analysis,
  implementation,
  review,
  testResults,
  commit,
  monitor,
  summary: `Token 限制修复${monitor.ci_status === 'success' ? '成功' : '失败'}`,
  improvements: {
    old_initial_tokens: analysis.current_behavior.initial_max_tokens,
    new_initial_tokens: analysis.proposed_solution.new_initial_max_tokens,
    retry_reduction: `减少到 ${testResults.integration_test.retry_count} 次重试`,
  },
}

return JSON.stringify(result, null, 2)
