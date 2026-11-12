/**
 * Spawn and drive the Python kernel over NDJSON.
 *
 * The front-end never decides anything about authorization or evidence: it
 * sends user intent, renders what the kernel reports, and answers the one
 * blocking question the kernel asks.
 */

import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { createInterface } from 'node:readline';

import {
  PROTOCOL_VERSION,
  parseFrame,
  type ApprovalChoice,
  type FrontEndMessage,
  type KernelFrame,
} from './protocol.js';

export interface HostOptions {
  /** Python executable; defaults to whatever `python` resolves to. */
  python?: string;
  /** Module path of the host. */
  module?: string;
  models: string[];
  cwd?: string;
  level?: string;
  /** Session to restore at startup (`xenon --resume <序号|名称>`). */
  resume?: string;
  onFrame: (frame: KernelFrame) => void;
  onStderr?: (line: string) => void;
  onExit?: (code: number | null) => void;
}

export class KernelHost {
  private child: ChildProcessWithoutNullStreams;
  private closed = false;

  constructor(private readonly options: HostOptions) {
    const env = { ...process.env };
    // A parent process (an editor, another agent) may export a base URL that
    // hijacks every request.  The kernel must talk to the endpoints in its own
    // models.yaml, so drop the inherited overrides.
    delete env.ANTHROPIC_BASE_URL;
    delete env.ANTHROPIC_API_KEY;
    delete env.ANTHROPIC_AUTH_TOKEN;

    this.child = spawn(
      options.python ?? 'python',
      [
        '-m',
        options.module ?? 'xenon.frontend.host',
        ...options.models.flatMap((m) => ['--model', m]),
        ...(options.resume ? ['--resume', options.resume] : []),
      ],
      {
        cwd: options.cwd ?? process.cwd(),
        env,
        stdio: ['pipe', 'pipe', 'pipe'],
      },
    );

    createInterface({ input: this.child.stdout }).on('line', (line) => {
      const frame = parseFrame(line);
      if (!frame) return;
      if (frame.v !== PROTOCOL_VERSION) {
        this.options.onFrame({
          v: PROTOCOL_VERSION,
          type: 'error',
          error: `protocol version ${frame.v} from kernel; this UI speaks ${PROTOCOL_VERSION}`,
        });
        return;
      }
      this.options.onFrame(frame);
    });

    createInterface({ input: this.child.stderr }).on('line', (line) => {
      this.options.onStderr?.(line);
    });

    this.child.on('exit', (code) => {
      this.closed = true;
      this.options.onExit?.(code);
    });
  }

  get alive(): boolean {
    return !this.closed;
  }

  private send(message: FrontEndMessage): void {
    if (this.closed) return;
    this.child.stdin.write(`${JSON.stringify(message)}\n`);
  }

  startTurn(objective: string): void {
    this.send({
      v: PROTOCOL_VERSION,
      type: 'turn.start',
      objective,
      level: this.options.level ?? 'auto-edit',
    });
  }

  answerApproval(id: string, choice: ApprovalChoice): void {
    this.send({ v: PROTOCOL_VERSION, type: 'approval.response', id, choice });
  }

  answerPrompt(id: string, value: string): void {
    this.send({ v: PROTOCOL_VERSION, type: 'prompt.response', id, value });
  }

  cancelTurn(): void {
    this.send({ v: PROTOCOL_VERSION, type: 'turn.cancel' });
  }

  selectSession(name: string): void {
    this.send({ v: PROTOCOL_VERSION, type: 'session.select', name });
  }

  shutdown(): void {
    if (this.closed) return;
    this.send({ v: PROTOCOL_VERSION, type: 'shutdown' });
    // Everything after this point must be a no-op.  Leaving ``closed`` false
    // until the child's exit event meant any late frame (a pending file
    // request, a status tick) wrote to an ended stdin and crashed Node with
    // ERR_STREAM_WRITE_AFTER_END — the user saw a stack trace on /exit.
    this.closed = true;
    this.child.stdin.end();
    // A host that ignores shutdown must not hold the terminal hostage.
    const timer = setTimeout(() => this.child.kill(), 2000);
    timer.unref();
  }

  runCommand(name: string, args = ''): void {
    this.send({ v: PROTOCOL_VERSION, type: 'command', name, args });
  }

  fetchCatalog(): void {
    this.send({ v: PROTOCOL_VERSION, type: 'catalog' });
  }

  requestFiles(id: string, query: string): void {
    this.send({ v: PROTOCOL_VERSION, type: 'files.request', id, query });
  }

  fetchSessions(): void {
    this.send({ v: PROTOCOL_VERSION, type: 'sessions' });
  }

  fetchStatus(): void {
    this.send({ v: PROTOCOL_VERSION, type: 'status' });
  }
}
