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
  SessionReportFrame,
  StatusFrame,
  TurnEndFrame,
} from './protocol.js';

export interface TimelineEntry {
  id: number;
  kind: ActionFrame['kind'];
  /** One-line summary, always safe to show. */
  text: string;
  /** Full kernel text, revealed only when details are expanded. */
  detail: string;
  tone: 'normal' | 'muted' | 'warn' | 'error' | 'success';
}

/** A block in the scrollback: a finished turn, a command's output, or the
 *  session report.  The answer is kept structured so the UI can render it as
 *  Markdown; it is never flattened into the action lines. */
export interface TranscriptBlock {
  id: number;
  kind: 'turn' | 'command' | 'notice' | 'report';
  title: string;
  lines: string[];
  /** Full text behind each line, for Ctrl+O expansion. */
  details?: string[];
  tone: 'normal' | 'muted' | 'warn' | 'error' | 'success';
  /** Turn blocks only: the kernel's final answer, rendered as Markdown. */
  answer?: string;
  metrics?: { steps: number; tool_calls: number };
  debt?: { kind: string; reason: string }[];
  planned?: boolean;
  /** The operator stopped this turn; it failed nothing and delivered nothing. */
  cancelled?: boolean;
  evidence?: string;
  ledger?: string;
  /** Report blocks only: session accounting. */
  report?: SessionReportFrame;
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
  /** Wall-clock start, so the UI can run a local second counter between
   *  progress frames (the kernel only ticks during model calls). */
  startedAt: number;
  /** A cancel was accepted and the kernel has not ended the turn yet. */
  cancelling: boolean;
}

export interface ReadyState {
  models: string[];
  cwd: string;
  setupNeeded: boolean;
  version: string;
  mode: string;
  modeDescription: string;
  modelCount: number;
  loadedModels: number;
  probed: boolean;
  availableProviders: number;
  failures: { name: string; reason: string }[];
}

export interface AppState {
  turn: TurnState;
  transcript: TranscriptBlock[];
  commands: CommandInfo[];
  sessions: SessionInfo[];
  status: StatusFrame | null;
  /** Facts the kernel reported at boot; the welcome card renders them. */
  ready: ReadyState | null;
  /** A question from a command handler, waiting for an answer. */
  prompt: PromptRequestFrame | null;
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
  startedAt: 0,
  cancelling: false,
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

export function reduce(state: AppState, frame: KernelFrame, now = Date.now()): AppState {
  switch (frame.type) {
    case 'ready':
      return {
        ...state,
        ready: {
          models: frame.models,
          cwd: frame.cwd,
          setupNeeded: frame.setup_needed === true,
          version: frame.version ?? '',
          mode: frame.mode ?? '',
          modeDescription: frame.mode_description ?? '',
          modelCount: frame.model_count ?? frame.models.length,
          loadedModels: frame.loaded_models ?? 0,
          probed: frame.probed === true,
          availableProviders: frame.available_providers ?? 0,
          failures: frame.failures ?? [],
        },
      };

    case 'turn.start':
      return {
        ...state,
        turn: {
          ...emptyTurn(),
          objective: frame.objective,
          running: true,
          startedAt: now,
        },
      };

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
              detail: fullAction(frame),
              tone: TONES[frame.kind] ?? 'normal',
            },
          ],
        },
      };

    case 'approval.request':
      return { ...state, turn: { ...state.turn, approval: frame } };

    case 'turn.end': {
      const turn = {
        ...state.turn,
        running: false,
        approval: null,
        cancelling: false,
        end: frame,
      };
      const block: TranscriptBlock = {
        id: nextId++,
        kind: 'turn',
        title: turn.objective,
        lines: turn.entries.map((entry) => entry.text),
        details: turn.entries.map((entry) => entry.detail),
        tone: frame.cancelled
          ? 'muted'
          : frame.error
            ? 'error'
            : frame.evidence?.status === 'verified'
              ? 'success'
              : 'normal',
        answer: frame.cancelled
          ? frame.answer || '本轮已中断'
          : frame.answer || frame.error || '',
        metrics: frame.metrics,
        debt: frame.debt,
        planned: frame.planned === true,
        cancelled: frame.cancelled === true,
        evidence: frame.evidence?.status,
        ledger: frame.ledger,
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
      // the user pressed Ctrl+C between turns.  An accepted one must show,
      // though: the reply can take seconds to die, and until it does the
      // screen would still look like a normal run.
      if (!frame.accepted) return state;
      return { ...state, turn: { ...state.turn, cancelling: true } };

    case 'session.report':
      return {
        ...state,
        transcript: pushBlock(state.transcript, {
          id: nextId++,
          kind: 'report',
          title: '本次会话省钱报告',
          lines: [],
          tone: 'muted',
          report: frame,
        }),
      };

    case 'status': {
      // The terminal polls `status` twice a second while a turn runs, and the
      // dashboard's panels query on top of that.  Most answers are identical
      // down to the field: reducing them to a fresh object made every
      // subscriber repaint (Ink included) for facts that did not change, which
      // is exactly the constant flicker.  Same facts in, same state out.
      //
      // The clocks are the exception that proves it: the kernel ticks them to
      // 0.1 s, so two polls are never *exactly* equal — the bar still only
      // draws whole seconds.  Comparing at the display's precision is what
      // makes "same facts" true for a status answer at all.
      const normalized = normalizeStatus(frame);
      if (sameStatus(state.status, normalized)) return state;
      return { ...state, status: normalized };
    }

    case 'error':
      return {
        ...state,
        error: frame.error,
        turn: {
          ...state.turn,
          entries: [
            ...state.turn.entries,
            { id: nextId++, kind: 'error', text: frame.error, detail: frame.error, tone: 'error' },
          ],
        },
      };

    default:
      return state;
  }
}

/** Round the wall clocks to the second the status bar actually shows.
 *
 *  `elapsed_seconds` and `session_elapsed_seconds` arrive with 0.1 s
 *  resolution, so every poll (2/s while a turn runs) looked like new
 *  information: the whole Ink frame was rewritten for a clock whose visible
 *  value had not moved.  The bar formats whole seconds, so that is the unit
 *  the state compares and stores. */
function normalizeStatus(frame: StatusFrame): StatusFrame {
  const next: StatusFrame = { ...frame };
  if (typeof frame.elapsed_seconds === 'number') {
    next.elapsed_seconds = Math.floor(frame.elapsed_seconds);
  }
  if (typeof frame.session_elapsed_seconds === 'number') {
    next.session_elapsed_seconds = Math.floor(frame.session_elapsed_seconds);
  }
  if (typeof frame.stalled_seconds === 'number') {
    next.stalled_seconds = Math.floor(frame.stalled_seconds);
  }
  return next;
}

/** True when a status frame carries the same facts as the one on screen. */
function sameStatus(
  previous: StatusFrame | null,
  next: StatusFrame,
): boolean {
  if (!previous) return false;
  const before = previous as unknown as Record<string, unknown>;
  const after = next as unknown as Record<string, unknown>;
  const keys = Object.keys(after);
  if (keys.length !== Object.keys(before).length) return false;
  for (const key of keys) {
    const left = before[key];
    const right = after[key];
    if (Array.isArray(left) || Array.isArray(right)) {
      if (JSON.stringify(left) !== JSON.stringify(right)) return false;
    } else if (left !== right) {
      return false;
    }
  }
  return true;
}

function notice(text: string): TranscriptBlock {
  return { id: nextId++, kind: 'notice', title: '', lines: [text], tone: 'muted' };
}

/** Append a front-end notice (no kernel frame behind it: `/dashboard`, a host
 *  error).  One entry point so the scrollback cap stays in one place. */
export function appendNotice(state: AppState, text: string): AppState {
  return { ...state, transcript: pushBlock(state.transcript, notice(text)) };
}

function actionDetail(frame: ActionFrame): string {
  if (frame.kind === 'tool' && frame.tool) {
    const input = frame.input ?? {};
    const target =
      (input.command as string | undefined) ??
      (input.file_path as string | undefined) ??
      (input.path as string | undefined) ??
      '';
    const inputJson =
      target === '' ? JSON.stringify(input) : '';
    return `${frame.tool}${target ? ` · ${target}` : inputJson === '{}' ? '' : ` · ${inputJson}`}`;
  }
  return frame.text ?? '';
}

/** One-line summary for the live timeline (details stay behind Ctrl+O). */
function describeAction(frame: ActionFrame): string {
  return truncate(actionDetail(frame), frame.kind === 'tool' ? 120 : 400);
}

/** The full text behind the summary, revealed when details are expanded. */
function fullAction(frame: ActionFrame): string {
  return actionDetail(frame);
}

function truncate(text: string, limit: number): string {
  const flat = text.replace(/\s+/g, ' ').trim();
  return flat.length > limit ? `${flat.slice(0, limit - 1)}…` : flat;
}

/** Split one input line into a slash command and its arguments. */
/**
 * The welcome card is the empty-session identity, so it survives notices.
 *
 * `/dashboard` (or the `--dashboard` flag) appends a notice in the first
 * second; counting that as "conversation started" hid the version, model count
 * and cwd exactly when a person is looking for them.
 */
export function showWelcomeCard(state: AppState): boolean {
  if (!state.ready) return false;
  return !state.transcript.some((block) => block.kind !== 'notice');
}

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
