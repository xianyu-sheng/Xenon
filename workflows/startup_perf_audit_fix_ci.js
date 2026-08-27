export const meta = {
  name: 'startup-perf-audit-fix-ci',
  description: 'Xenon 启动性能：证据审核 → 修复 → 真实启动验证 → PR + CI 闸门（失败带日志回滚重修）',
  phases: [
    { title: '审核', detail: '三路并行审计启动路径，按实测秒数排序瓶颈' },
    { title: '定案', detail: '合并审计结论，剔除无证据的调参与破坏正确性的提案' },
    { title: '修复', detail: '只实现已定案的、有证据支撑的改动' },
    { title: '验证', detail: '真实 xenon 二进制端到端计时 + CI 等价测试闸门' },
    { title: '提交', detail: '开分支提交推送并建 PR' },
    { title: 'CI', detail: '监控 CI；失败则带错误日志回滚并重修' },
  ],
}

// ═══════════════════════════════════════════════════════════════════
// 已实测的地面事实（由主 agent 在启动本工作流前测得，勿重新猜测）
// ═══════════════════════════════════════════════════════════════════
const REPO = '/home/xianyu-sheng/Xenon'

const BASELINE = `
仓库：${REPO}（注意是大写 X，不是 /home/xianyu-sheng/xenon）
当前 HEAD：8750423 "perf(startup): 补齐 c18eb52 漏掉的第二条 benchmark 注册路径"
分支：main，且 origin/main 落后 1 个提交（8750423 尚未推送）
版本：pyproject version = 0.8.5
入口：pyproject [project.scripts] xenon = "xenon.main:cli"
已安装：/home/xianyu-sheng/Xenon/.xenon-venv/bin/xenon（post-commit hook 每次提交自动重装）

【实测端到端启动】printf '/exit\\n' | xenon    → 1.72s / 2.67s / 2.89s

【实测分阶段拆解】（同一进程内逐段 perf_counter，非猜测）
  import xenon.repl.repl                 0.194s
  REPL()                                 0.066s
  REPL._check_first_run()                1.184s   ← 绝对主导项
  REPL._load_custom_commands()           0.041s
  REPL._preload_mcp_server_configs()     0.004s
  REPL._check_auto_resume()              0.001s
  合计                                    1.491s

【_check_first_run 内部实测】
  load_credentials()                     0.003s（5 条凭证）
  get_configured_providers()             1.940s（5 个 provider）
      DeepSeek   models=3   err=''
      OpenAI     models=0   err='HTTP 401: Incorrect API key...'   ← 死 key，每次启动都完整走一遍 401 往返
      Anthropic  models=0   err='HTTP 401: authentication_error'   ← 死 key，同上
      字节豆包    models=2   err=''
      Test       models=1   err=''

关键代码坐标：
  xenon/repl/repl.py:1171          REPL.run()（真正的启动路径）
  xenon/repl/repl.py:1177          startup = self._check_first_run()
  xenon/repl/repl.py:1359          def _check_first_run()
  xenon/repl/provider_registry.py:43    MODEL_LIST_TIMEOUT = 8.0
  xenon/repl/provider_registry.py:551   with _create_http_client(timeout=MODEL_LIST_TIMEOUT)
  xenon/repl/model_pool.py:125          benchmark_fetcher 调用点（启动路径已绕开，register() 默认仍会调）
`

// 上一轮的教训：三条提案核实后被否，禁止再提
const REJECTED = `
以下三条在上一轮已被核实否决，任何 agent 不得再次提出（提出即视为审计失败）：

1. get_configured_providers(refresh_models=False)
   否决理由：全仓库 grep 无任何"后台刷新"路径，注释在撒谎。实测代价是模型列表
   从实时探测退回内置兜底 15 个，其中含用户已失效的 claude-sonnet-4-20250514、
   gpt-4o。且直接违反该函数 docstring 里"避免把内置兜底列表误展示为最新模型"
   的承诺。拿正确性换 0.6s 不做。

2. MODEL_LIST_TIMEOUT 8.0 → 2.0
   否决理由：无任何证据表明 2.0s 对慢网络、对 Ark(豆包) 大目录分页足够。凭感觉调参。

3. 对 401/403 做 1 小时 provider 冷却
   否决理由：_failed_providers 是模块级 dict，set_provider_key() 不清理它。用户换上
   好 key 后 1 小时内该 provider 仍被静默跳过——把性能问题变成正确性 bug。

另：上一轮"26.65s → 0.31s / 86x"的数字是假的，因为脚本只调 from_config() 绕过了
REPL 真正走的 _check_first_run()。本工作流一切计时必须来自真实 xenon 二进制。
`

const MEASURE_PROTOCOL = `
【计时协议 —— 唯一可信的测法，禁止用自写脚本调库函数代替】
必须测真实二进制，且必须多次取样（抖动很大，单次样本无意义）：

    cd ${REPO}
    for i in 1 2 3 4 5; do
      /usr/bin/time -f "run$i: %e s" bash -c "printf '/exit\\n' | xenon >/dev/null 2>&1" 2>&1
    done

改代码后 xenon 二进制不会自动更新（post-commit hook 只在 commit 时重装）。
未提交就想测，必须先手动装：cd ${REPO} && .xenon-venv/bin/python -m pip install -e . -q
或直接用 .xenon-venv/bin/xenon 前先确认 pip install -e . 是可编辑安装（是的，改 .py 立即生效）。

分阶段拆解用这个（同一进程逐段计时，只用于定位，不用于对外报数）：
    printf '/exit\\n' | python3 -c "
    import time,sys
    t0=time.perf_counter()
    from xenon.repl.repl import REPL
    print(f'import {time.perf_counter()-t0:.3f}s',file=sys.stderr)
    t=time.perf_counter(); r=REPL(); print(f'init {time.perf_counter()-t:.3f}s',file=sys.stderr)
    t=time.perf_counter(); r._check_first_run(); print(f'first_run {time.perf_counter()-t:.3f}s',file=sys.stderr)
    " 2>&1 >/dev/null
注意类名是 REPL，不是 XenonREPL。
`

const TEST_PROTOCOL = `
【测试闸门 —— 与 .github/workflows/ci.yml 等价，本地先跑，别把失败推给 CI】
CI 实际会跑这四步（python 3.10/3.11/3.12 矩阵）：
  1. python -m compileall -q xenon tests
  2. ruff check xenon tests evals
  3. python -m pytest tests xenon/tests -m "not live and not e2e" -q \\
       --cov=xenon --cov-fail-under=55 --timeout=120
  4. python -m pytest tests/e2e -m e2e -q --timeout=60
CI 环境变量：OPENAI_API_KEY="" DEEPSEEK_API_KEY="" XENON_ASSUME_YES=1
  → 意味着 CI 里没有任何真 key，任何依赖真实 provider 探测的新测试都必须能在
    零 key 环境下通过，否则会挂 CI。

本地跑用 ${REPO}/.xenon-venv/bin/python -m pytest ...（该 venv 有 pytest 且装了本仓库）。

已知既有失败（不是你造成的，出现了不要去"修"，也不要当成回归）：
  - tests/test_q4_code_index_project_context.py::test_refresh_skips_unchanged_key_file_reads
  - tests/test_repl_real_usage.py::TestCompactReal::test_long_session_triggers_compact_need（隔离跑通过，全量下抖动）
除这两项外任何失败都算真回归，必须修掉才能进入提交阶段。
`

// ═══════════════════════════════════════════════════════════════════
phase('审核')
// 三路并行、互相独立的审计。分开是为了拿到三个不受污染的视角，
// 而不是一个 agent 顺着自己第一个猜想一路走到底。
// ═══════════════════════════════════════════════════════════════════

const AUDIT_SCHEMA = {
  type: 'object',
  required: ['findings', 'measured_total', 'method'],
  properties: {
    method: { type: 'string', description: '你实际跑了哪些命令来取证（原文粘贴命令）' },
    measured_total: { type: 'string', description: '你自己实测的端到端启动时间，5 次取样原始值' },
    findings: {
      type: 'array',
      description: '按实测耗时降序排列，只收录你亲自测出秒数的项',
      items: {
        type: 'object',
        required: ['location', 'measured_cost', 'root_cause', 'evidence', 'fixable_safely'],
        properties: {
          location: { type: 'string', description: 'file.py:line in function()' },
          measured_cost: { type: 'string', description: '实测耗时，如 "1.18s"。没测出来就别写这条' },
          root_cause: { type: 'string', description: '网络往返 / 磁盘 IO / import / CPU / 锁等待' },
          evidence: { type: 'string', description: '支撑该耗时的命令输出原文' },
          fixable_safely: { type: 'boolean', description: '能否在不牺牲正确性的前提下修掉' },
          proposed_fix: { type: 'string', description: '具体改法；fixable_safely=false 则写为什么不能改' },
        },
      },
    },
  },
}

const AUDIT_COMMON = `你在只读审计 Xenon 的启动性能。**这一阶段禁止修改任何文件**，只取证。

${BASELINE}
${REJECTED}
${MEASURE_PROTOCOL}

铁律：
- 每一条 finding 都必须附上你亲自跑出来的秒数和命令输出原文。测不出秒数的怀疑，不要写进 findings。
- 不要复述上面已给的基线数字充当你的发现；你的价值在于把 1.18s 的 _check_first_run 再往下拆，
  或者找出基线没覆盖到的成本。
- 如果你的结论是"某项无法安全优化"，照实写 fixable_safely=false，这和找到优化点一样有价值。`

const audits = await parallel([
  () => agent(
    `${AUDIT_COMMON}

**你的切入角度：网络层。**
_check_first_run 的 1.18s 里，get_configured_providers() 占 1.94s（单独测时）。5 个 provider
中 OpenAI 和 Anthropic 是死 key，每次启动都完整走一遍 HTTP 401 往返。

要回答：
1. 这 5 个 provider 的探测是并行还是串行？读 xenon/repl/provider_registry.py 确认，
   并用实测证明（比如把 provider 数量与总耗时对照）。若已并行，总耗时应≈最慢那个，是吗？
2. 单个 401 往返实测多少秒？两个死 key 加起来贡献了这 1.94s 里的多少？
3. 有没有磁盘缓存（provider_models.json）？命中时多少秒、未命中多少秒？缓存 key 含什么？
   实测：删掉缓存跑一次，再跑一次，对比。
4. 关键：有没有办法在**不**牺牲"模型列表实时准确"的前提下，把启动阻塞在网络上的时间去掉？
   例如先用缓存立即渲染、同时后台刷新（注意：REJECTED 第 1 条否掉的是"退回内置兜底列表"，
   不是"用真实缓存 + 后台刷新"——后者若能落地是合法方向，但你必须确认后台刷新路径真的存在
   或真的能被写出来，不能像上一轮那样注释里写了代码里没有）。`,
    { schema: AUDIT_SCHEMA, label: '审计 A：网络与 provider 探测', phase: '审核', effort: 'high' }
  ),
  () => agent(
    `${AUDIT_COMMON}

**你的切入角度：import 与进程冷启动。**
实测 import xenon.repl.repl = 0.194s，REPL() = 0.066s。但端到端是 1.72~2.89s，而分阶段
合计只有 1.491s —— 有 0.2~1.4s 的差额没被解释掉。

要回答：
1. 这个差额去哪了？python 解释器启动 + xenon/main.py 的 argparse 与顶层 import 各占多少？
   用 python -X importtime -c "import xenon.main" 2>&1 | sort -k2 -rn | head -30 取证。
2. 有哪些重量级第三方库在 import 时被无条件拉起（yaml/httpx/openai/tiktoken/rich 等）？
   哪些其实只在特定命令下才需要，能改成惰性 import？逐个给出实测节省。
3. xenon --version 和 xenon --help 各多少秒？它们和完整启动的差值说明了什么？
4. .pyc 是否被有效缓存？重复运行的方差（1.72 vs 2.89，差 68%）来自哪里？
   这个方差本身可能就是最大的用户体验问题，查清它。`,
    { schema: AUDIT_SCHEMA, label: '审计 B：import 与进程冷启动', phase: '审核', effort: 'high' }
  ),
  () => agent(
    `${AUDIT_COMMON}

**你的切入角度：磁盘 IO、benchmark_fetcher、以及"看起来已修好其实没修"的地方。**

要回答：
1. benchmark_fetcher：~/.xenon/benchmark_cache.json 从未生成过。查清 HuggingFace
   leaderboard 那个 API 是不是早已失效——如果是，model_pool.register() 默认路径每次都在
   做一次注定失败的网络请求然后落到 _infer_capability 兜底。实测 register() 一次多少秒，
   启动路径究竟碰不碰它（xenon/repl/model_pool.py:125）。
2. 启动时读了哪些文件？用 strace -f -e trace=openat -c 或
   strace -f -e trace=openat 2>&1 | grep -c ENOENT 统计，找出失败的探测性 open
   （ENOENT 风暴也是成本）。
3. 有没有在启动路径上做 tiktoken 编码表下载/加载、code index 扫描、会话历史全量读取
   这类隐藏 IO？
4. 交叉检查上一轮那两条已提交的"修复"（c18eb52、8750423）是否真的生效了，
   还有没有第三条漏掉的 benchmark 注册路径。git show 这两个提交，然后 grep 全仓库确认。`,
    { schema: AUDIT_SCHEMA, label: '审计 C：磁盘 IO 与残留问题', phase: '审核', effort: 'high' }
  ),
])

const [auditA, auditB, auditC] = audits
for (const [name, a] of [['A 网络', auditA], ['B import', auditB], ['C IO', auditC]]) {
  if (!a) { log(`⚠️  审计 ${name} 无返回`); continue }
  log(`── 审计 ${name}：实测总时 ${a.measured_total}`)
  for (const f of a.findings || []) {
    log(`   ${f.measured_cost.padEnd(8)} ${f.location}  ${f.fixable_safely ? '可修' : '不可修'} — ${f.root_cause}`)
  }
}

// ═══════════════════════════════════════════════════════════════════
phase('定案')
// 独立评审员。它的任务不是"再想优化点"，而是把三份审计里
// 没有实测支撑、或用正确性换速度的提案剔掉。上一轮正是缺了这一关。
// ═══════════════════════════════════════════════════════════════════

const PLAN_SCHEMA = {
  type: 'object',
  required: ['approved', 'rejected', 'expected_after', 'out_of_scope'],
  properties: {
    approved: {
      type: 'array',
      description: '批准实现的改动，按性价比降序。可以为空数组——若真无安全优化点，如实报空',
      items: {
        type: 'object',
        required: ['location', 'change', 'expected_saving', 'why_safe', 'how_to_verify'],
        properties: {
          location: { type: 'string' },
          change: { type: 'string', description: '具体改什么，精确到函数与行为' },
          expected_saving: { type: 'string', description: '基于实测的预期节省' },
          why_safe: { type: 'string', description: '为什么不牺牲正确性；命中 REJECTED 三条的一律不得批准' },
          how_to_verify: { type: 'string', description: '用什么命令证明它真的生效了' },
        },
      },
    },
    rejected: {
      type: 'array',
      description: '被你剔掉的提案及理由（这部分和 approved 同等重要，必须写）',
      items: {
        type: 'object',
        required: ['proposal', 'reason'],
        properties: { proposal: { type: 'string' }, reason: { type: 'string' } },
      },
    },
    expected_after: { type: 'string', description: '全部批准项落地后的预期端到端启动时间区间' },
    out_of_scope: { type: 'string', description: '真实存在但不该由代码修的（例如死 key 该由用户在 /model 里删）' },
  },
}

const plan = await agent(
  `你是独立评审员。三份并行审计已完成，你的职责是**筛除**而非新增。

${BASELINE}
${REJECTED}

── 审计 A（网络）──
${JSON.stringify(auditA, null, 2)}

── 审计 B（import）──
${JSON.stringify(auditB, null, 2)}

── 审计 C（IO/残留）──
${JSON.stringify(auditC, null, 2)}

筛除标准，逐条对照，任一命中即打入 rejected：
1. 没有实测秒数支撑 → 剔除。"我认为这里可能慢"不算证据。
2. 命中 REJECTED 三条中任意一条 → 剔除，并在 reason 里注明是重犯。
3. 拿正确性/准确性换速度 → 剔除。模型列表必须保持实时准确，这是硬约束。
4. 凭感觉调超时/并发/重试数字，没有对慢网络与大目录分页的论证 → 剔除。
5. 引入的状态无法被正常操作重置（如模块级缓存不响应 set_provider_key）→ 剔除。
6. 预期节省 < 0.1s 但改动跨越 3 个以上文件 → 剔除，不值当。

━━━ 必须裁决的正面冲突：审计 A 与审计 C 对"negative caching"结论相反 ━━━
两份审计都实测到同一事实：provider_registry.py:493 的写缓存条件是 "if models and use_cache"，
401 返回空列表 → 永不写缓存 → 两个死 key 每次启动各付约 1.0~1.9s（openai 10 次采样
med=1.86s，尾部有一次 8.055s 打满 MODEL_LIST_TIMEOUT）。但结论相反：

· 审计 C 主张【可以做】：给失败结果也写缓存条目，TTL 取短值（如 300s）。它的核心论证是
  "这与被否决的第 3 条有本质区别"——被否决的方案是模块级 _failed_providers dict，
  set_provider_key() 不清理它所以换 key 后仍被静默跳过；而磁盘缓存条目的 key 里含
  api_key_hash，provider_registry.py:450 的 _is_cache_valid 已经在比对 cached_hash，
  用户一换 key hash 立变、条目自动失效，下次启动立刻重新探测。
· 审计 A 主张【不能做】：认为"唯一语义无损的方向是缓存失败结果本身——但那正是被否决的
  第 3 条，同样的正确性 bug 会以 negative cache 的形式重现"。

你必须亲自去读 provider_registry.py 的 _is_cache_valid（约 450 行）和 fetch_provider_models
（约 493/525 行），自己确认 api_key_hash 是否真的参与缓存有效性判断。**不要靠这两段文字
二选一，去读代码。** 然后裁决：
  - 若 api_key_hash 确实参与校验，审计 C 的区分成立，negative caching 不等于被否决的第 3 条，
    可以批准（但必须要求实现时附一个"换 key 后立即重新探测"的测试）。
  - 若不成立，或你发现别的重置漏洞（例如用户在 models.yaml 里改 base_url、或 key 相同但
    额度恢复、或 TTL 内 key 被平台重新激活），则维持否决并写清漏洞。
  - 特别注意 TTL 内的一个真实缺口：key 没变、但服务端从 401 恢复正常（付费恢复、限流解除），
    此时磁盘负缓存会让用户在 TTL 内看不到已恢复的 provider。判断 300s 这个量级是否可接受，
    以及是否该只对 401/403（认证类，key 不变则结论不变）负缓存、而不对 5xx/超时（瞬时故障）
    负缓存。这个区分本身可能就是正确答案。

另外必须判断：**死 key 的 401 往返到底该不该由代码修？**
用户的 OpenAI/Anthropic key 已失效，每次启动都真实走一遍 401。诚实的答案可能是
"这该由用户在 /model 里删掉或更新 key，不该由代码加冷却来掩盖"。如果你认为如此，
写进 out_of_scope，并说明代码侧的正确做法是什么（例如：把 401 这类**永久性认证失败**
与超时/5xx 这类**瞬时故障**区分开，在启动摘要里明确提示用户"OpenAI key 认证失败，
请 /model 更新"——这是把隐藏成本变成可见提示，而不是静默跳过）。

如果结论是"当前 1.7~2.9s 已无安全的大额优化空间"，就如实报 approved: []。
报空比编一个假优化诚实得多。上一轮的教训就是硬凑出了一个 86x 的假数字。

━━━ 产品语义变更一律不得批准，只能进 out_of_scope ━━━
审计里有两条属于**改变产品行为**而非性能优化，即使它们实测有效也不许批准落地
（本工作流没有用户授权做产品决策，必须由用户自己拍）：
  · 把 XENON_SKIP_MODEL_PROBE 提升为面向用户的 --offline 开关（实测能到 0.61s、方差 5%）
  · 改成异步探测 / 探测结果后置渲染（把模型状态从启动横幅里挪走）
这两条写进 out_of_scope，并注明"实测数据支持，但属产品决策，建议单独征询用户"。
approved 里只放**行为不变、纯粹去掉浪费**的改动。`,
  { schema: PLAN_SCHEMA, label: '定案：筛除无证据提案', phase: '定案', effort: 'high' }
)

if (!plan) throw new Error('定案阶段无返回，中止')

log(`✅ 批准 ${(plan.approved || []).length} 项，剔除 ${(plan.rejected || []).length} 项`)
for (const a of plan.approved || []) log(`   ✔ ${a.location} — ${a.change} (省 ${a.expected_saving})`)
for (const r of plan.rejected || []) log(`   ✘ ${r.proposal} — ${r.reason}`)
if (plan.out_of_scope) log(`ℹ️  非代码问题：${plan.out_of_scope}`)
log(`🎯 预期落地后：${plan.expected_after}`)

// 诚实出口：没有安全优化点就不要为了"有产出"而乱改代码。
if (!plan.approved || plan.approved.length === 0) {
  log('⛔ 无安全优化点，按约定不改代码、不提交。')
  return {
    outcome: 'no_safe_optimization',
    baseline: '1.72 / 2.67 / 2.89 s',
    rejected: plan.rejected,
    out_of_scope: plan.out_of_scope,
    note: '审计结论为当前启动已无可在不牺牲正确性前提下取得的大额优化。剩余成本主要是两个失效 key 的真实 401 往返，应由用户在 /model 更新或删除。',
  }
}

// ═══════════════════════════════════════════════════════════════════
phase('修复')
// ═══════════════════════════════════════════════════════════════════

const IMPL_SCHEMA = {
  type: 'object',
  required: ['modified_files', 'implemented', 'skipped', 'tests_added'],
  properties: {
    modified_files: { type: 'array', items: { type: 'string' } },
    implemented: {
      type: 'array',
      description: '实际落地的改动',
      items: {
        type: 'object',
        required: ['location', 'what_changed'],
        properties: { location: { type: 'string' }, what_changed: { type: 'string' } },
      },
    },
    skipped: {
      type: 'array',
      description: '批准了但你实现时发现不该做的，写清为什么（发现问题就跳过是对的，硬做才是错的）',
      items: {
        type: 'object',
        required: ['location', 'why_skipped'],
        properties: { location: { type: 'string' }, why_skipped: { type: 'string' } },
      },
    },
    tests_added: { type: 'array', items: { type: 'string' }, description: '新增/修改的测试文件与用例名' },
  },
}

const impl = await agent(
  `实现已定案的启动性能改动。**只实现下面这份批准清单，不要自行扩大范围。**

${BASELINE}
${REJECTED}
${TEST_PROTOCOL}

── 批准清单（这是你唯一的施工范围）──
${JSON.stringify(plan.approved, null, 2)}

要求：
1. 严格按清单实现。清单外的"顺手优化"一概不做——上一轮就是靠夹带私货把三条坏改动混进来的。
2. 实现过程中若发现某条批准项其实做不了或会引入 bug，**跳过它并写进 skipped**。
   跳过是合法结果，硬做出一个假修复不是。
3. 每条改动都要有对应测试。测试必须能在零 key 环境下通过（CI 里 OPENAI_API_KEY=""），
   用 mock/monkeypatch 而不是真实网络。新测试放进 tests/ 下已有的启动相关文件，
   优先 tests/test_startup_performance.py 或 tests/test_startup_experience.py，
   遵循这些文件现有的 fixture 风格（temp_cache_dir / mock_credentials / mock_fetch）。
4. 注释只写"为什么"，不写"是什么"。且**绝不写代码里不存在的机制**——上一轮有个注释
   声称"首次使用时自动触发后台刷新"，而全仓库没有该路径。如果你写了后台刷新，就要真的
   写出那条路径并有测试覆盖它。
5. 改完立即自查：ruff check xenon tests evals 必须干净（CI 第 2 步会拦）。
6. 中文注释与提交语境保持仓库现有风格。

改完不要提交，交给下一阶段验证。`,
  { schema: IMPL_SCHEMA, label: '实现批准的改动', phase: '修复', effort: 'high' }
)

if (!impl) throw new Error('实现阶段无返回，中止')
log(`📝 改了 ${(impl.modified_files || []).length} 个文件，落地 ${(impl.implemented || []).length} 项，跳过 ${(impl.skipped || []).length} 项`)
for (const s of impl.skipped || []) log(`   ⊘ ${s.location} — ${s.why_skipped}`)

if (!impl.implemented || impl.implemented.length === 0) {
  log('⛔ 全部批准项在实现阶段被判定不可做，不提交。')
  return { outcome: 'all_skipped_at_impl', skipped: impl.skipped, plan_approved: plan.approved }
}

// ═══════════════════════════════════════════════════════════════════
phase('验证')
// 真实二进制计时 + CI 等价测试。这一关是上一轮唯一真正失守的地方。
// ═══════════════════════════════════════════════════════════════════

const VERIFY_SCHEMA = {
  type: 'object',
  required: ['before_samples', 'after_samples', 'improvement', 'offpath_improvement', 'behavior_identical', 'ci_equivalent_pass', 'regressions', 'model_list_intact'],
  properties: {
    before_samples: { type: 'string', description: '修复前 5 次原始秒数（在 stash 状态下实测，不要抄基线）' },
    after_samples: { type: 'string', description: '修复后 5 次原始秒数' },
    improvement: { type: 'string', description: '启动路径的诚实结论。预期是"不变"；若不变就明说不变，不要把方差包装成提升' },
    offpath_improvement: { type: 'string', description: '三处改动真正的收益实测：/setup 单/多模型注册、batch_register 导入无 tier YAML 的前后耗时' },
    behavior_identical: { type: 'boolean', description: '关键：改动前后每个模型的 capability.tier 是否逐个相等（纯去浪费的唯一证明）' },
    behavior_evidence: { type: 'string', description: 'tier 逐个对比的实测输出' },
    ci_equivalent_pass: { type: 'boolean', description: 'CI 四步是否全绿（已知两项既有失败除外）' },
    test_summary: { type: 'string', description: 'pytest 尾部统计原文' },
    regressions: { type: 'array', items: { type: 'string' }, description: '除已知两项外的失败；为空才算通过' },
    model_list_intact: { type: 'boolean', description: '关键：启动仍显示实时探测的模型数，没退回内置兜底列表' },
    model_list_evidence: { type: 'string', description: '启动横幅里"已准备 N 个模型 · M 个提供商"的原文' },
  },
}

const verify = await agent(
  `验证启动性能改动。**你是这条流水线的诚实闸门，你的数字会被直接报给用户，不许美化。**

${BASELINE}
${MEASURE_PROTOCOL}
${TEST_PROTOCOL}

已落地的改动：
${JSON.stringify(impl.implemented, null, 2)}

步骤，按序执行：
1. 先装可编辑包，确保 xenon 二进制跑的是改后代码：
   cd ${REPO} && .xenon-venv/bin/python -m pip install -e . -q
2. 测 after：按计时协议跑 5 次，记原始值。
3. 测 before：git stash（含未跟踪测试文件用 -u），重装，跑 5 次，记原始值，然后 git stash pop 恢复。
   **必须真的 stash 后重测**，不要抄用上面基线里的数字——环境和负载会变。
4. 取**中位数**对比，不要拿最快的那次算倍数。方差很大（1.72~2.89），用中位数才诚实。
5. 跑 CI 四步等价命令（见测试协议）。任何非已知两项的失败都是回归，写进 regressions。
6. **正确性校验（最关键）**：真实启动一次，看启动横幅里报了多少个模型多少个提供商。
   改前是"27 个模型 · 3 个提供商"这一量级的实时探测结果。如果模型数掉回内置兜底
   （15 个左右且含 gpt-4o、claude-sonnet-4-20250514 这些死型号），说明改动用正确性换了速度，
   立即 model_list_intact=false。

━━━ 关键前提：本次改动预期对启动时间零影响，不要去凑一个提升出来 ━━━
定案阶段已实测判定：三处 skip_benchmark 全部**不在启动路径上**
（setup_wizard.interactive_setup 只由 /setup 触发；batch_register 只由 xenon models import
与 /model 触发；启动路径 repl.py:1432 早已是 skip_benchmark=True，探针实测启动时
_fetch_remote 调用数为 0）。因此第 2/3 步的 before/after 端到端数字**应当在噪声内相等**，
中位数都在 1.8~2.9s。

这是预期结果，不是失败。你的 improvement 字段就该如实写成
"启动时间不变（before med X.XXs / after med Y.YYs，差异在方差内）；本次改动的收益不在启动路径"。
**严禁**为了让数字好看而把方差当成提升（比如挑 before 最慢那次比 after 最快那次）。
上一轮就是这么造出 86x 假数字的。

7. 因此你必须**额外**测出这三处改动真正的收益，这才是本次 PR 的价值所在：
   a) 对 setup_wizard 路径：造一个 5 模型的注册场景，对 model_pool.register() 分别在
      skip_benchmark=True/False 下计时（默认值实测约 1.575s/次，5 次约 8.2s）。
   b) 对 batch_register 路径：构造一个**不含 tier 字段**的多模型 YAML，对 batch_register()
      计时，对比改动前后。
   c) 这两组数字用真实调用测（HF 端点确实是 404，不要 mock 掉网络，否则测不出真实等待）。
   把它们写进 offpath_improvement 字段。
8. **行为不变的硬断言**：对 batch_register，导入同一份无 tier 的 YAML，改动前后
   每个模型的 capability.tier 必须逐个相等。这是"纯去浪费而非改变行为"的唯一证明，
   必须实测并写进 behavior_identical。若有任何一个 tier 变了，立即报 false 并停止。

清理你造的临时文件。`,
  { schema: VERIFY_SCHEMA, label: '真实启动验证 + CI 等价测试', phase: '验证', effort: 'high' }
)

if (!verify) throw new Error('验证阶段无返回，中止')
log(`⏱️  启动路径：${verify.improvement}`)
log(`   before: ${verify.before_samples}`)
log(`   after:  ${verify.after_samples}`)
log(`🎯 非启动路径真实收益：${verify.offpath_improvement}`)
log(`🔒 行为不变（tier 逐个相等）：${verify.behavior_identical ? '是' : '❌ 否'}`)
log(`🧪 CI 等价测试：${verify.ci_equivalent_pass ? '全绿' : '有失败'} — ${verify.test_summary || ''}`)
log(`🔍 模型列表完整性：${verify.model_list_intact ? '正常' : '❌ 已退化'} ${verify.model_list_evidence || ''}`)

if (verify.behavior_identical === false) {
  throw new Error(`行为改变：tier 在改动前后不相等。证据：${verify.behavior_evidence}。这三处改动的全部前提是"纯去浪费、行为不变"，前提破了必须撤回。`)
}
if (!verify.model_list_intact) {
  throw new Error(`正确性回归：模型列表退回兜底。证据：${verify.model_list_evidence}。这正是 REJECTED 第 1 条禁止的交换，必须撤回改动。`)
}
if (!verify.ci_equivalent_pass || (verify.regressions || []).length > 0) {
  throw new Error(`本地 CI 等价测试未过，不推送。回归项：${JSON.stringify(verify.regressions)}`)
}

// ═══════════════════════════════════════════════════════════════════
phase('提交')
// 走分支 + PR，不直推 main。CI 在 PR to main 上会触发。
// 注意：origin/main 目前落后 1 个提交（8750423 未推），分支从当前 HEAD 切出，
// PR 会带上那条一起过 CI —— 这是好事，那条也还没被 CI 验证过。
// ═══════════════════════════════════════════════════════════════════

// 分支名必须是静态字面量：Date.now()/new Date() 在工作流脚本里被禁用（会破坏 resume 的
// 缓存键一致性）。若该分支已存在，交给提交 agent 处理（复用或加后缀）。
const BRANCH = 'perf/skip-benchmark-remaining-paths'

const COMMIT_SCHEMA = {
  type: 'object',
  required: ['branch', 'commit_sha', 'pushed', 'pr_url', 'pr_number'],
  properties: {
    branch: { type: 'string' },
    commit_sha: { type: 'string', description: '前 7 位' },
    commit_message: { type: 'string' },
    pushed: { type: 'boolean' },
    pr_url: { type: 'string' },
    pr_number: { type: 'string' },
  },
}

const commit = await agent(
  `提交并开 PR。

仓库 ${REPO}，当前在 main 分支，HEAD=8750423（该提交还没推上 origin/main）。

步骤：
1. git checkout -b ${BRANCH}
   若该分支已存在（本工作流曾重试过），改用 git checkout ${BRANCH} 复用它，
   或加一个简短后缀（如 -v2）另开；不要因为分支冲突就中止。
2. 只 add 本次相关文件（改动源文件 + 新测试 + 本工作流脚本 workflows/startup_perf_audit_fix_ci.js）。
   **不要 git add -A**。当前工作区还有一个无关的未跟踪文件 workflows/analyze_startup_bottleneck.js，
   那是上一轮的产物，不要提交它。
3. 提交信息用中文，遵循仓库现有风格。

   **标题不要写成 perf(startup)** —— 本次改动实测对启动时间零影响，写 startup 是误导。
   用 perf(benchmark): 或 perf(setup): 这一类，准确描述"补齐剩余三处 skip_benchmark"。

   正文必须包含且必须诚实：
   - 问题：HF open-llm-leaderboard results 端点已永久 404（curl 实测），
     estimate_tier 100% 回退 _infer_capability；c18eb52/8750423 修了启动路径，
     但漏了 setup_wizard 两处与 batch_register 一处，每个模型仍白等约 1.575s
   - 修复：逐条列三处改动
   - 启动时间：**明确写"不变"**。${verify.improvement}
     并说明原因：这三处都不在启动路径上（/setup 与 xenon models import 才触发），
     启动路径 repl.py:1432 早已是 skip_benchmark=True，探针实测启动时 _fetch_remote 调用数为 0
   - 真实收益：${verify.offpath_improvement}
   - 行为不变证明：改动前后每个模型 capability.tier 逐个相等（${verify.behavior_identical ? '已实测确认' : '未确认'}）
   - 正确性：模型列表仍为实时探测，未退回内置兜底（${verify.model_list_evidence || 'n/a'}）
   - 若有 skipped 项，在正文里说明为什么没做
   结尾加：Co-Authored-By: Claude <noreply@anthropic.com>
4. git push -u origin ${BRANCH}
   若因代理/沙箱失败，用 dangerouslyDisableSandbox 重试（本机 Clash 会拦 SSH 22）。
5. gh pr create --base main --head ${BRANCH}
   标题 70 字符以内，同样不要用 startup 字样误导。正文含：
   - 改动摘要
   - 验证方式与结果（启动不变 + 非启动路径的真实收益 + tier 逐个相等）
   - "已知既有失败两项"的说明（免得 reviewer 误判）
   - 注意本 PR 会顺带带上未推送的 8750423，说明一句
   PR 正文结尾加：🤖 Generated with [Claude Code](https://claude.com/claude-code)

输出 pr_number 只要数字。`,
  { schema: COMMIT_SCHEMA, label: '提交并开 PR', phase: '提交', effort: 'medium' }
)

if (!commit || !commit.pushed) throw new Error('提交或推送失败，中止')
log(`📦 ${commit.commit_sha} → ${commit.branch}`)
log(`🔗 PR #${commit.pr_number}: ${commit.pr_url}`)

// ═══════════════════════════════════════════════════════════════════
phase('CI')
// 用户明确要求：CI 挂了就带着错误日志回滚继续修，直到过。
// 最多 3 轮，避免无限烧。
// ═══════════════════════════════════════════════════════════════════

const CI_SCHEMA = {
  type: 'object',
  required: ['conclusion', 'failed_jobs', 'error_log', 'run_url'],
  properties: {
    conclusion: { type: 'string', description: 'success / failure / cancelled / timed_out' },
    failed_jobs: { type: 'array', items: { type: 'string' }, description: '失败的 job 名（含 python 版本/OS 矩阵项）' },
    error_log: { type: 'string', description: '失败日志原文关键段落，要能据此定位问题。成功则留空' },
    run_url: { type: 'string' },
    failing_step: { type: 'string', description: '挂在 CI 哪一步：compileall / ruff / offline tests / e2e / package / platform-smoke' },
  },
}

const FIX_SCHEMA = {
  type: 'object',
  required: ['root_cause', 'fix_applied', 'reverted', 'new_sha'],
  properties: {
    root_cause: { type: 'string', description: '根据 CI 日志定位的真实原因，不是猜的' },
    fix_applied: { type: 'string', description: '这一轮改了什么' },
    reverted: { type: 'boolean', description: '是否需要回滚某条改动（无法在保正确性前提下修好时应回滚）' },
    reverted_what: { type: 'string' },
    local_ci_pass: { type: 'boolean', description: '本地 CI 等价命令是否已复现并通过' },
    new_sha: { type: 'string' },
  },
}

let ciResult = null
const ciHistory = []

for (let round = 1; round <= 3; round++) {
  log(`🔄 CI 第 ${round} 轮，监控 PR #${commit.pr_number}`)

  ciResult = await agent(
    `监控 PR #${commit.pr_number}（分支 ${commit.branch}）的 CI 结果。

仓库 ${REPO}。

步骤：
1. gh run list --branch ${commit.branch} --limit 5   找到本次 run
2. gh run watch <run-id> --exit-status   等它结束（CI 通常 2~3 分钟；矩阵 3 个 python 版本 + 3 个 OS smoke）
   若 watch 超时，改用轮询：每 30s 一次 gh run view <run-id> --json status,conclusion
3. 若失败：gh run view <run-id> --log-failed 抓失败日志。
   **error_log 要粘贴足以定位问题的原文**（含具体 assert / traceback / ruff 规则号），
   不要只写"测试失败"。下游要靠这段日志修，摘要没用。
4. 判定挂在哪一步，填 failing_step。

注意 CI 结构（.github/workflows/ci.yml）：
  job test: python 3.10/3.11/3.12 矩阵 × [compileall, ruff, 离线测试+覆盖率≥55, e2e]
  job package: build + twine check
  job platform-smoke: ubuntu/windows/macos × [import, xenon --version]
CI 里没有任何真 API key（OPENAI_API_KEY=""、DEEPSEEK_API_KEY=""、XENON_ASSUME_YES=1），
新测试若依赖真实 provider 探测会在这里挂——这是最可能的失败原因。
另外 platform-smoke 跑 windows/macos，任何 Linux-only 假设（路径分隔符、strace、/usr/bin/time）
若进了生产代码或被 import 的测试模块，会在这里挂。`,
    { schema: CI_SCHEMA, label: `CI 监控（第 ${round} 轮）`, phase: 'CI', effort: 'medium' }
  )

  if (!ciResult) { log('⚠️  CI 监控无返回'); break }
  ciHistory.push({ round, conclusion: ciResult.conclusion, failing_step: ciResult.failing_step })

  if (ciResult.conclusion === 'success') { log(`✅ CI 通过：${ciResult.run_url}`); break }

  log(`❌ CI ${ciResult.conclusion}，挂在 ${ciResult.failing_step}：${(ciResult.failed_jobs || []).join(', ')}`)
  if (round === 3) { log('⛔ 3 轮仍未过，停止自动修复，交人工。'); break }

  const fix = await agent(
    `CI 挂了，带着日志修。这是第 ${round} 轮修复。

${REJECTED}
${TEST_PROTOCOL}
${MEASURE_PROTOCOL}

── CI 失败信息 ──
挂在步骤：${ciResult.failing_step}
失败 job：${JSON.stringify(ciResult.failed_jobs)}
run: ${ciResult.run_url}
日志原文：
${ciResult.error_log}

── 本次 PR 的改动 ──
${JSON.stringify(impl.implemented, null, 2)}

要求：
1. **先在本地复现**。用日志里的具体命令复现失败，别凭日志猜着改。
   若失败只在 CI 的零 key 环境出现，本地用 env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY -u DEEPSEEK_API_KEY
   加 OPENAI_API_KEY="" 等空值来复现。
2. 定位根因后判断：
   - 能在**不牺牲正确性**的前提下修好 → 修，reverted=false
   - 修不好，或修好的代价是让模型列表退回兜底 / 引入无法重置的状态 →
     **回滚那条改动**（git revert 或直接改回），reverted=true，写清 reverted_what。
     用户明确要求"回滚继续修复"——保住正确性优先于保住那点性能提升。
   - 若回滚后已无任何改动剩下，如实说明，不要硬留一个空改动撑场面。
3. 修完必须本地跑通 CI 四步等价命令（local_ci_pass 才能填 true），已知两项既有失败除外。
4. 若性能相关改动被回滚，重新按计时协议实测一次，拿到诚实的新数字。
5. commit 到同一分支 ${commit.branch} 并 push（沙箱失败就 dangerouslyDisableSandbox）。
   提交信息说明这是修 CI 的第 ${round} 轮，以及回滚了什么。`,
    { schema: FIX_SCHEMA, label: `带日志修复（第 ${round} 轮）`, phase: 'CI', effort: 'high' }
  )

  if (!fix) { log('⚠️  修复无返回，中止循环'); break }
  log(`🔧 根因：${fix.root_cause}`)
  log(`   ${fix.reverted ? `已回滚：${fix.reverted_what}` : `已修：${fix.fix_applied}`} → ${fix.new_sha}`)
  ciHistory[ciHistory.length - 1].fix = fix.reverted ? `回滚 ${fix.reverted_what}` : fix.fix_applied
}

return {
  outcome: ciResult && ciResult.conclusion === 'success' ? 'ci_green' : 'ci_not_green',
  bottleneck_top: (plan.approved || []).map((a) => a.location),
  rejected_proposals: (plan.rejected || []).map((r) => r.proposal),
  out_of_scope: plan.out_of_scope,
  implemented: impl.implemented,
  skipped: impl.skipped,
  perf: verify.improvement,
  before: verify.before_samples,
  after: verify.after_samples,
  model_list_intact: verify.model_list_intact,
  branch: commit.branch,
  pr: commit.pr_url,
  ci: ciResult ? ciResult.conclusion : 'unknown',
  ci_rounds: ciHistory,
  ci_run: ciResult ? ciResult.run_url : null,
}
