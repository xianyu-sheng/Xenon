/**
 * Execute the dashboard page's inline script against a minimal DOM.
 *
 * The page is a single HTML string with no build step, so a missing helper
 * (an undefined `applyLayout`, a panel that never renders) would only show up
 * in a browser.  These tests run the script in a VM with just enough of the
 * DOM to catch runtime errors and hold the nine panel contracts: origin and
 * verification badges, the side-by-side diff, waterfall stalls/retries, the
 * plan's current step and dependencies, the five approval actions, baseline
 * pollution, delivery commit and the fork tree.
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';
import vm from 'node:vm';

import { DASHBOARD_HTML } from './page.js';

class FakeClassList {
  private readonly names = new Set<string>();

  add(name: string): void {
    this.names.add(name);
  }

  remove(name: string): void {
    this.names.delete(name);
  }

  contains(name: string): boolean {
    return this.names.has(name);
  }

  toggle(name: string, force?: boolean): boolean {
    const next = force === undefined ? !this.names.has(name) : force;
    if (next) this.names.add(name);
    else this.names.delete(name);
    return next;
  }
}

interface FakeEvent {
  preventDefault(): void;
  key?: string;
  target?: { tagName?: string };
}

class FakeElement {
  private html = '';
  /** How many times this element's markup was replaced (flicker meter). */
  writes = 0;
  textContent = '';
  value = '';
  placeholder = '';
  title = '';
  scrollTop = 0;
  scrollHeight = 0;
  clientHeight = 0;
  tagName = 'DIV';

  get innerHTML(): string {
    return this.html;
  }

  set innerHTML(value: string) {
    this.html = value;
    this.writes += 1;
  }
  onclick: ((this: FakeElement) => void) | null = null;
  readonly classList = new FakeClassList();
  private readonly listeners = new Map<string, Array<(event: FakeEvent) => void>>();

  constructor(readonly id: string) {}

  addEventListener(type: string, fn: (event: FakeEvent) => void): void {
    const list = this.listeners.get(type) ?? [];
    list.push(fn);
    this.listeners.set(type, list);
  }

  focus(): void {}

  querySelectorAll(): FakeElement[] {
    return [];
  }

  requestSubmit(): void {
    for (const fn of this.listeners.get('submit') ?? []) fn({ preventDefault() {} });
  }
}

const SNAPSHOT = {
  rev: 1,
  commandRunning: null,
  state: {
    ready: { cwd: 'D:/work', mode: '单循环', version: '9.9.9' },
    status: { cwd: 'D:/work', mode: '单循环', models: ['vendor/model'], phase: 'thinking', steps: 2, tool_calls: 1 },
    transcript: [{ kind: 'notice', title: '提示', lines: ['hello'] }],
    turn: {
      id: 't-9',
      objective: 'write and verify a.txt',
      running: false,
      approval: {
        capability: 'file_write',
        reason: 'writes a.txt',
        targets: ['a.txt'],
        options: ['once', 'turn', 'allow', 'deny', 'cancel'],
      },
    },
    prompt: null,
    commands: [
      { name: '/mcp', description: 'MCP 服务器' },
      { name: '/mode', description: '查看当前循环（单一循环，无需切换）' },
    ],
    error: '',
  },
};

const ANSWERS: Record<string, unknown> = {
  'dashboard.snapshot': {
    cwd: 'D:/work',
    mode: '单循环 · Plan → React（逐步） → Reflect（每步校验）',
    models: ['vendor/model'],
    turn: { id: 't-9', objective: 'write and verify a.txt', running: false },
    evidence: { status: 'partial', accepted: true },
    ledger: 'D:/work/.xenon/t-9.jsonl',
    status: { tokens: 42, steps: 2 },
    debt: [
      { kind: 'file_write', reason: 'not verified', target: 'a.txt', next_action: 'write a test for a.txt', attempts: 2 },
    ],
    delivery: { commit: 'abc1234', verified_paths: ['a.txt'], attributed_paths: [], unverified_paths: [] },
    approvals: ['file_write'],
    prompts: [],
  },
  'workspace.status': {
    cwd: 'D:/work',
    is_git: true,
    branch: 'main',
    head: 'abc1234',
    ledger: 'D:/work/.xenon/t-9.jsonl',
    dirty: [
      { status: 'M', path: 'a.txt' },
      { status: '??', path: 'stray.txt' },
    ],
    dirty_count: 2,
    baseline: [{ status: '??', path: 'stray.txt' }],
    baseline_count: 1,
    session_created: ['fresh.txt'],
    session_modified: ['a.txt'],
    delivery: { commit: 'abc1234', verified_paths: ['a.txt'], attributed_paths: ['fresh.txt'], unverified_paths: [] },
  },
  'diff.get': {
    files: [
      { status: 'M', path: 'a.txt', origin: 'modified', verification: 'verified' },
      { status: '??', path: 'fresh.txt', origin: 'created', verification: 'attributed' },
      { status: '??', path: 'stray.txt', origin: 'baseline', verification: 'unverified' },
    ],
    diff: '--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-one\n+two\n',
    truncated: false,
    baseline: [{ status: '??', path: 'stray.txt' }],
    baseline_count: 1,
    delivery: { commit: 'abc1234', verified_paths: ['a.txt'], attributed_paths: ['fresh.txt'], unverified_paths: [] },
  },
  'ledger.events': {
    events: [
      { seq: 1, kind: 'step_started', ts: '2026-11-14T00:00:00Z', source: 'runtime', payload: { step_id: '1', tokens: 10 } },
      { seq: 2, kind: 'step_started', ts: '2026-11-14T00:00:40Z', source: 'runtime', payload: { step_id: '1', tokens: 5 } },
      { seq: 3, kind: 'tool_receipt', ts: '2026-11-14T00:00:45Z', source: 'tool', payload: { usage: { total_tokens: 7 } } },
    ],
    chain: { valid: true, events: 3 },
  },
  'evidence.chain': {
    status: 'partial',
    chain: { valid: true, events: 3 },
    facts: [{ seq: 3, payload: { tool: 'fs_write', ok: true } }],
    verdicts: [],
    claims: [],
    delivered: { seq: 4, payload: { answer: 'done' } },
    projection: { evidence: 'partial', v0: 1 },
  },
  'plan.get': {
    plan: {
      steps: { '1': { status: 'closed', task: 'write' }, '2': { status: 'running', task: 'verify' } },
      plan: {
        steps: [
          { id: '1', kind: 'edit', intent: 'write', depends_on: [] },
          { id: '2', kind: 'test', intent: 'verify', depends_on: [1] },
        ],
      },
      versions: 2,
    },
    proposed: false,
    accepted: true,
    current: '2',
  },
  'report.get': {
    models: [{ model: 'vendor/model', calls: 1, tokens: 42, cache_hit_pct: 50, cost_cny: 0.01 }],
    totals: { calls: 1, tokens: 42, cost_cny: 0.01, saved_cny: 0.01, savings_pct: 50 },
  },
  'sessions.list': { sessions: [{ name: 's-1', saved_at: '2026-11-14', messages: 3 }] },
  'memory.list': { items: [], count: 0 },
  'mcp.status': { servers: [], tools: [] },
  'sessions.tree': {
    nodes: [
      { id: 'root', parent: null, turns: 2, events: 5, first_at: 1000, last_at: 2000 },
      { id: 'fork-1', parent: 'root', turns: 1, events: 3, first_at: 3000, last_at: 4000 },
    ],
    roots: ['root'],
  },
};

function answer(url: string): unknown {
  const parsed = new URL(url, 'http://127.0.0.1');
  if (!parsed.searchParams.get('kind')) return SNAPSHOT;
  const kind = parsed.searchParams.get('kind') as string;
  return ANSWERS[kind] ?? { error: `no stub for ${kind}` };
}

function buildPage(): { context: Record<string, any>; element: (id: string) => FakeElement } {
  const elements = new Map<string, FakeElement>();
  const element = (id: string): FakeElement => {
    let found = elements.get(id);
    if (!found) {
      found = new FakeElement(id);
      elements.set(id, found);
    }
    return found;
  };
  const context: Record<string, any> = {
    document: {
      getElementById: element,
      addEventListener: () => undefined,
      querySelectorAll: () => [],
      body: element('body'),
    },
    location: { search: '?t=secret', hash: '' },
    window: { innerWidth: 1600, addEventListener: () => undefined },
    EventSource: class {
      onerror: (() => void) | null = null;
      onopen: (() => void) | null = null;
      addEventListener(): void {}
    },
    fetch: async (url: string) => ({
      ok: true,
      status: 200,
      json: async () => (url.includes('/api/action') ? { ok: true } : answer(url)),
    }),
    URLSearchParams,
    setTimeout,
    clearTimeout,
    console,
  };
  vm.createContext(context);
  const match = /<script>([\s\S]*?)<\/script>/.exec(DASHBOARD_HTML);
  const script = match?.[1] ?? '';
  assert.ok(script, 'the page carries one inline script');
  vm.runInContext(script, context, { filename: 'dashboard-page.js' });
  return { context, element };
}

async function settle(): Promise<void> {
  for (let i = 0; i < 8; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));
}

test('the page boots, marks a wide window as grid mode and fills the four cells', async () => {
  const { context, element } = buildPage();
  await settle();

  assert.match(element('meta').innerHTML, /D:\/work/);
  assert.equal(element('body').classList.contains('gridmode'), true);
  context.loadGrid(true);
  await settle();
  assert.match(element('cell-overview').innerHTML, /总览/);
  assert.match(element('cell-overview').innerHTML, /abc1234/);
  assert.match(element('cell-diff').innerHTML, /既有改动/);
  assert.match(element('cell-timeline').innerHTML, /哈希链/);
  assert.match(element('cell-evidence').innerHTML, /证据链/);
});

test('the composer completes /mcp and Tab accepts the highlighted entry', async () => {
  const { context, element } = buildPage();
  await settle();

  const input = element('input');
  input.value = '/mc';
  context.renderAcMenu();
  assert.match(element('acmenu').innerHTML, /\/mcp/);
  assert.equal(element('acmenu').classList.contains('hidden'), false);

  context.completeAc(0);
  assert.equal(input.value, '/mcp ');
  assert.equal(element('acmenu').classList.contains('hidden'), true);
});

test('the diff panel shows origin/verification badges, baseline and a side-by-side view', async () => {
  const { context, element } = buildPage();
  await settle();
  context.switchTab('diff');
  await settle();

  const panel = element('panel').innerHTML;
  assert.match(panel, /既有改动/);
  assert.match(panel, /baseline 污染/);
  assert.match(panel, /已交付 abc1234/);
  assert.match(panel, /并排/);

  context.lastDiffMode = 'side';
  context.renderDiff(element('panel'));
  assert.match(element('panel').innerHTML, /side-row/);
});

test('the timeline is a waterfall with stalls, retries and token totals', async () => {
  const { context, element } = buildPage();
  await settle();
  context.switchTab('timeline');
  await settle();

  const panel = element('panel').innerHTML;
  assert.match(panel, /waterfall/);
  assert.match(panel, /停滞/);
  assert.match(panel, /重试/);
  assert.match(panel, /token 22/);
});

test('the evidence panel exposes the projection JSON', async () => {
  const { context, element } = buildPage();
  await settle();
  context.switchTab('evidence');
  await settle();
  assert.match(element('panel').innerHTML, /证据投影/);
});

test('the plan board marks the proposed state, current step and dependencies', async () => {
  const { context, element } = buildPage();
  await settle();
  context.switchTab('plan');
  await settle();

  const panel = element('panel').innerHTML;
  assert.match(panel, /计划已批准/);
  assert.match(panel, /当前步骤 2/);
  assert.match(panel, /依赖 1/);
});

test('the approval inbox renders all five actions and an input box', async () => {
  const { context, element } = buildPage();
  await settle();
  context.switchTab('approvals');
  await settle();

  const panel = element('panel').innerHTML;
  assert.match(panel, /本次允许/);
  assert.match(panel, /本轮允许/);
  assert.match(panel, /全部允许（30 分钟）/);
  assert.match(panel, /拒绝/);
  assert.match(panel, /取消任务/);
});

test('the workspace panel separates session changes from baseline pollution', async () => {
  const { context, element } = buildPage();
  await settle();
  context.switchTab('workspace');
  await settle();

  const panel = element('panel').innerHTML;
  assert.match(panel, /交付 commit/);
  assert.match(panel, /abc1234/);
  assert.match(panel, /baseline 污染/);
  assert.match(panel, /既有改动/);
  assert.match(panel, /新建/);
});

test('the sessions panel renders the fork tree above saved sessions', async () => {
  const { context, element } = buildPage();
  await settle();
  context.switchTab('sessions');
  await settle();

  const panel = element('panel').innerHTML;
  assert.match(panel, /会话树/);
  assert.match(panel, /fork-1/);
  assert.match(panel, /已保存会话/);
});

test('a frame that reports the same facts repaints nothing', async () => {
  const { context, element } = buildPage();
  await settle();
  const transcript = element('transcript').writes;
  const meta = element('meta').writes;
  const cards = element('cards').writes;
  const live = element('live').writes;

  context.render(SNAPSHOT);
  context.render(SNAPSHOT);
  await settle();

  assert.equal(element('transcript').writes, transcript);
  assert.equal(element('meta').writes, meta);
  assert.equal(element('cards').writes, cards);
  assert.equal(element('live').writes, live);
});

test('answering an approval replaces the card with an acknowledgement', async () => {
  const { context, element } = buildPage();
  await settle();
  const transcript = element('transcript').writes;

  context.chooseApproval('once');
  await settle();

  // The conversation did not change, so it must not be rebuilt.
  assert.equal(element('transcript').writes, transcript);
  const cards = element('cards').innerHTML;
  assert.match(cards, /已应答：本次允许/);
  assert.match(cards, /等待它继续/);
  assert.doesNotMatch(cards, /需要审批/);
});

test('the approvals panel refreshes without blanking or repainting unchanged data', async () => {
  const { context, element } = buildPage();
  await settle();
  context.switchTab('approvals');
  await settle();
  const panel = element('panel');
  assert.match(panel.innerHTML, /审批收件箱/);
  const writes = panel.writes;

  context.scheduleRefresh();
  await new Promise((resolve) => setTimeout(resolve, 300));
  await settle();

  assert.equal(panel.writes, writes);
  assert.match(panel.innerHTML, /审批收件箱/);
  assert.doesNotMatch(panel.innerHTML, /读取中/);
});

test('the conversation and the sidebar own their scroll independently', () => {
  // The page itself must not scroll: with `body` scrolling, a wheel over the
  // sidebar moved the conversation (and vice versa).  Each region gets its own
  // scroll box, and `overscroll-behavior: contain` stops a wheel at its end
  // from chaining to the next scroller.
  assert.match(DASHBOARD_HTML, /body \{[^}]*overflow: hidden/s);
  assert.match(DASHBOARD_HTML, /main \{[^}]*overflow: hidden/s);
  assert.match(DASHBOARD_HTML, /\.transcript, #live, #cards \{[^}]*overflow-y: auto/s);
  assert.match(DASHBOARD_HTML, /\.transcript, #live, #cards \{[^}]*overscroll-behavior: contain/s);
  assert.match(DASHBOARD_HTML, /\.panel \{[^}]*overflow: auto/s);
  assert.match(DASHBOARD_HTML, /\.panel \{[^}]*overscroll-behavior: contain/s);
  assert.match(DASHBOARD_HTML, /#grid \{[^}]*overscroll-behavior: contain/s);
});

test('the composer says when the kernel is waiting for an answer', async () => {
  const { context, element } = buildPage();
  await settle();
  context.render({
    ...SNAPSHOT,
    state: {
      ...SNAPSHOT.state,
      prompt: { id: 'p-1', message: '选择模型 #', default: '', secret: false },
    },
  });
  assert.match(element('input').placeholder, /回答上方的问题/);
  assert.match(element('cards').innerHTML, /选择模型/);
});
