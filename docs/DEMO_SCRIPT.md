# Xenon 90 秒演示视频分镜脚本

> 目标：让观众在 90 秒内看懂一句话——**模型可以提案，但只有证据才能交付。**
>
> 核心剧情：模型声称“测试全部通过”，Xenon 因找不到 TestRun 回执拒绝交付；
> 真实执行并通过验证后，`Finalizer` 才提交。
>
> 分发适配：X / 知乎 / B 站 / Reddit / Hacker News。竖屏（9:16）切短视频平台，
> 横屏（16:9）切 B 站 / YouTube / HN。字幕全程硬字幕，默认静音可看。

---

## 录制前准备

**方案 A：确定性录制（推荐，零网络、可重复、不会翻车）**

```bash
# 1. 准备环境
python -m venv .venv && .venv/Scripts/python -m pip install -e .   # Windows
# source .venv/bin/activate && pip install -e .                    # Linux/macOS

# 2. 先跑一遍确认输出，作为分镜素材
python -m evals.false_completion
pytest tests/test_false_completion_benchmark.py -q
```

终端录屏工具：Windows 推荐 OBS / ScreenToGif，macOS 自带录屏（Cmd+Shift+5），
跨平台可用 [asciinema](https://asciinema.org) + [agg](https://github.com/asciinema/agg) 生成 GIF。
终端字体调大到 16pt 以上，暗色主题，窗口宽度 100 列左右。

**方案 B：真实运行录制（需要 API Key，更有现场感）**

提前在 `~/.xenon/credentials.yaml` 配好 Key，准备一个带测试的最小仓库，
设计一个“模型容易口头宣称完成”的小任务（如修复一个失败的单测）。先完整彩排
2–3 次，确认真实输出节奏与分镜一致，再正式录。

---

## 分镜表（总时长 90 秒）

| # | 时长 | 画面 | 口播 / 硬字幕 |
|---|---|---|---|
| 1 | 0–8s | 黑底大字，逐行打出：“你的 AI 代理说：『已修复，测试全部通过。』” | 字幕：**它真的跑测试了吗？** |
| 2 | 8–18s | 终端里普通代理输出绿色“Done / tests passed”，随后手动跑测试，满屏红色 FAIL | 口播：很多代理的“通过”，只是模型的一句话，不是事实。 |
| 3 | 18–28s | Xenon Logo + 定位语定格 | 字幕：**Xenon——不会把“测试通过”当成事实的可验证 AI 编码代理。** |
| 4 | 28–40s | 启动 Xenon，模型输出 capability action；旁白高亮 `claim: tests passed` | 口播：模型可以提案，但每个声明都必须绑定真实回执。 |
| 5 | 40–55s | 画面定格在 controller 输出：`false_test_claim → blocked`，高亮“无 TestRun receipt” | 字幕：**没有回执，就是不能交付。** 口播：零匹配、多匹配、无法验证，分别记为 unresolved、ambiguous、abstained，从不猜。 |
| 6 | 55–70s | 模型重新执行真实测试，终端出现真实通过结果，生成 typed receipt，V0/V1/V2 逐项打勾 | 口播：真实验证通过后，回执进入哈希链账本。 |
| 7 | 70–82s | `Finalizer` 执行 preflight audit → commit，输出 commit sha；若审计失败则显示自动回滚 | 口播：交付提交由 Finalizer 独占，审计不过自动回滚。 |
| 8 | 82–90s | 收尾卡片：定位语 + GitHub 地址 + Star 按钮示意 | 字幕：**模型可以提案，但只有证据才能交付。** `github.com/xianyu-sheng/Xenon` |

---

## 口播完整版（约 220 字，可直接录音）

> 你的 AI 代理说“已修复，测试全部通过”——但它真的跑了吗？
> 很多代理的“通过”只是模型的一句话。Xenon 是一个不会把“测试通过”当成事实的
> 可验证 AI 编码代理。模型可以提案，但每个声明都必须绑定真实回执。
> 当它声称测试通过、系统里却没有 TestRun 回执时，Xenon 直接阻断交付。
> 直到测试被真实执行、三层验证逐项通过，回执进入哈希链账本，
> Finalizer 才会生成提交；审计不过，就自动回滚。
> 模型可以提案，但只有证据才能交付。项目开源，地址在屏幕上。

---

## 录制注意事项

- **不要演示虚构输出。** 所有终端画面必须来自真实命令，失败回放也保留真实报错。
- 方案 A 的 `false_test_claim` 用例对应剧情第 5 镜；9 例基准的完整口径见
  [false-completion 证据](evidence/false-completion/README.md)。
- 封面图建议直接用分镜 1 的大字 + 分镜 5 的红色 `blocked`，冲突感最强。
- 发布时仓库链接置顶评论；英文版本把口播中的术语固定为
  `evidence receipts / fail-closed verification / replayable ledger`。
- 若后续录制了正式视频，把视频链接回填到 README 的“工作演示”小节替换本脚本链接。
