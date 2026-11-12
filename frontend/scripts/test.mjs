/**
 * Run the compiled reducer tests without relying on shell glob expansion.
 *
 * A glob passed to `node --test` is expanded by the shell on some platforms
 * and left literal on others; discovering the files here keeps CI (Node 20)
 * and Windows/macOS identical.
 */

import { spawnSync } from 'node:child_process';
import { readdirSync } from 'node:fs';

const files = readdirSync('dist')
  .filter((name) => name.endsWith('.test.js'))
  .map((name) => `dist/${name}`);

if (files.length === 0) {
  console.error('no compiled tests in dist/: run `npm run build` first');
  process.exit(1);
}

const result = spawnSync(process.execPath, ['--test', ...files], { stdio: 'inherit' });
process.exit(result.status ?? 1);
