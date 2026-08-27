export const meta = {
  name: 'analyze-startup-bottleneck',
  description: '分析并修复 Xenon 启动瓶颈',
  phases: [
    { title: '分析', detail: '找出启动慢的真正原因' },
    { title: '实现', detail: '编写性能分析和修复代码' },
    { title: '测试', detail: '验证启动时间优化效果' },
    { title: '提交', detail: '提交修复代码' },
  ],
}

// ── 问题背景 ──
// 用户反馈：
// 1. PR #81 优化了 get_configured_providers() 的并行请求
// 2. 刚刚优化了 model_pool.from_config() 跳过 benchmark 查询
// 3. 但实际运行时间仍然很慢（用户感觉 7-8 秒）
// 4. `time xenon --help` 只有 0.1s，说明问题在 REPL 主循环启动

// ── 分析任务 ──
phase('分析')
const ANALYSIS_SCHEMA = {
  type: 'object',
  required: ['bottleneck_location', 'root_cause', 'evidence', 'fix_strategy'],
  properties: {
    bottleneck_location: {
      type: 'string',
      description: '瓶颈位置（文件:行号 in 函数名）'
    },
    root_cause: {
      type: 'string',
      description: '根本原因（网络请求/CPU密集/IO阻塞/等待输入等）'
    },
    evidence: {
      type: 'string',
      description: '证据（时间分析、日志、strace等）'
    },
    estimated_time: {
      type: 'string',
      description: '该瓶颈耗时估计'
    },
    fix_strategy: {
      type: 'string',
      description: '修复策略（延迟加载/并行/缓存/跳过等）'
    }
  }
}

log('🔍 开始分析启动瓶颈...')
const analysis = await agent(
  `分析 Xenon 启动性能瓶颈：

**已知信息**：
1. \`xenon --help\` 只需 0.1s，说明导入和参数解析很快
2. 实际启动 REPL 很慢（7-8秒），但不是 from_config() 的问题（已优化到 0.3s）
3. 问题可能在 \`xenon/repl/repl.py\` 的 \`start_repl()\` 或 \`REPL.run()\` 中

**分析步骤**：
1. 编写性能分析脚本，精确测量启动各阶段耗时：
   - 导入模块
   - 创建 Registry/REPL 对象
   - from_config()
   - **repl.run() 的前几步**（可能在等待用户输入或初始化某些组件）

2. 使用以下工具：
   - 在关键函数前后插入 \`time.time()\` 记录
   - 或使用 Python profiler (cProfile)
   - 或 strace 追踪系统调用

3. 找出真正的瓶颈：
   - 如果是 \`input()\` 等待：说明没问题，只是等待用户输入
   - 如果是网络请求：找出是哪个 API 调用
   - 如果是 CPU 密集：找出是哪个计算
   - 如果是文件 IO：找出是哪个文件操作

**输出要求**：
- bottleneck_location: 精确到文件:行号
- root_cause: 明确说明是什么导致的延迟
- evidence: 提供时间数据或日志证据
- fix_strategy: 可行的修复方案`,
  {
    schema: ANALYSIS_SCHEMA,
    effort: 'high',
    phase: '分析'
  }
)

if (!analysis) {
  throw new Error('分析失败')
}

log(`📍 瓶颈位置: ${analysis.bottleneck_location}`)
log(`🔍 根本原因: ${analysis.root_cause}`)
log(`⏱️  耗时估计: ${analysis.estimated_time}`)
log(`💡 修复策略: ${analysis.fix_strategy}`)

// ── 实现修复 ──
phase('实现')
const IMPLEMENTATION_SCHEMA = {
  type: 'object',
  required: ['modified_files', 'changes_summary', 'performance_improvement'],
  properties: {
    modified_files: {
      type: 'array',
      items: { type: 'string' },
      description: '修改的文件列表'
    },
    changes_summary: {
      type: 'string',
      description: '改动摘要'
    },
    performance_improvement: {
      type: 'string',
      description: '预期性能提升'
    }
  }
}

log('🔧 开始实现修复...')
const implementation = await agent(
  `根据分析结果实现修复：

**瓶颈**: ${analysis.bottleneck_location}
**原因**: ${analysis.root_cause}
**策略**: ${analysis.fix_strategy}

**实现要求**：
1. 修改相关代码文件
2. 确保不破坏现有功能
3. 添加必要的注释说明优化点
4. 如果涉及网络请求，考虑：
   - 并行执行
   - 延迟加载
   - 添加缓存
   - 增加超时控制
5. 如果涉及 CPU 密集，考虑：
   - 算法优化
   - 缓存结果
   - 延迟计算

**输出要求**：
- 列出所有修改的文件
- 简要说明每个文件的改动
- 预期的性能提升（如 "5s → 0.5s"）`,
  {
    schema: IMPLEMENTATION_SCHEMA,
    effort: 'high',
    phase: '实现'
  }
)

if (!implementation) {
  throw new Error('实现失败')
}

log(`✅ 修改了 ${implementation.modified_files.length} 个文件`)
log(`📝 改动: ${implementation.changes_summary}`)
log(`🚀 预期提升: ${implementation.performance_improvement}`)

// ── 测试验证 ──
phase('测试')
const TEST_SCHEMA = {
  type: 'object',
  required: ['startup_time_before', 'startup_time_after', 'improvement_ratio', 'all_tests_pass'],
  properties: {
    startup_time_before: {
      type: 'string',
      description: '优化前启动时间'
    },
    startup_time_after: {
      type: 'string',
      description: '优化后启动时间'
    },
    improvement_ratio: {
      type: 'string',
      description: '提升倍数（如 "10x"）'
    },
    all_tests_pass: {
      type: 'boolean',
      description: '所有测试是否通过'
    },
    test_output: {
      type: 'string',
      description: '测试输出摘要'
    }
  }
}

log('🧪 开始测试...')
const test_result = await agent(
  `测试启动性能优化效果：

**测试步骤**：
1. 编写真实启动时间测试脚本：
   \`\`\`python
   import time
   import subprocess

   # 测试实际 REPL 启动到可以接受输入的时间
   start = time.time()
   proc = subprocess.Popen(
       ['xenon'],
       stdin=subprocess.PIPE,
       stdout=subprocess.PIPE,
       stderr=subprocess.PIPE,
       text=True
   )
   # 等待出现提示符或超时
   # ...
   elapsed = time.time() - start
   proc.terminate()
   \`\`\`

2. 运行现有测试套件确保无回归：
   \`\`\`bash
   python -m pytest tests/test_model_pool.py -xvs
   python -m pytest tests/test_provider_registry.py -xvs
   \`\`\`

3. 重新安装并测试：
   \`\`\`bash
   pip install -e .
   time xenon --version
   \`\`\`

**输出要求**：
- startup_time_before: 优化前的实际时间
- startup_time_after: 优化后的实际时间
- improvement_ratio: 计算提升倍数
- all_tests_pass: true/false
- test_output: 包含关键测试结果`,
  {
    schema: TEST_SCHEMA,
    effort: 'medium',
    phase: '测试'
  }
)

if (!test_result) {
  throw new Error('测试失败')
}

log(`⏱️  优化前: ${test_result.startup_time_before}`)
log(`⏱️  优化后: ${test_result.startup_time_after}`)
log(`🚀 提升: ${test_result.improvement_ratio}`)
log(`✅ 测试通过: ${test_result.all_tests_pass}`)

if (!test_result.all_tests_pass) {
  throw new Error('测试未通过，请修复后再提交')
}

// ── 提交代码 ──
phase('提交')
const COMMIT_SCHEMA = {
  type: 'object',
  required: ['commit_message', 'commit_sha', 'pushed'],
  properties: {
    commit_message: {
      type: 'string',
      description: 'Git 提交信息'
    },
    commit_sha: {
      type: 'string',
      description: '提交的 SHA'
    },
    pushed: {
      type: 'boolean',
      description: '是否已推送到远程'
    }
  }
}

log('📦 提交修复...')
const commit_result = await agent(
  `提交启动性能优化：

**提交信息格式**：
\`\`\`
perf(startup): [具体优化点]

问题：
- [描述瓶颈]

修复：
- [描述改动]

性能提升：
- 启动时间: ${test_result.startup_time_before} → ${test_result.startup_time_after} (${test_result.improvement_ratio})

Co-Authored-By: Claude <noreply@anthropic.com>
\`\`\`

**步骤**：
1. \`git add [修改的文件]\`
2. \`git commit -m "[提交信息]"\`
3. \`git push origin main\` （禁用沙箱）

**输出要求**：
- commit_message: 完整的提交信息
- commit_sha: 提交的 SHA（前7位）
- pushed: 是否成功推送`,
  {
    schema: COMMIT_SCHEMA,
    effort: 'low',
    phase: '提交'
  }
)

if (!commit_result || !commit_result.pushed) {
  throw new Error('提交或推送失败')
}

log(`✅ 已提交: ${commit_result.commit_sha}`)
log(`📤 已推送到 origin/main`)

// ── 返回结果 ──
return {
  bottleneck: analysis.bottleneck_location,
  root_cause: analysis.root_cause,
  fix_strategy: analysis.fix_strategy,
  improvement: test_result.improvement_ratio,
  commit_sha: commit_result.commit_sha,
  success: true
}
