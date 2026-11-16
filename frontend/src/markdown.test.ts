/**
 * The answer pane reader.  It only has to be faithful: markup it understands
 * is dropped, everything else is shown exactly as the kernel wrote it.
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { parseInline, parseMarkdown } from './markdown.js';

function text(spans: { text: string }[]): string {
  return spans.map((span) => span.text).join('');
}

test('a windows path keeps every backslash', () => {
  const line = '位置: D:\\Xenon\\.xenon\\memory\\local\\project.md';
  assert.equal(text(parseInline(line)), line);
});

test('punctuation escapes in markdown still work', () => {
  assert.equal(text(parseInline('\\*not emphasis\\*')), '*not emphasis*');
});

test('a backslash before a letter is literal', () => {
  assert.equal(text(parseInline('C:\\new\\test')), 'C:\\new\\test');
});

test('bold and inline code drop their markers', () => {
  const spans = parseInline('**粗** 与 `code`');
  assert.equal(text(spans), '粗 与 code');
  assert.equal(spans[0]?.bold, true);
  assert.equal(spans[2]?.code, true);
});

test('fenced code keeps its body verbatim', () => {
  const blocks = parseMarkdown('```python\nprint("C:\\\\temp")\n```');
  assert.equal(blocks.length, 1);
  assert.deepEqual(blocks[0], {
    kind: 'code',
    language: 'python',
    lines: ['print("C:\\\\temp")'],
  });
});

test('headings and bullets become blocks, not raw text', () => {
  const blocks = parseMarkdown('# 标题\n- 一\n- 二');
  assert.equal(blocks[0]?.kind, 'heading');
  assert.equal(blocks[1]?.kind, 'bullet');
  assert.equal(blocks[2]?.kind, 'bullet');
});
