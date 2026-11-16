/**
 * Rendering.  Every fact shown here came from a kernel frame; nothing is
 * inferred, and the evidence line is displayed exactly as reported.
 *
 * Visual parity with the retired Python TUI is deliberate: the runner logo,
 * the welcome card, the token bar, the collapsed `💭` summary and the
 * verification reminders all existed there and are restored here — in Ink,
 * because the Python process no longer owns the screen.
 */

import { Box, Static, Text, Transform, useStdout } from 'ink';
import React, { useEffect, useState } from 'react';

import { linkPaths } from './links.js';
import { parseMarkdown, type InlineSpan } from './markdown.js';
import type {
  ApprovalRequestFrame,
  CommandInfo,
  PromptRequestFrame,
  SessionReportFrame,
  StatusFrame,
  TurnEndFrame,
} from './protocol.js';
import {
  approvalLabel,
  type ReadyState,
  type TimelineEntry,
  type TranscriptBlock,
} from './state.js';
import type { BrandRow } from './brand.js';

const TONE_COLOR: Record<TimelineEntry['tone'], string | undefined> = {
  normal: undefined,
  muted: 'gray',
  warn: 'yellow',
  error: 'red',
  success: 'green',
};

const KIND_GLYPH: Record<TimelineEntry['kind'], string> = {
  think: '·',
  tool: '→',
  observe: ' ',
  step: '#',
  step_done: '✓',
  review: '?',
  error: '✗',
  warning: '!',
  tip: 'i',
};

const SPINNER = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];

/**
 * One line of text whose paths become OSC-8 links *after* layout.
 *
 * Ink measures what it lays out.  When the escape sequences are part of the
 * string handed to `<Text>`, its width pass misreads them: the line is not
 * truncated, the terminal wraps it, and every redraw leaves a stray row behind
 * (a long `open obligation …` message repeated the logo down the screen).
 * `<Transform>` runs on the already-truncated string at write time, so the
 * visible text is measured alone and the zero-width links are added afterwards.
 */
function LinkLine({
  text,
  ...props
}: { text: string } & React.ComponentProps<typeof Text>): React.ReactElement {
  return (
    <Transform transform={(line) => linkPaths(line)}>
      <Text {...props}>{text}</Text>
    </Transform>
  );
}

/** Cycle a spinner glyph while `active`; stops and returns to idle otherwise. */
export function useSpinner(active: boolean, intervalMs = 110): string {
  const [index, setIndex] = useState(0);
  useEffect(() => {
    if (!active) {
      setIndex(0);
      return;
    }
    const timer = setInterval(() => setIndex((value) => (value + 1) % SPINNER.length), intervalMs);
    return () => clearInterval(timer);
  }, [active, intervalMs]);
  return SPINNER[index] ?? SPINNER[0]!;
}

// ── brand ───────────────────────────────────────────────────────────

export function BrandLogo({ rows }: { rows: BrandRow[] }): React.ReactElement {
  return (
    <Box flexDirection="column" marginTop={1} paddingLeft={4}>
      {rows.map((row, index) => (
        <Text key={index}>
          {row.map((segment, column) => (
            <Text
              key={column}
              color={segment.color}
              bold={segment.bold}
              dimColor={segment.dim}
            >
              {segment.text}
            </Text>
          ))}
        </Text>
      ))}
    </Box>
  );
}

/** The card the Python TUI printed after the logo; all facts from `ready`. */
export function WelcomeCard({ ready }: { ready: ReadyState }): React.ReactElement {
  const { stdout } = useStdout();
  const first = ready.models[0];
  const extra = ready.models.length > 1 ? ` +${ready.models.length - 1}` : '';
  const model = first ? `${first}${extra}` : '未配置 — 输入 /setup 开始';
  // Same clamp as the Python panel: never wider than the terminal.
  const columns = stdout?.columns || 80;
  const width = Math.min(76, Math.max(48, columns - 4));
  return (
    <Box
      flexDirection="column"
      borderStyle="round"
      borderColor="#155e75"
      paddingX={2}
      paddingY={1}
      marginTop={1}
      width={width}
    >
      <Text>
        <Text bold color="#67e8f9">{'XENON'}</Text>
        {ready.version ? <Text dimColor>{`  v${ready.version}`}</Text> : null}
      </Text>
      <Text bold color="#f8fafc">
        Your AI coding workspace
      </Text>
      <Text color="#94a3b8">Plan, build, and iterate without leaving the terminal.</Text>
      <Box marginTop={1}>
        {/* One loop, fixed phases.  The internal preset name is not shown:
            it is configuration, not something the user chooses. */}
        <Text color="#94a3b8">{'LOOP    '}</Text>
        <Text bold color="#c4b5fd">
          {ready.mode || '?'}
        </Text>
        {ready.modeDescription ? <Text dimColor>{`  ${ready.modeDescription}`}</Text> : null}
      </Box>
      <Box>
        <Text color="#94a3b8">{'MODEL   '}</Text>
        <Text bold color={first ? 'green' : 'gray'}>
          {model}
        </Text>
      </Box>
      <Box>
        <Text color="#64748b">{'TIP     '}</Text>
        <Text dimColor>{tipFor(ready)}</Text>
      </Box>
    </Box>
  );
}

const CARD_TIPS = [
  '/help 查看命令  ·  /mode 查看循环',
  'Ctrl+O 展开执行详情  ·  Ctrl+C 取消/退出',
  '/setup 配置向导  ·  /tools 查看工具',
];

function tipFor(ready: ReadyState): string {
  // Stable per session: picking at render time would reshuffle on every frame.
  const seed = (ready.version + ready.mode).split('').reduce((sum, ch) => sum + ch.charCodeAt(0), 0);
  return CARD_TIPS[seed % CARD_TIPS.length]!;
}

/** The startup summary line(s): local facts only, never a guessed provider. */
export function StartupSummary({ ready }: { ready: ReadyState }): React.ReactElement {
  const loaded = ready.loadedModels || ready.modelCount;
  return (
    <Box flexDirection="column">
      {ready.setupNeeded ? (
        <Text dimColor>
          {'· 尚未配置 API Key，输入 /setup 进入配置向导'}
        </Text>
      ) : loaded > 0 ? (
        <Text dimColor>
          {ready.probed
            ? `· 已准备 ${loaded} 个模型${ready.availableProviders ? ` · ${ready.availableProviders} 个提供商` : ''} · auto 按任务难度选择`
            : `· 已加载 ${loaded} 个配置模型 · auto 按任务难度选择 · /model 查看完整目录`}
        </Text>
      ) : null}
      {ready.failures.map((failure) => (
        <Text key={failure.name} color="yellow" dimColor>
          {`· ${failure.name} 模型列表不可用：${failure.reason}；已跳过`}
        </Text>
      ))}
    </Box>
  );
}

// ── live turn ───────────────────────────────────────────────────────

export function Timeline({
  entries,
  expanded = false,
}: {
  entries: TimelineEntry[];
  expanded?: boolean;
}): React.ReactElement {
  return (
    <Box flexDirection="column">
      {entries.map((entry) => (
        <LinkLine
          key={entry.id}
          color={TONE_COLOR[entry.tone]}
          wrap={expanded ? 'wrap' : 'truncate-end'}
          text={`  ${KIND_GLYPH[entry.kind]} ${expanded ? entry.detail : entry.text}`}
        />
      ))}
    </Box>
  );
}

// ── answers ─────────────────────────────────────────────────────────

function Inline({ spans }: { spans: InlineSpan[] }): React.ReactElement {
  return (
    <Text>
      {spans.map((span, index) => (
        <Text
          key={index}
          bold={span.bold}
          color={span.code ? '#fde68a' : undefined}
        >
          {span.text}
        </Text>
      ))}
    </Text>
  );
}

/** Markdown answer body: the anchor line the Python TUI used, then the text. */
export function Answer({ text, title = 'Assistant' }: { text: string; title?: string }): React.ReactElement {
  const blocks = parseMarkdown(text);
  return (
    <Box flexDirection="column" marginTop={1}>
      <Text>
        <Text bold color="#67e8f9">{'● '}</Text>
        <Text bold>{title}</Text>
      </Text>
      <Box flexDirection="column" paddingLeft={2}>
        {blocks.map((block, index) => {
          switch (block.kind) {
            case 'code':
              return (
                <Box
                  key={index}
                  flexDirection="column"
                  borderStyle="round"
                  borderColor="#334155"
                  paddingX={1}
                  marginY={0}
                >
                  {block.language ? <Text dimColor>{block.language}</Text> : null}
                  {block.lines.map((line, row) => (
                    <Text key={row} color="#e2e8f0">
                      {line || ' '}
                    </Text>
                  ))}
                </Box>
              );
            case 'heading':
              return (
                <Text key={index} bold color="#cbd5e1">
                  <Inline spans={block.spans} />
                </Text>
              );
            case 'bullet':
              return (
                <Text key={index} wrap="wrap">
                  {`${'  '.repeat(block.depth)}• `}
                  <Inline spans={block.spans} />
                </Text>
              );
            case 'rule':
              return (
                <Text key={index} dimColor>
                  {'─'.repeat(40)}
                </Text>
              );
            default:
              return (
                <Text key={index} wrap="wrap">
                  <Inline spans={block.spans} />
                </Text>
              );
          }
        })}
      </Box>
    </Box>
  );
}

// ── transcript ──────────────────────────────────────────────────────

export function Transcript({
  blocks,
  expanded = false,
}: {
  blocks: TranscriptBlock[];
  expanded?: boolean;
}): React.ReactElement {
  return (
    <Static items={blocks}>
      {(block) => (
        <Box key={block.id} flexDirection="column" marginTop={block.kind === 'turn' ? 1 : 0}>
          {renderBlock(block, expanded)}
        </Box>
      )}
    </Static>
  );
}

/**
 * What the kernel says cannot be auto-verified.
 *
 * Mirrors ``render_verification_reminders``: the debt list when there is one,
 * otherwise the evidence status itself — a turn can be ``unverified`` with no
 * itemised obligation, and that still has to be disclosed.
 */
function verificationItems(block: TranscriptBlock): string[] {
  if (block.debt && block.debt.length > 0) {
    return block.debt.map((item) =>
      item.kind ? `${item.kind}: ${item.reason}` : item.reason,
    );
  }
  // Chat answers carry no task evidence; only a kernel turn (the one that
  // reports run metrics) can be disclosed as partial/unverified.
  const taskTurn = block.metrics !== undefined;
  if (taskTurn && (block.evidence === 'partial' || block.evidence === 'unverified')) {
    return [`证据状态：${block.evidence}`];
  }
  return [];
}

function renderBlock(block: TranscriptBlock, expanded: boolean): React.ReactElement {
  if (block.kind === 'report' && block.report) {
    return <SessionReport report={block.report} />;
  }
  const details = expanded && block.details ? block.details : block.lines;
  return (
    <>
      {block.title ? (
        <Text bold color={block.kind === 'command' ? 'cyan' : undefined}>
          {block.kind === 'command' ? block.title : `❯ ${block.title}`}
        </Text>
      ) : null}
      {details.map((line, index) => (
        // Paths become clickable links so a ledger file or a written source
        // file can be opened without copy-paste.  The links are added by
        // `Transform` after truncation — see `LinkLine`.
        <LinkLine
          key={index}
          color={TONE_COLOR[block.tone]}
          wrap={expanded ? 'wrap' : 'truncate-end'}
          text={block.kind === 'command' ? `  ${line}` : line}
        />
      ))}
      {block.cancelled ? (
        <Text dimColor>{'  ⏹ 本轮已中断（Ctrl+C），未交付任何结论'}</Text>
      ) : null}
      {block.kind === 'turn' && block.metrics && !expanded && !block.cancelled ? (
        <Text dimColor>
          {`  💭 ${block.metrics.steps} 步 · ${block.metrics.tool_calls} 个工具${
            block.evidence ? ` · 证据 ${block.evidence}` : ''
          }`}
        </Text>
      ) : null}
      {block.answer ? <Answer text={block.answer} /> : null}
      {block.planned ? <Text dimColor>{'· 计划已接受，执行等待确认；本轮未写盘。'}</Text> : null}
      {!block.cancelled && verificationItems(block).length > 0 ? (
        // The reason can name every dirty file in the worktree.  Without a
        // width the box stretched past the terminal and its border wrapped
        // into three broken rows; 100% keeps it inside and lets the text wrap.
        <Box
          flexDirection="column"
          borderStyle="round"
          borderColor="yellow"
          paddingX={1}
          marginTop={1}
          width="100%"
        >
          <Text color="yellow" bold>
            {block.evidence === 'unverified'
              ? '⚠️ 降级发布：部分内容无法自动核验，请人工核对'
              : '⚠️ 待人工核验清单'}
          </Text>
          {verificationItems(block).map((line, index) => (
            <Text key={index} color="yellow" wrap="wrap">
              {`- ${line}`}
            </Text>
          ))}
          <Text dimColor wrap="wrap">
            以上项目 Xenon 无法自动验证真伪。答案已照常输出，请自行核对后再信任相应结论。
          </Text>
        </Box>
      ) : null}
      {block.ledger ? (
        <LinkLine dimColor wrap="truncate-end" text={`账本 ${block.ledger}`} />
      ) : null}
    </>
  );
}

/** The exit-time savings panel, drawn from kernel accounting. */
export function SessionReport({ report }: { report: SessionReportFrame }): React.ReactElement {
  const totals = report.totals;
  const rate = totals.cache_hit_pct === null ? 'n/a' : `${totals.cache_hit_pct.toFixed(0)}%`;
  return (
    <Box
      flexDirection="column"
      borderStyle="round"
      borderColor="gray"
      paddingX={1}
      marginTop={1}
      width="100%"
    >
      <Text bold>📊 本次会话省钱报告</Text>
      {report.models.map((row) => {
        const short = row.model.includes('/') ? row.model.split('/').pop()! : row.model;
        const hit = row.cache_hit_pct === null ? 'n/a' : `${row.cache_hit_pct.toFixed(0)}%`;
        return (
          <Text key={row.model} dimColor wrap="truncate-end">
            {`${short}  ${row.calls} 次 · ${row.tokens.toLocaleString()}t · 💾${hit} · 💰¥${row.cost_cny.toFixed(4)}`}
          </Text>
        );
      })}
      <Text>
        {`合计  ${totals.tokens.toLocaleString()} tokens · 💾${rate} · 💰¥${totals.cost_cny.toFixed(4)} · 💡省 ¥${totals.saved_cny.toFixed(4)} (${totals.savings_pct}%)`}
      </Text>
    </Box>
  );
}

// ── panels ──────────────────────────────────────────────────────────

export function ApprovalPanel({
  request,
}: {
  request: ApprovalRequestFrame;
}): React.ReactElement {
  return (
    <Box flexDirection="column" borderStyle="round" borderColor="yellow" paddingX={1}>
      <Text bold>
        {request.kind === 'plan' ? '📋 计划待审批' : `需要授权 · ${request.risk}`}
      </Text>
      <Text>{`能力 ${request.capability}（${request.tool}）`}</Text>
      {request.targets.length > 0 && (
        <Text color="cyan" wrap="truncate-end">{`作用范围 ${request.targets.join('、')}`}</Text>
      )}
      <Text color="gray">{request.reason}</Text>
      {request.advisories && request.advisories.length > 0 && (
        <Text color="yellow">
          {`⚠️ 附带 ${request.advisories.length} 条可疑内容提示（内核标注，非结论）`}
        </Text>
      )}
      {request.diff_preview && (
        <Box flexDirection="column" marginTop={1}>
          {request.diff_preview.split('\n').slice(0, 30).map((line, index) => (
            <Text key={index} color="gray" wrap="truncate-end">
              {line}
            </Text>
          ))}
        </Box>
      )}
      <Box marginTop={1}>
        <Text color="gray">
          {request.options
            .map((choice, index) => `[${index + 1}] ${approvalLabel(choice)}`)
            .join('   ')}
        </Text>
      </Box>
      {/* The input line is hidden while an approval is pending, so a terminal
          user saw a card with no cursor and reported "终端没法输入" — while the
          browser worked.  Say that the digits *are* the input. */}
      <Text color="gray">
        {`⌨ 按数字键 ${request.options.length ? `1-${request.options.length}` : '1-N'} 立即选择，无需回车`}
      </Text>
    </Box>
  );
}

export function EvidenceFooter({ end }: { end: TurnEndFrame }): React.ReactElement {
  const status = end.evidence?.status ?? '未报告';
  const color = status === 'verified' ? 'green' : status === 'unverified' ? 'yellow' : undefined;
  return (
    <Box flexDirection="column" marginTop={1}>
      <Text>
        <Text color={color}>{`证据 ${status}`}</Text>
        {` · ${end.phase}`}
        {end.accepted === undefined ? '' : end.accepted ? ' · 已交付' : ' · 未交付'}
      </Text>
      {end.ledger && <LinkLine color="gray" wrap="truncate-end" text={`账本 ${end.ledger}`} />}
    </Box>
  );
}

export function PromptPanel({
  request,
  value,
}: {
  request: PromptRequestFrame;
  value: string;
}): React.ReactElement {
  const shown = request.secret ? '•'.repeat(value.length) : value;
  return (
    <Box flexDirection="column" borderStyle="round" borderColor="cyan" paddingX={1}>
      <Text bold>{request.message}</Text>
      {request.choices.length > 0 && (
        <Text color="gray">{request.choices.join('  ')}</Text>
      )}
      <Text>{`${shown}█`}</Text>
      <Text color="gray">
        {request.default ? `默认 ${request.default} · ` : ''}Enter 确认
      </Text>
    </Box>
  );
}

export function Completer({
  matches,
  active,
}: {
  matches: CommandInfo[];
  active: number;
}): React.ReactElement | null {
  if (matches.length === 0) return null;
  return (
    <Box flexDirection="column" marginLeft={2}>
      {matches.map((match, index) => (
        <Text
          key={match.name}
          color={index === active ? 'cyan' : 'gray'}
          inverse={index === active}
          wrap="truncate-end"
        >
          {`${match.name}  ${match.summary}`}
        </Text>
      ))}
    </Box>
  );
}

// ── status ──────────────────────────────────────────────────────────

function formatWindow(tokens: number): string {
  if (tokens >= 1_000_000) {
    const value = tokens / 1_000_000;
    return Math.abs(value - Math.round(value)) < 0.05 ? `${Math.round(value)}M` : `${value.toFixed(1)}M`;
  }
  if (tokens >= 1000) return `${Math.round(tokens / 1000)}K`;
  return String(tokens);
}

function tokenBar(pct: number, width = 10): string {
  const filled = Math.max(0, Math.min(width, Math.round((pct / 100) * width)));
  return '█'.repeat(filled) + '░'.repeat(width - filled);
}

/** Session duration, formatted exactly like the Python bar: MM:SS / H:MM:SS. */
function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  if (minutes >= 60) {
    const hours = Math.floor(minutes / 60);
    return `${hours}:${String(minutes % 60).padStart(2, '0')}:${String(rest).padStart(2, '0')}`;
  }
  return `${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}`;
}

/** Live turn line: spinner, kernel phase/step/tool counts, local clock. */
export function StatusLine({
  running,
  phase,
  steps,
  toolCalls,
  elapsedSeconds,
  chars,
  providerNote,
  stalledSeconds,
  cancelling,
}: {
  running: boolean;
  phase?: string;
  steps?: number;
  toolCalls?: number;
  elapsedSeconds: number;
  chars: number;
  providerNote: string;
  stalledSeconds?: number;
  cancelling?: boolean;
}): React.ReactElement {
  const glyph = useSpinner(running);
  if (!running) return <Text>{''}</Text>;
  if (cancelling) {
    return (
      <Text color="yellow">
        {`⏹ 正在中断…  ${elapsedSeconds.toFixed(0)}s`}
      </Text>
    );
  }
  const parts: string[] = [];
  if (providerNote) parts.push(providerNote);
  if (phase) parts.push(phase);
  if (steps !== undefined && steps > 0) parts.push(`step ${steps}`);
  if (toolCalls !== undefined && toolCalls > 0) parts.push(`tool ${toolCalls}`);
  parts.push(`${elapsedSeconds.toFixed(0)}s`);
  if (chars > 0) parts.push(`${chars} 字符`);
  if (stalledSeconds !== undefined && stalledSeconds >= 5) parts.push(`停滞 ${stalledSeconds.toFixed(0)}s`);
  return (
    <Text color="gray">
      {`${glyph} 运行中  ${parts.join(' · ')}`}
    </Text>
  );
}

/**
 * Bottom status bar.  Mirrors the Python toolbar as far as the kernel sends
 * facts: model · mode · level, token bar and window, tool count, message
 * count, duration and stream mode, plus cache/cost when the provider reported
 * them.  Missing facts are omitted, never invented.
 */
export function StatusBar({ status }: { status: StatusFrame | null }): React.ReactElement {
  if (!status) return <Text color="gray">· 正在连接内核…</Text>;
  const model = status.models.join(', ') || '(未设置)';
  const pct =
    status.context_used_pct !== undefined ? status.context_used_pct * 100 : undefined;
  const window = status.max_tokens ? ` ${formatWindow(status.max_tokens)}` : '';
  const live: string[] = [];
  if (status.phase) live.push(status.phase);
  if (status.steps) live.push(`step ${status.steps}`);
  if (status.tool_calls) live.push(`tool ${status.tool_calls}`);
  if (status.stalled_seconds !== undefined && status.stalled_seconds >= 5) {
    live.push(`停滞 ${status.stalled_seconds.toFixed(0)}s`);
  }
  const cache: string[] = [];
  if (status.cache_hit_pct !== undefined) cache.push(`💾${status.cache_hit_pct.toFixed(0)}%`);
  if (status.cost_cny !== undefined) {
    cache.push(`💰${status.cost_cny < 0.01 ? '<0.01' : status.cost_cny.toFixed(2)}`);
  }
  if (status.savings_pct !== undefined) cache.push(`💡${status.savings_pct}%`);
  if (status.needs_compact) cache.push('⚠ /compact');
  const stats = [
    ...cache,
    ...(status.tool_calls ? [`🔧${status.tool_calls}`] : []),
    ...(status.messages !== undefined ? [`${status.messages}m`] : []),
    // The bar shows how long the *session* has been open, like the Python
    // toolbar; the in-turn clock lives on the live status line.
    ...(status.session_elapsed_seconds !== undefined
      ? [formatDuration(status.session_elapsed_seconds)]
      : []),
    ...(status.streaming !== undefined ? [status.streaming ? '⚡' : '⏸'] : []),
  ].join(' · ');
  return (
    <Box flexDirection="column">
      <Text color="gray" wrap="truncate-end">
        {`· ${model}  ·  ${status.mode || '?'}${status.level ? `  ·  ${status.level}` : ''}${
          pct !== undefined ? `  ${tokenBar(pct)} ${pct.toFixed(1)}%${window}` : ''
        }`}
        {live.length > 0 ? `  ·  ${live.join(' · ')}` : ''}
      </Text>
      {stats ? (
        <Text dimColor wrap="truncate-end">
          {stats}
        </Text>
      ) : null}
    </Box>
  );
}
