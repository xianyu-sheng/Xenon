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
  assert.ok(block?.lines.includes('done'));
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
