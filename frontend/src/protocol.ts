/**
 * Wire types for the kernel ⇄ front-end protocol.
 *
 * Mirrors docs/design/frontend-protocol.md.  Keep this file dumb: it describes
 * what the kernel sends, nothing about how to render it, and it must never grow
 * a field the Python host does not actually send — a type that lies about the
 * protocol is worse than no type at all.
 */

export const PROTOCOL_VERSION = 1;

/** Approval choices, in the order the panel should offer them. */
export type ApprovalChoice = 'once' | 'turn' | 'allow' | 'deny' | 'cancel';

export interface Envelope {
  v: number;
  turn?: string;
}

export interface ReadyFrame extends Envelope {
  type: 'ready';
  models: string[];
  cwd: string;
  /** True when the kernel found no credentials and no configured models. */
  setup_needed?: boolean;
}

export interface TurnStartFrame extends Envelope {
  type: 'turn.start';
  objective: string;
}

/** Kernel progress while a model call is generating. */
export interface ProgressFrame extends Envelope {
  type: 'progress';
  elapsed_seconds: number;
  chars: number;
}

/** Provider phase: start / retry / end / timeout.  Only the kernel knows
 *  whether this is a first attempt or a retry, so it is forwarded verbatim. */
export interface ProviderFrame extends Envelope {
  type: 'provider';
  kind?: string;
  phase?: string;
  attempt?: number;
  provider_attempts?: number;
  reason?: string;
  [key: string]: unknown;
}

export type ActionKind =
  | 'think'
  | 'tool'
  | 'observe'
  | 'step'
  | 'step_done'
  | 'review'
  | 'error'
  | 'warning'
  | 'tip';

export interface ActionFrame extends Envelope {
  type: 'action';
  kind: ActionKind;
  text?: string;
  tool?: string;
  input?: Record<string, unknown>;
  index?: number;
  status?: string;
  score?: number;
}

/** The one frame that blocks the kernel until the front-end answers. */
export interface ApprovalRequestFrame extends Envelope {
  type: 'approval.request';
  id: string;
  /** "tool" for a tool call, "plan" for a submitted plan. */
  kind?: string;
  capability: string;
  tool: string;
  targets: string[];
  reason: string;
  risk: string;
  diff_preview: string | null;
  /** Untrusted-content advisories attached by the kernel, when any. */
  advisories?: unknown[];
  options: ApprovalChoice[];
}

/** A question a command handler asked; the front-end owns the answer. */
export interface PromptRequestFrame extends Envelope {
  type: 'prompt.request';
  id: string;
  message: string;
  default: string;
  choices: string[];
  secret: boolean;
}

export interface TurnCancelResultFrame extends Envelope {
  type: 'turn.cancel.result';
  accepted: boolean;
}

export interface TurnEndFrame extends Envelope {
  type: 'turn.end';
  phase: string;
  answer: string;
  accepted?: boolean;
  error?: string;
  /** Only on failure frames; a client may retry on `retryable` without
   *  parsing provider prose. */
  error_kind?: string;
  retryable?: boolean;
  ledger?: string;
  /** Computed by the kernel.  The front-end displays it and never recomputes
   *  it — that would be inventing the product's only claim. */
  evidence?: { status: string };
}

export interface ErrorFrame extends Envelope {
  type: 'error';
  error: string;
}

export interface CommandResultFrame extends Envelope {
  type: 'command.result';
  id: string;
  name: string;
  /** Rendered text the Python handler returned. Empty when the command printed
   *  nothing of its own (it may have updated state the status bar reflects). */
  output?: string;
  error?: string;
  /** The command asked to end the session (``/exit``).  Ending is the
   *  front-end's decision — the kernel does not terminate itself. */
  exit?: boolean;
}

export interface CommandInfo {
  name: string;
  summary: string;
  usage: string;
}

export interface CatalogFrame extends Envelope {
  type: 'catalog';
  commands: CommandInfo[];
}

export interface SessionInfo {
  name: string;
  saved_at: string;
  messages: number;
  paradigm: string;
}

export interface SessionsFrame extends Envelope {
  type: 'sessions';
  sessions: SessionInfo[];
  error?: string;
}

export interface StatusFrame extends Envelope {
  type: 'status';
  models: string[];
  mode: string;
  level: string;
  cwd: string;
  /** Live kernel facts; present only while a turn is running. */
  phase?: string;
  steps?: number;
  tool_calls?: number;
  context_tokens?: number;
  context_used_pct?: number;
  elapsed_seconds?: number;
  stalled_seconds?: number;
}

export interface FilesResultFrame extends Envelope {
  type: 'files.result';
  id: string;
  /** Workspace-relative paths, already filtered and bounded by the kernel. */
  paths: string[];
}

export type KernelFrame =
  | ReadyFrame
  | TurnStartFrame
  | ProgressFrame
  | ProviderFrame
  | ActionFrame
  | ApprovalRequestFrame
  | PromptRequestFrame
  | TurnCancelResultFrame
  | TurnEndFrame
  | ErrorFrame
  | CommandResultFrame
  | CatalogFrame
  | SessionsFrame
  | StatusFrame
  | FilesResultFrame;

export interface TurnStartMessage {
  v: number;
  type: 'turn.start';
  objective: string;
  level?: string;
}

export interface ApprovalResponseMessage {
  v: number;
  type: 'approval.response';
  id: string;
  choice: ApprovalChoice;
}

export interface CommandMessage {
  v: number;
  type: 'command';
  name: string;
  args: string;
}

export interface CatalogMessage {
  v: number;
  type: 'catalog';
}

export interface SessionsMessage {
  v: number;
  type: 'sessions';
}

export interface StatusMessage {
  v: number;
  type: 'status';
}

export interface ShutdownMessage {
  v: number;
  type: 'shutdown';
}

export interface TurnCancelMessage {
  v: number;
  type: 'turn.cancel';
}

export interface SessionSelectMessage {
  v: number;
  type: 'session.select';
  name: string;
}

export interface PromptResponseMessage {
  v: number;
  type: 'prompt.response';
  id: string;
  value: string;
}

export interface FilesRequestMessage {
  v: number;
  type: 'files.request';
  id: string;
  /** Substring typed after `@`; the kernel filters workspace-relative paths. */
  query: string;
}

export type FrontEndMessage =
  | TurnStartMessage
  | ApprovalResponseMessage
  | CommandMessage
  | CatalogMessage
  | SessionsMessage
  | StatusMessage
  | ShutdownMessage
  | TurnCancelMessage
  | SessionSelectMessage
  | PromptResponseMessage
  | FilesRequestMessage;

/** Parse one line; returns null for anything that is not a frame we know. */
export function parseFrame(line: string): KernelFrame | null {
  const text = line.trim();
  if (!text) return null;
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    return null;
  }
  if (typeof value !== 'object' || value === null) return null;
  const frame = value as Partial<KernelFrame> & { type?: unknown };
  if (typeof frame.type !== 'string') return null;
  return frame as KernelFrame;
}
