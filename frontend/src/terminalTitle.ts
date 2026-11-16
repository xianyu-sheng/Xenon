/**
 * Terminal-tab activity, ported from xenon/repl/terminal_activity.py.
 *
 * Terminal emulators have no shared protocol for an animated tab bitmap; they
 * do widely support the window title.  Xenon therefore renders its runner as a
 * fixed-width three-cell block dash:
 *
 *     █·· Xenon  ->  ·█· Xenon  ->  ··█ Xenon
 *
 * Only the title changes — no cursor movement, no body output.  The animation
 * stops whenever Xenon is waiting for a person (approval, prompt, input).
 *
 * This lives in the front-end now: the Python host's stdout is the NDJSON
 * protocol, so it can never write an escape sequence again.
 */

export type ActivityState = 'idle' | 'running' | 'waiting' | 'interrupted' | 'error';

const UNICODE_FRAMES = ['█·· Xenon', '·█· Xenon', '··█ Xenon', '·░█ Xenon', '░█· Xenon'];
const ASCII_FRAMES = ['*.. Xenon', '.*. Xenon', '..* Xenon', '.+* Xenon', '+*. Xenon'];

export interface TerminalTitleOptions {
  enabled?: boolean;
  intervalMs?: number;
  ascii?: boolean;
  /** Injectable for tests: receives the already-sanitized title. */
  write?: (title: string) => void;
}

export class TerminalTitle {
  private readonly frames: string[];
  private readonly intervalMs: number;
  private readonly enabled: boolean;
  private readonly writeTitle: (title: string) => void;
  private readonly originalTitle: string;
  private state: ActivityState = 'idle';
  private frameIndex = 0;
  private timer: NodeJS.Timeout | null = null;
  private closed = false;
  private detail = '';

  constructor(options: TerminalTitleOptions = {}) {
    this.intervalMs = Math.max(80, options.intervalMs ?? 250);
    this.enabled = options.enabled ?? terminalAnimationEnabled();
    this.frames =
      (options.ascii ?? process.env.XENON_TERMINAL_ASCII === '1')
        ? ASCII_FRAMES
        : UNICODE_FRAMES;
    this.writeTitle = options.write ?? writePlatformTitle;
    this.originalTitle = process.platform === 'win32' ? safeTitle() : '';
  }

  get current(): ActivityState {
    return this.state;
  }

  /** Publish the static idle identity (called once at boot). */
  start(): void {
    this.setState('idle');
  }

  setState(state: ActivityState, detail = ''): void {
    if (!this.enabled || this.closed) return;
    this.state = state;
    this.detail = detail;
    this.frameIndex = 0;
    this.stopTimer();
    if (state === 'running') {
      this.emit(this.frames[0]!);
      this.frameIndex = 1;
      this.timer = setInterval(() => {
        if (this.state !== 'running') return;
        this.emit(this.frames[this.frameIndex % this.frames.length]!);
        this.frameIndex += 1;
      }, this.intervalMs);
      this.timer.unref();
      return;
    }
    this.emit(this.staticTitle(state, detail));
  }

  /** Stop the worker and restore the title the shell had before Xenon. */
  close(): void {
    if (this.closed) return;
    this.closed = true;
    this.stopTimer();
    if (this.enabled) {
      this.emit(this.originalTitle || fallbackShellTitle());
    }
  }

  private stopTimer(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }

  private staticTitle(state: ActivityState, detail: string): string {
    if (state === 'idle') return this.frames[0]!;
    const ascii = this.frames === ASCII_FRAMES;
    const marker = state === 'error' ? (ascii ? '!' : '█') : ascii ? '-' : '☆';
    const defaults: Partial<Record<ActivityState, string>> = {
      waiting: '等待确认',
      interrupted: '已中断',
      error: '出错',
    };
    const suffix = sanitize(detail || defaults[state] || '');
    return `${marker} Xenon${suffix ? ` · ${suffix}` : ''}`;
  }

  private emit(title: string): void {
    try {
      this.writeTitle(sanitize(title));
    } catch {
      // A title enhancement must never affect a run.
    }
  }
}

export function terminalAnimationEnabled(): boolean {
  if (process.env.XENON_TERMINAL_ANIMATION === '0') return false;
  if ((process.env.TERM ?? '').toLowerCase() === 'dumb') return false;
  if (process.env.CI) return false;
  return Boolean(process.stdout.isTTY);
}

function sanitize(title: string): string {
  return title
    .replace(/\x1b/g, '')
    .replace(/\x07/g, '')
    .replace(/[\r\n]/g, ' ')
    .slice(0, 96);
}

function safeTitle(): string {
  try {
    return process.title;
  } catch {
    return '';
  }
}

function fallbackShellTitle(): string {
  const directory = process.cwd().split(/[\\/]/).filter(Boolean).pop() ?? '';
  const shell = (process.env.SHELL ?? '').split(/[\\/]/).pop() ?? '';
  if (directory && shell) return `${directory} — ${shell}`;
  return directory || shell || 'Terminal';
}

function writePlatformTitle(title: string): void {
  if (process.platform === 'win32') {
    // libuv maps the process-title setter to SetConsoleTitleW on Windows.
    process.title = title;
    return;
  }
  process.stdout.write(`\x1b]0;${title}\x07`);
}
