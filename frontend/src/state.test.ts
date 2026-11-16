/**
 * Reducer tests.  This is the part with real logic, so it is the part worth
 * asserting on: a UI that drops or invents a frame is worse than one that
 * renders plainly.
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { parseFrame, type KernelFrame } from './protocol.js';
import {
  activeFileQuery,
  applyFileCompletion,
  completeCommand,
  emptyApp,
  parseInput,
  reduce,
  showWelcomeCard,
  type AppState,
} from './state.js';

const feed = (frames: KernelFrame[], start: AppState = emptyApp()): AppState =>
  frames.reduce(reduce, start);

test('a turn starts clean even if the previous one left entries', () => {
  const dirty = feed([
    { v: 1, type: 'turn.start', objective: 'first' },
    { v: 1, type: 'action', kind: 'tool', tool: 'bash', input: { command: 'ls' } },
  ]);
  assert.equal(dirty.turn.entries.length, 1);

  const next = reduce(dirty, { v: 1, type: 'turn.start', objective: 'second' });
  assert.equal(next.turn.objective, 'second');
  assert.equal(next.turn.running, true);
  assert.deepEqual(next.turn.entries, []);
  assert.equal(next.turn.end, null);
});

test('a finished turn is appended to the scrollback', () => {
  const state = feed([
    { v: 1, type: 'turn.start', objective: 'do it' },
    { v: 1, type: 'action', kind: 'tool', tool: 'bash', input: { command: 'ls' } },
    {
      v: 1,
      type: 'turn.end',
      phase: 'finalized',
      answer: 'done',
      accepted: true,
      evidence: { status: 'verified' },
    },
  ]);

  assert.equal(state.transcript.length, 1);
  const block = state.transcript[0];
  assert.equal(block?.title, 'do it');
  assert.equal(block?.tone, 'success');
  // The answer stays structured so the UI can render it as Markdown; the
  // action lines stay separate and never absorb it.
  assert.equal(block?.answer, 'done');
  assert.equal(block?.lines.includes('done'), false);
  assert.equal(block?.lines.length, 1);
  assert.equal(block?.details?.length, 1);
});

test('a tool action names the tool and what it touches', () => {
  const state = feed([
    { v: 1, type: 'turn.start', objective: 'x' },
    { v: 1, type: 'action', kind: 'tool', tool: 'bash', input: { command: 'ls -la' } },
  ]);

  assert.equal(state.turn.entries[0]?.text, 'bash · ls -la');
});

test('the evidence footer shows what the kernel reported, unchanged', () => {
  const state = feed([
    { v: 1, type: 'turn.start', objective: 'x' },
    {
      v: 1,
      type: 'turn.end',
      phase: 'finalized',
      answer: 'ok',
      accepted: true,
      evidence: { status: 'verified' },
    },
  ]);

  assert.equal(state.turn.running, false);
  assert.equal(state.turn.end?.evidence?.status, 'verified');
});

test('the stale evidence footer does not survive into the next turn', () => {
  const finished = feed([
    { v: 1, type: 'turn.start', objective: 'x' },
    { v: 1, type: 'turn.end', phase: 'finalized', answer: 'ok', evidence: { status: 'verified' } },
  ]);
  const next = reduce(finished, { v: 1, type: 'turn.start', objective: 'y' });

  assert.equal(next.turn.end, null);
  assert.equal(next.turn.running, true);
});

test('an approval clears when the turn ends', () => {
  const requested = feed([
    { v: 1, type: 'turn.start', objective: 'x' },
    {
      v: 1,
      type: 'approval.request',
      id: 'a-1',
      capability: 'shell',
      tool: 'bash',
      targets: ['ls'],
      reason: 'x',
      risk: 'CRITICAL',
      diff_preview: null,
      options: ['once', 'turn', 'allow', 'deny', 'cancel'],
    },
  ]);
  assert.ok(requested.turn.approval);

  const ended = reduce(requested, { v: 1, type: 'turn.end', phase: 'failed', answer: '' });
  assert.equal(ended.turn.approval, null);
});

test('a provider retry becomes a note, not a timeline entry', () => {
  const state = feed([
    { v: 1, type: 'turn.start', objective: 'x' },
    { v: 1, type: 'provider', kind: 'retry', attempt: 2, provider_attempts: 3, reason: '429' },
  ]);

  assert.equal(state.turn.providerNote, '重试 2/3 · 429');
  assert.equal(state.turn.entries.length, 0);
});

test('progress updates the counter without touching the timeline', () => {
  const state = feed([
    { v: 1, type: 'turn.start', objective: 'x' },
    { v: 1, type: 'progress', elapsed_seconds: 3.5, chars: 120 },
  ]);

  assert.equal(state.turn.elapsedSeconds, 3.5);
  assert.equal(state.turn.chars, 120);
  assert.equal(state.turn.entries.length, 0);
});

test('a command result lands in the scrollback, not the turn', () => {
  const state = feed([
    { v: 1, type: 'command.result', id: 'c-1', name: '/permissions', output: '执行级别: write' },
  ]);

  assert.equal(state.transcript.length, 1);
  assert.equal(state.transcript[0]?.kind, 'command');
  assert.equal(state.transcript[0]?.title, '/permissions');
  assert.deepEqual(state.transcript[0]?.lines, ['执行级别: write']);
  assert.equal(state.turn.entries.length, 0);
});

test('a command that printed nothing adds no block', () => {
  const state = feed([{ v: 1, type: 'command.result', id: 'c-1', name: '/clear', output: '' }]);
  assert.equal(state.transcript.length, 0);
});

test('output printed before a question is shown before the answer', () => {
  const state = feed([
    { v: 1, type: 'command.output', id: 'c-1', name: '/set_model', output: '1  anthropic\n2  deepseek' },
    { v: 1, type: 'command.result', id: 'c-1', name: '/set_model', output: '✅ 已切换' },
  ]);

  assert.equal(state.transcript.length, 2);
  assert.deepEqual(state.transcript[0]?.lines, ['1  anthropic', '2  deepseek']);
  assert.deepEqual(state.transcript[1]?.lines, ['✅ 已切换']);
});

test('a failing command is shown as an error block', () => {
  const state = feed([
    { v: 1, type: 'command.result', id: 'c-1', name: '/nope', error: '无此命令' },
  ]);

  assert.equal(state.transcript[0]?.tone, 'error');
  assert.ok(state.transcript[0]?.lines[0]?.includes('无此命令'));
});

test('the catalog and status frames populate their slices', () => {
  const state = feed([
    { v: 1, type: 'catalog', commands: [{ name: '/help', summary: 'h', usage: '' }] },
    { v: 1, type: 'status', models: ['m'], mode: 'direct', level: 'write', cwd: '/x' },
  ]);

  assert.equal(state.commands.length, 1);
  assert.equal(state.status?.mode, 'direct');
});

test('an unknown frame type is ignored rather than crashing the view', () => {
  const state = reduce(emptyApp(), { v: 1, type: 'something.new' } as unknown as KernelFrame);
  assert.deepEqual(state.turn.entries, []);
});

test('parseFrame rejects junk instead of pretending it is a frame', () => {
  assert.equal(parseFrame(''), null);
  assert.equal(parseFrame('not json'), null);
  assert.equal(parseFrame('{"nope":1}'), null);
  assert.equal(parseFrame('{"type":"ready","v":1,"models":[],"cwd":"/x"}')?.type, 'ready');
});

// ── input parsing ───────────────────────────────────────────────────

test('a slash input becomes a command with its arguments', () => {
  assert.deepEqual(parseInput('/model gpt-4o'), {
    kind: 'command',
    name: '/model',
    args: 'gpt-4o',
  });
  assert.deepEqual(parseInput('/help'), { kind: 'command', name: '/help', args: '' });
});

test('anything else is an objective', () => {
  assert.deepEqual(parseInput('修一下这个 bug'), {
    kind: 'objective',
    objective: '修一下这个 bug',
  });
  assert.equal(parseInput('   '), null);
});

test('completion only matches slash input', () => {
  const commands = [
    { name: '/model', summary: 'm', usage: '' },
    { name: '/mode', summary: 'd', usage: '' },
    { name: '/help', summary: 'h', usage: '' },
  ];

  assert.equal(completeCommand(commands, 'model').length, 0);
  assert.deepEqual(
    completeCommand(commands, '/mo').map((c) => c.name),
    ['/model', '/mode'],
  );
  assert.equal(completeCommand(commands, '/zzz').length, 0);
});

test('an abbreviation spelled out of the name still matches', () => {
  const commands = [
    { name: '/permissions', summary: 'p', usage: '' },
    { name: '/plan', summary: 'l', usage: '' },
    { name: '/model', summary: 'm', usage: '' },
  ];

  assert.deepEqual(
    completeCommand(commands, '/prm').map((c) => c.name),
    ['/permissions'],
  );
});

test('an exact or prefix hit outranks an abbreviation, ties keep catalog order', () => {
  const commands = [
    { name: '/verify', summary: 'v', usage: '' },
    { name: '/version', summary: 'n', usage: '' },
    { name: '/evidence', summary: 'e', usage: '' },
  ];

  // "ev" only matches /evidence: /verify has no "v" after the "e".
  assert.deepEqual(
    completeCommand(commands, '/ev').map((c) => c.name),
    ['/evidence'],
  );
  // "ve" is a prefix of /verify + /version, and only an abbreviation inside
  // /evidence — prefix hits come first, catalog order keeps the tie stable.
  assert.deepEqual(
    completeCommand(commands, '/ve').map((c) => c.name),
    ['/verify', '/version', '/evidence'],
  );
  // Both are plain prefixes: the catalog order is preserved.
  assert.deepEqual(
    completeCommand(commands, '/ver').map((c) => c.name),
    ['/verify', '/version'],
  );
  // The exact name only matches itself here ("verify" has no "s").
  assert.deepEqual(
    completeCommand(commands, '/version').map((c) => c.name),
    ['/version'],
  );
});

test('the ready frame carries the first-run hint', () => {
  const state = feed([
    { v: 1, type: 'ready', models: [], cwd: '/x', setup_needed: true },
  ]);

  assert.equal(state.ready?.setupNeeded, true);
  assert.deepEqual(state.ready?.models, []);
});

test('a ready frame without the flag leaves the hint off', () => {
  const state = feed([{ v: 1, type: 'ready', models: ['m'], cwd: '/x' }]);

  assert.equal(state.ready?.setupNeeded, false);
});

// ── @path completion ────────────────────────────────────────────────

test('the trailing @token is the file query', () => {
  assert.deepEqual(activeFileQuery('看看 @src/ma'), { start: 3, query: 'src/ma' });
  assert.deepEqual(activeFileQuery('@'), { start: 0, query: '' });
});

test('a finished path no longer counts as a query', () => {
  assert.equal(activeFileQuery('看看 @src/main.py 的写法'), null);
  assert.equal(activeFileQuery('no at sign here'), null);
});

test('completing replaces only the active @ token', () => {
  assert.equal(
    applyFileCompletion('看看 @src/ma', 'src/main.py'),
    '看看 @src/main.py',
  );
  assert.equal(applyFileCompletion('plain text', 'src/main.py'), 'plain text');
});

// ── welcome / results / report ──────────────────────────────────────

test('the ready frame carries the welcome-card facts, not just the hint', () => {
  const state = feed([
    {
      v: 1,
      type: 'ready',
      models: ['deepseek-v4-pro', 'deepseek-v4-flash'],
      cwd: 'D:\\proj',
      setup_needed: false,
      version: '1.0.0',
      mode: '单循环',
      mode_description: 'Plan → React（逐步） → Reflect（每步校验）',
      model_count: 2,
      loaded_models: 2,
      probed: false,
      available_providers: 0,
      failures: [{ name: 'OpenAI', reason: '认证失败' }],
    },
  ]);

  assert.equal(state.ready?.version, '1.0.0');
  assert.equal(state.ready?.modeDescription, 'Plan → React（逐步） → Reflect（每步校验）');
  assert.equal(state.ready?.loadedModels, 2);
  assert.equal(state.ready?.failures[0]?.name, 'OpenAI');
});

test('a turn end carries the run counters and undischarged obligations', () => {
  const state = feed([
    { v: 1, type: 'turn.start', objective: 'x' },
    {
      v: 1,
      type: 'turn.end',
      phase: 'finalized',
      answer: 'ok',
      accepted: true,
      evidence: { status: 'partial' },
      ledger: 'C:\\ledger\\task.jsonl',
      metrics: { steps: 3, tool_calls: 2 },
      debt: [{ kind: 'test_passed', reason: '测试未运行' }],
      planned: false,
    },
  ]);

  const block = state.transcript[0];
  assert.deepEqual(block?.metrics, { steps: 3, tool_calls: 2 });
  assert.equal(block?.debt?.[0]?.reason, '测试未运行');
  assert.equal(block?.evidence, 'partial');
  assert.equal(block?.ledger, 'C:\\ledger\\task.jsonl');
  assert.equal(block?.planned, false);
});

test('the session report lands in the scrollback as its own block', () => {
  const state = feed([
    {
      v: 1,
      type: 'session.report',
      models: [
        { model: 'deepseek/v4', calls: 2, tokens: 1200, cache_hit_pct: 61, cost_cny: 0.03 },
      ],
      totals: {
        calls: 2,
        tokens: 1200,
        cost_cny: 0.03,
        saved_cny: 0.01,
        savings_pct: 25,
        cache_hit_pct: 61,
      },
    },
  ]);

  assert.equal(state.transcript.length, 1);
  assert.equal(state.transcript[0]?.kind, 'report');
  assert.equal(state.transcript[0]?.report?.totals.savings_pct, 25);
});

test('a cancelled turn reads as stopped, not as an empty failure', () => {
  const state = feed([
    { v: 1, type: 'turn.start', objective: '写一段散文' },
    { v: 1, type: 'turn.cancel.result', accepted: true },
    { v: 1, type: 'turn.end', phase: 'aborted', answer: '', cancelled: true },
  ]);

  const block = state.transcript[0];
  assert.equal(state.turn.cancelling, false);
  assert.equal(block?.cancelled, true);
  assert.equal(block?.tone, 'muted');
  assert.equal(block?.answer, '本轮已中断');
});

test('an accepted cancel marks the running turn as stopping', () => {
  const state = feed([
    { v: 1, type: 'turn.start', objective: 'x' },
    { v: 1, type: 'turn.cancel.result', accepted: true },
  ]);
  assert.equal(state.turn.cancelling, true);
});

test('a cancel with nothing to stop changes nothing', () => {
  const state = reduce(emptyApp(), { v: 1, type: 'turn.cancel.result', accepted: false });
  assert.equal(state.turn.cancelling, false);
  assert.equal(state.transcript.length, 0);
});

test('a turn records when it started so the clock can run locally', () => {
  const state = reduce(emptyApp(), { v: 1, type: 'turn.start', objective: 'x' }, 1234);
  assert.equal(state.turn.startedAt, 1234);
});

test('an expanded action keeps the full text, not just the summary', () => {
  const long = 'x'.repeat(500);
  const state = feed([
    { v: 1, type: 'turn.start', objective: 'x' },
    { v: 1, type: 'action', kind: 'observe', text: long },
  ]);

  const entry = state.turn.entries[0];
  assert.equal(entry?.detail, long);
  assert.ok((entry?.text.length ?? 0) < long.length);
});

test('the status frame keeps the live and accounting fields', () => {
  const state = feed([
    {
      v: 1,
      type: 'status',
      models: ['m'],
      mode: 'direct',
      level: 'auto-edit',
      cwd: '.',
      phase: 'thinking',
      steps: 2,
      tool_calls: 1,
      stalled_seconds: 7,
      session_elapsed_seconds: 73,
      tokens: 1000,
      max_tokens: 128000,
      messages: 4,
      needs_compact: false,
      cache_hit_pct: 42,
      cost_cny: 0.01,
      savings_pct: 30,
      streaming: false,
    },
  ]);
  assert.equal(state.status?.steps, 2);
  assert.equal(state.status?.max_tokens, 128000);
  assert.equal(state.status?.cache_hit_pct, 42);
  assert.equal(state.status?.session_elapsed_seconds, 73);
});

test('a notice does not hide the welcome card, a turn does', () => {
  // `--dashboard` appends its URL notice before the first turn; the version,
  // model count and cwd are exactly what a person is looking for at boot.
  const ready = feed([{ v: 1, type: 'ready', models: ['m'], cwd: '/x' }]);
  assert.equal(showWelcomeCard(ready), true);
  const noticed = feed([{ v: 1, type: 'command.result', id: 'c-1', name: '/x', output: 'note' }], ready);
  assert.equal(showWelcomeCard(noticed), false);
  assert.equal(showWelcomeCard(emptyApp()), false);
});

test('a status poll with the same facts keeps the same state object', () => {
  const status = {
    v: 1,
    type: 'status',
    models: ['vendor/model'],
    mode: '单循环',
    level: 'read',
    cwd: 'D:/work',
    steps: 2,
    tool_calls: 1,
    phase: 'react',
  } as unknown as KernelFrame;
  const first = reduce(emptyApp(), status, 0);
  assert.notEqual(first, emptyApp());
  // The terminal polls twice a second while a turn runs; an identical answer
  // must not wake every renderer (that was the constant flicker).
  assert.equal(reduce(first, { ...status }, 0), first);

  const moved = reduce(first, { ...status, steps: 3 } as unknown as KernelFrame, 0);
  assert.notEqual(moved, first);
  assert.equal(moved.status?.steps, 3);
});

test('sub-second clock ticks are not new facts', () => {
  // The kernel reports the clocks to 0.1 s.  The bar shows whole seconds, so
  // two polls inside the same second must leave the state object alone — that
  // is what stops the twice-a-second redraw while nothing visible moved.
  const tick = (elapsed: number, session: number) =>
    ({
      v: 1,
      type: 'status',
      models: ['vendor/model'],
      mode: '单循环',
      level: 'read',
      cwd: 'D:/work',
      phase: 'react',
      elapsed_seconds: elapsed,
      session_elapsed_seconds: session,
    }) as unknown as KernelFrame;

  const first = reduce(emptyApp(), tick(3.2, 74.9), 0);
  assert.equal(first.status?.elapsed_seconds, 3);
  assert.equal(first.status?.session_elapsed_seconds, 74);
  assert.equal(reduce(first, tick(3.7, 74.1), 0), first);

  const next = reduce(first, tick(4.0, 75.2), 0);
  assert.notEqual(next, first);
  assert.equal(next.status?.elapsed_seconds, 4);
  assert.equal(next.status?.session_elapsed_seconds, 75);
});
