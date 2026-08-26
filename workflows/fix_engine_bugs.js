export const meta = {
  name: 'fix-engine-bugs',
  description: '修复 7 个引擎的关键 bug',
  phases: [
    { title: '准备', detail: '读取审查报告，制定修复计划' },
    { title: '修复-CRITICAL', detail: '修复 CRITICAL 级别 bug' },
    { title: '修复-HIGH', detail: '修复 HIGH 级别 bug' },
    { title: '审查', detail: '代码审查修复质量' },
    { title: '测试', detail: '运行测试验证修复' },
    { title: '提交', detail: '创建 PR 并推送' },
    { title: '监控', detail: '监控 CI 状态' },
  ],
}

// ── Schema 定义 ────────────────────────────────────────────

const FIX_PLAN_SCHEMA = {
  type: 'object',
  properties: {
    critical_fixes: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          issue: { type: 'string' },
          file: { type: 'string' },
          line_range: { type: 'string' },
          fix_strategy: { type: 'string' },
        }
      }
    },
    high_fixes: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          issue: { type: 'string' },
          file: { type: 'string' },
          line_range: { type: 'string' },
          fix_strategy: { type: 'string' },
        }
      }
    },
    estimated_impact: { type: 'string' },
  },
  required: ['critical_fixes', 'high_fixes', 'estimated_impact']
}

const FIX_RESULT_SCHEMA = {
  type: 'object',
  properties: {
    fixed_files: {
      type: 'array',
      items: { type: 'string' }
    },
    fixes_applied: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          severity: { type: 'string' },
          issue: { type: 'string' },
          file: { type: 'string' },
          fixed: { type: 'boolean' },
        }
      }
    },
  },
  required: ['fixed_files', 'fixes_applied']
}

const REVIEW_SCHEMA = {
  type: 'object',
  properties: {
    approved: { type: 'boolean' },
    issues_found: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          severity: { type: 'string', enum: ['critical', 'major', 'minor'] },
          file: { type: 'string' },
          description: { type: 'string' },
        }
      }
    },
    suggestions: { type: 'array', items: { type: 'string' } },
  },
  required: ['approved', 'issues_found']
}

const TEST_SCHEMA = {
  type: 'object',
  properties: {
    all_passed: { type: 'boolean' },
    failed_tests: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          test_name: { type: 'string' },
          error: { type: 'string' },
        }
      }
    },
    coverage_change: { type: 'string' },
  },
  required: ['all_passed', 'failed_tests']
}

const COMMIT_SCHEMA = {
  type: 'object',
  properties: {
    branch_name: { type: 'string' },
    commit_sha: { type: 'string' },
    pr_number: { type: 'number' },
    pr_url: { type: 'string' },
  },
  required: ['branch_name', 'commit_sha', 'pr_url']
}

const MONITOR_SCHEMA = {
  type: 'object',
  properties: {
    ci_status: { type: 'string', enum: ['pending', 'success', 'failure'] },
    action_taken: { type: 'string' },
    rollback_details: { type: 'string' },
  },
  required: ['ci_status', 'action_taken']
}

// ── 工作流执行 ────────────────────────────────────────────

phase('准备')
log('📋 制定修复计划...')

const plan = await agent(
  `根据审查报告制定修复计划：

## 需要修复的问题

### CRITICAL 级别
1. **ReActEngine evidence 重复捕获** (react_engine.py:620-626)
   - 重复调用 ExecutionEvidence.capture() 3次
   - 造成性能浪费和逻辑混乱

### HIGH 级别
2. **未结构化内容被当作 final_answer** (react_engine.py:920, plan_execute_engine.py:1183)
   - LLM 格式错误时直接把原始文本当答案
   - 用户看到混乱的中间状态

3. **工具补救逻辑判断错误** (plan_execute_engine.py:1003-1008)
   - write_ok_before 判断逻辑不清晰
   - 可能导致错误判定成功

## 任务
制定详细的修复计划，包括：
1. 每个 bug 的修复策略
2. 需要修改的文件和行号范围
3. 预期影响评估

返回结构化计划。`,
  {
    schema: FIX_PLAN_SCHEMA,
    label: '制定修复计划',
    phase: '准备',
    model: 'claude-opus-5',
    effort: 'medium'
  }
)

if (!plan) {
  throw new Error('制定计划失败')
}

log(`✓ CRITICAL 修复: ${plan.critical_fixes.length} 个`)
log(`✓ HIGH 修复: ${plan.high_fixes.length} 个`)
log(`✓ 预期影响: ${plan.estimated_impact}`)

// ── 修复 CRITICAL 问题 ────────────────────────────────────

phase('修复-CRITICAL')
log('🔧 修复 CRITICAL 级别问题...')

const criticalFixes = await agent(
  `修复 CRITICAL 级别问题：

${JSON.stringify(plan.critical_fixes, null, 2)}

## 具体要求

### 1. ReActEngine evidence 重复捕获 (react_engine.py:620-626)

**当前代码：**
\`\`\`python
if not getattr(self, '_verification_enabled', True):
    pass
elif not getattr(self, '_verification_active', False):
    self.verification_loop.reset()
    self.verification_loop._active = True
    self._verification_active = True
    evidence = ExecutionEvidence.capture(tracker, workspace_root_for(self))  # 第1次
else:
    evidence = ExecutionEvidence.capture(tracker, workspace_root_for(self))  # 第2次
    outcome_tag = "fixed" if evidence.successful_tests else "still_failing"
    self.verification_loop.record_outcome(evidence, outcome=outcome_tag)
evidence = ExecutionEvidence.capture(tracker, workspace_root_for(self))  # 第3次 ← 问题在这
\`\`\`

**修复方案：**
- 移除重复调用
- 在 if-elif-else 内部各分支中正确捕获一次
- 最后统一在 feed 之前捕获一次即可

请阅读文件，精确定位问题代码，应用修复。

返回修复结果。`,
  {
    schema: FIX_RESULT_SCHEMA,
    label: '修复 CRITICAL bug',
    phase: '修复-CRITICAL',
    model: 'claude-opus-5',
    effort: 'high'
  }
)

if (!criticalFixes) {
  throw new Error('CRITICAL 修复失败')
}

const criticalSuccess = criticalFixes.fixes_applied.every(f => f.fixed)
if (!criticalSuccess) {
  log('⚠️ 部分 CRITICAL 修复失败')
  criticalFixes.fixes_applied.filter(f => !f.fixed).forEach(f => {
    log(`  - ${f.file}: ${f.issue}`)
  })
}

log(`✓ CRITICAL 修复完成: ${criticalFixes.fixed_files.join(', ')}`)

// ── 修复 HIGH 问题 ────────────────────────────────────────

phase('修复-HIGH')
log('🔧 修复 HIGH 级别问题...')

const highFixes = await agent(
  `修复 HIGH 级别问题：

${JSON.stringify(plan.high_fixes, null, 2)}

## 具体要求

### 1. 未结构化内容处理 (react_engine.py:920)

**当前代码：**
\`\`\`python
if malformed_response_retries < max_malformed_response_retries:
    malformed_response_retries += 1
    self.callback.on_warning(
        "LLM 返回了未结构化的中间内容，要求输出完整 final_answer"
    )
    # ... 重试逻辑 ...
    continue
result = raw_cleaned  # ← 直接接受未结构化内容
\`\`\`

**修复方案：**
- 重试耗尽后，不要直接接受未结构化内容
- 添加明确的警告标记，告知用户 LLM 格式错误
- 格式：
  \`\`\`python
  result = (
      "⚠️ LLM 持续返回格式错误的响应。\\n\\n"
      "最后一次输出：\\n" + raw_cleaned[:500]
  )
  self.callback.on_warning("LLM 格式错误重试耗尽，已添加警告标记")
  \`\`\`

### 2. 未结构化内容处理 (plan_execute_engine.py:1183)

**类似问题**：迷你 ReAct 模式也有相同问题
- 定位 line 1182-1184 附近
- 应用相同的修复策略

### 3. 工具补救逻辑 (plan_execute_engine.py:1003-1008)

**当前代码：**
\`\`\`python
if target_write:
    remediated = self._has_successful_write(tracker) and not write_ok_before
\`\`\`

**修复方案：**
- 明确变量命名，避免逻辑混乱
- 改为：
  \`\`\`python
  if target_write:
      write_ok_after = self._has_successful_write(tracker)
      remediated = write_ok_after and not write_ok_before
  \`\`\`

请逐个应用修复，返回结果。`,
  {
    schema: FIX_RESULT_SCHEMA,
    label: '修复 HIGH bug',
    phase: '修复-HIGH',
    model: 'claude-opus-5',
    effort: 'high'
  }
)

if (!highFixes) {
  throw new Error('HIGH 修复失败')
}

const highSuccess = highFixes.fixes_applied.every(f => f.fixed)
if (!highSuccess) {
  log('⚠️ 部分 HIGH 修复失败')
  highFixes.fixes_applied.filter(f => !f.fixed).forEach(f => {
    log(`  - ${f.file}: ${f.issue}`)
  })
}

log(`✓ HIGH 修复完成: ${highFixes.fixed_files.join(', ')}`)

// ── 审查修复 ──────────────────────────────────────────────

phase('审查')
log('👀 审查修复质量...')

const allFixedFiles = [
  ...new Set([...criticalFixes.fixed_files, ...highFixes.fixed_files])
]

const review = await agent(
  `审查引擎 bug 修复：

## 修复摘要

CRITICAL 修复：
${JSON.stringify(criticalFixes.fixes_applied, null, 2)}

HIGH 修复：
${JSON.stringify(highFixes.fixes_applied, null, 2)}

修改文件：
${allFixedFiles.join('\n')}

## 审查要点

1. **逻辑正确性**：修复是否真正解决了问题？
2. **无副作用**：是否引入新的 bug？
3. **代码风格**：是否符合项目规范？
4. **向后兼容**：是否影响现有行为？
5. **性能影响**：是否改善了性能？

重点检查：
- evidence 捕获是否只调用一次？
- 未结构化内容是否有明确警告？
- 工具补救逻辑是否清晰？
- 是否有遗漏的边界情况？

返回审查结果。`,
  {
    schema: REVIEW_SCHEMA,
    label: '审查修复',
    phase: '审查',
    model: 'claude-opus-5',
    effort: 'high'
  }
)

if (!review) {
  throw new Error('审查失败')
}

if (!review.approved) {
  const critical = review.issues_found.filter(i => i.severity === 'critical')
  if (critical.length > 0) {
    log('❌ 发现严重问题，工作流终止')
    critical.forEach(issue => {
      log(`  - [CRITICAL] ${issue.file}: ${issue.description}`)
    })
    throw new Error('修复质量不合格')
  }
}

log(`✓ 审查${review.approved ? '通过' : '有建议'}`)

// ── 测试修复 ──────────────────────────────────────────────

phase('测试')
log('🧪 运行测试验证修复...')

const test = await agent(
  `测试引擎 bug 修复：

修改文件：
${allFixedFiles.join('\n')}

## 测试任务

1. **运行现有测试**：
   \`\`\`bash
   cd /home/xianyu-sheng/Xenon
   /usr/bin/python3 -m pytest tests/ -v -k "engine" --tb=short
   \`\`\`

2. **针对性测试**：
   - 测试 ReActEngine 的验证循环（确保 evidence 只捕获一次）
   - 测试未结构化内容处理（模拟 LLM 格式错误）
   - 测试工具补救逻辑（模拟写工具失败后成功）

3. **回归测试**：
   - 运行全量测试确保无破坏：
     \`\`\`bash
     /usr/bin/python3 -m pytest tests/ --tb=short -x
     \`\`\`

如果测试失败，提供详细错误信息。

返回测试结果。`,
  {
    schema: TEST_SCHEMA,
    label: '运行测试',
    phase: '测试',
    model: 'claude-opus-5',
    effort: 'medium'
  }
)

if (!test) {
  throw new Error('测试失败')
}

if (!test.all_passed) {
  log('❌ 测试失败')
  test.failed_tests.forEach(t => {
    log(`  - ${t.test_name}: ${t.error}`)
  })
  throw new Error('测试未通过，需要修复')
}

log(`✓ 所有测试通过`)

// ── 提交修复 ──────────────────────────────────────────────

phase('提交')
log('📦 提交修复...')

const commit = await agent(
  `提交引擎 bug 修复：

修改文件：
${allFixedFiles.join('\n')}

## 提交流程

1. 确保在 main 分支
2. 创建新分支：fix/engine-bugs
3. 暂存修改：git add ${allFixedFiles.join(' ')}
4. 提交：
   \`\`\`
   git commit -m "fix: 修复引擎关键 bug

- [CRITICAL] 修复 ReActEngine evidence 重复捕获
- [HIGH] 加强未结构化内容警告标记
- [HIGH] 修正工具补救逻辑判断

详细修复：
${criticalFixes.fixes_applied.map(f => `- ${f.issue} (${f.file})`).join('\n')}
${highFixes.fixes_applied.map(f => `- ${f.issue} (${f.file})`).join('\n')}

Co-Authored-By: Claude <noreply@anthropic.com>"
   \`\`\`

5. 推送（禁用沙箱）：git push -u origin fix/engine-bugs
6. 创建 PR：
   \`\`\`bash
   gh pr create --title "fix: 修复引擎关键 bug" --body "## 问题

审查发现 7 个引擎存在以下关键 bug：

1. **[CRITICAL] ReActEngine evidence 重复捕获** (react_engine.py:620-626)
   - 重复调用 ExecutionEvidence.capture() 3次，造成性能浪费

2. **[HIGH] 未结构化内容处理过于宽松** (react_engine.py:920, plan_execute_engine.py:1183)
   - LLM 格式错误时直接把原始文本当答案，用户看到混乱输出

3. **[HIGH] 工具补救逻辑判断错误** (plan_execute_engine.py:1003-1008)
   - 变量命名不清晰，可能导致错误判定

## 修复方案

- 移除重复的 evidence 捕获
- 添加明确的警告标记（⚠️ LLM 持续返回格式错误的响应）
- 明确变量命名（write_ok_after）

## 测试

- [x] 现有测试全部通过
- [x] 针对性测试验证修复
- [x] 回归测试无破坏

Co-Authored-By: Claude <noreply@anthropic.com>
🤖 Generated with [Claude Code](https://claude.com/claude-code)"
   \`\`\`

返回提交信息。`,
  {
    schema: COMMIT_SCHEMA,
    label: '提交修复',
    phase: '提交',
    model: 'claude-opus-5',
    effort: 'low'
  }
)

if (!commit) {
  throw new Error('提交失败')
}

log(`✓ 分支: ${commit.branch_name}`)
log(`✓ 提交: ${commit.commit_sha}`)
log(`✓ PR: ${commit.pr_url}`)

// ── 监控 CI ───────────────────────────────────────────────

phase('监控')
log('📊 监控 CI...')

const monitor = await agent(
  `监控 CI 状态：

PR: ${commit.pr_url}
分支: ${commit.branch_name}

## 监控任务

1. 等待 CI 启动（最多 1 分钟）
2. 检查 CI 状态：gh pr checks ${commit.pr_number}
3. 如果失败：
   - 获取日志：gh run view <run-id> --log-failed
   - 分析原因
   - 决定是否回滚
4. 如果成功：
   - 标注为 "ready for review"

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
  throw new Error('监控失败')
}

log(`✓ CI 状态: ${monitor.ci_status}`)
log(`✓ 行动: ${monitor.action_taken}`)

// ── 总结 ──────────────────────────────────────────────────

return {
  success: monitor.ci_status === 'success',
  plan,
  critical_fixes: criticalFixes,
  high_fixes: highFixes,
  review,
  test,
  commit,
  monitor,
  summary: `修复 ${criticalFixes.fixes_applied.length + highFixes.fixes_applied.length} 个引擎 bug，CI ${monitor.ci_status}`
}
