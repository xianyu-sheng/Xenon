# DeepSeek prompt-cache TTL（实测）

设计文档 §3.1 的问题：**命中是否会延长缓存寿命？停顿超过 TTL 后是否重新全价？**
第三方路由可能伪造缓存指标，因此这里直接打官方端点（`https://api.deepseek.com`），
使用同一段 512+ token 的固定前缀，读取响应自带的 `prompt_cache_hit_tokens` /
`prompt_cache_miss_tokens`。

## 结果（2026-11-11）

| 实验 | 间隔 | 命中 | 说明 |
|---|---|---|---|
| 预热（`result-20261111-185108.json`） | 3s | 512 / 752 | 第二次调用命中 |
| 预热后等待 | 300s | 512 / 752 | 5 分钟后仍命中 |
| **对照：不预热**（`control-300.json`） | 300s | 512 / 752 | **没有中间命中，5 分钟后仍然命中** |
| **对照：不预热**（`control-900.json`） | 900s | 512 / 752 | **没有中间命中，15 分钟后仍然命中** |

结论（15 分钟尺度）：**DeepSeek 的缓存寿命 ≥ 15 分钟，且不依赖中间是否命中。**
因此 §3.1 里"停顿超过 5 分钟就重新全价"的担心，在 DeepSeek 上**至少对 15 分钟不成立**；
"5 分钟 TTL"是 Anthropic 的语义，不能套到 DeepSeek。

`512 / 752` 不是全量命中：前缀 752 tokens，命中按 64-token 块生效，剩余 240 tokens
（用户消息与块边界）本来就不在缓存块内。比较两次调用要看**命中数是否一致**，而不是
看它是否等于 prompt_tokens。

## 尚未回答

- **命中是否延长 TTL**：需要"不预热、间隔 > TTL"和"预热后同样间隔"两组不同结果才能证明；
  目前 15 分钟对照仍然命中，说明还没跨过 TTL，无法在这个尺度上回答。
- 更长尺度（1 小时）未测；这需要更长的挂机时间，且不改变当前结论的适用范围。

## 复现

```bash
python docs/evidence/cache-ttl/probe.py --gap 300
python docs/evidence/cache-ttl/probe.py --control --gap 900 --out docs/evidence/cache-ttl/control-900.json
```

`--control` 跳过"立即再调一次"，因此测的是**没有中间命中**时的寿命。

## 边界

- 这是 **DeepSeek 官方端点**的行为，不外推到其他厂商；Anthropic 的 TTL 语义不同
  （默认 5 分钟，`cache_control` 可设 1 小时）。
- 结论只覆盖"同一固定前缀在无变更时的命中"；前缀只要有一个字节变化，命中失效。
