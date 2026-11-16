/**
 * Session controller — the one place a front-end decides what a keystroke means.
 *
 * The terminal UI renders this state; the local dashboard (Part E) will render
 * the same state from the same object.  Keeping submit/approval/prompt handling
 * here, instead of inside App.tsx, is what makes "both ends drive one session"
 * possible without either end owning a second copy of the rules.
 *
 * The state itself stays a pure `reduce` over kernel frames; this class only
 * sequences the host calls and notifies subscribers.
 */

import type { KernelFrame } from './protocol.js';
import type { ApprovalChoice } from './protocol.js';
import { appendNotice, emptyApp, parseInput, reduce, type AppState } from './state.js';

/** The subset of KernelHost the controller needs; a fake is used in tests. */
export interface SessionHost {
  runCommand(name: string, args?: string): void;
  startTurn(objective: string): void;
  answerApproval(id: string, choice: ApprovalChoice): void;
  answerPrompt(id: string, value: string): void;
  cancelTurn(): void;
  requestFiles(id: string, query: string): void;
  fetchStatus(): void;
  fetchCatalog(): void;
  shutdown(): void;
  query(type: string, id: string, params?: Record<string, unknown>): void;
}

export interface SessionOptions {
  /** Called when the kernel exited or the session was shut down. */
  onExit?: (code: number | null) => void;
  /** Milliseconds to let the session report reach the screen. */
  reportGraceMs?: number;
  /** Grace before exiting when the host never answers `shutdown`. */
  shutdownGraceMs?: number;
  /** Opens the local dashboard and returns its URL, or null when unavailable. */
  dashboard?: () => Promise<string | null>;
  /** Stops the dashboard when the session ends. */
  closeDashboard?: () => void;
}

/** How long a dashboard query may wait before the panel shows a timeout. */
const QUERY_TIMEOUT_MS = 5000;

export interface SessionSnapshot {
  readonly state: AppState;
  /** A command was sent and has not answered yet (kernel sends no progress). */
  readonly commandRunning: string | null;
  /** Latest `files.result`, keyed by request id so stale replies are ignored. */
  readonly fileResults: { id: string; paths: string[] } | null;
}

type Listener = () => void;
type FrameListener = (frame: KernelFrame) => void;

export class SessionController {
  private readonly host: SessionHost;
  private readonly options: SessionOptions;
  private snapshot: SessionSnapshot = {
    state: emptyApp(),
    commandRunning: null,
    fileResults: null,
  };
  private readonly listeners = new Set<Listener>();
  private readonly frameListeners = new Set<FrameListener>();
  private shuttingDown = false;
  private exitTimer: ReturnType<typeof setTimeout> | null = null;
  private querySeq = 0;
  private readonly pendingQueries = new Map<
    string,
    { resolve: (frame: Record<string, unknown>) => void; timer: ReturnType<typeof setTimeout> }
  >();

  constructor(host: SessionHost, options: SessionOptions = {}) {
    this.host = host;
    this.options = options;
  }

  // ── observation ────────────────────────────────────────────────────
  getSnapshot = (): SessionSnapshot => this.snapshot;

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  /** Raw frames, for consumers that need the protocol rather than a render. */
  onFrame = (listener: FrameListener): (() => void) => {
    this.frameListeners.add(listener);
    return () => {
      this.frameListeners.delete(listener);
    };
  };

  private publish(patch: Partial<SessionSnapshot>): void {
    this.snapshot = { ...this.snapshot, ...patch };
    for (const listener of this.listeners) listener();
  }

  private updateState(next: AppState): void {
    // A frame that changes nothing (a query answer, a status poll answering
    // the same numbers, a cancelled cancel) must not wake the renderers.  The
    // reducer returns the same object in those cases on purpose; publishing a
    // new snapshot anyway repainted the terminal and the whole dashboard for
    // every panel query.
    if (next === this.snapshot.state) return;
    this.publish({ state: next });
  }

  // ── kernel frames ──────────────────────────────────────────────────
  handleFrame = (frame: KernelFrame): void => {
    for (const listener of this.frameListeners) listener(frame);
    // A dashboard query answers `<kind>.result` with the id it was sent under.
    // Correlate before reducing: these frames carry panel facts, not session
    // state, and the reducer's default branch would drop them anyway.
    this.resolveQuery(frame);
    this.updateState(reduce(this.snapshot.state, frame));

    // A result or a question ends the "command running" hint: the panel is the
    // new focus, and a result means the work is done.
    if (
      frame.type === 'command.result' ||
      frame.type === 'prompt.request' ||
      frame.type === 'approval.request' ||
      frame.type === 'error' ||
      frame.type === 'turn.start'
    ) {
      if (this.snapshot.commandRunning !== null) {
        this.publish({ commandRunning: null });
      }
    }
    if (frame.type === 'files.result') {
      this.publish({ fileResults: { id: frame.id, paths: frame.paths } });
    }
    // The status bar reflects what the kernel reports, so refresh it whenever
    // a turn or a command could have changed it.
    if (frame.type === 'turn.end' || frame.type === 'command.result') {
      this.host.fetchStatus();
    }
    // The kernel answers `shutdown` with the session report, then exits.  Give
    // the report one frame to reach the terminal before unmounting.
    if (frame.type === 'session.report' && this.exitTimer) {
      clearTimeout(this.exitTimer);
      this.exitTimer = setTimeout(
        () => this.options.onExit?.(null),
        this.options.reportGraceMs ?? 200,
      );
    }
  };

  private resolveQuery(frame: KernelFrame): void {
    if (!frame.type.endsWith('.result')) return;
    const id = (frame as { id?: unknown }).id;
    if (typeof id !== 'string') return;
    const waiter = this.pendingQueries.get(id);
    if (!waiter) return;
    clearTimeout(waiter.timer);
    this.pendingQueries.delete(id);
    waiter.resolve(frame as unknown as Record<string, unknown>);
  }

  handleHostExit = (code: number | null): void => {
    if (code !== 0 && code !== null) {
      this.updateState({
        ...this.snapshot.state,
        error: `内核退出（code ${code}）`,
      });
    }
    if (this.exitTimer) clearTimeout(this.exitTimer);
    this.failPendingQueries('内核已退出');
    this.options.closeDashboard?.();
    this.options.onExit?.(code);
  };

  // ── user actions ───────────────────────────────────────────────────
  submit(text: string): void {
    const parsed = parseInput(text);
    if (!parsed) return;
    // While the kernel is blocked on a question (``/set_model``'s picker,
    // ``/setup``), a line typed into the input box is that question's answer —
    // the terminal is blocked in the prompt, so the box *is* the prompt.  The
    // dashboard used to send it as a new turn: ``turn.start`` was accepted
    // alongside the still-waiting command, the picker stayed open, and the
    // "11" the person typed ran as a task against the old model.
    if (parsed.kind === 'objective' && this.snapshot.state.prompt) {
      this.answerPrompt(parsed.objective);
      return;
    }
    if (parsed.kind === 'command') {
      if (parsed.name === '/exit' || parsed.name === '/quit') {
        this.shutdown();
        return;
      }
      // The dashboard lives in this process (it *is* a second renderer of this
      // controller), so the kernel never sees the command.
      if (parsed.name === '/dashboard') {
        void this.openDashboard();
        return;
      }
      this.publish({ commandRunning: parsed.name });
      this.host.runCommand(parsed.name, parsed.args);
      return;
    }
    this.host.startTurn(parsed.objective);
  }

  /** Fire a command with no "running" hint (Shift+Tab mode cycling). */
  runCommand(name: string, args = ''): void {
    this.host.runCommand(name, args);
  }

  answerApproval(choice: ApprovalChoice): void {
    const request = this.snapshot.state.turn.approval;
    if (!request) return;
    this.host.answerApproval(request.id, choice);
    // The card is gone the moment it is answered; keeping it up would invite a
    // second, meaningless choice.  The kernel's `user_approval` frame follows.
    this.updateState({
      ...this.snapshot.state,
      turn: { ...this.snapshot.state.turn, approval: null },
    });
  }

  answerPrompt(value: string): void {
    const request = this.snapshot.state.prompt;
    if (!request) return;
    this.host.answerPrompt(request.id, value || request.default);
    this.updateState({ ...this.snapshot.state, prompt: null });
  }

  /** Ctrl+C while a turn runs: cooperative cancel (the kernel closes its ledger). */
  cancelTurn(): void {
    this.host.cancelTurn();
  }

  requestFiles(id: string, query: string): void {
    this.host.requestFiles(id, query);
  }

  clearFileResults(): void {
    if (this.snapshot.fileResults !== null) this.publish({ fileResults: null });
  }

  fetchStatus(): void {
    this.host.fetchStatus();
  }

  fetchCatalog(): void {
    this.host.fetchCatalog();
  }

  /** A front-end notice in the scrollback, with no kernel frame behind it. */
  notify(text: string): void {
    this.updateState(appendNotice(this.snapshot.state, text));
  }

  /**
   * Ask the kernel a read-only dashboard question.
   *
   * Resolves with the `<kind>.result` frame, or `{error}` on timeout: a panel
   * must degrade to a message, never hang the session or throw into a caller.
   */
  query(
    kind: string,
    params: Record<string, unknown> = {},
    timeoutMs = QUERY_TIMEOUT_MS,
  ): Promise<Record<string, unknown>> {
    const id = `q-${(this.querySeq += 1)}`;
    return new Promise<Record<string, unknown>>((resolve) => {
      // Not unref'd: the timeout is part of the query's contract, and a timer
      // that does not keep the loop alive resolves into nothing.  Shutdown
      // clears every pending timer instead.
      const timer = setTimeout(() => {
        this.pendingQueries.delete(id);
        resolve({ type: `${kind}.result`, id, error: '查询超时' });
      }, timeoutMs);
      this.pendingQueries.set(id, { resolve, timer });
      this.host.query(kind, id, params);
    });
  }

  private failPendingQueries(reason: string): void {
    for (const [id, waiter] of this.pendingQueries) {
      clearTimeout(waiter.timer);
      waiter.resolve({ id, error: reason });
    }
    this.pendingQueries.clear();
  }

  /** `/dashboard`: start the local web view, or say why it is unavailable. */
  async openDashboard(): Promise<string | null> {
    if (!this.options.dashboard) {
      this.notify('仪表盘需要 TS 前端：当前入口不支持。');
      return null;
    }
    try {
      const url = await this.options.dashboard();
      if (!url) {
        this.notify('仪表盘未启动。');
        return null;
      }
      this.notify(
        `🖥 仪表盘已启动：${url}\n仅监听本机回环地址，终端与浏览器共享同一会话；关闭会话即停止。`,
      );
      return url;
    } catch (error) {
      this.notify(
        `仪表盘启动失败：${error instanceof Error ? error.message : String(error)}`,
      );
      return null;
    }
  }

  shutdown(): void {
    if (this.shuttingDown) return;
    this.shuttingDown = true;
    this.failPendingQueries('会话已结束');
    this.options.closeDashboard?.();
    this.host.shutdown();
    // A host that never answers must not hold the terminal hostage.
    this.exitTimer = setTimeout(
      () => this.options.onExit?.(null),
      this.options.shutdownGraceMs ?? 800,
    );
  }
}
