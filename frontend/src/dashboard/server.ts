/**
 * The local dashboard server (Part E).
 *
 * It is a *second renderer* of the same `SessionController` the terminal uses:
 * full state goes out over SSE, and a small set of actions come back in.  It
 * never spawns a kernel, never reads the ledger itself, and never gains an
 * endpoint that the terminal could not already do — the terminal remains the
 * default entry and the only place a turn can be started from a keyboard.
 *
 * Safety model:
 *   * bound to 127.0.0.1 only (no external interfaces, ever);
 *   * a random per-process token, required on every route;
 *   * a Host-header allow-list, so a DNS-rebinding page cannot reach us;
 *   * no shell/eval endpoint: actions are the same four messages the Ink UI
 *     sends, plus read-only query frames.
 */

import { randomBytes, timingSafeEqual } from 'node:crypto';
import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http';
import type { AddressInfo } from 'node:net';

import type { ApprovalChoice } from '../protocol.js';
import type { SessionController, SessionSnapshot } from '../session.js';
import { DASHBOARD_HTML } from './page.js';

/** The slice of `SessionController` the server drives (a fake in tests). */
export interface DashboardController {
  getSnapshot(): SessionSnapshot;
  subscribe(listener: () => void): () => void;
  submit(text: string): void;
  runCommand(name: string, args?: string): void;
  answerApproval(choice: ApprovalChoice): void;
  answerPrompt(value: string): void;
  cancelTurn(): void;
  fetchStatus(): void;
  query(
    kind: string,
    params?: Record<string, unknown>,
    timeoutMs?: number,
  ): Promise<Record<string, unknown>>;
}

export interface DashboardServerOptions {
  controller: DashboardController;
  /** Bind address; only loopback values are accepted. */
  host?: string;
  /** 0 (default) asks the OS for a free port. */
  port?: number;
  /** Test hook; production always generates a fresh token. */
  token?: string;
  env?: NodeJS.ProcessEnv;
}

/** Read-only query frames the dashboard may ask the kernel for. */
export const QUERY_KINDS: ReadonlySet<string> = new Set([
  'dashboard.snapshot',
  'workspace.status',
  'diff.get',
  'ledger.events',
  'evidence.chain',
  'plan.get',
  'memory.list',
  'mcp.status',
  'report.get',
  'sessions.list',
  'sessions.tree',
]);

const CHOICES: ReadonlySet<string> = new Set(['once', 'turn', 'allow', 'deny', 'cancel']);
const MAX_BODY_BYTES = 65_536;
const MAX_CLIENTS = 16;
const HEARTBEAT_MS = 15_000;
const QUERY_WAIT_MS = 6_000;

/** `XENON_DASHBOARD=0` disables the feature for an environment. */
export function dashboardEnabled(env: NodeJS.ProcessEnv = process.env): boolean {
  return String(env.XENON_DASHBOARD ?? '').trim() !== '0';
}

function safeEqual(a: string, b: string): boolean {
  const left = Buffer.from(a, 'utf8');
  const right = Buffer.from(b, 'utf8');
  return left.length === right.length && timingSafeEqual(left, right);
}

export class DashboardServer {
  private readonly controller: DashboardController;
  private readonly host: string;
  private readonly port: number;
  private readonly env: NodeJS.ProcessEnv;
  private readonly token: string;
  private server: Server | null = null;
  private url: string | null = null;
  private rev = 0;
  private lastPayload = '';
  private lastSignature = '';
  private readonly clients = new Set<ServerResponse>();
  private heartbeat: ReturnType<typeof setInterval> | null = null;
  private unsubscribe: (() => void) | null = null;
  private pendingBroadcast: ReturnType<typeof setTimeout> | null = null;

  constructor(options: DashboardServerOptions) {
    this.controller = options.controller;
    this.host = options.host ?? '127.0.0.1';
    this.port = options.port ?? 0;
    this.env = options.env ?? process.env;
    this.token = options.token ?? randomBytes(16).toString('hex');
  }

  /** The URL a person can open, token included; null before `start()`. */
  get address(): string | null {
    return this.url;
  }

  get enabled(): boolean {
    return dashboardEnabled(this.env);
  }

  async start(): Promise<string> {
    if (this.url) return this.url;
    if (!this.enabled) {
      throw new Error('仪表盘已关闭（XENON_DASHBOARD=0）');
    }
    if (this.host !== '127.0.0.1' && this.host !== 'localhost' && this.host !== '::1') {
      throw new Error(`仪表盘只能监听回环地址，拒绝 ${this.host}`);
    }

    this.rev = 0;
    this.lastSignature = this.signature();
    this.lastPayload = this.serialize();
    this.server = createServer((req, res) => {
      try {
        this.route(req, res);
      } catch (error) {
        this.sendJson(res, 500, { error: error instanceof Error ? error.message : String(error) });
      }
    });
    this.server.on('clientError', (_error, socket) => socket.destroy());

    this.unsubscribe = this.controller.subscribe(() => this.queueBroadcast());
    this.heartbeat = setInterval(() => {
      for (const client of this.clients) client.write(': ping\n\n');
    }, HEARTBEAT_MS);
    this.heartbeat.unref?.();

    await new Promise<void>((resolve, reject) => {
      const server = this.server as Server;
      server.once('error', reject);
      server.listen(this.port, this.host, () => {
        server.off('error', reject);
        resolve();
      });
    });
    const address = this.server.address() as AddressInfo;
    this.url = `http://127.0.0.1:${address.port}/?t=${this.token}`;
    return this.url;
  }

  async close(): Promise<void> {
    for (const client of this.clients) {
      client.end();
    }
    this.clients.clear();
    if (this.heartbeat) {
      clearInterval(this.heartbeat);
      this.heartbeat = null;
    }
    if (this.pendingBroadcast) {
      clearTimeout(this.pendingBroadcast);
      this.pendingBroadcast = null;
    }
    this.unsubscribe?.();
    this.unsubscribe = null;
    const server = this.server;
    this.server = null;
    this.url = null;
    if (!server) return;
    await new Promise<void>((resolve) => {
      server.close(() => resolve());
      // A browser or an HTTP client keeps its socket alive after the last
      // response.  `close()` alone would wait for that idle socket, so quitting
      // xenon would hang on a dashboard tab; the session is ending, so drop
      // them.
      server.closeAllConnections?.();
    });
  }

  // ── state broadcast ────────────────────────────────────────────────
  private signature(): string {
    const snapshot = this.controller.getSnapshot();
    return JSON.stringify({
      state: snapshot.state,
      commandRunning: snapshot.commandRunning,
    });
  }

  private serialize(): string {
    const snapshot = this.controller.getSnapshot();
    return JSON.stringify({
      rev: this.rev,
      state: snapshot.state,
      commandRunning: snapshot.commandRunning,
    });
  }

  /**
   * Coalesce a burst into one frame.
   *
   * A turn emits progress/status/action frames in bursts, and each publish used
   * to write a full snapshot (the whole transcript) to every open tab.  Frames
   * of one burst are rendered once, a frame that changes nothing is not sent,
   * and the revision only advances when a frame actually goes out — so the
   * browser never sees a repaint for facts identical to the ones it has.
   */
  private queueBroadcast(): void {
    if (this.pendingBroadcast) return;
    this.pendingBroadcast = setTimeout(() => {
      this.pendingBroadcast = null;
      this.broadcast();
    }, 16);
    this.pendingBroadcast.unref?.();
  }

  private broadcast(): void {
    const signature = this.signature();
    if (signature === this.lastSignature) return;
    this.lastSignature = signature;
    this.rev += 1;
    this.lastPayload = this.serialize();
    const frame = `id: ${this.rev}\nevent: state\ndata: ${this.lastPayload}\n\n`;
    for (const client of this.clients) client.write(frame);
  }

  // ── routing ────────────────────────────────────────────────────────
  private route(req: IncomingMessage, res: ServerResponse): void {
    // Only a page served from this very port may reach the API.  Together
    // with the token this stops a rebound hostname from driving the session.
    if (!this.hostAllowed(req)) {
      this.sendJson(res, 403, { error: 'forbidden host' });
      return;
    }
    const url = new URL(req.url ?? '/', 'http://127.0.0.1');
    if (!this.authorized(req, url)) {
      this.sendJson(res, 401, { error: 'unauthorized' });
      return;
    }
    const path = url.pathname;

    if (req.method === 'GET' && (path === '/' || path === '/index.html')) {
      res.writeHead(200, {
        'Content-Type': 'text/html; charset=utf-8',
        'Cache-Control': 'no-store',
        'X-Content-Type-Options': 'nosniff',
        'X-Frame-Options': 'DENY',
        'Referrer-Policy': 'no-referrer',
        'Content-Security-Policy':
          "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:",
      });
      res.end(DASHBOARD_HTML);
      return;
    }
    if (req.method === 'GET' && path === '/api/state') {
      this.sendJson(res, 200, this.snapshotJson());
      return;
    }
    if (req.method === 'GET' && path === '/api/events') {
      this.sse(req, res);
      return;
    }
    if (req.method === 'GET' && path === '/api/query') {
      void this.handleQuery(url, res);
      return;
    }
    if (req.method === 'POST' && path === '/api/action') {
      void this.handleAction(req, res);
      return;
    }
    if (path.startsWith('/api/')) {
      this.sendJson(res, req.method === 'GET' || req.method === 'POST' ? 404 : 405, {
        error: 'unknown endpoint',
      });
      return;
    }
    this.sendJson(res, 404, { error: 'not found' });
  }

  private hostAllowed(req: IncomingMessage): boolean {
    const host = String(req.headers.host ?? '');
    if (!host) return false;
    const name = host.startsWith('[') ? host.slice(0, host.indexOf(']') + 1) : host.split(':')[0];
    return name === '127.0.0.1' || name === 'localhost' || name === '::1' || name === '[::1]';
  }

  private authorized(req: IncomingMessage, url: URL): boolean {
    const fromQuery = url.searchParams.get('t') ?? '';
    if (fromQuery && safeEqual(fromQuery, this.token)) return true;
    const header = String(req.headers.authorization ?? '');
    if (header.startsWith('Bearer ')) {
      return safeEqual(header.slice(7), this.token);
    }
    return false;
  }

  private snapshotJson(): Record<string, unknown> {
    const snapshot = this.controller.getSnapshot();
    return {
      rev: this.rev,
      state: snapshot.state,
      commandRunning: snapshot.commandRunning,
      fileResults: snapshot.fileResults,
    };
  }

  // ── SSE ────────────────────────────────────────────────────────────
  private sse(req: IncomingMessage, res: ServerResponse): void {
    if (this.clients.size >= MAX_CLIENTS) {
      this.sendJson(res, 503, { error: 'too many dashboard clients' });
      return;
    }
    res.writeHead(200, {
      'Content-Type': 'text/event-stream; charset=utf-8',
      'Cache-Control': 'no-store',
      Connection: 'keep-alive',
      'X-Accel-Buffering': 'no',
    });
    res.write('retry: 3000\n\n');
    this.clients.add(res);
    // Every event is a complete snapshot, so a reconnect either needs the
    // current one (Last-Event-ID older than now) or nothing at all.  There is
    // no partial history that could be missed or rendered twice.
    //
    // A first connection has no header at all and must always get a snapshot —
    // treating the absent header as `0` skipped it whenever `rev` was still 0,
    // leaving a freshly opened tab blank until something happened.
    const header = req.headers['last-event-id'];
    const last = header === undefined ? Number.NaN : Number.parseInt(String(header), 10);
    if (!Number.isFinite(last) || last < this.rev) {
      res.write(`id: ${this.rev}\nevent: state\ndata: ${this.lastPayload}\n\n`);
    }
    req.on('close', () => {
      this.clients.delete(res);
    });
  }

  // ── queries ────────────────────────────────────────────────────────
  private async handleQuery(url: URL, res: ServerResponse): Promise<void> {
    const kind = url.searchParams.get('kind') ?? '';
    if (!QUERY_KINDS.has(kind)) {
      this.sendJson(res, 400, { error: `unknown query: ${kind}` });
      return;
    }
    let params: Record<string, unknown> = {};
    const raw = url.searchParams.get('params');
    if (raw) {
      try {
        const parsed: unknown = JSON.parse(raw);
        if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
          throw new Error('params must be an object');
        }
        params = parsed as Record<string, unknown>;
      } catch (error) {
        this.sendJson(res, 400, {
          error: `bad params: ${error instanceof Error ? error.message : String(error)}`,
        });
        return;
      }
    }
    const answer = await this.controller.query(kind, params, QUERY_WAIT_MS);
    this.sendJson(res, 200, answer);
  }

  // ── actions ────────────────────────────────────────────────────────
  private async handleAction(req: IncomingMessage, res: ServerResponse): Promise<void> {
    let body: unknown;
    try {
      body = await readJson(req);
    } catch (error) {
      const tooBig = error instanceof Error && error.message === 'body too large';
      this.sendJson(res, tooBig ? 413 : 400, {
        error: error instanceof Error ? error.message : 'bad request',
      });
      return;
    }
    if (typeof body !== 'object' || body === null || Array.isArray(body)) {
      this.sendJson(res, 400, { error: 'body must be a JSON object' });
      return;
    }
    const action = body as Record<string, unknown>;
    const kind = action.action;
    try {
      switch (kind) {
        case 'submit': {
          const text = requireString(action.text, 'text');
          this.controller.submit(text);
          break;
        }
        case 'command': {
          const name = requireString(action.name, 'name');
          if (!name.startsWith('/')) throw new Error('command names start with /');
          // Arguments are optional: most commands take none.  Requiring a
          // non-empty string made every argument-less command (`/cost`,
          // `/status`) fail with 400 when it was sent with `args: ''`, which
          // is exactly what a programmatic or hand-written client does.
          const args = optionalString(action.args, 'args');
          this.controller.runCommand(name, args);
          break;
        }
        case 'approval': {
          const choice = requireString(action.choice, 'choice');
          if (!CHOICES.has(choice)) throw new Error(`unknown choice: ${choice}`);
          this.controller.answerApproval(choice as ApprovalChoice);
          break;
        }
        case 'prompt': {
          // An empty answer is meaningful: the terminal maps it to the
          // prompt's default.  Rejecting it would make Enter-on-default work
          // in the terminal and fail in the browser.
          const value = optionalString(action.value, 'value');
          this.controller.answerPrompt(value);
          break;
        }
        case 'cancel':
          this.controller.cancelTurn();
          break;
        case 'status':
          this.controller.fetchStatus();
          break;
        default:
          this.sendJson(res, 400, { error: `unknown action: ${String(kind)}` });
          return;
      }
    } catch (error) {
      this.sendJson(res, 400, { error: error instanceof Error ? error.message : 'bad action' });
      return;
    }
    this.sendJson(res, 202, { ok: true });
  }

  private sendJson(res: ServerResponse, status: number, payload: unknown): void {
    if (res.headersSent) {
      res.end();
      return;
    }
    const body = JSON.stringify(payload);
    res.writeHead(status, {
      'Content-Type': 'application/json; charset=utf-8',
      'Cache-Control': 'no-store',
      'X-Content-Type-Options': 'nosniff',
      'Content-Length': Buffer.byteLength(body),
    });
    res.end(body);
  }
}

function requireString(value: unknown, name: string): string {
  if (typeof value !== 'string' || value.trim() === '') {
    throw new Error(`${name} must be a non-empty string`);
  }
  return value;
}

/** An optional text field: absent means "", and "" is a value of its own. */
function optionalString(value: unknown, name: string): string {
  if (value === undefined || value === null) return '';
  if (typeof value !== 'string') {
    throw new Error(`${name} must be a string`);
  }
  return value;
}

function readJson(req: IncomingMessage): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    let size = 0;
    req.on('data', (chunk: Buffer) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        // Answer 413 rather than resetting the socket: the client must be able
        // to read why its request was refused.  Stop reading, let the handler
        // write the response, and let `Connection: close` end the connection.
        reject(new Error('body too large'));
        req.pause();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => {
      const text = Buffer.concat(chunks).toString('utf8');
      if (!text.trim()) {
        resolve(null);
        return;
      }
      try {
        resolve(JSON.parse(text));
      } catch {
        reject(new Error('invalid JSON body'));
      }
    });
    req.on('error', reject);
  });
}
