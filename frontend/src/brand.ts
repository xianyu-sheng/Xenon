/**
 * The Xenon startup identity: a pixel runner draws a track, sprints along it,
 * and sheds particles that assemble the XENON wordmark.
 *
 * The frames are generated from scripts/brand (the same source the Python
 * player used) into `brand-frames.ts`.  The animation belongs to this process
 * because the Python host's stdout carries the NDJSON protocol — it physically
 * cannot write a three-second ANSI animation any more.
 */

import { FRAMES, FRAME_COUNT, type BrandColor, type BrandRun } from './brand-frames.js';

export interface BrandSegment {
  text: string;
  color?: string;
  bold?: boolean;
  dim?: boolean;
}

export type BrandRow = BrandSegment[];

/** Semantic keys resolve to the same palette as xenon/repl/theme.py "logo.*". */
const PALETTE: Record<BrandColor, { color: string; bold?: boolean; dim?: boolean }> = {
  hot: { color: '#f0f9ff', bold: true },
  track: { color: '#334155' },
  runner: { color: '#67e8f9', bold: true },
  'letter-a': { color: '#67e8f9' },
  'letter-b': { color: '#818cf8' },
  'letter-c': { color: '#c4b5fd' },
  dash: { color: '#67e8f9' },
  spark: { color: '#67e8f9' },
  ghost: { color: '#94a3b8', dim: true },
};

const PLAIN = { color: undefined, bold: false, dim: false } as const;

function styleFor(key: BrandColor | ''): Omit<BrandSegment, 'text'> {
  if (key === '') return PLAIN;
  return PALETTE[key] ?? PLAIN;
}

/** Playback is a terminal enhancement, never a requirement. */
export function animationEnabled(): boolean {
  if (process.env.XENON_TERMINAL_ANIMATION === '0') return false;
  if ((process.env.TERM ?? '').toLowerCase() === 'dumb') return false;
  if (process.env.CI) return false;
  return Boolean(process.stdout.isTTY);
}

/** Rows for one authored frame, trailing spaces removed. */
export function renderFrame(index: number): BrandRow[] {
  const safe = Math.max(0, Math.min(FRAME_COUNT - 1, index));
  return FRAMES[safe]!.map((row) => toSegments(row, 0, rowWidth(row)));
}

function rowWidth(row: BrandRun[]): number {
  return row.reduce((width, [, run]) => width + run.length, 0);
}

/** Convert a run row to Ink segments; `end` bounds the visible width. */
function toSegments(row: BrandRun[], start: number, end: number): BrandRow {
  const segments: BrandSegment[] = [];
  let column = 0;
  for (const [key, run] of row) {
    for (const char of run) {
      const inRange = column >= start && column < end;
      if (inRange) {
        // Interior spaces are layout (the wordmark's letter gaps); only the
        // trailing run is trimmed, exactly as the Python player does.
        const style = char === ' ' ? PLAIN : styleFor(key);
        const last = segments[segments.length - 1];
        if (
          last &&
          last.color === style.color &&
          last.bold === style.bold &&
          last.dim === style.dim
        ) {
          last.text += char;
        } else {
          segments.push({ text: char, ...style });
        }
      }
      column += 1;
    }
  }
  trimTrailing(segments);
  return segments;
}

function trimTrailing(segments: BrandSegment[]): void {
  while (segments.length > 0) {
    const last = segments[segments.length - 1]!;
    last.text = last.text.replace(/\s+$/, '');
    if (last.text === '') segments.pop();
    else break;
  }
}

/**
 * The settled mark, cropped to its lit bounds: `────── XENON ──────`.
 *
 * Used as the static fallback (pipes, CI, NO_COLOR) and as the block the
 * scrollback keeps after the animation settles.
 */
export function settledMark(): BrandRow[] {
  const frame = FRAMES[FRAME_COUNT - 1]!;
  const rows = frame.map((row) => row.flatMap(([key, run]) => [...run].map((char) => [char, key] as const)));
  const litRows = rows
    .map((row, index) => ({ row, index }))
    .filter(({ row }) => row.some(([char]) => char !== ' '));
  if (litRows.length === 0) return [];
  const firstRow = litRows[0]!.index;
  const lastRow = litRows[litRows.length - 1]!.index;
  let firstColumn = Number.POSITIVE_INFINITY;
  let lastColumn = -1;
  for (const row of rows) {
    row.forEach(([char], column) => {
      if (char === ' ') return;
      firstColumn = Math.min(firstColumn, column);
      lastColumn = Math.max(lastColumn, column);
    });
  }
  if (lastColumn < 0) return [];
  const out: BrandRow[] = [];
  for (let r = firstRow; r <= lastRow; r += 1) {
    const segments: BrandSegment[] = [];
    for (let c = firstColumn; c <= lastColumn; c += 1) {
      const [char, key] = rows[r]![c] ?? [' ', ''];
      const style = styleFor(key as BrandColor | '');
      const last = segments[segments.length - 1];
      if (last && last.color === style.color && last.bold === style.bold && last.dim === style.dim) {
        last.text += char;
      } else {
        segments.push({ text: char, ...style });
      }
    }
    out.push(segments);
  }
  return out;
}

/** Duration and cadence of the authored storyboard (Python: 3.0 s at 12 FPS). */
export const ANIMATION_DURATION_MS = 3000;
export const ANIMATION_FPS = 12;
