/**
 * Clickable-path tests.
 *
 * The rule that matters: link real paths, never link prose.  A terminal that
 * renders `a/b` in a sentence as a hyperlink is worse than no links at all.
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { findPaths, linkFor, linkPaths } from './links.js';

const OSC8 = '\u001b]8';
const ST = '\u001b\\';

test('a code file with a line anchor becomes a link', () => {
  const line = 'see src/main.ts:42 for the call';
  const linked = linkPaths(line);

  assert.ok(linked.includes(OSC8), 'no OSC-8 opener');
  assert.ok(linked.includes('src/main.ts:42'), 'the visible text must survive');
  assert.ok(linked.includes('#L42'), 'the line anchor must reach the URI');
});

test('an absolute path is linked even without a code extension', () => {
  const line = '账本 C:\\Users\\Administrator\\.xenon\\ledger\\repl\\repl-abc.jsonl';
  const linked = linkPaths(line);

  assert.ok(linked.includes(OSC8), line);
  assert.ok(linked.includes('repl-abc.jsonl'));
});

test('a bare word with a slash is not a link', () => {
  const line = 'and/or either way';
  assert.equal(linkPaths(line), line);
});

test('prose with a ratio is not a link', () => {
  const line = '命中率 3/4，其余 1/4';
  assert.equal(linkPaths(line), line);
});

test('a fenced code block line is left alone', () => {
  const line = '```python';
  assert.equal(linkPaths(line), line);
});

test('an already-linked line is not wrapped twice', () => {
  const once = linkPaths('src/main.ts');
  assert.equal(linkPaths(once), once);
});

test('the link can be disabled entirely', () => {
  const line = 'src/main.ts:1';
  assert.equal(linkPaths(line, { enabled: false }), line);
});

test('findPaths reports every path in one line, in order', () => {
  const found = findPaths('a.py then b/c.ts:7');
  assert.deepEqual(
    found.map((m) => m.text),
    ['a.py', 'b/c.ts:7'],
  );
  assert.equal(found[1]?.line, 7);
});

test('a relative path gets a file:// URI that a terminal can open', () => {
  const uri = linkFor('src/main.ts', 3);
  assert.ok(uri.includes('file://./src/main.ts#L3'), uri);
});

test('a Windows path keeps its drive letter in the URI', () => {
  const uri = linkFor('C:\\x\\y.py', 1);
  assert.ok(uri.includes('file://C:/x/y.py#L1'), uri);
});

test('a line without paths is returned untouched', () => {
  const line = '没有任何路径的一句话';
  assert.equal(linkPaths(line), line);
});

test('a Windows path keeps the whole path in the URI, not just the file name', () => {
  const line = '账本 C:\\Users\\Administrator\\.xenon\\ledger\\repl\\repl-abc.jsonl';
  const linked = linkPaths(line);
  const uri = linked.match(/\u001b\]8;;([^\u001b]*)\u001b\\/)?.[1] ?? '';

  assert.ok(uri.includes('repl-abc.jsonl'), uri);
  assert.ok(
    uri.toLowerCase().includes('c:/users/administrator'),
    `the directory part was lost: ${uri}`,
  );
});
