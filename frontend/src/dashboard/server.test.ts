/** Local dashboard server: loopback only, token on every route, narrow actions. */

import assert from 'node:assert/strict';
import { request } from 'node:http';
import test from 'node:test';

import type { ApprovalChoice } from '../protocol.js';
import { emptyApp } from '../state.js';
import type { SessionSnapshot } from '../session.js';
import { DashboardServer, dashboardEnabled, QUERY_KINDS, type DashboardController } from './server.js';

class FakeController implements DashboardController {
  readonly calls: string[] = [];
  snapshot: SessionSnapshot = {
    state: emptyApp(),
    commandRunning: null,
    fileResults: null,
  };
  private readonly listeners = new Set<() => void>();

  getSnapshot = (): SessionSnapshot => this.snapshot;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  private emit(): void {
    for (const listener of this.listeners) listener();
  }

  /** Notify without changing the snapshot: the no-op publish the reducer
   *  produces for a query answer or an identical status poll. */
  emitSameState(): void {
    this.emit();
  }

  submit(text: string): void {
    this.calls.push(`submit:${text}`);
    this.snapshot = {
      ...this.snapshot,
      state: { ...this.snapshot.state, turn: { ...this.snapshot.state.turn, objective: text } },
    };
    this.emit();
  }
  runCommand(name: string, args = ''): void {
    this.calls.push(`command:${name}:${args}`);
  }
  answerApproval(choice: ApprovalChoice): void {
    this.calls.push(`approval:${choice}`);
  }
  answerPrompt(value: string): void {
    this.calls.push(`prompt:${value}`);
  }
  cancelTurn(): void {
    this.calls.push('cancel');
  }
  fetchStatus(): void {
    this.calls.push('status');
  }
  query(kind: string, params = {}): Promise<Record<string, unknown>> {
    this.calls.push(`query:${kind}`);
    return Promise.resolve({ type: `${kind}.result`, id: 'q-test', kind, params });
  }
}

interface Running {
  base: string;
  token: string;
  server: DashboardServer;
}

async function start(controller: FakeController): Promise<Running> {
  const server = new DashboardServer({ controller, env: {} });
  const url = await server.start();
  const parsed = new URL(url);
  return {
    base: `${parsed.protocol}//${parsed.host}`,
    token: parsed.searchParams.get('t') ?? '',
    server,
  };
}

async function withServer(
  fn: (controller: FakeController, running: Running) => Promise<void>,
): Promise<void> {
  const controller = new FakeController();
  const running = await start(controller);
  try {
    await fn(controller, running);
  } finally {
    await running.server.close();
  }
}

const post = (running: Running, body: unknown, token = running.token) =>
  fetch(`${running.base}/api/action?t=${encodeURIComponent(token)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: typeof body === 'string' ? body : JSON.stringify(body),
  });

test('the token is required on every route', async () => {
  await withServer(async (_controller, running) => {
    assert.equal((await fetch(`${running.base}/api/state`)).status, 401);
    assert.equal((await fetch(`${running.base}/api/state?t=wrong`)).status, 401);
    assert.equal((await fetch(`${running.base}/?t=${running.token}`)).status, 200);
    assert.equal((await fetch(`${running.base}/`)).status, 401);
  });
});

test('a non-loopback Host header is refused even with the token', async () => {
  await withServer(async (_controller, running) => {
    const status = await new Promise<number>((resolve, reject) => {
      const req = request(`${running.base}/api/state?t=${running.token}`, {
        headers: { Host: 'evil.example' },
      }, (res) => {
        res.resume();
        resolve(res.statusCode ?? 0);
      });
      req.on('error', reject);
      req.end();
    });
    assert.equal(status, 403);
  });
});

test('the page is self-contained and the state endpoint answers', async () => {
  await withServer(async (_controller, running) => {
    const page = await fetch(`${running.base}/?t=${running.token}`);
    assert.equal(page.status, 200);
    assert.match(page.headers.get('content-type') ?? '', /text\/html/);
    const html = await page.text();
    assert.match(html, /Xenon Dashboard/);
    assert.doesNotMatch(html, /src="http/);

    const state = (await (
      await fetch(`${running.base}/api/state?t=${running.token}`)
    ).json()) as { rev: number; state: { transcript: unknown[] } };
    assert.equal(typeof state.rev, 'number');
    assert.equal(state.state.transcript.length, 0);
  });
});

test('actions reach the controller and invalid ones are rejected', async () => {
  await withServer(async (controller, running) => {
    assert.equal((await post(running, { action: 'submit', text: '你好' })).status, 202);
    assert.deepEqual(controller.calls, ['submit:你好']);

    assert.equal((await post(running, { action: 'command', name: '/status' })).status, 202);
    // No-argument commands and Enter-on-default prompts send "".  Rejecting an
    // empty string here made `/cost` work in the terminal but 400 in the
    // browser — the two ends must accept the same answers.
    assert.equal((await post(running, { action: 'command', name: '/cost', args: '' })).status, 202);
    assert.equal((await post(running, { action: 'prompt', value: '' })).status, 202);
    assert.ok(controller.calls.includes('command:/cost:'));
    assert.ok(controller.calls.includes('prompt:'));
    assert.equal((await post(running, { action: 'command', name: 'status' })).status, 400);
    assert.equal((await post(running, { action: 'command', name: '/status', args: 7 })).status, 400);
    assert.equal((await post(running, { action: 'approval', choice: 'allow' })).status, 202);
    assert.equal((await post(running, { action: 'approval', choice: 'maybe' })).status, 400);
    assert.equal((await post(running, { action: 'prompt', value: 'abc' })).status, 202);
    assert.equal((await post(running, { action: 'cancel' })).status, 202);
    assert.equal((await post(running, { action: 'run-shell', command: 'rm -rf /' })).status, 400);
    assert.equal((await post(running, { action: 'submit', text: '' })).status, 400);
    assert.equal((await post(running, 'not json')).status, 400);
    assert.ok(controller.calls.includes('approval:allow'));
  });
});

test('an oversized body is refused before it is buffered', async () => {
  await withServer(async (_controller, running) => {
    const huge = JSON.stringify({ action: 'submit', text: 'x'.repeat(70_000) });
    assert.equal((await post(running, huge)).status, 413);
  });
});

test('query frames are forwarded, and only the known kinds', async () => {
  await withServer(async (controller, running) => {
    const answer = (await (
      await fetch(
        `${running.base}/api/query?t=${running.token}&kind=diff.get&params=${encodeURIComponent(
          JSON.stringify({ limit: 3 }),
        )}`,
      )
    ).json()) as { kind: string; params: unknown };
    assert.equal(answer.kind, 'diff.get');
    assert.deepEqual(answer.params, { limit: 3 });

    const unknown = await fetch(`${running.base}/api/query?t=${running.token}&kind=shell.exec`);
    assert.equal(unknown.status, 400);
    assert.ok(controller.calls.includes('query:diff.get'));
    assert.ok(QUERY_KINDS.has('dashboard.snapshot'));
  });
});

test('SSE sends the current snapshot and then every change', async () => {
  await withServer(async (controller, running) => {
    const abort = new AbortController();
    const response = await fetch(`${running.base}/api/events?t=${running.token}`, {
      signal: abort.signal,
    });
    assert.equal(response.status, 200);
    assert.match(response.headers.get('content-type') ?? '', /text\/event-stream/);
    const reader = (response.body as ReadableStream<Uint8Array>).getReader();
    const decoder = new TextDecoder();
    let text = '';
    const readUntil = async (needle: string): Promise<void> => {
      for (let i = 0; i < 20 && !text.includes(needle); i += 1) {
        const chunk = await Promise.race([
          reader.read(),
          new Promise<'timeout'>((resolve) => setTimeout(() => resolve('timeout'), 3000)),
        ]);
        if (chunk === 'timeout' || chunk.done) break;
        text += decoder.decode(chunk.value);
      }
      assert.ok(text.includes(needle), `expected ${needle} in:\n${text}`);
    };
    await readUntil('event: state');
    assert.match(text, /"commandRunning":null/);

    controller.submit('跑一下测试');
    await readUntil('跑一下测试');
    // Every event is a complete snapshot; Last-Event-ID can only skip, never
    // replay a partial history.
    assert.match(text, /id: 1/);

    abort.abort();
    await reader.cancel().catch(() => undefined);
  });
});

test('a publish that changes nothing neither advances rev nor writes a frame', async () => {
  await withServer(async (controller, running) => {
    const revOf = async (): Promise<number> =>
      ((await (await fetch(`${running.base}/api/state?t=${running.token}`)).json()) as { rev: number }).rev;
    const before = await revOf();
    controller.emitSameState();
    await new Promise((resolve) => setTimeout(resolve, 60));
    assert.equal(await revOf(), before);

    controller.submit('first');
    await new Promise((resolve) => setTimeout(resolve, 60));
    assert.equal(await revOf(), before + 1);
  });
});

test('a reconnect carrying a current Last-Event-ID is not re-sent history', async () => {
  await withServer(async (controller, running) => {
    controller.submit('first');
    await new Promise((resolve) => setTimeout(resolve, 20));
    const status = await new Promise<number>((resolve, reject) => {
      const req = request(`${running.base}/api/events?t=${running.token}`, {
        headers: { 'Last-Event-ID': '99' },
      }, (res) => {
        res.resume();
        res.destroy();
        resolve(res.statusCode ?? 0);
      });
      req.on('error', reject);
      req.end();
    });
    assert.equal(status, 200);
  });
});

test('XENON_DASHBOARD=0 refuses to start, and close makes the port unreachable', async () => {
  const disabled = new DashboardServer({ controller: new FakeController(), env: { XENON_DASHBOARD: '0' } });
  assert.equal(disabled.enabled, false);
  await assert.rejects(disabled.start(), /关闭/);
  assert.equal(dashboardEnabled({}), true);
  assert.equal(dashboardEnabled({ XENON_DASHBOARD: '1' }), true);

  const controller = new FakeController();
  const running = await start(controller);
  await running.server.close();
  await assert.rejects(fetch(`${running.base}/api/state?t=${running.token}`));
});
