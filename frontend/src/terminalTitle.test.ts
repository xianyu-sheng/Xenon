/**
 * Terminal-tab activity: the title is the only animation a terminal tab has,
 * and it must never leak control characters.  The writer is injectable, so the
 * state machine can be asserted without a terminal.
 */

import assert from 'node:assert/strict';
import { setTimeout as delay } from 'node:timers/promises';
import { test } from 'node:test';

import { TerminalTitle } from './terminalTitle.js';

function recorder(): { writes: string[]; write: (title: string) => void } {
  const writes: string[] = [];
  return { writes, write: (title) => writes.push(title) };
}

test('idle publishes the settled runner mark', () => {
  const sink = recorder();
  const title = new TerminalTitle({ enabled: true, write: sink.write });

  title.start();

  assert.deepEqual(sink.writes, ['█·· Xenon']);
});

test('waiting for a person names the reason and stops moving', () => {
  const sink = recorder();
  const title = new TerminalTitle({ enabled: true, write: sink.write });

  title.setState('waiting', '等待确认');
  assert.equal(sink.writes.at(-1), '☆ Xenon · 等待确认');

  title.setState('waiting', '等待输入');
  assert.equal(sink.writes.at(-1), '☆ Xenon · 等待输入');

  title.setState('error');
  assert.equal(sink.writes.at(-1), '█ Xenon · 出错');
});

test('ascii mode never emits block glyphs', () => {
  const sink = recorder();
  const title = new TerminalTitle({ enabled: true, ascii: true, write: sink.write });

  title.start();
  title.setState('waiting');
  title.setState('error');

  assert.deepEqual(sink.writes, [
    '*.. Xenon',
    '- Xenon · 等待确认',
    '! Xenon · 出错',
  ]);
});

test('a title is sanitized before it reaches the terminal', () => {
  const sink = recorder();
  const title = new TerminalTitle({ enabled: true, write: sink.write });

  title.setState('waiting', '\x1b[31m红\x07\r\n剩下');

  assert.equal(sink.writes.at(-1), '☆ Xenon · [31m红  剩下');
});

test('running advances the runner on its own timer', async () => {
  const sink = recorder();
  const title = new TerminalTitle({ enabled: true, intervalMs: 100, write: sink.write });

  title.start();
  title.setState('running');
  await delay(360);
  title.setState('idle');

  const running = sink.writes.slice(1, -1);
  assert.ok(running.length >= 2, `expected animation frames, got ${sink.writes.join(' | ')}`);
  assert.equal(new Set(running).size, running.length, 'each frame differs from the last');
  assert.deepEqual(running.slice(0, 3), ['█·· Xenon', '·█· Xenon', '··█ Xenon']);
});

test('a disabled title writes nothing, and close restores once', () => {
  const sink = recorder();
  const title = new TerminalTitle({ enabled: false, write: sink.write });

  title.start();
  title.setState('running');
  title.close();

  assert.deepEqual(sink.writes, []);
});
