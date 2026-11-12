/**
 * Application state: the frames the kernel sent, reduced to what we render.
 *
 * A pure reducer on purpose — it is the part worth testing, and it must never
 * invent anything the kernel did not report.
 *
 * Two kinds of state live here:
 *   * the turn in flight (timeline, approval, evidence), and
 *   * the session shell around it (status bar, command catalog, transcript).
 * They are separate because a command must not disturb a running turn, and a
 * turn must not clear what a command just printed.
 */

import type {
  ActionFrame,
  ApprovalChoice,
  ApprovalRequestFrame,
  CommandInfo,
  KernelFrame,
  PromptRequestFrame,
  SessionInfo,
  StatusFrame,
  TurnEndFrame,
} from './protocol.js';

export interface TimelineEntry {
  id: number;
  kind: ActionFrame['kind'];
  text: string;
  tone: 'normal' | 'muted' | 'warn' | 'error' | 'success';
}

/** A block in the scrollback: either a finished turn or a command's output. */
export interface TranscriptBlock {
  id: number;
  kind: 'turn' | 'command' | 'notice';
  title: string;
  lines: string[];
  tone: 'normal' | 'muted' | 'warn' | 'error' | 'success';
}

export interface TurnState {
  objective: string;
  running: boolean;
  entries: TimelineEntry[];
  approval: ApprovalRequestFrame | null;
  end: TurnEndFrame | null;
  elapsedSeconds: number;
  chars: number;
  providerNote: string;
}

export interface AppState {
  turn: TurnState;
  transcript: TranscriptBlock[];
  commands: CommandInfo[];
  sessions: SessionInfo[];
  status: StatusFrame | null;
  /** A question from a command handler, waiting for an answer. */
  prompt: PromptRequestFrame | null;
  ready: { models: string[]; cwd: string; setupNeeded: boolean } | null;
  error: string;
}

export const emptyTurn = (): TurnState => ({
  objective: '',
  running: false,
  entries: [],
  approval: null,
  end: null,
  elapsedSeconds: 0,
  chars: 0,
  providerNote: '',
});

export const emptyApp = (): AppState => ({
  turn: emptyTurn(),
  transcript: [],
  commands: [],
  sessions: [],
  status: null,
  prompt: null,
  ready: null,
  error: '',
});

const TONES: Record<ActionFrame['kind'], TimelineEntry['tone']> = {
  think: 'muted',
  tool: 'normal',
  observe: 'muted',
  step: 'normal',
  step_done: 'success',
  review: 'normal',
  error: 'error',
  warning: 'warn',
  tip: 'muted',
};

const APPROVAL_LABELS: Record<ApprovalChoice, string> = {
  once: '本次允许',
  turn: '本轮允许（该能力本轮不再问）',
  allow: '全部允许（仅这条命令/路径，30 分钟）',
  deny: '拒绝',
  cancel: '取消整个任务',
};

export const approvalLabel = (choice: ApprovalChoice): string => APPROVAL_LABELS[choice];

let nextId = 1;

/** Cap the scrollback: a long session must not eat memory or redraw cost. */
const MAX_BLOCKS = 200;

function pushBlock(blocks: TranscriptBlock[], block: TranscriptBlock): TranscriptBlock[] {
  const next = [...blocks, block];
  return next.length > MAX_BLOCKS ? next.slice(next.length - MAX_BLOCKS) : next;
}

export function reduce(state: AppState, frame: KernelFrame): AppState {
  switch (frame.type) {
    case 'ready':
      return {
        ...state,
        ready: {
          models: frame.models,
          cwd: frame.cwd,
          setupNeeded: frame.setup_needed === true,
        },
      };

    case 'turn.start':
      return { ...state, turn: { ...emptyTurn(), objective: frame.objective, running: true } };

    case 'progress':
      return {
        ...state,
        turn: {
          ...state.turn,
          elapsedSeconds: frame.elapsed_seconds,
          chars: frame.chars,
        },
      };

    case 'provider': {
      const kind = String(frame.kind ?? '');
      const attempt = frame.attempt ?? 1;
      const total = frame.provider_attempts ?? 1;
      const note =
        kind === 'retry'
          ? `重试 ${attempt}/${total}${frame.reason ? ` · ${frame.reason}` : ''}`
          : kind === 'timeout'
            ? '请求超时'
            : '';
      return note ? { ...state, turn: { ...state.turn, providerNote: note } } : state;
    }

    case 'action':
      return {
        ...state,
        turn: {
          ...state.turn,
          entries: [
            ...state.turn.entries,
            {
              id: nextId++,
              kind: frame.kind,
              text: describeAction(frame),
              tone: TONES[frame.kind] ?? 'normal',
            },
          ],
        },
      };

    case 'approval.request':
      return { ...state, turn: { ...state.turn, approval: frame } };

    case 'turn.end': {
      const turn = { ...state.turn, running: false, approval: null, end: frame };
      const lines: string[] = turn.entries.map((entry) => entry.text);
      if (frame.answer) lines.push(frame.answer);
      if (frame.error) lines.push(frame.error);
      const block: TranscriptBlock = {
        id: nextId++,
        kind: 'turn',
        title: turn.objective,
        lines,
        tone: frame.error ? 'error' : frame.evidence?.status === 'verified' ? 'success' : 'normal',
      };
      return { ...state, turn, transcript: pushBlock(state.transcript, block) };
    }

    case 'command.output': {
      // Printed before a question: show it now, not after the answer.
      const text = frame.output ?? '';
      if (!text.trim()) return state;
      return {
        ...state,
        transcript: pushBlock(state.transcript, {
          id: nextId++,
          kind: 'command',
          title: frame.name,
          lines: text.split('\n'),
          tone: 'normal',
        }),
      };
    }

    case 'command.result': {
      if (frame.exit) {
        return { ...state, transcript: pushBlock(state.transcript, notice('会话已结束')) };
      }
      const text = frame.error ? `错误：${frame.error}` : (frame.output ?? '');
      if (!text.trim()) return state;
      return {
        ...state,
        transcript: pushBlock(state.transcript, {
          id: nextId++,
          kind: 'command',
          title: frame.name,
          lines: text.split('\n'),
          tone: frame.error ? 'error' : 'normal',
        }),
      };
    }

    case 'catalog':
      return { ...state, commands: frame.commands };

    case 'sessions':
      return {
        ...state,
        sessions: frame.sessions,
        error: frame.error ?? state.error,
      };

    case 'prompt.request':
      return { ...state, prompt: frame };

    case 'turn.cancel.result':
      // A cancel that found nothing to cancel is not an error worth showing:
      // the user pressed Ctrl+C between turns.
      return state;

    case 'status':
      return { ...state, status: frame };

    case 'error':
      return {
        ...state,
        error: frame.error,
        turn: {
          ...state.turn,
          entries: [
            ...state.turn.entries,
            { id: nextId++, kind: 'error', text: frame.error, tone: 'error' },
          ],
        },
      };

    default:
      return state;
  }
}

function notice(text: string): TranscriptBlock {
  return { id: nextId++, kind: 'notice', title: '', lines: [text], tone: 'muted' };
}

function describeAction(frame: ActionFrame): string {
  if (frame.kind === 'tool' && frame.tool) {
    const input = frame.input ?? {};
    const detail =
      (input.command as string | undefined) ??
      (input.file_path as string | undefined) ??
      (input.path as string | undefined) ??
      '';
    return `${frame.tool}${detail ? ` · ${truncate(detail, 90)}` : ''}`;
  }
  return truncate(frame.text ?? '', 400);
}

function truncate(text: string, limit: number): string {
  const flat = text.replace(/\s+/g, ' ').trim();
  return flat.length > limit ? `${flat.slice(0, limit - 1)}…` : flat;
}

/** Split one input line into a slash command and its arguments. */
export function parseInput(
  text: string,
): { kind: 'command'; name: string; args: string } | { kind: 'objective'; objective: string } | null {
  const trimmed = text.trim();
  if (!trimmed) return null;
  if (trimmed.startsWith('/')) {
    const space = trimmed.indexOf(' ');
    if (space === -1) return { kind: 'command', name: trimmed, args: '' };
    return {
      kind: 'command',
      name: trimmed.slice(0, space),
      args: trimmed.slice(space + 1).trim(),
    };
  }
  return { kind: 'objective', objective: trimmed };
}

/**
 * The `@path` token at the end of the buffer, if one is being typed.
 *
 * Only the trailing token counts: once the caret has moved past it (a space
 * follows), the completion menu is no longer about that path.
 */
export function activeFileQuery(text: string): { start: number; query: string } | null {
  const match = /@([^\s@]*)$/.exec(text);
  if (!match) return null;
  const query = match[1] ?? '';
  return { start: text.length - query.length - 1, query };
}

/** Replace the active `@token` with a chosen workspace path. */
export function applyFileCompletion(text: string, path: string): string {
  const active = activeFileQuery(text);
  if (!active) return text;
  return `${text.slice(0, active.start)}@${path}`;
}

/** True when every character of `needle` appears in `haystack`, in order. */
function isSubsequence(needle: string, haystack: string): boolean {
  let at = 0;
  for (const char of needle) {
    at = haystack.indexOf(char, at);
    if (at < 0) return false;
    at += 1;
  }
  return true;
}

/**
 * Commands matching a partial input, for the inline completer.
 *
 * Ranked so the palette answers what people actually type: an exact name, then
 * a real prefix, then an abbreviation spelled out of the name ("prm" →
 * permissions).  Ties keep catalog order, so the menu never reshuffles itself.
 */
export function completeCommand(commands: CommandInfo[], prefix: string): CommandInfo[] {
  if (!prefix.startsWith('/')) return [];
  const needle = prefix.slice(1).toLowerCase();
  if (!needle) return commands.slice(0, 8);
  const scored: { rank: number; command: CommandInfo }[] = [];
  for (const command of commands) {
    const name = command.name.slice(1).toLowerCase();
    let rank = 0;
    if (name === needle) rank = 3;
    else if (name.startsWith(needle)) rank = 2;
    else if (isSubsequence(needle, name)) rank = 1;
    if (rank > 0) scored.push({ rank, command });
  }
  scored.sort((a, b) => b.rank - a.rank);
  return scored.slice(0, 8).map((entry) => entry.command);
}
