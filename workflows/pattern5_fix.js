export const meta = {
  name: 'pattern5-fix-workflow',
  description: 'Pattern 5 并发安全问题修复工作流 - 多Agent协作',
  phases: [
    { title: '分析', detail: '理解问题和测试要求' },
    { title: '实现', detail: '编写修复代码' },
    { title: '审查', detail: '代码审查和质量检查' },
    { title: '测试', detail: '运行测试验证修复' },
    { title: '提交', detail: '提交代码并创建PR' },
    { title: '监控', detail: '监控CI状态并处理失败' },
  ],
}

// ═══════════════════════════════════════════════════════════════════════════
// Phase 1: 分析问题
// ═══════════════════════════════════════════════════════════════════════════

phase('分析')
log('🔍 Phase 1: 分析 Pattern 5 的问题和测试要求...')

const ANALYSIS_SCHEMA = {
  type: 'object',
  required: ['problems', 'files_to_modify', 'test_file', 'estimated_complexity'],
  properties: {
    problems: {
      type: 'array',
      items: {
        type: 'object',
        required: ['id', 'title', 'current_behavior', 'expected_behavior', 'files', 'priority'],
        properties: {
          id: { type: 'number' },
          title: { type: 'string' },
          current_behavior: { type: 'string' },
          expected_behavior: { type: 'string' },
          files: { type: 'array', items: { type: 'string' } },
          priority: { type: 'string', enum: ['high', 'medium', 'low'] },
        },
      },
    },
    files_to_modify: { type: 'array', items: { type: 'string' } },
    test_file: { type: 'string' },
    estimated_complexity: { type: 'string', enum: ['simple', 'moderate', 'complex'] },
  },
}

const analysis = await agent(
  `分析 Pattern 5 的并发安全问题。

阅读测试文件 tests/test_pattern5_concurrency.py，理解：
1. 有哪些具体问题需要修复
2. 每个问题的当前行为和期望行为
3. 需要修改哪些源文件
4. 问题的复杂度评估

返回结构化的分析结果。`,
  { schema: ANALYSIS_SCHEMA, label: '分析器', model: 'claude-sonnet-4-20250514' }
)

if (!analysis) {
  throw new Error('分析阶段失败：无法解析问题')
}

log(`📋 发现 ${analysis.problems.length} 个问题需要修复`)
log(`📁 需要修改 ${analysis.files_to_modify.length} 个文件`)
log(`🎯 复杂度评估: ${analysis.estimated_complexity}`)

// ═══════════════════════════════════════════════════════════════════════════
// Phase 2: 实现修复
// ═══════════════════════════════════════════════════════════════════════════

phase('实现')
log('💻 Phase 2: 实现修复代码...')

const IMPLEMENTATION_SCHEMA = {
  type: 'object',
  required: ['success', 'modified_files', 'summary'],
  properties: {
    success: { type: 'boolean' },
    modified_files: { type: 'array', items: { type: 'string' } },
    summary: { type: 'string' },
    issues_encountered: { type: 'array', items: { type: 'string' } },
  },
}

const implementation = await agent(
  `根据分析结果实现 Pattern 5 的修复。

分析结果：
${JSON.stringify(analysis, null, 2)}

任务：
1. 阅读需要修改的源文件
2. 实现每个问题的修复
3. 确保代码风格与项目一致
4. 添加必要的注释说明修复原因

要求：
- 每个修改都要有明确的注释说明是哪个问题的修复
- 保持代码的线程安全性
- 不要破坏现有功能

完成后返回修改摘要。`,
  { schema: IMPLEMENTATION_SCHEMA, label: '实现者', model: 'claude-sonnet-4-20250514' }
)

if (!implementation || !implementation.success) {
  throw new Error(`实现阶段失败: ${implementation?.issues_encountered?.join(', ')}`)
}

log(`✅ 修改了 ${implementation.modified_files.length} 个文件`)
log(`📝 ${implementation.summary}`)

// ═══════════════════════════════════════════════════════════════════════════
// Phase 3: 代码审查
// ═══════════════════════════════════════════════════════════════════════════

phase('审查')
log('🔎 Phase 3: 代码审查和质量检查...')

const REVIEW_SCHEMA = {
  type: 'object',
  required: ['approved', 'issues', 'suggestions'],
  properties: {
    approved: { type: 'boolean' },
    issues: {
      type: 'array',
      items: {
        type: 'object',
        required: ['severity', 'file', 'description'],
        properties: {
          severity: { type: 'string', enum: ['critical', 'major', 'minor'] },
          file: { type: 'string' },
          line: { type: 'number' },
          description: { type: 'string' },
          suggestion: { type: 'string' },
        },
      },
    },
    suggestions: { type: 'array', items: { type: 'string' } },
  },
}

const review = await agent(
  `审查 Pattern 5 的修复代码。

已修改的文件：
${implementation.modified_files.map(f => `- ${f}`).join('\n')}

审查要点：
1. 线程安全：确保正确使用锁保护共享状态
2. 边界条件：检查边界值处理（如百分比 0-1 范围）
3. 错误处理：确保异常被正确捕获
4. 代码质量：变量命名、注释完整性
5. 性能影响：不引入明显的性能问题
6. 向后兼容：不破坏现有API

如果发现 critical 或 major 问题，设置 approved=false。`,
  { schema: REVIEW_SCHEMA, label: '审查者', model: 'claude-sonnet-4-20250514' }
)

if (!review) {
  throw new Error('审查阶段失败：无法获取审查结果')
}

if (review.issues.length > 0) {
  log(`⚠️  发现 ${review.issues.length} 个问题：`)
  for (const issue of review.issues) {
    log(`  [${issue.severity}] ${issue.file}: ${issue.description}`)
  }
}

if (!review.approved) {
  throw new Error('代码审查未通过：存在严重问题，需要修复')
}

log('✅ 代码审查通过')

// ═══════════════════════════════════════════════════════════════════════════
// Phase 4: 测试验证
// ═══════════════════════════════════════════════════════════════════════════

phase('测试')
log('🧪 Phase 4: 运行测试验证修复...')

const TEST_SCHEMA = {
  type: 'object',
  required: ['all_passed', 'pattern5_results', 'related_tests_results'],
  properties: {
    all_passed: { type: 'boolean' },
    pattern5_results: {
      type: 'object',
      required: ['passed', 'failed', 'total'],
      properties: {
        passed: { type: 'number' },
        failed: { type: 'number' },
        total: { type: 'number' },
        failures: { type: 'array', items: { type: 'string' } },
      },
    },
    related_tests_results: {
      type: 'object',
      required: ['passed', 'failed', 'total'],
      properties: {
        passed: { type: 'number' },
        failed: { type: 'number' },
        total: { type: 'number' },
        failures: { type: 'array', items: { type: 'string' } },
      },
    },
    error_logs: { type: 'array', items: { type: 'string' } },
  },
}

const testResults = await agent(
  `运行测试验证 Pattern 5 的修复。

步骤：
1. 运行 Pattern 5 测试: pytest tests/test_pattern5_concurrency.py -v
2. 运行相关模块测试: pytest tests/test_repl.py tests/test_status_bar.py -v
3. 收集测试结果和错误日志

返回详细的测试结果。`,
  { schema: TEST_SCHEMA, label: '测试者', model: 'claude-sonnet-4-20250514' }
)

if (!testResults) {
  throw new Error('测试阶段失败：无法运行测试')
}

log(`📊 Pattern 5 测试: ${testResults.pattern5_results.passed}/${testResults.pattern5_results.total} 通过`)
log(`📊 相关测试: ${testResults.related_tests_results.passed}/${testResults.related_tests_results.total} 通过`)

if (!testResults.all_passed) {
  log('❌ 测试失败，记录失败信息用于回滚...')
  const failureInfo = {
    pattern5_failures: testResults.pattern5_results.failures,
    related_failures: testResults.related_tests_results.failures,
    error_logs: testResults.error_logs,
  }
  throw new Error(`测试失败: ${JSON.stringify(failureInfo, null, 2)}`)
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
  `提交 Pattern 5 的修复代码。

已修改的文件：
${implementation.modified_files.join('\n')}

步骤：
1. 创建新分支: fix/pattern5-concurrency
2. 添加修改的文件: git add <files>
3. 提交: git commit -m "fix: Pattern 5 并发安全问题修复"
4. 推送: git push -u origin fix/pattern5-concurrency
5. 创建PR: gh pr create --title "fix: Pattern 5 并发安全问题修复" --body "<详细描述>" --base main

提交信息格式：
\`\`\`
fix: Pattern 5 并发安全问题修复

修复 Pattern 5 中发现的 4 个并发安全问题:

## 问题1: StatusBar.refresh() 回调机制
<描述修复>

## 问题2: StatusBar._parse_pct() 百分比解析统一
<描述修复>

## 问题3: ContextManager 线程安全
<描述修复>

## 问题4: 进度条超限保护
<描述修复>

## 测试
- test_pattern5_concurrency.py: X/X 通过
- 相关测试: X/X 通过

Co-Authored-By: Claude <noreply@anthropic.com>
\`\`\`

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
log('👀 Phase 6: 监控CI状态并处理失败...')

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
1. 等待CI开始运行（最多等待2分钟）
2. 轮询CI状态，每30秒检查一次（最多等待10分钟）
3. 如果CI失败，获取失败日志
4. 分析失败原因并决定是否需要回滚

命令参考：
- 查看CI状态: gh run list --branch fix/pattern5-concurrency --limit 1
- 查看具体运行: gh run view <run-id>
- 查看日志: gh run view <run-id> --log-failed

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
        log(`     日志: ${check.failure_logs.slice(0, 3).join('\n     ')}`)
      }
    }
  }

  if (monitor.needs_rollback) {
    log(`🔙 需要回滚: ${monitor.rollback_reason}`)

    // 执行回滚
    const rollback = await agent(
      `执行回滚操作。

失败原因：
${monitor.rollback_reason}

失败日志：
${JSON.stringify(monitor.checks.filter(c => c.conclusion === 'failure'), null, 2)}

步骤：
1. 切换回main分支: git checkout main
2. 删除失败的分支: git branch -D fix/pattern5-concurrency
3. 删除远程分支: git push origin --delete fix/pattern5-concurrency
4. 关闭PR（可选）: gh pr close ${commit.pr_number} --comment "CI失败，已回滚。将在修复后重新提交。"

返回回滚结果的文本说明。`,
      { label: '回滚者', model: 'claude-sonnet-4-20250514' }
    )

    throw new Error(`CI失败，已回滚。失败信息：\n${rollback}`)
  }
}

if (monitor.ci_status === 'success') {
  log('✅ CI通过！')
  log(`🎉 Pattern 5 修复成功完成！`)
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
  summary: `Pattern 5 并发安全问题修复${monitor.ci_status === 'success' ? '成功' : '失败'}`,
}

return JSON.stringify(result, null, 2)
