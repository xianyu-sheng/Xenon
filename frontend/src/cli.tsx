#!/usr/bin/env node
/**
 * Entry point.  Kept separate from App so the UI can be imported (and tested)
 * without spawning a process.
 */

import { render } from 'ink';
import React from 'react';

import { App } from './App.js';

const argv = process.argv.slice(2);
if (argv.includes('--help') || argv.includes('-h')) {
  process.stdout.write(
    [
      'xenon-ui — terminal front-end for Xenon',
      '',
      'Usage: xenon-ui [--model <id>]... [--level read-only|auto-edit|bypass]',
      '                [--python <exe>] [--cwd <dir>] [--resume <序号|名称>]',
      '',
      'The kernel runs in Python; this process only speaks the NDJSON protocol',
      'described in docs/design/frontend-protocol.md.',
      '',
    ].join('\n'),
  );
  process.exit(0);
}

const models: string[] = [];
let level = 'auto-edit';
let python = 'python';
let cwd: string | undefined;
let resume: string | undefined;
for (let i = 0; i < argv.length; i += 1) {
  const arg = argv[i];
  const next = argv[i + 1];
  if ((arg === '--model' || arg === '-m') && next) {
    models.push(next);
    i += 1;
  } else if (arg === '--level' && next) {
    level = next;
    i += 1;
  } else if (arg === '--python' && next) {
    python = next;
    i += 1;
  } else if (arg === '--cwd' && next) {
    cwd = next;
    i += 1;
  } else if ((arg === '--resume' || arg === '-r') && next) {
    resume = next;
    i += 1;
  }
}

const instance = render(
  React.createElement(App, { args: { models, level, python, cwd, resume } }),
  { exitOnCtrlC: false },
);

void instance.waitUntilExit();
