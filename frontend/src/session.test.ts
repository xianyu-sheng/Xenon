/** SessionController: one set of rules for every front-end (terminal, dashboard). */

import assert from 'node:assert/strict';
import test from 'node:test';

import type { ApprovalChoice, KernelFrame } from './protocol.js';
import { SessionController, type SessionHost } from './session.js';

class FakeHost implements SessionHost {
  readonly calls: string[] = [];
  runCommand(name: string, args = ''): void {
    this.calls.push(`command:${name}:${args}`);
  }
  startTurn(objective: string): void {
    this.calls.push(`turn:${objective}`);
  }
  answerApproval(id: string, choice: ApprovalChoice): void {
    this.calls.push(`approval:${id}:${choice}`);
  }
  answerPrompt(id: string, value: string): void {
    this.calls.push(`prompt:${id}:${value}`);
  }
  cancelTurn(): void {
    this.calls.push('cancel');
  }
  requestFiles(id: string, query: string): void {
    this.calls.push(`files:${id}:${query}`);
  }
  fetchStatus(): void {
    this.calls.push('status');
  }
  fetchCatalog(): void {
    this.calls.push('catalog');
  }
  shutdown(): void {
    this.calls.push('shutdown');
  }
  query(type: string, id: string): void {
    this.calls.push(`query:${type}:${id}`);
  }
}

const turnStart = (objective: string): KernelFrame =>
  ({ v: 1, type: 'turn.start', objective, ts: 0 }) as unknown as KernelFrame;

const approvalRequest = (id: string): KernelFrame =>
  ({
    v: 1,
    type: 'approval.request',
    id,
    tool: 'shell',
    capability: 'shell',
    risk: 'moderate',
    reason: 'run tests',
    targets: ['pytest'],
    options: ['once', 'deny'],
    ts: 0,
  }) as unknown as KernelFrame;

const promptRequest = (id: string, def = ''): KernelFrame =>
  ({
    v: 1,
    type: 'prompt.request',
    id,
    message: 'name?',
    choices: [],
    default: def,
    secret: false,
    ts: 0,
  }) as unknown as KernelFrame;

test('a plain sentence starts a turn and never runs a command', () => {
  const host = new FakeHost();
  const session = new SessionController(host);
  session.submit('解释一下这段代码');
  assert.deepEqual(host.calls, ['turn:解释一下这段代码']);
  assert.equal(session.getSnapshot().commandRunning, null);
});

test('a slash command is sent verbatim and marked running until it answers', () => {
  const host = new FakeHost();
  const session = new SessionController(host);
  session.submit('/models');
  assert.deepEqual(host.calls, ['command:/models:']);

  // Commands produce no progress frames; the terminal would look frozen.
  assert.equal(session.getSnapshot().commandRunning, '/models');
  session.handleFrame({
    v: 1,
    type: 'command.result',
    name: '/models',
    ok: true,
    text: 'ok',
    ts: 0,
  } as unknown as KernelFrame);
  assert.equal(session.getSnapshot().commandRunning, null);
  // A command result refreshes the status bar so the header cannot go stale.
  assert.ok(host.calls.includes('status'));
});

test('/exit shuts the session down instead of forwarding the command', () => {
  const host = new FakeHost();
  let exited = false;
  const session = new SessionController(host, {
    onExit: () => {
      exited = true;
    },
    shutdownGraceMs: 5,
  });
  session.submit('/exit');
  assert.deepEqual(host.calls, ['shutdown']);
  return new Promise<void>((resolve) => {
    setTimeout(() => {
      assert.ok(exited, 'the terminal must not hang on a silent host');
      resolve();
    }, 30);
  });
});

test('an approval answer clears the card and forwards the choice exactly once', () => {
  const host = new FakeHost();
  const session = new SessionController(host);
  session.handleFrame(turnStart('run tests'));
  session.handleFrame(approvalRequest('a-1'));

  assert.ok(session.getSnapshot().state.turn.approval);
  session.answerApproval('once');
  assert.deepEqual(host.calls, ['approval:a-1:once']);
  assert.equal(session.getSnapshot().state.turn.approval, null);

  // A second choice — a double key press, or the dashboard racing the
  // terminal — must not produce a second response.
  session.answerApproval('deny');
  assert.deepEqual(host.calls, ['approval:a-1:once']);
});

test('a prompt answer sends the typed value, falling back to the default', () => {
  const host = new FakeHost();
  const session = new SessionController(host);
  session.handleFrame(promptRequest('p-1', 'fallback'));
  session.answerPrompt('');
  assert.deepEqual(host.calls, ['prompt:p-1:fallback']);
  assert.equal(session.getSnapshot().state.prompt, null);
});

test('subscribers see every frame, and unsubscribing stops them', () => {
  const host = new FakeHost();
  const session = new SessionController(host);
  let notified = 0;
  const off = session.subscribe(() => {
    notified += 1;
  });
  session.handleFrame(turnStart('one'));
  assert.equal(notified, 1);
  off();
  session.handleFrame(turnStart('two'));
  // The state still advances — only the listener was detached.  A renderer
  // that unsubscribed (unmounted terminal, closed dashboard tab) must not be
  // the reason the session stops tracking the kernel.
  assert.equal(notified, 1);
  assert.equal(session.getSnapshot().state.turn.objective, 'two');
});

test('raw frame listeners receive the protocol frame unchanged', () => {
  const host = new FakeHost();
  const session = new SessionController(host);
  const seen: string[] = [];
  const off = session.onFrame((frame) => seen.push(frame.type));
  session.handleFrame(turnStart('one'));
  off();
  session.handleFrame(turnStart('two'));
  assert.deepEqual(seen, ['turn.start']);
});

test('a non-zero kernel exit becomes a visible error, then exits the app', () => {
  const host = new FakeHost();
  const codes: (number | null)[] = [];
  const session = new SessionController(host, { onExit: (code) => codes.push(code) });
  session.handleHostExit(3);
  assert.equal(codes[0], 3);
  assert.match(session.getSnapshot().state.error, /code 3/);
});

test('a dashboard query resolves on its own id and ignores other results', async () => {
  const host = new FakeHost();
  const session = new SessionController(host);
  const pending = session.query('diff.get', { limit: 5 });

  const call = host.calls.find((entry) => entry.startsWith('query:diff.get:'));
  assert.ok(call, 'the query must reach the host');
  const id = call.split(':').at(-1) as string;

  // A different result frame — same id namespace, different request — must
  // not settle this promise.
  session.handleFrame({ v: 1, type: 'status', ts: 0 } as unknown as KernelFrame);
  session.handleFrame({ v: 1, type: 'files.result', id: 'f-1', paths: [] } as unknown as KernelFrame);
  session.handleFrame({
    v: 1,
    type: 'diff.get.result',
    id,
    files: [{ status: 'M', path: 'a.ts' }],
    diff: '+x',
    ts: 0,
  } as unknown as KernelFrame);

  const result = await pending;
  assert.deepEqual(result.files, [{ status: 'M', path: 'a.ts' }]);
});

test('a query that is never answered becomes a timeout, not a hang', async () => {
  const host = new FakeHost();
  const session = new SessionController(host);
  const result = await session.query('plan.get', {}, 5);
  assert.match(String(result.error), /超时/);
});

test('/dashboard is a front-end command: it opens the panel, not the kernel', async () => {
  const host = new FakeHost();
  const session = new SessionController(host, {
    dashboard: async () => 'http://127.0.0.1:9/?t=secret',
    closeDashboard: () => host.calls.push('dashboard:close'),
  });
  session.submit('/dashboard');
  assert.equal(host.calls.length, 0, 'the kernel must not see /dashboard');

  await new Promise<void>((resolve) => setTimeout(resolve, 0));
  const blocks = session.getSnapshot().state.transcript;
  assert.ok(blocks.some((block) => block.lines.join('\n').includes('127.0.0.1:9')));
  session.shutdown();
  assert.ok(host.calls.includes('dashboard:close'));
});

test('/dashboard without a panel says so instead of failing silently', () => {
  const host = new FakeHost();
  const session = new SessionController(host);
  session.submit('/dashboard');
  const blocks = session.getSnapshot().state.transcript;
  assert.match(blocks.at(-1)?.lines.join('\n') ?? '', /不支持/);
});

test('files.result is kept by request id so a stale reply cannot fill the menu', () => {
  const host = new FakeHost();
  const session = new SessionController(host);
  session.handleFrame({
    v: 1,
    type: 'files.result',
    id: 'f-1',
    paths: ['a.ts'],
    ts: 0,
  } as unknown as KernelFrame);
  assert.deepEqual(session.getSnapshot().fileResults, { id: 'f-1', paths: ['a.ts'] });
  session.clearFileResults();
  assert.equal(session.getSnapshot().fileResults, null);
});

test('text typed while the kernel waits on a question answers that question', () => {
  const host = new FakeHost();
  const session = new SessionController(host);
  session.handleFrame(promptRequest('p-1'));
  // This is exactly what the dashboard used to get wrong: the "/set_model"
  // picker was open, the person typed "11" into the input box, and it ran as a
  // turn against the old model instead of answering the picker.
  session.submit('11');
  assert.deepEqual(host.calls, ['prompt:p-1:11']);
  assert.equal(session.getSnapshot().state.prompt, null);
});

test('a slash command still runs while a question waits', () => {
  const host = new FakeHost();
  const session = new SessionController(host);
  session.handleFrame(promptRequest('p-2'));
  session.submit('/exit');
  assert.deepEqual(host.calls, ['shutdown']);
});

test('query answers and identical status frames do not notify subscribers', () => {
  const host = new FakeHost();
  const session = new SessionController(host);
  let notified = 0;
  session.subscribe(() => { notified += 1; });

  session.handleFrame({ v: 1, type: 'dashboard.snapshot.result', id: 'q-1', cwd: 'D:/x' } as unknown as KernelFrame);
  assert.equal(notified, 0);

  const status = { v: 1, type: 'status', models: [], mode: '单循环', level: 'read', cwd: 'D:/x' } as unknown as KernelFrame;
  session.handleFrame(status);
  session.handleFrame({ ...status });
  assert.equal(notified, 1);
});
