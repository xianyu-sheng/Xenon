/**
 * Rendering.  Every fact shown here came from a kernel frame; nothing is
 * inferred, and the evidence line is displayed exactly as reported.
 */

import { Box, Static, Text } from 'ink';
import React from 'react';

import { linkPaths } from './links.js';
import type {
  ApprovalRequestFrame,
  CommandInfo,
  PromptRequestFrame,
  StatusFrame,
  TurnEndFrame,
} from './protocol.js';
import { approvalLabel, type TimelineEntry, type TranscriptBlock } from './state.js';

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

export function Timeline({ entries }: { entries: TimelineEntry[] }): React.ReactElement {
  return (
    <Box flexDirection="column">
      {entries.map((entry) => (
        <Text key={entry.id} color={TONE_COLOR[entry.tone]} wrap="truncate-end">
          {`  ${KIND_GLYPH[entry.kind]} ${linkPaths(entry.text)}`}
        </Text>
      ))}
    </Box>
  );
}

/**
 * Scrollback.  Rendered through Ink's ``<Static>``: once a block is printed it
 * stays in the terminal's own scrollback and is never redrawn.  A plain
 * ``<Text>`` list is re-rendered on every frame, which silently loses earlier
 * output as soon as the live area above it grows — measured with `/cost`, whose
 * result vanished the moment the next frame arrived.
 */
export function Transcript({ blocks }: { blocks: TranscriptBlock[] }): React.ReactElement {
  return (
    <Static items={blocks}>
      {(block) => (
        <Box key={block.id} flexDirection="column" marginTop={block.kind === 'turn' ? 1 : 0}>
          {block.title ? (
            <Text bold color={block.kind === 'command' ? 'cyan' : undefined}>
              {block.kind === 'command' ? block.title : `❯ ${block.title}`}
            </Text>
          ) : null}
          {block.lines.map((line, index) => (
            <Text key={index} color={TONE_COLOR[block.tone]} wrap="truncate-end">
              {/* Paths become clickable links so a ledger file or a written
                  source file can be opened without copy-paste. */}
              {block.kind === 'command' ? `  ${linkPaths(line)}` : linkPaths(line)}
            </Text>
          ))}
        </Box>
      )}
    </Static>
  );
}

export function ApprovalPanel({
  request,
}: {
  request: ApprovalRequestFrame;
}): React.ReactElement {
  return (
    <Box flexDirection="column" borderStyle="round" borderColor="yellow" paddingX={1}>
      <Text bold>
        {request.kind === 'plan' ? '需要批准 · 计划' : `需要授权 · ${request.risk}`}
      </Text>
      <Text>{`能力 ${request.capability}（${request.tool}）`}</Text>
      {request.targets.length > 0 && (
        <Text color="cyan" wrap="truncate-end">{`作用范围 ${request.targets.join('、')}`}</Text>
      )}
      <Text color="gray">{request.reason}</Text>
      {request.advisories && request.advisories.length > 0 && (
        <Text color="yellow">
          {`· 附带 ${request.advisories.length} 条可疑内容提示（内核标注，非结论）`}
        </Text>
      )}
      {request.diff_preview && (
        <Box flexDirection="column" marginTop={1}>
          {request.diff_preview.split('\n').slice(0, 20).map((line, index) => (
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
      {end.ledger && (
        <Text color="gray" wrap="truncate-end">
          {`账本 ${linkPaths(end.ledger)}`}
        </Text>
      )}
    </Box>
  );
}

export function StatusLine({
  running,
  elapsedSeconds,
  chars,
  providerNote,
}: {
  running: boolean;
  elapsedSeconds: number;
  chars: number;
  providerNote: string;
}): React.ReactElement {
  if (!running) return <Text>{''}</Text>;
  const detail = providerNote || `${elapsedSeconds.toFixed(0)}s · ${chars} 字符`;
  return <Text color="gray">{`· 运行中  ${detail}`}</Text>;
}

export function StatusBar({ status }: { status: StatusFrame | null }): React.ReactElement {
  if (!status) return <Text color="gray">· 正在连接内核…</Text>;
  const model = status.models.join(', ') || '(未设置)';
  const context =
    status.context_used_pct !== undefined
      ? `  ·  context ${(status.context_used_pct * 100).toFixed(1)}%`
      : '';
  const phase = status.phase ? `  ·  ${status.phase}` : '';
  return (
    <Text color="gray" wrap="truncate-end">
      {`· ${model}  ·  ${status.mode || '?'}${status.level ? `  ·  ${status.level}` : ''}${phase}${context}`}
    </Text>
  );
}

/** A question a command handler asked; the front-end owns the answer. */
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
