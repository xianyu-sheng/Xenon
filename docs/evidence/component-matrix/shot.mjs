// Screenshot a live page after it has had time to load its data.
//
// `chrome --headless --screenshot` fires at the load event, which is before
// the dashboard's SSE snapshot and its git-backed queries answer; the panels
// came out blank.  This drives the DevTools protocol instead: navigate, wait,
// then capture.  Node 22 has a native WebSocket, so there is no dependency.
//
// usage: node shot.mjs <url> <out.png> [width] [height] [wait_ms]

import { spawn } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const [url, out, widthArg, heightArg, waitArg] = process.argv.slice(2);
if (!url || !out) {
  console.error('usage: node shot.mjs <url> <out.png> [width] [height] [wait_ms]');
  process.exit(2);
}
const width = Number(widthArg || 1600);
const height = Number(heightArg || 1100);
const wait = Number(waitArg || 9000);
const chrome = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const profile = mkdtempSync(join(tmpdir(), 'xenon-shot-'));
const port = 9300 + Math.floor(Math.random() * 500);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const proc = spawn(
  chrome,
  [
    '--headless=new',
    '--disable-gpu',
    '--hide-scrollbars',
    `--user-data-dir=${profile}`,
    `--remote-debugging-port=${port}`,
    'about:blank',
  ],
  { stdio: 'ignore' },
);

let target = null;
for (let attempt = 0; attempt < 60 && !target; attempt += 1) {
  try {
    const response = await fetch(`http://127.0.0.1:${port}/json/list`);
    const targets = await response.json();
    target = targets.find((item) => item.type === 'page' && item.webSocketDebuggerUrl);
  } catch {
    await sleep(200);
  }
}
if (!target) {
  console.error('chrome did not expose a page target');
  proc.kill();
  process.exit(1);
}

// The browser-level endpoint only answers Browser/Target domains; the page
// endpoint is the one that has Page.captureScreenshot.
const socket = new WebSocket(target.webSocketDebuggerUrl);
const pending = new Map();
let nextId = 0;
const send = (method, params = {}) =>
  new Promise((resolve, reject) => {
    const id = (nextId += 1);
    pending.set(id, { resolve, reject });
    socket.send(JSON.stringify({ id, method, params }));
  });
socket.onmessage = (event) => {
  const message = JSON.parse(event.data);
  const slot = message.id ? pending.get(message.id) : null;
  if (!slot) return;
  pending.delete(message.id);
  if (message.error) slot.reject(new Error(message.error.message));
  else slot.resolve(message.result);
};
await new Promise((resolve, reject) => {
  socket.onopen = resolve;
  socket.onerror = reject;
});

await send('Page.enable');
await send('Emulation.setDeviceMetricsOverride', {
  width,
  height,
  deviceScaleFactor: 1.5,
  mobile: false,
});
await send('Page.navigate', { url });
await sleep(wait);
const shot = await send('Page.captureScreenshot', { format: 'png' });
writeFileSync(out, Buffer.from(shot.data, 'base64'));
proc.kill();
console.log(`wrote ${out}`);
