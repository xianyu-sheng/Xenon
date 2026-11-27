/**
 * The dashboard page: one self-contained document, served from memory.
 *
 * No build step and no CDN: the whole point is that a local, offline panel can
 * be trusted.  It renders the same `AppState` the terminal renders plus the
 * read-only query panels, and it sends actions back through `/api/action` —
 * the same four things a keystroke in the terminal can do.
 *
 * The source is kept free of backticks and `${` so it can live in a raw
 * template literal (regex backslashes stay intact).
 */

export const DASHBOARD_HTML = String.raw`<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Xenon Dashboard</title>
<style>
:root {
  --bg: #0b1020; --panel: #121a2e; --panel2: #0f1728; --line: #24304a;
  --fg: #d7e0f4; --dim: #7d8bab; --accent: #67e8f9; --green: #4ade80;
  --red: #f87171; --amber: #fbbf24; --violet: #c4b5fd;
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  /* The page itself never scrolls: the conversation and the side panel are
     independent scroll views.  Without this a wheel over a pane that fits its
     content chained up to the document and moved both columns at once. */
  overflow: hidden;
}
header {
  display: flex; align-items: center; gap: 14px; padding: 10px 16px;
  border-bottom: 1px solid var(--line); background: var(--panel2);
  position: sticky; top: 0; z-index: 5;
}
.brand { font-weight: 700; letter-spacing: 2px; color: var(--accent); }
.brand small { color: var(--dim); letter-spacing: 0; font-weight: 400; margin-left: 8px; }
.meta { flex: 1; display: flex; flex-wrap: wrap; gap: 6px 14px; color: var(--dim); }
.meta b { color: var(--fg); font-weight: 500; }
.dot { width: 9px; height: 9px; border-radius: 50%; background: var(--red); }
.dot.on { background: var(--green); box-shadow: 0 0 8px var(--green); }
main { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(0, 1fr); gap: 0; height: calc(100vh - 46px); min-height: 0; overflow: hidden; }
.left { display: flex; flex-direction: column; min-width: 0; min-height: 0; border-right: 1px solid var(--line); }
.right { display: flex; flex-direction: column; min-width: 0; min-height: 0; }
.banner { margin: 10px 14px 0; padding: 8px 10px; border: 1px solid var(--amber); border-radius: 6px; color: var(--amber); white-space: pre-wrap; }
.banner:empty { display: none; }
.transcript, #live, #cards { overflow-y: auto; overscroll-behavior: contain; }
/* min-height keeps the flex child from growing past the column (which made
   the whole page scroll); contain stops a wheel at the edge from chaining
   into the sibling pane. */
.transcript { flex: 1 1 auto; min-height: 0; padding: 10px 14px; }
#live, #cards { flex: 0 0 auto; max-height: 34vh; }
.block { border: 1px solid var(--line); border-radius: 8px; margin-bottom: 12px; background: var(--panel); overflow: hidden; }
.block > .head { display: flex; gap: 8px; align-items: baseline; padding: 7px 10px; border-bottom: 1px solid var(--line); background: var(--panel2); }
.block > .head .title { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.badge { padding: 1px 7px; border-radius: 99px; border: 1px solid currentColor; font-size: 11px; white-space: nowrap; }
.badge.verified { color: var(--green); }
.badge.partial { color: var(--amber); }
.badge.unverified, .badge.contradicted { color: var(--red); }
.badge.muted { color: var(--dim); }
.block > .body { padding: 8px 12px; }
.lines { margin: 0; padding-left: 18px; color: var(--dim); }
.lines li { margin: 1px 0; }
.answer { margin: 6px 0 2px; color: var(--fg); }
.answer pre { background: #0a0f1c; border: 1px solid var(--line); border-radius: 6px; padding: 8px; overflow-x: auto; }
.answer code { color: #a5f3fc; }
.answer h3, .answer h4 { margin: 8px 0 4px; color: var(--accent); }
.answer ul { margin: 4px 0; padding-left: 20px; }
.debt { margin-top: 8px; padding: 6px 8px; border: 1px dashed var(--amber); border-radius: 6px; color: var(--amber); }
.debt ul { margin: 4px 0 0; padding-left: 18px; }
.metrics { color: var(--dim); margin-top: 6px; }
.live { margin: 0 14px 8px; border: 1px solid var(--accent); border-radius: 8px; padding: 8px 12px; background: var(--panel); }
.live .obj { font-weight: 600; margin-bottom: 4px; }
.live ol { margin: 4px 0; padding-left: 20px; }
.live .status { color: var(--dim); }
#cards { padding: 0 14px; }
.card { border: 1px solid var(--amber); border-radius: 8px; padding: 10px 12px; margin-bottom: 10px; background: #1a1a12; }
.card h4 { margin: 0 0 6px; color: var(--amber); }
.card.note { border-color: var(--green); background: #0e1a12; }
.card.note h4 { color: var(--green); }
.card .why { color: var(--dim); margin-bottom: 8px; }
.card .targets { color: var(--fg); background: #0a0f1c; border-radius: 6px; padding: 6px 8px; margin-bottom: 8px; white-space: pre-wrap; }
.btns { display: flex; flex-wrap: wrap; gap: 6px; }
button {
  background: #1b2740; color: var(--fg); border: 1px solid var(--line);
  border-radius: 6px; padding: 4px 10px; font: inherit; cursor: pointer;
}
button:hover { border-color: var(--accent); color: var(--accent); }
button.primary { border-color: var(--accent); color: var(--accent); }
#acmenu { margin: 0 14px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); overflow: hidden; }
#acmenu button { display: block; width: 100%; text-align: left; border: 0; border-bottom: 1px solid var(--line); border-radius: 0; background: transparent; padding: 4px 10px; }
#acmenu button.active { background: #17243c; color: var(--accent); }
#acmenu small { color: var(--dim); margin-left: 8px; }
#composer { display: flex; gap: 8px; padding: 10px 14px; border-top: 1px solid var(--line); background: var(--panel2); }
#composer textarea {
  flex: 1; resize: vertical; min-height: 42px; max-height: 180px; background: #0a0f1c;
  color: var(--fg); border: 1px solid var(--line); border-radius: 6px; padding: 8px; font: inherit;
}
#composer textarea:focus { outline: none; border-color: var(--accent); }
#tabs { display: flex; flex-wrap: wrap; gap: 4px; padding: 8px; border-bottom: 1px solid var(--line); background: var(--panel2); }
#tabs button { padding: 3px 9px; font-size: 12px; }
#tabs button.active { border-color: var(--accent); color: var(--accent); background: #17243c; }
.panel { flex: 1; min-height: 0; overflow: auto; overscroll-behavior: contain; padding: 12px; }
.panel h3 { margin: 0 0 8px; color: var(--accent); font-size: 13px; }
.panel h4 { margin: 14px 0 6px; color: var(--violet); font-size: 12px; }
.panel table { border-collapse: collapse; width: 100%; margin: 4px 0 8px; }
.panel th, .panel td { border-bottom: 1px solid var(--line); padding: 3px 6px; text-align: left; vertical-align: top; }
.panel th { color: var(--dim); font-weight: 500; }
.panel pre { background: #0a0f1c; border: 1px solid var(--line); border-radius: 6px; padding: 8px; overflow: auto; margin: 4px 0 10px; }
.panel details { margin: 3px 0; }
.panel summary { cursor: pointer; color: var(--dim); }
.add { color: var(--green); }
.del { color: var(--red); }
.hunk { color: var(--violet); }
.err { color: var(--red); }
.empty { color: var(--dim); }
.hidden { display: none !important; }
.help {
  position: fixed; right: 20px; bottom: 20px; width: 300px; z-index: 20;
  background: var(--panel); border: 1px solid var(--accent); border-radius: 10px; padding: 12px 14px;
}
.help h4 { margin: 0 0 6px; color: var(--accent); }
.help ul { margin: 0; padding-left: 18px; color: var(--dim); }
/* Four-cell board for the wide layout: the left conversation pane stays,
   the right side becomes 总览 / Diff / 执行 / 证据 at once.  Narrower
   viewports fall back to the tab strip. */
#grid { display: none; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); grid-auto-rows: minmax(190px, 1fr); gap: 8px; padding: 8px; flex: 1; min-height: 0; overflow: auto; overscroll-behavior: contain; }
.gridcell { border: 1px solid var(--line); border-radius: 8px; background: var(--panel); padding: 8px 10px; min-height: 0; overflow: auto; overscroll-behavior: contain; }
.gridcell h4 { margin: 0 0 6px; color: var(--accent); font-size: 12px; position: sticky; top: 0; background: var(--panel); }
/* Grid cells are narrow: give the label column a fixed width and let the
   value wrap, or the two columns fight over the same pixels and the rows
   render on top of each other. */
.gridcell table { border-collapse: collapse; width: 100%; table-layout: fixed; }
.gridcell th, .gridcell td { border-bottom: 1px solid var(--line); padding: 2px 6px; text-align: left; vertical-align: top; word-break: break-word; }
.gridcell th { width: 3.5em; color: var(--dim); font-weight: 500; white-space: nowrap; }
.gridcell .metrics { margin-top: 4px; }
body.gridmode #grid { display: grid; }
body.gridmode #panel { display: none; }
.side { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
.side pre { margin: 0; }
.flow { display: flex; flex-wrap: wrap; gap: 8px; }
.step { border: 1px solid var(--line); border-radius: 8px; padding: 7px 9px; min-width: 150px; max-width: 240px; background: var(--panel2); }
.step.current { border-color: var(--accent); box-shadow: 0 0 8px rgba(103, 232, 249, .25); }
.step .deps { color: var(--dim); font-size: 11px; margin-top: 4px; }
.bar { height: 7px; border-radius: 99px; background: var(--accent); min-width: 2px; }
.bar.stall { background: var(--amber); }
.bar.retry { background: var(--violet); }
.waterfall td { white-space: nowrap; }
.waterfall tr.folded td { color: var(--dim); font-style: italic; }
.tree { margin: 0; padding-left: 17px; }
.tree li { margin: 2px 0; }
@media (max-width: 1399px) {
  body.gridmode #grid { display: none; }
  body.gridmode #panel { display: block; }
}
@media (max-width: 999px) {
  /* One column: the panes stack, so the document itself has to scroll. */
  html, body { height: auto; overflow: auto; }
  main { grid-template-columns: 1fr; height: auto; overflow: visible; }
  .right { border-top: 1px solid var(--line); min-height: 420px; }
  .transcript { max-height: 55vh; }
  .side { grid-template-columns: 1fr; }
}
</style>
</head>
<body>
<header>
  <div class="brand">XENON <small>本地面板</small></div>
  <div class="meta" id="meta"></div>
  <div class="dot" id="conn" title="未连接"></div>
</header>
<main>
  <section class="left">
    <div class="banner" id="banner"></div>
    <div class="transcript" id="transcript"></div>
    <div id="live"></div>
    <div id="cards"></div>
    <div id="acmenu" class="hidden"></div>
    <form id="composer">
      <textarea id="input" rows="2" placeholder="输入任务，或 /命令。Enter 执行，Shift+Enter 换行"></textarea>
      <button class="primary" type="submit">发送</button>
    </form>
  </section>
  <section class="right">
    <nav id="tabs"></nav>
    <div id="grid">
      <div class="gridcell" id="cell-overview"></div>
      <div class="gridcell" id="cell-diff"></div>
      <div class="gridcell" id="cell-timeline"></div>
      <div class="gridcell" id="cell-evidence"></div>
    </div>
    <div class="panel" id="panel"><div class="empty">加载中…</div></div>
  </section>
</main>
<div class="help hidden" id="help">
  <h4>键位</h4>
  <ul>
    <li>Enter 执行 · Shift+Enter 换行</li>
    <li>/ 补全命令（↑/↓ 选择，Tab 补全）</li>
    <li>1-9 切换右侧面板</li>
    <li>j / k 滚动面板</li>
    <li>? 显示 / 隐藏本帮助</li>
    <li>宽屏（≥1400px）默认四宫格；点标签回到单页</li>
  </ul>
</div>
<script>
'use strict';
var BT = String.fromCharCode(96);
var token = new URLSearchParams(location.search).get('t') || '';
var state = null;
var commandRunning = null;
var activeTab = 'overview';
var layout = 'auto';  // auto | grid | tabs
var acIndex = 0;
var acItems = [];
var lastDiff = null;
var lastDiffMode = 'unified';
var lastTurnId = '';
var gridLoadedAt = 0;
var lastGridKey = '';
var lastRev = -1;
var refreshTimer = null;
var lastPanelKey = '';
var answeredApproval = null;
var TABS = [
  ['overview', '总览'], ['diff', 'Diff'], ['timeline', '执行过程'],
  ['evidence', '证据链'], ['plan', '计划'], ['approvals', '审批'],
  ['cost', '成本'], ['workspace', '工作区'], ['sessions', '会话']
];

function $(id) { return document.getElementById(id); }

// Replacing innerHTML is the page's only expensive operation: it throws away
// and rebuilds every node, which flickers, resets scroll anchoring and kills
// focus.  Every renderer goes through paint(), which writes only when the
// markup actually changed.  A frame that reports the same facts (the common
// case: a status poll during a turn) then costs nothing at all.
var painted = typeof WeakMap === 'function' ? new WeakMap() : null;
function paint(node, html) {
  if (!node) return false;
  if (painted && painted.get(node) === html) return false;
  if (painted) painted.set(node, html);
  node.innerHTML = html;
  return true;
}
function unpainted(node) { return !painted || !painted.has(node); }

function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function md(text) {
  var src = esc(text == null ? '' : String(text));
  var lines = src.split('\n');
  var out = [];
  var inCode = false;
  var inList = false;
  var fence = BT + BT + BT;
  for (var i = 0; i < lines.length; i += 1) {
    var line = lines[i];
    if (line.indexOf(fence) === 0) {
      if (inCode) { out.push('</code></pre>'); inCode = false; }
      else { if (inList) { out.push('</ul>'); inList = false; } out.push('<pre><code>'); inCode = true; }
      continue;
    }
    if (inCode) { out.push(line + '\n'); continue; }
    var h = /^(#{1,4})\s+(.*)$/.exec(line);
    if (h) { if (inList) { out.push('</ul>'); inList = false; } out.push('<h4>' + inline(h[2]) + '</h4>'); continue; }
    var li = /^\s*[-*]\s+(.*)$/.exec(line);
    if (li) { if (!inList) { out.push('<ul>'); inList = true; } out.push('<li>' + inline(li[1]) + '</li>'); continue; }
    if (inList) { out.push('</ul>'); inList = false; }
    out.push(inline(line) + '<br>');
  }
  if (inList) out.push('</ul>');
  if (inCode) out.push('</code></pre>');
  return out.join('');
}

function inline(text) {
  var code = new RegExp(BT + '([^' + BT + ']+)' + BT, 'g');
  return text.replace(code, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>');
}

function badge(status) {
  var value = String(status || '未报告');
  var cls = ['verified', 'partial', 'unverified', 'contradicted'].indexOf(value) >= 0 ? value : 'muted';
  return '<span class="badge ' + cls + '">' + esc(value) + '</span>';
}

function table(headers, rows) {
  var html = '<table><thead><tr>';
  for (var i = 0; i < headers.length; i += 1) html += '<th>' + esc(headers[i]) + '</th>';
  html += '</tr></thead><tbody>';
  for (var r = 0; r < rows.length; r += 1) {
    html += '<tr>';
    for (var c = 0; c < rows[r].length; c += 1) html += '<td>' + rows[r][c] + '</td>';
    html += '</tr>';
  }
  return html + '</tbody></table>';
}

function kv(rows) {
  var html = '<table><tbody>';
  for (var i = 0; i < rows.length; i += 1) {
    html += '<tr><th>' + esc(rows[i][0]) + '</th><td>' + esc(rows[i][1]) + '</td></tr>';
  }
  return html + '</tbody></table>';
}

// ── state rendering ────────────────────────────────────────────────
function render(payload) {
  // A malformed bootstrap answer must not blank the panel: keep the last
  // good state and wait for the SSE snapshot.
  if (!payload || !payload.state) return;
  state = payload.state;
  commandRunning = payload.commandRunning;
  var turnId = state.turn && state.turn.id ? String(state.turn.id) : '';
  if (turnId !== lastTurnId) { lastTurnId = turnId; lastDiff = null; answeredApproval = null; }
  // An answered approval is acknowledged until the turn it belonged to ends.
  if (answeredApproval && answeredApproval.turn && !(state.turn && state.turn.running)) {
    answeredApproval = null;
  }
  // A different request is a different decision: drop any previous note.
  var currentApprovalId = state.turn && state.turn.approval ? state.turn.approval.id : null;
  if (answeredApproval && currentApprovalId && currentApprovalId !== answeredApproval.id) {
    answeredApproval = null;
  }
  lastRev = payload.rev;
  renderMeta();
  renderTranscript();
  renderLive();
  renderCards();
  renderComposerHint();
  $('banner').textContent = state.error || '';
  applyLayout();
  if (document.body.classList.contains('gridmode')) loadGrid(false);
  scheduleRefresh();
}

function renderComposerHint() {
  var input = $('input');
  if (!input) return;
  // While the kernel waits on a question, this box is the answer field: the
  // terminal is blocked inside that prompt, so the same line goes back as the
  // answer instead of opening a turn.
  var hint = state.prompt
    ? '回答上方的问题（Enter 提交）'
    : '输入任务，或 /命令。Enter 执行，Shift+Enter 换行';
  if (input.placeholder !== hint) input.placeholder = hint;
}

function renderMeta() {
  var status = state.status || {};
  var ready = state.ready || {};
  var parts = [];
  parts.push('目录 <b>' + esc(ready.cwd || status.cwd || '') + '</b>');
  if (status.mode || ready.mode) parts.push('循环 <b>' + esc(status.mode || ready.mode) + '</b>');
  if (status.models && status.models.length) parts.push('模型 <b>' + esc(status.models.join(', ')) + '</b>');
  if (ready.version) parts.push('版本 <b>' + esc(ready.version) + '</b>');
  if (state.turn && state.turn.running) parts.push('<b style="color:var(--accent)">轮次运行中</b>');
  if (commandRunning) parts.push('命令 <b>' + esc(commandRunning) + '</b>');
  if (state.prompt) parts.push('<b style="color:var(--amber)">等待回答</b>');
  paint($('meta'), parts.join('<span style="color:var(--line)">│</span>'));
}

function renderTranscript() {
  var blocks = state.transcript || [];
  var html = '';
  for (var i = 0; i < blocks.length; i += 1) {
    var block = blocks[i];
    html += '<div class="block">';
    html += '<div class="head"><span class="title">' + esc(block.title || labelFor(block.kind)) + '</span>';
    if (block.evidence) html += badge(block.evidence);
    if (block.cancelled) html += '<span class="badge muted">已中断</span>';
    if (block.planned) html += '<span class="badge muted">仅计划</span>';
    html += '</div><div class="body">';
    if (block.answer) html += '<div class="answer">' + md(block.answer) + '</div>';
    if (block.lines && block.lines.length && block.kind !== 'report') {
      html += '<ul class="lines">';
      for (var j = 0; j < block.lines.length; j += 1) html += '<li>' + esc(block.lines[j]) + '</li>';
      html += '</ul>';
    }
    if (block.report) html += renderReport(block.report);
    if (block.metrics) html += '<div class="metrics">步骤 ' + esc(block.metrics.steps) + ' · 工具调用 ' + esc(block.metrics.tool_calls) + '</div>';
    if (block.debt && block.debt.length) {
      html += '<div class="debt">⚠ 降级发布：部分内容无法自动核验，请人工核对<ul>';
      for (var d = 0; d < block.debt.length; d += 1) {
        html += '<li>' + esc(block.debt[d].kind || '') + ' · ' + esc(block.debt[d].reason || '') + '</li>';
      }
      html += '</ul></div>';
    }
    if (block.ledger) html += '<div class="metrics">账本 ' + esc(block.ledger) + '</div>';
    html += '</div></div>';
  }
  var box = $('transcript');
  var atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
  var changed = paint(box, html || '<div class="empty">还没有对话。在下方输入任务，或在终端里继续。</div>');
  if (changed && atBottom) box.scrollTop = box.scrollHeight;
}

function labelFor(kind) {
  if (kind === 'notice') return '提示';
  if (kind === 'report') return '会话报告';
  return kind;
}

function renderReport(report) {
  var html = table(['模型', '调用', 'tokens', '缓存命中', '花费 ¥'], (report.models || []).map(function (m) {
    return [esc(m.model), esc(m.calls), esc(m.tokens), m.cache_hit_pct == null ? '—' : esc(m.cache_hit_pct) + '%', esc(m.cost_cny)];
  }));
  if (report.totals) {
    var t = report.totals;
    html += '<div class="metrics">合计 ' + esc(t.calls) + ' 次 · ' + esc(t.tokens) + ' tokens · ¥' + esc(t.cost_cny) +
      ' · 节省 ¥' + esc(t.saved_cny) + '（' + esc(t.savings_pct) + '%）</div>';
  }
  return html;
}

function renderLive() {
  var turn = state.turn || {};
  if (!turn.running) { paint($('live'), ''); return; }
  var html = '<div class="live"><div class="obj">❯ ' + esc(turn.objective) + '</div><ol>';
  var entries = turn.entries || [];
  for (var i = 0; i < entries.length; i += 1) {
    html += '<li style="color:' + toneColor(entries[i].tone) + '">' + esc(entries[i].text) + '</li>';
  }
  html += '</ol><div class="status">';
  var status = state.status || {};
  if (status.phase) html += esc(status.phase) + ' · ';
  html += '步骤 ' + esc(status.steps == null ? 0 : status.steps) + ' · 工具 ' + esc(status.tool_calls == null ? 0 : status.tool_calls);
  if (status.stalled_seconds) html += ' · 停滞 ' + esc(status.stalled_seconds) + 's';
  if (turn.providerNote) html += ' · ' + esc(turn.providerNote);
  if (turn.cancelling) html += ' · 正在取消…';
  html += '</div></div>';
  paint($('live'), html);
}

function toneColor(tone) {
  if (tone === 'error') return 'var(--red)';
  if (tone === 'warn') return 'var(--amber)';
  if (tone === 'success') return 'var(--green)';
  if (tone === 'muted') return 'var(--dim)';
  return 'var(--fg)';
}

function renderCards() {
  var html = '';
  var approval = state.turn && state.turn.approval;
  // Once a choice is sent, that request is no longer a card: a stale frame
  // must not resurrect it, and the person must see that the answer landed.
  var showing = approval && !(answeredApproval && answeredApproval.id === approval.id) ? approval : null;
  if (showing) {
    html += '<div class="card"><h4>需要审批 · ' + esc(showing.capability || showing.tool) + '</h4>';
    html += '<div class="why">' + esc(showing.reason || '') + '</div>';
    if (showing.targets && showing.targets.length) {
      html += '<div class="targets">' + esc(showing.targets.join('\n')) + '</div>';
    }
    if (showing.diff_preview) html += '<pre>' + esc(showing.diff_preview) + '</pre>';
    html += '<div class="btns">';
    var options = showing.options || [];
    for (var i = 0; i < options.length; i += 1) {
      html += '<button data-approval="' + esc(options[i]) + '">' + (i + 1) + '. ' + esc(choiceLabel(options[i])) + '</button>';
    }
    html += '</div></div>';
  }
  if (answeredApproval) {
    html += '<div class="card note"><h4>✓ 已应答：' + esc(answeredApproval.label) + '</h4>';
    html += '<div class="why">已发送给内核，等待它继续本轮。若长时间停在这里，说明内核没有确认这次应答。</div></div>';
  }
  if (state.prompt) {
    html += '<div class="card"><h4>输入 · ' + esc(state.prompt.message) + '</h4>';
    html += '<div class="why">默认：' + esc(state.prompt.default || '（空）') + (state.prompt.secret ? ' · 敏感输入' : '') + '</div>';
    html += '<div class="btns"><input id="promptValue" type="' + (state.prompt.secret ? 'password' : 'text') + '" style="flex:1;background:#0a0f1c;color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:4px 8px">';
    html += '<button id="promptSend" class="primary">提交</button></div></div>';
  }
  // Only touch the DOM when the cards actually changed: rebuilding this box on
  // every frame is what wiped a half-typed prompt answer and dropped focus.
  if (!paint($('cards'), html)) return;
  var buttons = document.querySelectorAll('button[data-approval]');
  for (var b = 0; b < buttons.length; b += 1) {
    buttons[b].onclick = function () { chooseApproval(this.getAttribute('data-approval')); };
  }
  var send = $('promptSend');
  if (send) {
    send.onclick = function () {
      act({ action: 'prompt', value: $('promptValue').value });
    };
  }
}

/** Send an approval choice and say so immediately, before the kernel answers. */
function chooseApproval(choice) {
  var approval = state && state.turn && state.turn.approval;
  if (!approval) return;
  answeredApproval = {
    id: approval.id,
    label: choiceLabel(choice),
    turn: !!(state.turn && state.turn.running)
  };
  renderCards();
  if (activeTab === 'approvals') loadPanel();
  act({ action: 'approval', choice: choice }).then(function (body) {
    if (body && body.ok) return;
    // The request never reached the server: bring the card back rather than
    // show an acknowledgement that is not real.
    answeredApproval = null;
    renderCards();
    if (activeTab === 'approvals') loadPanel();
  });
}

function choiceLabel(choice) {
  var labels = { once: '本次允许', turn: '本轮允许', allow: '全部允许（30 分钟）', deny: '拒绝', cancel: '取消任务' };
  return labels[choice] || choice;
}

// ── composer slash completion ──────────────────────────────────────
function commandMatches(needle) {
  var commands = (state && state.commands) || [];
  var scored = [];
  for (var i = 0; i < commands.length; i += 1) {
    var name = String(commands[i].name || '').slice(1).toLowerCase();
    var rank = 0;
    if (name === needle) rank = 3;
    else if (name.indexOf(needle) === 0) rank = 2;
    else if (needle && name.indexOf(needle) > 0) rank = 1;
    if (rank > 0) scored.push({ rank: rank, item: commands[i] });
  }
  scored.sort(function (a, b) { return b.rank - a.rank; });
  return scored.slice(0, 8).map(function (entry) { return entry.item; });
}

function hideAcMenu() {
  acItems = [];
  acIndex = 0;
  $('acmenu').classList.add('hidden');
  $('acmenu').innerHTML = '';
}

function renderAcMenu() {
  var text = $('input').value;
  var trimmed = text.trim();
  if (!trimmed || trimmed.charAt(0) !== '/' || trimmed.indexOf(' ') >= 0) return hideAcMenu();
  var items = commandMatches(trimmed.slice(1).toLowerCase());
  if (!items.length) return hideAcMenu();
  if (items.length === 1 && String(items[0].name).toLowerCase() === trimmed.toLowerCase()) return hideAcMenu();
  acItems = items;
  if (acIndex >= items.length) acIndex = 0;
  var html = '';
  for (var i = 0; i < items.length; i += 1) {
    html += '<button data-ac="' + i + '" class="' + (i === acIndex ? 'active' : '') + '">' + esc(items[i].name) +
      '<small>' + esc(items[i].description || '') + '</small></button>';
  }
  $('acmenu').innerHTML = html;
  $('acmenu').classList.remove('hidden');
  var buttons = $('acmenu').querySelectorAll('button');
  for (var b = 0; b < buttons.length; b += 1) {
    buttons[b].onclick = function () { completeAc(Number(this.getAttribute('data-ac'))); };
  }
}

function completeAc(index) {
  var chosen = acItems[index];
  if (!chosen) return;
  var input = $('input');
  input.value = chosen.name + ' ';
  hideAcMenu();
  input.focus();
}

// ── actions ────────────────────────────────────────────────────────
function act(payload) {
  return fetch('/api/action?t=' + encodeURIComponent(token), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  }).then(function (res) {
    return res.json().catch(function () { return {}; }).then(function (body) {
      if (!res.ok) throw new Error(body.error || ('HTTP ' + res.status));
      return body;
    });
  }).catch(function (error) {
    banner('操作失败：' + error.message);
  });
}

function banner(text) {
  var box = $('banner');
  box.textContent = text;
  setTimeout(function () { if (box.textContent === text) box.textContent = state && state.error || ''; }, 5000);
}

function query(kind, params) {
  var url = '/api/query?t=' + encodeURIComponent(token) + '&kind=' + encodeURIComponent(kind);
  if (params) url += '&params=' + encodeURIComponent(JSON.stringify(params));
  return fetch(url).then(function (res) { return res.json(); });
}

// ── tabs ───────────────────────────────────────────────────────────
function renderTabs() {
  var html = '';
  for (var i = 0; i < TABS.length; i += 1) {
    html += '<button data-tab="' + TABS[i][0] + '" class="' + (TABS[i][0] === activeTab ? 'active' : '') + '">' +
      (i + 1) + '. ' + TABS[i][1] + '</button>';
  }
  html += '<button id="gridbtn" title="四宫格">▦ 宫格</button>';
  $('tabs').innerHTML = html;
  var buttons = $('tabs').querySelectorAll('button[data-tab]');
  for (var b = 0; b < buttons.length; b += 1) {
    buttons[b].onclick = function () { switchTab(this.getAttribute('data-tab')); };
  }
  var grid = $('gridbtn');
  if (grid) grid.onclick = function () { layout = 'grid'; applyLayout(); loadGrid(true); };
}

function switchTab(name) {
  activeTab = name;
  layout = 'tabs';
  lastPanelKey = '';  // the newly opened tab must load on the next frame
  applyLayout();
  renderTabs();
  loadPanel();
}

function applyLayout() {
  var wide = window.innerWidth >= 1400;
  var grid = wide && (layout === 'grid' || layout === 'auto');
  document.body.classList.toggle('gridmode', grid);
  var button = $('gridbtn');
  if (button) button.classList.toggle('active', grid);
}

window.addEventListener('resize', function () {
  applyLayout();
  // Crossing the 1400px line turns the grid on; fill it without waiting for
  // the next SSE frame.
  if (document.body.classList.contains('gridmode')) loadGrid(false);
});

/**
 * What the live panels actually render, so a frame that only ticks a clock
 * (elapsed seconds, a poll answering the same numbers) does not reload them.
 */
function panelKey() {
  if (!state) return '';
  var turn = state.turn || {};
  var status = state.status || {};
  var approval = turn.approval;
  var prompt = state.prompt;
  return [
    turn.id || '', turn.running ? '1' : '0',
    approval ? String(approval.id || approval.capability || 'a') : '',
    prompt ? String(prompt.id || 'p') : '',
    (state.transcript || []).length,
    status.phase || '',
    status.steps == null ? '' : status.steps,
    status.tool_calls == null ? '' : status.tool_calls,
    commandRunning || ''
  ].join('|');
}

function scheduleRefresh() {
  // Only panels whose data is cheap and live are refreshed automatically;
  // git-backed panels reload when the person opens them (or on the tab).
  if (activeTab !== 'overview' && activeTab !== 'approvals') return;
  var key = panelKey();
  if (key === lastPanelKey) return;
  lastPanelKey = key;
  if (refreshTimer) clearTimeout(refreshTimer);
  refreshTimer = setTimeout(function () {
    if (activeTab === 'overview' || activeTab === 'approvals') loadPanel();
  }, 250);
}

function loadGrid(force) {
  if (!document.body.classList.contains('gridmode')) return;
  var now = Date.now();
  // The four cells are backed by git and ledger queries; reloading them for a
  // frame that only moved a clock is wasted work (the diff.get behind one
  // cell shells out to git).
  var key = panelKey();
  if (!force && key === lastGridKey) return;
  if (!force && now - gridLoadedAt < 2500) return;
  gridLoadedAt = now;
  lastGridKey = key;
  query('dashboard.snapshot').then(function (snap) {
    if (snap.error && !snap.cwd) return;
    var html = '<h4>总览</h4>' + kv([
      ['目录', snap.cwd],
      ['循环', snap.mode || ''],
      ['模型', (snap.models || []).join(', ')],
      ['证据', snap.evidence ? snap.evidence.status : ''],
      ['债务', String((snap.debt || []).length)],
      ['交付', snap.delivery ? (snap.delivery.commit || '未提交') : '未交付']
    ]);
    if (snap.turn && snap.turn.objective) html += '<div class="metrics">❯ ' + esc(short(snap.turn.objective, 90)) + '</div>';
    paint($('cell-overview'), html);
  }).catch(function (error) { paint($('cell-overview'), '<h4>总览</h4><div class="err">' + esc(error.message) + '</div>'); });
  query('diff.get').then(function (data) {
    var html = '<h4>Diff</h4>';
    if (data.error && !(data.files || []).length) return paint($('cell-diff'), html + '<div class="err">' + esc(data.error) + '</div>');
    var files = data.files || [];
    html += '<div class="metrics">' + files.length + ' 个文件' + (data.baseline_count ? ' · 既有改动 ' + data.baseline_count : '') + '</div>';
    for (var i = 0; i < files.length && i < 6; i += 1) {
      html += '<div>' + badge(files[i].verification) + ' ' + esc(files[i].status) + ' ' + esc(files[i].path) + '</div>';
    }
    var lines = String(data.diff || '').split('\n').slice(0, 10);
    html += '<pre>' + esc(lines.join('\n')) + '</pre>';
    paint($('cell-diff'), html);
  }).catch(function (error) { paint($('cell-diff'), '<h4>Diff</h4><div class="err">' + esc(error.message) + '</div>'); });
  query('ledger.events', { limit: 80 }).then(function (data) {
    var html = '<h4>执行过程</h4>';
    if (data.error) return paint($('cell-timeline'), html + '<div class="err">' + esc(data.error) + '</div>');
    if (data.chain) html += '<div>哈希链 ' + (data.chain.valid ? '<span class="badge verified">完整</span>' : '<span class="badge contradicted">断裂</span>') + ' · ' + esc(data.chain.events) + ' 条</div>';
    var events = (data.events || []).slice(-6);
    for (var i = 0; i < events.length; i += 1) html += '<div class="metrics">#' + esc(events[i].seq) + ' ' + esc(events[i].kind) + '</div>';
    paint($('cell-timeline'), html);
  }).catch(function (error) { paint($('cell-timeline'), '<h4>执行过程</h4><div class="err">' + esc(error.message) + '</div>'); });
  query('evidence.chain').then(function (data) {
    var html = '<h4>证据链 ' + badge(data.status) + '</h4>';
    if (data.chain) html += '<div>L0 ' + (data.chain.valid ? '<span class="badge verified">完整</span>' : '<span class="badge contradicted">断裂</span>') + '</div>';
    html += '<div class="metrics">事实 ' + ((data.facts || []).length) + ' · 核验 ' + ((data.verdicts || []).length) + ' · 主张 ' + ((data.claims || []).length) + ' · 交付 ' + (data.delivered ? '有' : '无') + '</div>';
    paint($('cell-evidence'), html);
  }).catch(function (error) { paint($('cell-evidence'), '<h4>证据链</h4><div class="err">' + esc(error.message) + '</div>'); });
}

function loadPanel() {
  var panel = $('panel');
  if (activeTab === 'overview') return loadOverview(panel);
  if (activeTab === 'diff') return loadDiff(panel);
  if (activeTab === 'timeline') return loadTimeline(panel);
  if (activeTab === 'evidence') return loadEvidence(panel);
  if (activeTab === 'plan') return loadPlan(panel);
  if (activeTab === 'approvals') return loadApprovals(panel);
  if (activeTab === 'cost') return loadCost(panel);
  if (activeTab === 'workspace') return loadWorkspace(panel);
  if (activeTab === 'sessions') return loadSessions(panel);
}

function fail(panel, error) {
  paint(panel, '<div class="err">' + esc(error || '查询失败') + '</div>');
}

function loadOverview(panel) {
  if (unpainted(panel)) paint(panel, '<div class="empty">读取中…</div>');
  Promise.all([query('dashboard.snapshot'), query('workspace.status')]).then(function (both) {
    var snap = both[0], ws = both[1];
    if (snap.error && !snap.cwd) return fail(panel, snap.error);
    var delivery = snap.delivery || ws.delivery;
    var html = '<h3>总览</h3>';
    html += kv([
      ['目录', snap.cwd],
      ['循环', snap.mode],
      ['模型', (snap.models || []).join(', ')],
      ['轮次', snap.turn ? snap.turn.id : ''],
      ['目标', snap.turn ? snap.turn.objective : ''],
      ['证据', snap.evidence ? snap.evidence.status : ''],
      ['账本', snap.ledger || '（本轮无）'],
      ['分支', ws.is_git ? ws.branch + ' @ ' + (ws.head || '') : '非 git 仓库'],
      ['脏文件', String(ws.dirty_count == null ? 0 : ws.dirty_count) + (ws.baseline_count ? '（既有 ' + ws.baseline_count + '）' : '')],
      ['交付 commit', delivery ? (delivery.commit || '未提交') : '未交付']
    ]);
    html += '<h4>内核状态</h4>' + kv(Object.keys(snap.status || {}).map(function (k) {
      return [k, String(snap.status[k])];
    }));
    if (snap.debt && snap.debt.length) {
      html += '<h4>债务清单 · ' + snap.debt.length + '</h4>';
      html += table(['义务', '原因', '尝试', '下一步'], snap.debt.map(function (d) {
        return [esc(d.kind + (d.target ? ' ' + d.target : '')), esc(d.reason), esc(d.attempts), esc(d.next_action || '—')];
      }));
    }
    if ((snap.approvals || []).length || (snap.prompts || []).length) {
      html += '<h4>等待中的交互</h4>';
      html += '<div>审批：' + esc((snap.approvals || []).join(', ') || '无') + '</div>';
      html += '<div>提问：' + esc((snap.prompts || []).join(', ') || '无') + '</div>';
    }
    paint(panel, html);
  }).catch(function (error) { fail(panel, error.message); });
}

function loadDiff(panel) {
  if (lastDiff && Date.now() - (lastDiff.at || 0) < 4000) return renderDiff(panel);
  if (unpainted(panel)) paint(panel, '<div class="empty">读取中…</div>');
  query('diff.get').then(function (data) {
    data.at = Date.now();
    lastDiff = data;
    renderDiff(panel);
  }).catch(function (error) { fail(panel, error.message); });
}

function originBadge(origin) {
  if (origin === 'created') return '<span class="badge partial">新建</span>';
  if (origin === 'modified') return '<span class="badge partial">修改</span>';
  if (origin === 'baseline') return '<span class="badge unverified">既有改动</span>';
  return '';
}

function renderDiff(panel) {
  var data = lastDiff || {};
  if (data.error && !(data.files || []).length) return fail(panel, data.error);
  var delivery = data.delivery || {};
  var html = '<h3>文件 Diff</h3>';
  html += '<div class="metrics">';
  html += '<button data-diffmode="unified" class="' + (lastDiffMode === 'unified' ? 'active' : '') + '">unified</button>';
  html += '<button data-diffmode="side" class="' + (lastDiffMode === 'side' ? 'active' : '') + '">并排</button>';
  if (delivery.commit) html += ' <span class="badge verified">已交付 ' + esc(delivery.commit) + '</span>';
  if (data.baseline_count) html += ' <span class="badge unverified">既有改动 ' + esc(data.baseline_count) + '（非本会话）</span>';
  html += '</div>';
  html += table(['状态', '路径', '来源', '核验'], (data.files || []).map(function (f) {
    return [esc(f.status), esc(f.path), originBadge(f.origin), badge(f.verification)];
  }));
  html += '<div class="metrics">已核验 ' + ((delivery.verified_paths || []).length) +
    ' · 归因 ' + ((delivery.attributed_paths || []).length) +
    ' · 未核验 ' + ((delivery.unverified_paths || []).length) + '</div>';
  if (data.truncated) html += '<div class="banner" style="margin:0 0 8px">diff 已截断</div>';
  if (lastDiffMode === 'side') html += renderSideDiff(String(data.diff || ''));
  else html += renderUnifiedDiff(String(data.diff || ''));
  if ((data.baseline || []).length) {
    html += '<h4>baseline 污染 · ' + data.baseline.length + '</h4>';
    html += '<div class="empty">这些改动在会话开始前就已存在，不是本次任务的产物，交付时不会被计入核验。</div>';
    html += table(['状态', '路径'], data.baseline.map(function (f) { return [esc(f.status), esc(f.path)]; }));
  }
  panel.innerHTML = html;
  var buttons = panel.querySelectorAll('button[data-diffmode]');
  for (var b = 0; b < buttons.length; b += 1) {
    buttons[b].onclick = function () {
      lastDiffMode = this.getAttribute('data-diffmode');
      renderDiff(panel);
    };
  }
}

function renderUnifiedDiff(patch) {
  var html = '<pre id="diffBody">';
  var lines = patch.split('\n');
  for (var i = 0; i < lines.length; i += 1) {
    var line = lines[i];
    var cls = line.indexOf('+++') === 0 || line.indexOf('---') === 0 ? 'hunk' :
      line.charAt(0) === '+' ? 'add' : line.charAt(0) === '-' ? 'del' :
      line.indexOf('@@') === 0 ? 'hunk' : '';
    html += '<span class="' + cls + '">' + esc(line) + '</span>\n';
  }
  return html + '</pre>';
}

function diffSideRows(patch) {
  // Pair each removed line with the added line that replaced it.  Git emits a
  // run of '-' lines then a run of '+' lines, so holding the deletes until the
  // adds arrive gives a stable side-by-side reading without a diff engine.
  var rows = [];
  var deletes = [];
  var lines = patch.split('\n');
  for (var i = 0; i < lines.length; i += 1) {
    var line = lines[i];
    if (line.indexOf('---') === 0 || line.indexOf('+++') === 0 || line.indexOf('@@') === 0) {
      while (deletes.length) rows.push([deletes.shift(), '']);
      rows.push([line, '']);
    } else if (line.charAt(0) === '-') {
      deletes.push(line);
    } else if (line.charAt(0) === '+') {
      rows.push([deletes.length ? deletes.shift() : '', line]);
    } else {
      while (deletes.length) rows.push([deletes.shift(), '']);
      rows.push([line, line]);
    }
  }
  while (deletes.length) rows.push([deletes.shift(), '']);
  return rows;
}

function renderSideDiff(patch) {
  var rows = diffSideRows(patch).map(function (pair) {
    var left = pair[0], right = pair[1];
    var lcls = left.indexOf('@@') === 0 ? 'hunk' : left.charAt(0) === '-' ? 'del' : '';
    var rcls = right.indexOf('@@') === 0 ? 'hunk' : right.charAt(0) === '+' ? 'add' : '';
    return ['<span class="' + lcls + '">' + esc(left) + '</span>', '<span class="' + rcls + '">' + esc(right) + '</span>'];
  });
  return '<pre class="side-by-side">' + rows.map(function (pair) {
    return '<span class="side-row">' + pair[0] + '│' + pair[1] + '</span>\n';
  }).join('') + '</pre>';
}

// 执行过程默认折叠「记账事件」（状态流转 / token 用量 / 思考）。它们对回放是
// 骨架，对读的人是噪声：一次真实任务有上百条，其中大半只是 running→thinking。
// 账本一条不少，这里只是视图（#441）。
var showAllEvents = false;

function loadTimeline(panel) {
  if (unpainted(panel)) paint(panel, '<div class="empty">读取中…</div>');
  query('ledger.events', { limit: 200, fold: !showAllEvents }).then(function (data) {
    if (data.error) return fail(panel, data.error);
    var html = '<h3>执行过程' +
      (showAllEvents ? ' <a href="#" id="fold-events">只看结论性事件</a>'
                     : ' <a href="#" id="show-events">显示全部事件</a>') + '</h3>';
    if (data.chain) {
      html += '<div>哈希链：' + (data.chain.valid ? '<span class="badge verified">完整</span>' : '<span class="badge contradicted">断裂</span>') +
        ' · ' + esc(data.chain.events) + ' 条事件</div>';
    }
    var events = data.events || [];
    if (data.folded) {
      html += '<div class="metrics">本轮 ' + esc(data.total) + ' 条事件 · 显示 ' + esc(data.shown) +
        ' 行 · 已折叠 ' + esc(data.folded) + ' 条记账事件</div>';
    }
    var times = events.map(function (e) { return Date.parse(e.ts) || 0; });
    var total = 0;
    for (var t = 1; t < times.length; t += 1) if (times[t] && times[t - 1]) total += Math.max(0, (times[t] - times[t - 1]) / 1000);
    if (total) html += '<div class="metrics">总耗时 ' + esc(fmtDur(total)) + ' · ' + events.length + ' 事件 · token ' + esc(String(tokenTotals(events))) + '</div>';
    html += '<table class="waterfall"><thead><tr><th>#</th><th>时间线</th><th>事件</th><th>Δ</th><th>token</th><th>负载</th></tr></thead><tbody>';
    var prevKind = '';
    var maxGap = 0;
    var gaps = [];
    for (var g = 1; g < times.length; g += 1) {
      var gap = times[g] && times[g - 1] ? Math.max(0, (times[g] - times[g - 1]) / 1000) : 0;
      gaps.push(gap);
      if (gap > maxGap) maxGap = gap;
    }
    for (var i = 0; i < events.length; i += 1) {
      var e = events[i];
      var delta = i > 0 ? gaps[i - 1] : 0;
      var stall = delta > 30;
      var retry = i > 0 && e.kind === prevKind;
      var cancelled = /interrupt|cancel/.test(e.kind);
      var width = maxGap ? Math.max(2, Math.round(delta / maxGap * 100)) : 0;
      var cellClass = 'bar' + (stall ? ' stall' : '') + (retry ? ' retry' : '');
      if (e.collapsed) {
        var foldInfo = e.payload || {};
        html += '<tr class="folded"><td>' + esc(foldInfo.from_seq) + '</td>';
        html += '<td><div class="bar-track"><span class="bar" style="width:2%"></span></div></td>';
        html += '<td>⋯ ' + esc(foldInfo.count) + ' 条记账事件（' + esc((foldInfo.kinds || []).join(', ')) + '）</td>';
        html += '<td>—</td><td></td>';
        html += '<td><details><summary>seq ' + esc(foldInfo.from_seq) + '–' + esc(foldInfo.to_seq) +
          ' 已折叠</summary><pre>' + esc('账本里仍在：用「显示全部事件」或 xenon replay 查看。') + '</pre></details></td></tr>';
        prevKind = e.kind;
        continue;
      }
      html += '<tr id="event-' + esc(e.seq) + '"><td>' + esc(e.seq) + '</td>';
      html += '<td><div class="bar-track"><span class="' + cellClass + '" style="width:' + width + '%"></span></div></td>';
      html += '<td>' + esc(e.kind) + (retry ? ' <span class="badge unverified">重试</span>' : '') +
        (cancelled ? ' <span class="badge contradicted">取消/中断</span>' : '') + '</td>';
      html += '<td>' + (i === 0 ? '—' : esc(fmtDur(delta))) + (stall ? ' <span class="badge unverified">停滞</span>' : '') + '</td>';
      html += '<td>' + esc(String(tokenOf(e.payload) || '')) + '</td>';
      html += '<td><details><summary>' + esc(short(JSON.stringify(e.payload), 80)) + '</summary><pre>' + esc(JSON.stringify(e.payload, null, 2)) + '</pre></details></td></tr>';
      prevKind = e.kind;
    }
    html += '</tbody></table>';
    paint(panel, html);
    var showLink = document.getElementById('show-events');
    if (showLink) showLink.onclick = function (ev) { ev.preventDefault(); showAllEvents = true; loadTimeline(panel); };
    var foldLink = document.getElementById('fold-events');
    if (foldLink) foldLink.onclick = function (ev) { ev.preventDefault(); showAllEvents = false; loadTimeline(panel); };
    var hash = /^#event=(\d+)$/.exec(location.hash);
    if (hash) {
      var row = document.getElementById('event-' + hash[1]);
      if (row) { row.style.background = '#1c2b47'; row.scrollIntoView(); }
    }
  }).catch(function (error) { fail(panel, error.message); });
}

function loadEvidence(panel) {
  if (unpainted(panel)) paint(panel, '<div class="empty">读取中…</div>');
  query('evidence.chain').then(function (data) {
    var html = '<h3>证据链 ' + badge(data.status) + '</h3>';
    if (data.chain) {
      html += '<div>L0 哈希链：' + (data.chain.valid ? '<span class="badge verified">完整</span>' : '<span class="badge contradicted">断裂</span>') + '</div>';
    }
    if (data.error && !(data.facts || []).length) html += '<div class="empty">' + esc(data.error) + '</div>';
    html += evidencePart('事实（工具回执）', data.facts);
    html += evidencePart('核验（步骤判定）', data.verdicts);
    html += evidencePart('主张', data.claims);
    if (data.delivered) {
      html += '<h4>交付</h4><pre>' + esc(JSON.stringify(data.delivered.payload, null, 2)) + '</pre>';
    }
    if (data.projection && Object.keys(data.projection).length) {
      html += '<h4>证据投影</h4><details><summary>V0/V1/V2 投影 JSON</summary><pre>' +
        esc(JSON.stringify(data.projection, null, 2)) + '</pre></details>';
    }
    paint(panel, html);
  }).catch(function (error) { fail(panel, error.message); });
}

function evidencePart(title, rows) {
  var html = '<h4>' + esc(title) + ' · ' + ((rows || []).length) + '</h4>';
  if (!rows || !rows.length) return html + '<div class="empty">无</div>';
  html += '<table><tbody>';
  for (var i = 0; i < rows.length; i += 1) {
    html += '<tr><td>' + esc(rows[i].seq) + '</td><td><details><summary>' +
      esc(short(JSON.stringify(rows[i].payload), 90)) + '</summary><pre>' +
      esc(JSON.stringify(rows[i].payload, null, 2)) + '</pre></details></td></tr>';
  }
  return html + '</tbody></table>';
}

function loadPlan(panel) {
  if (unpainted(panel)) paint(panel, '<div class="empty">读取中…</div>');
  query('plan.get').then(function (data) {
    if (data.error) return fail(panel, data.error);
    var projection = data.plan || {};
    var statuses = projection.steps || {};
    var declared = projection.plan && projection.plan.steps ? projection.plan.steps : projection.plan;
    var defs = [];
    if (Array.isArray(declared)) defs = declared;
    else if (declared && typeof declared === 'object' && Array.isArray(declared.steps)) defs = declared.steps;
    var order = [];
    var byId = {};
    for (var d = 0; d < defs.length; d += 1) {
      var item = defs[d] || {};
      var id = String(item.id == null ? (d + 1) : item.id);
      byId[id] = item;
      order.push(id);
    }
    Object.keys(statuses).forEach(function (id) {
      if (!byId[id]) { byId[id] = {}; order.push(id); }
    });
    var html = '<h3>计划看板</h3>';
    if (data.proposed && !data.accepted) html += '<div class="banner" style="margin:0 0 8px">待批准计划：终端里确认后才会执行</div>';
    else if (data.accepted) html += '<div class="metrics">计划已批准 · 版本 ' + esc(projection.versions == null ? 1 : projection.versions) + '</div>';
    else if (data.rejected) html += '<div class="banner" style="margin:0 0 8px">计划已驳回，未执行</div>';
    if (data.current) html += '<div class="metrics"><span class="badge partial">当前步骤 ' + esc(data.current) + '</span></div>';
    if (!order.length) { html += '<div class="empty">本轮没有计划</div>'; paint(panel, html); return; }
    html += '<div class="flow">';
    for (var i = 0; i < order.length; i += 1) {
      var id2 = order[i];
      var def = byId[id2] || {};
      var live = statuses[id2] || {};
      var status = String(live.status || def.status || 'pending');
      var task = live.task || def.intent || def.task || def.description || '';
      var deps = def.depends_on || [];
      html += '<div class="step' + (id2 === data.current ? ' current' : '') + '">';
      html += '<div><b>' + esc(id2) + '</b> ' + badge(planBadge(status)) + ' <span class="badge muted">' + esc(def.kind || '') + '</span></div>';
      if (task) html += '<div>' + esc(short(String(task), 140)) + '</div>';
      if (deps.length) html += '<div class="deps">依赖 ' + esc(deps.map(String).join(', ')) + '</div>';
      html += '</div>';
      if (i < order.length - 1) html += '<div class="deps">↓</div>';
    }
    html += '</div>';
    html += '<div class="metrics">版本 ' + esc(projection.versions == null ? 1 : projection.versions) +
      ' · 状态 ' + (data.accepted ? '已批准' : data.proposed ? '待批准' : data.rejected ? '已驳回' : '未报告') + '</div>';
    paint(panel, html);
  }).catch(function (error) { fail(panel, error.message); });
}

function planBadge(status) {
  if (status === 'closed' || status === 'satisfied') return 'verified';
  if (status === 'running') return 'partial';
  if (status === 'released-with-gaps' || status === 'released_with_gaps') return 'unverified';
  return status;
}

function loadApprovals(panel) {
  if (unpainted(panel)) paint(panel, '<div class="empty">读取中…</div>');
  query('dashboard.snapshot').then(function (snap) {
    if (snap.error && !snap.cwd) return fail(panel, snap.error);
    var html = '<h3>审批收件箱</h3>';
    html += '<div>待审批：' + esc((snap.approvals || []).join(', ') || '无') + '</div>';
    html += '<div>待回答：' + esc((snap.prompts || []).join(', ') || '无') + '</div>';
    html += '<h4>当前卡片</h4>';
    var approval = state.turn && state.turn.approval;
    var showing = approval && !(answeredApproval && answeredApproval.id === approval.id) ? approval : null;
    if (showing) {
      html += '<div class="card"><h4>' + esc(showing.capability || showing.tool || '审批') + '</h4>';
      if (showing.reason) html += '<div class="why">' + esc(showing.reason) + '</div>';
      if (showing.targets && showing.targets.length) html += '<div class="targets">' + esc(showing.targets.join('\n')) + '</div>';
      if (showing.diff_preview) html += '<pre>' + esc(showing.diff_preview) + '</pre>';
      var options = showing.options && showing.options.length ? showing.options : ['once', 'turn', 'allow', 'deny', 'cancel'];
      html += '<div class="btns">';
      for (var i = 0; i < options.length; i += 1) {
        html += '<button data-approval="' + esc(options[i]) + '">' + esc(choiceLabel(options[i])) + '</button>';
      }
      html += '</div>';
      html += '<div class="empty">终端与浏览器看到的是同一个请求；任一侧作答后另一侧立即失效。</div></div>';
    } else if (answeredApproval) {
      html += '<div class="card note"><h4>✓ 已应答：' + esc(answeredApproval.label) + '</h4>' +
        '<div class="why">已发送给内核，等待它继续。若长时间停在这里，说明内核没有确认这次应答。</div></div>';
    } else {
      html += '<div class="empty">没有等待确认的请求</div>';
    }
    if (approval && answeredApproval && approval.id === answeredApproval.id) {
      html += '<div class="empty">这张卡片已经应答过，不会再接受第二次选择。</div>';
    }
    if (state.prompt) {
      html += '<div class="card"><h4>输入 · ' + esc(state.prompt.message) + '</h4>';
      html += '<div class="btns"><input id="panelPromptValue" type="' + (state.prompt.secret ? 'password' : 'text') +
        '" style="flex:1;background:#0a0f1c;color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:4px 8px">';
      html += '<button id="panelPromptSend" class="primary">提交</button></div></div>';
    }
    paint(panel, html);
    var buttons = panel.querySelectorAll('button[data-approval]');
    for (var b = 0; b < buttons.length; b += 1) {
      buttons[b].onclick = function () { chooseApproval(this.getAttribute('data-approval')); };
    }
    var send = document.getElementById('panelPromptSend');
    if (send) {
      send.onclick = function () { act({ action: 'prompt', value: document.getElementById('panelPromptValue').value }); };
    }
  }).catch(function (error) { fail(panel, error.message); });
}

function loadCost(panel) {
  if (unpainted(panel)) paint(panel, '<div class="empty">读取中…</div>');
  query('report.get').then(function (data) {
    if (data.error) return fail(panel, data.error);
    var html = '<h3>成本与缓存</h3>' + renderReport(data);
    paint(panel, html);
  }).catch(function (error) { fail(panel, error.message); });
}

function loadWorkspace(panel) {
  if (unpainted(panel)) paint(panel, '<div class="empty">读取中…</div>');
  query('workspace.status').then(function (ws) {
    if (ws.error) return fail(panel, ws.error);
    var delivery = ws.delivery || {};
    var html = '<h3>工作区与交付</h3>';
    html += kv([
      ['目录', ws.cwd],
      ['git', ws.is_git ? '是' : '否'],
      ['分支', ws.branch || ''],
      ['HEAD', ws.head || ''],
      ['账本', ws.ledger || '（本轮无）'],
      ['交付 commit', delivery.commit || '未交付'],
      ['已核验', (delivery.verified_paths || []).join(', ') || '无'],
      ['归因（未直接核验）', (delivery.attributed_paths || []).join(', ') || '无'],
      ['未核验', (delivery.unverified_paths || []).join(', ') || '无'],
      ['本会话新建', (ws.session_created || []).join(', ') || '无'],
      ['本会话修改', (ws.session_modified || []).join(', ') || '无']
    ]);
    html += '<h4>脏文件 · ' + (ws.dirty_count == null ? 0 : ws.dirty_count) + '</h4>';
    html += table(['状态', '路径', '来源'], (ws.dirty || []).map(function (f) {
      var origin = coveredBy(ws.session_created, f.path) ? '<span class="badge partial">新建</span>'
        : coveredBy(ws.session_modified, f.path) ? '<span class="badge partial">修改</span>'
        : '<span class="badge unverified">既有改动</span>';
      return [esc(f.status), esc(f.path), origin];
    }));
    if ((ws.baseline || []).length) {
      html += '<h4>baseline 污染 · ' + ws.baseline_count + '</h4>';
      html += '<div class="empty">会话开始前已存在，不计入本次交付。</div>';
      html += table(['状态', '路径'], ws.baseline.map(function (f) { return [esc(f.status), esc(f.path)]; }));
    }
    paint(panel, html);
  }).catch(function (error) { fail(panel, error.message); });
}

function coveredBy(list, path) {
  var wanted = String(path == null ? '' : path).replace(/\\/g, '/').replace(/^\.\//, '');
  var items = list || [];
  for (var i = 0; i < items.length; i += 1) {
    var candidate = String(items[i] == null ? '' : items[i]).replace(/\\/g, '/').replace(/^\.\//, '');
    if (candidate === wanted || candidate.slice(-(wanted.length + 1)) === '/' + wanted) return true;
  }
  return false;
}

function loadSessions(panel) {
  if (unpainted(panel)) paint(panel, '<div class="empty">读取中…</div>');
  Promise.all([query('sessions.list'), query('memory.list'), query('mcp.status'), query('sessions.tree')]).then(function (all) {
    var sessions = all[0], memory = all[1], mcp = all[2], tree = all[3];
    var html = '<h3>会话</h3>';
    html += renderSessionTree(tree);
    html += '<h4>已保存会话</h4>';
    html += table(['名称', '时间', '消息', '循环'], (sessions.sessions || []).map(function (s) {
      // Old sessions stored a preset name; every one of them is the same
      // single loop, so the panel says what is true now, not the old label.
      return [esc(s.name), esc(s.saved_at), esc(s.messages), '单循环'];
    }));
    html += '<h4>记忆 · ' + (memory.count == null ? 0 : memory.count) + '</h4>';
    var items = memory.items || [];
    if (!items.length) html += '<div class="empty">无</div>';
    for (var i = 0; i < items.length && i < 50; i += 1) {
      html += '<details><summary>' + esc(short(JSON.stringify(items[i]), 90)) + '</summary><pre>' +
        esc(JSON.stringify(items[i], null, 2)) + '</pre></details>';
    }
    html += '<h4>MCP 服务器 · ' + ((mcp.servers || []).length) + '</h4>';
    html += '<div>工具：' + esc((mcp.tools || []).join(', ') || '未连接') + '</div>';
    for (var s = 0; s < (mcp.servers || []).length; s += 1) {
      html += '<details><summary>' + esc(JSON.stringify(mcp.servers[s]).slice(0, 120)) + '</summary><pre>' +
        esc(JSON.stringify(mcp.servers[s], null, 2)) + '</pre></details>';
    }
    paint(panel, html);
  }).catch(function (error) { fail(panel, error.message); });
}

function renderSessionTree(tree) {
  var html = '<h4>会话树 · fork / rewind</h4>';
  if (!tree || tree.error) return html + '<div class="empty">' + esc((tree && tree.error) || '无事件日志') + '</div>';
  var nodes = tree.nodes || [];
  if (!nodes.length) return html + '<div class="empty">没有会话事件日志</div>';
  var byId = {};
  for (var i = 0; i < nodes.length; i += 1) byId[nodes[i].id] = nodes[i];
  var children = {};
  for (var n = 0; n < nodes.length; n += 1) {
    var parent = nodes[n].parent;
    if (parent && byId[parent]) {
      if (!children[parent]) children[parent] = [];
      children[parent].push(nodes[n].id);
    }
  }
  var roots = (tree.roots || []).slice();
  if (!roots.length) {
    for (var r = 0; r < nodes.length; r += 1) if (!nodes[r].parent || !byId[nodes[r].parent]) roots.push(nodes[r].id);
  }
  var lines = [];
  function walk(id, depth, seen) {
    if (seen[id]) return;
    seen[id] = true;
    var node = byId[id];
    if (!node) return;
    var line = '<div class="metrics tree-node">';
    for (var d = 0; d < depth; d += 1) line += '&nbsp;&nbsp;&nbsp;';
    line += (depth ? '└ ' : '') + '<b>' + esc(id) + '</b> · ' + esc(node.turns) + ' 轮 · ' + esc(node.events) + ' 事件';
    var when = node.last_at || node.first_at;
    if (when) line += ' · ' + esc(fmtTime(when));
    lines.push(line + '</div>');
    var kids = children[id] || [];
    for (var k = 0; k < kids.length; k += 1) walk(kids[k], depth + 1, seen);
  }
  var seen = {};
  for (var root = 0; root < roots.length; root += 1) walk(roots[root], 0, seen);
  for (var leftover = 0; leftover < nodes.length; leftover += 1) if (!seen[nodes[leftover].id]) walk(nodes[leftover].id, 0, seen);
  return html + '<div class="tree">' + lines.join('') + '</div>';
}

function fmtTime(seconds) {
  var value = Number(seconds);
  if (!value) return '';
  var date = new Date(value < 1e12 ? value * 1000 : value);
  return date.toLocaleString();
}

function fmtDur(seconds) {
  var value = Number(seconds) || 0;
  if (value < 60) return value.toFixed(1) + 's';
  var minutes = Math.floor(value / 60);
  var rest = Math.round(value - minutes * 60);
  return minutes + 'm' + (rest ? rest + 's' : '');
}

function tokenOf(payload) {
  if (!payload || typeof payload !== 'object') return 0;
  var keys = ['tokens', 'total_tokens', 'prompt_tokens', 'completion_tokens', 'input_tokens', 'output_tokens'];
  var total = 0;
  var usage = payload.usage && typeof payload.usage === 'object' ? payload.usage : {};
  for (var i = 0; i < keys.length; i += 1) {
    if (typeof payload[keys[i]] === 'number') total += payload[keys[i]];
    if (typeof usage[keys[i]] === 'number') total += usage[keys[i]];
  }
  return total;
}

function tokenTotals(events) {
  var total = 0;
  for (var i = 0; i < (events || []).length; i += 1) total += tokenOf(events[i].payload);
  return total;
}

function short(text, limit) {
  return text.length > limit ? text.slice(0, limit - 1) + '…' : text;
}

// ── wiring ─────────────────────────────────────────────────────────
renderTabs();

$('composer').addEventListener('submit', function (event) {
  event.preventDefault();
  var input = $('input');
  var text = input.value.trim();
  if (!text) return;
  act({ action: 'submit', text: text });
  input.value = '';
  hideAcMenu();
});

$('input').addEventListener('input', renderAcMenu);
$('input').addEventListener('keydown', function (event) {
  if (acItems.length && !$('acmenu').classList.contains('hidden')) {
    if (event.key === 'ArrowDown') { event.preventDefault(); acIndex = (acIndex + 1) % acItems.length; renderAcMenu(); return; }
    if (event.key === 'ArrowUp') { event.preventDefault(); acIndex = (acIndex - 1 + acItems.length) % acItems.length; renderAcMenu(); return; }
    if (event.key === 'Tab') { event.preventDefault(); completeAc(acIndex); return; }
    if (event.key === 'Escape') { event.preventDefault(); hideAcMenu(); return; }
  }
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    $('composer').requestSubmit();
  }
});

document.addEventListener('keydown', function (event) {
  var tag = (event.target && event.target.tagName) || '';
  if (tag === 'TEXTAREA' || tag === 'INPUT') return;
  if (event.key === '?') { $('help').classList.toggle('hidden'); return; }
  var index = Number.parseInt(event.key, 10);
  if (!Number.isNaN(index) && index >= 1 && index <= TABS.length) { switchTab(TABS[index - 1][0]); return; }
  if (event.key === 'j') { $('panel').scrollTop += 60; }
  if (event.key === 'k') { $('panel').scrollTop -= 60; }
});

var source = new EventSource('/api/events?t=' + encodeURIComponent(token));
source.addEventListener('state', function (event) {
  var payload = JSON.parse(event.data);
  render(payload);
  $('conn').classList.add('on');
  $('conn').title = '已连接';
});
source.onerror = function () {
  $('conn').classList.remove('on');
  $('conn').title = '连接中断，正在重连…';
};
source.onopen = function () {
  $('conn').classList.add('on');
  $('conn').title = '已连接';
};

fetch('/api/state?t=' + encodeURIComponent(token))
  .then(function (res) { return res.json(); })
  .then(function (payload) { render(payload); })
  .catch(function () { /* SSE will retry */ });

loadPanel();
</script>
</body>
</html>
`;
