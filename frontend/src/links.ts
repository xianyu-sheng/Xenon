/**
 * Turn file paths in kernel output into clickable links.
 *
 * Modern terminals (Windows Terminal, iTerm2, Kitty, WezTerm, VSCode) treat
 * OSC-8 escape sequences as hyperlinks.  The kernel reports paths — a ledger
 * file, a written source file — and the user should be able to open them
 * without copying text.
 *
 * Ported from the Python `repl/clickable_paths.py`, which was built and tested
 * but never wired to any renderer.  This is that wiring, in the layer that
 * actually renders.
 */

const OSC8 = '\u001b]8';
// BEL, not ST.  Both terminate an OSC-8 link by spec, but the ST form
// (`ESC \`) is swallowed inconsistently by emulators and by ANSI-aware string
// helpers, taking the link's own display text with it.  BEL is the form every
// terminal in the wild parses identically.
const ST = '\u0007';

/** File extensions that make a slash-containing token a path rather than prose. */
const CODE_EXTS = new Set([
  '.py', '.js', '.ts', '.jsx', '.tsx', '.java', '.c', '.cpp', '.h', '.go',
  '.rs', '.rb', '.php', '.html', '.css', '.json', '.yaml', '.yml', '.toml',
  '.xml', '.md', '.txt', '.sh', '.bat', '.ps1', '.sql', '.r', '.swift', '.kt',
  '.scala', '.clj', '.ex', '.exs', '.erl', '.hrl', '.vue', '.svelte',
  '.astro', '.tf', '.proto', '.cfg', '.ini', '.conf', '.lock', '.gradle',
  '.sbt', '.jsonl', '.log',
]);

/**
 * One path-like token.
 *
 * The Windows branch stops at the extension instead of swallowing following
 * prose, and allows spaces before it so `C:\Program Files\app.py` survives.
 */
const PATH_PATTERN = new RegExp(
  [
    '(?<![\\w])',
    '(',
    // Windows absolute path first: it must win over the bare-file branch, and
    // the lookbehind for a drive letter keeps `C:` from being split off as a
    // file name (`epl-abc.jsonl` was the bug that taught us this).
    '[A-Za-z]:\\\\[-\\w.\\\\ +@]*?\\.\\w+(?::\\d+)?(?=$|[\\s,;:!?)}\\].])',
    '|',
    // Prefixed or absolute path, optional extension and `:line`
    '(?:(?:~|\\.\\.?)?/|/)[-\\w.+@]+(?:/[-\\w.+@]+)*(?:\\.\\w+)?(?::\\d+)?',
    '|',
    // Bare relative path with at least one separator
    '(?:[-\\w.+@]+/)+[-\\w.+@]+(?:\\.\\w+)?(?::\\d+)?',
    '|',
    // A bare file name with a known extension: `hello.py`, `app.config.ts:12`.
    // Without this, the file a task just wrote is the one path never linked.
    '[-\\w.+@]+(?:\\.[-\\w.+@]+)*(?::\\d+)?',
    ')',
    '(?!\\w)',
  ].join(''),
  'g',
);

export interface PathMatch {
  text: string;
  index: number;
  /** 1-based line number when the token ended with `:42`. */
  line?: number;
}

/** Strip a trailing `:42` and report it separately. */
function splitLineAnchor(token: string): { path: string; line?: number } {
  const match = /^(.*?):(\d+)$/.exec(token);
  if (!match || match[1] === undefined) return { path: token };
  return { path: match[1], line: Number.parseInt(match[2] ?? '0', 10) };
}

function looksLikePath(token: string): boolean {
  // Windows absolute path: the drive letter plus separators is proof enough.
  if (/^[A-Za-z]:[\\/]/.test(token)) return true;
  if (token.startsWith('./') || token.startsWith('../')) return true;
  const dot = token.lastIndexOf('.');
  const ext = dot > 0 ? token.slice(dot).toLowerCase() : '';
  const hasExtension = ext !== '' && CODE_EXTS.has(ext);
  // A leading-slash token needs path *shape*, not just a leading slash:
  // single-segment, extension-less tokens are slash commands and prose
  // (`/max-steps`, `/permissions`), and wrapping those in OSC-8 both lies
  // about them being files and makes Ink's width slicing drop characters.
  if (token.startsWith('/') || token.startsWith('~/')) {
    const body = token.startsWith('~/') ? token.slice(2) : token.slice(1);
    const segments = body.split('/').filter(Boolean);
    return hasExtension || segments.length >= 2;
  }
  if (!hasExtension) return false;
  // A bare name needs the extension to be believable.  `3.14` and `v1.2` are
  // numbers, not files; `hello.py` is the file a task just wrote.
  if (!token.includes('/') && !token.includes('\\')) {
    return /^[A-Za-z0-9_][-\w.+@]*\.[A-Za-z][A-Za-z0-9]{0,9}$/.test(token);
  }
  return true;
}

/** Every path-like token in one line, in order. */
export function findPaths(line: string): PathMatch[] {
  const found: PathMatch[] = [];
  PATH_PATTERN.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = PATH_PATTERN.exec(line)) !== null) {
    const token = match[1];
    if (!token) continue;
    const { path, line: anchor } = splitLineAnchor(token);
    if (!looksLikePath(path)) continue;
    found.push({
      text: token,
      index: match.index,
      ...(anchor === undefined ? {} : { line: anchor }),
    });
  }
  return found;
}

/**
 * Wrap one path in an OSC-8 link.
 *
 * `file://` is the only scheme terminals reliably open; the line anchor rides
 * along in the URI so editors that support it jump straight there.
 */
export function linkFor(path: string, line?: number): string {
  const normalized = path.replace(/\\/g, '/');
  const absolute = /^[A-Za-z]:\//.test(normalized) || normalized.startsWith('/');
  const body = absolute ? normalized : `./${normalized.replace(/^\.\//, '')}`;
  const uri = `file://${body}${line === undefined ? '' : `#L${line}`}`;
  return `${OSC8};;${uri}${ST}`;
}

/**
 * Rewrite a line so its paths are clickable.
 *
 * Fenced code blocks and inline code are left alone: a link inside a code
 * sample would corrupt what the user is trying to read or copy.
 */
export function linkPaths(line: string, opts: { enabled?: boolean } = {}): string {
  if (opts.enabled === false) return line;
  if (line.trimStart().startsWith('```')) return line;
  if (line.includes(OSC8)) return line; // already linked; never double-wrap

  const matches = findPaths(line);
  if (matches.length === 0) return line;

  let out = '';
  let cursor = 0;
  for (const match of matches) {
    out += line.slice(cursor, match.index);
    const open = linkFor(match.text.replace(/:\d+$/, ''), match.line);
    out += `${open}${match.text}${OSC8};;${ST}`;
    cursor = match.index + match.text.length;
  }
  out += line.slice(cursor);
  return out;
}

/** Apply to every line of a block. */
export function linkPathsInLines(lines: string[], opts: { enabled?: boolean } = {}): string[] {
  return lines.map((line) => linkPaths(line, opts));
}
