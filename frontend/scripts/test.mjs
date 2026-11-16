/**
 * Run the compiled reducer tests without relying on shell glob expansion.
 *
 * A glob passed to `node --test` is expanded by the shell on some platforms
 * and left literal on others; discovering the files here keeps CI (Node 20)
 * and Windows/macOS identical.
 */

import { spawnSync } from 'node:child_process';
import { readdirSync } from 'node:fs';
import { join } from 'node:path';

/** dist mirrors src, so a test in src/dashboard/ must not be invisible. */
function collect(directory) {
  const found = [];
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) found.push(...collect(path));
    else if (entry.name.endsWith('.test.js')) found.push(path);
  }
  return found;
}

const files = collect('dist');

if (files.length === 0) {
  console.error('no compiled tests in dist/: run `npm run build` first');
  process.exit(1);
}

const result = spawnSync(process.execPath, ['--test', ...files], { stdio: 'inherit' });
process.exit(result.status ?? 1);
