/**
 * A deliberately small Markdown reader for the answer pane.
 *
 * The Python TUI rendered answers with rich.markdown.  Ink has no comparable
 * widget, and pulling a whole parser in would be a lot of surface for what
 * answers actually use: fenced code, headings, lists, bold and inline code.
 * Everything else is shown as written — this never invents formatting.
 */

export interface InlineSpan {
  text: string;
  bold?: boolean;
  code?: boolean;
}

export type MarkdownBlock =
  | { kind: 'code'; language: string; lines: string[] }
  | { kind: 'heading'; level: number; spans: InlineSpan[] }
  | { kind: 'bullet'; depth: number; marker: string; spans: InlineSpan[] }
  | { kind: 'paragraph'; spans: InlineSpan[] }
  | { kind: 'rule' };

const FENCE = /^\s*(```+|~~~+)\s*([\w+-]*)\s*$/;
const HEADING = /^\s*(#{1,6})\s+(.*)$/;
const BULLET = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
const RULE = /^\s*([-*_])(\s*\1){2,}\s*$/;

export function parseMarkdown(text: string): MarkdownBlock[] {
  const blocks: MarkdownBlock[] = [];
  const lines = text.replace(/\r\n?/g, '\n').split('\n');
  let index = 0;
  let paragraph: string[] = [];

  const flush = (): void => {
    if (paragraph.length === 0) return;
    blocks.push({ kind: 'paragraph', spans: parseInline(paragraph.join('\n')) });
    paragraph = [];
  };

  while (index < lines.length) {
    const line = lines[index]!;

    const fence = FENCE.exec(line);
    if (fence) {
      flush();
      const marker = fence[1]!;
      const language = fence[2] ?? '';
      const body: string[] = [];
      index += 1;
      while (index < lines.length && !lines[index]!.trimStart().startsWith(marker)) {
        body.push(lines[index]!);
        index += 1;
      }
      index += 1; // closing fence (or end of text)
      blocks.push({ kind: 'code', language, lines: body });
      continue;
    }

    if (RULE.test(line)) {
      flush();
      blocks.push({ kind: 'rule' });
      index += 1;
      continue;
    }

    const heading = HEADING.exec(line);
    if (heading) {
      flush();
      blocks.push({
        kind: 'heading',
        level: heading[1]!.length,
        spans: parseInline(heading[2] ?? ''),
      });
      index += 1;
      continue;
    }

    const bullet = BULLET.exec(line);
    if (bullet) {
      flush();
      const indent = bullet[1] ?? '';
      blocks.push({
        kind: 'bullet',
        depth: Math.floor(indent.replace(/\t/g, '  ').length / 2),
        marker: bullet[2] ?? '-',
        spans: parseInline(bullet[3] ?? ''),
      });
      index += 1;
      continue;
    }

    if (line.trim() === '') {
      flush();
      index += 1;
      continue;
    }

    paragraph.push(line);
    index += 1;
  }
  flush();
  return blocks;
}

/**
 * CommonMark treats a backslash as an escape only before ASCII punctuation.
 * Anything else — most importantly a Windows path — is literal text, so
 * `D:\Xenon\.xenon\memory` must survive the answer pane intact.
 */
const ESCAPABLE = new Set(
  '!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~'.split(''),
);

/**
 * True when the backslash at ``index`` sits inside a path token.
 *
 * ``\.xenon`` is a valid CommonMark escape (punctuation follows), but on
 * Windows it is a directory separator — the memory receipt rendered the real
 * file as `D:\Xenon.xenon\memory\...`.  A backslash after a token that
 * already contains a separator belongs to the path.
 */
function insidePath(text: string, index: number): boolean {
  const start = Math.max(
    text.lastIndexOf(' ', index),
    text.lastIndexOf('\t', index),
    text.lastIndexOf('\n', index),
  ) + 1;
  const before = text.slice(start, index);
  return before.includes('\\') || before.includes('/') || /^[A-Za-z]:$/.test(before);
}

/** Split a line into bold / inline-code spans, dropping the markers. */
export function parseInline(text: string): InlineSpan[] {
  const spans: InlineSpan[] = [];
  let buffer = '';
  let index = 0;
  const push = (span: InlineSpan): void => {
    if (buffer) {
      spans.push({ text: buffer });
      buffer = '';
    }
    spans.push(span);
  };

  while (index < text.length) {
    if (text[index] === '`') {
      const end = text.indexOf('`', index + 1);
      if (end > index + 1) {
        push({ text: text.slice(index + 1, end), code: true });
        index = end + 1;
        continue;
      }
    }
    if (text.startsWith('**', index)) {
      const end = text.indexOf('**', index + 2);
      if (end > index + 2) {
        push({ text: text.slice(index + 2, end), bold: true });
        index = end + 2;
        continue;
      }
    }
    if (
      text[index] === '\\' &&
      index + 1 < text.length &&
      ESCAPABLE.has(text[index + 1]!) &&
      !insidePath(text, index)
    ) {
      buffer += text[index + 1];
      index += 2;
      continue;
    }
    buffer += text[index];
    index += 1;
  }
  if (buffer) spans.push({ text: buffer });
  return spans;
}
