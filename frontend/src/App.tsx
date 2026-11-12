/**
 * xenon-ui — the terminal front-end.
 *
 * This is the whole UI now: input, slash commands, completion, status bar,
 * approval panel, evidence footer.  The Python side is a kernel host that only
 * answers questions and runs turns — it renders nothing.
 */

import { Box, Text, useApp, useInput } from 'ink';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { KernelHost } from './host.js';
import type { ApprovalChoice, KernelFrame } from './protocol.js';
import {
  activeFileQuery,
  applyFileCompletion,
  completeCommand,
  emptyApp,
  parseInput,
  reduce,
  type AppState,
} from './state.js';
import {
  ApprovalPanel,
  Completer,
  EvidenceFooter,
  PromptPanel,
  StatusBar,
  StatusLine,
  Timeline,
  Transcript,
} from './ui.js';

export interface CliArgs {
  models: string[];
  level: string;
  python: string;
  cwd?: string;
  resume?: string;
}

export function App({ args }: { args: CliArgs }): React.ReactElement {
  const { exit } = useApp();
  const [state, setState] = useState<AppState>(emptyApp);
  const [buffer, setBuffer] = useState('');
  const [promptValue, setPromptValue] = useState('');
  const [completionIndex, setCompletionIndex] = useState(0);
  const [fileResults, setFileResults] = useState<{ id: string; paths: string[] } | null>(
    null,
  );
  const [fileIndex, setFileIndex] = useState(0);
  const fileSeq = useRef(0);
  const pendingFile = useRef<{ id: string; query: string } | null>(null);

  const host = useMemo(
    () =>
      new KernelHost({
        models: args.models,
        level: args.level,
        python: args.python,
        cwd: args.cwd,
        resume: args.resume,
        onFrame: (frame: KernelFrame) => {
          setState((previous) => reduce(previous, frame));
          if (frame.type === 'files.result') {
            setFileResults({ id: frame.id, paths: frame.paths });
          }
          // The status bar reflects what the kernel reports, so refresh it
          // whenever a turn or a command could have changed it.
          if (frame.type === 'turn.end' || frame.type === 'command.result') {
            host.fetchStatus();
          }
        },
        onStderr: () => {
          /* Diagnostics stay out of the UI; the frames carry what matters. */
        },
        onExit: (code) => {
          if (code !== 0 && code !== null) {
            setState((previous) => ({ ...previous, error: `内核退出（code ${code}）` }));
          }
        },
      }),
    [args.models, args.level, args.python, args.cwd, args.resume],
  );

  useEffect(() => {
    // Ask for the command catalog once: it powers completion and /help hints.
    const timer = setTimeout(() => {
      host.fetchCatalog();
      host.fetchStatus();
    }, 300);
    return () => {
      clearTimeout(timer);
      host.shutdown();
    };
  }, [host]);

  const matches = useMemo(
    () => completeCommand(state.commands, buffer),
    [state.commands, buffer],
  );
  // Hide the menu once the input is a complete command (or has arguments): at
  // that point the hint is noise and Enter means "run it", not "complete it".
  const menuVisible = useMemo(() => {
    const trimmed = buffer.trim();
    if (!trimmed.startsWith('/') || trimmed.includes(' ')) return [];
    const exact = matches.length === 1 && matches[0]?.name.toLowerCase() === trimmed.toLowerCase();
    return exact ? [] : matches;
  }, [buffer, matches]);

  useEffect(() => {
    // Keep the highlighted match in range as the list shrinks.
    setCompletionIndex((current) => (current < matches.length ? current : 0));
  }, [matches.length]);

  // ── @path completion ─────────────────────────────────────────────
  const fileQuery = activeFileQuery(buffer);
  const fileQueryText = fileQuery ? fileQuery.query : null;
  useEffect(() => {
    if (fileQueryText === null) {
      pendingFile.current = null;
      setFileResults(null);
      setFileIndex(0);
      return;
    }
    const id = `f-${(fileSeq.current += 1)}`;
    pendingFile.current = { id, query: fileQueryText };
    setFileIndex(0);
    host.requestFiles(id, fileQueryText);
  }, [fileQueryText, host]);

  const fileVisible =
    fileQuery && fileResults && pendingFile.current?.id === fileResults.id
      ? fileResults.paths
      : [];
  useEffect(() => {
    setFileIndex((current) => (current < fileVisible.length ? current : 0));
  }, [fileVisible.length]);

  const completeFile = useCallback((path: string) => {
    setBuffer((current) => applyFileCompletion(current, path));
    pendingFile.current = null;
    setFileResults(null);
  }, []);

  const submit = useCallback(
    (text: string) => {
      const parsed = parseInput(text);
      if (!parsed) return;
      if (parsed.kind === 'command') {
        if (parsed.name === '/exit' || parsed.name === '/quit') {
          host.shutdown();
          exit();
          return;
        }
        host.runCommand(parsed.name, parsed.args);
        return;
      }
      host.startTurn(parsed.objective);
    },
    [exit, host],
  );

  const answer = useCallback(
    (choice: ApprovalChoice) => {
      const request = state.turn.approval;
      if (!request) return;
      host.answerApproval(request.id, choice);
      setState((previous) => ({
        ...previous,
        turn: { ...previous.turn, approval: null },
      }));
    },
    [host, state.turn.approval],
  );

  useInput((input, key) => {
    if (key.ctrl && input === 'c') {
      // First Ctrl+C stops the run; a second one leaves.  Killing the host
      // mid-turn would abandon the ledger without a terminal event.
      if (state.turn.running) {
        host.cancelTurn();
        return;
      }
      host.shutdown();
      exit();
      return;
    }

    if (state.prompt) {
      if (key.return) {
        host.answerPrompt(state.prompt.id, promptValue || state.prompt.default);
        setState((previous) => ({ ...previous, prompt: null }));
        setPromptValue('');
        return;
      }
      if (key.backspace || key.delete) {
        setPromptValue((current) => current.slice(0, -1));
        return;
      }
      if (!key.ctrl && !key.meta && input) {
        setPromptValue((current) => current + input);
      }
      return;
    }

    if (state.turn.approval) {
      const index = Number.parseInt(input, 10);
      if (!Number.isNaN(index) && index >= 1 && index <= state.turn.approval.options.length) {
        const choice = state.turn.approval.options[index - 1];
        if (choice) answer(choice);
      }
      return;
    }

    // Shift+Tab cycles the mode, matching the old TUI's muscle memory.  The
    // command is the same one the front-end would type by hand.
    if (key.tab && key.shift) {
      host.runCommand('/mode', 'next');
      return;
    }

    // While a `@path` menu is open, arrows move through it and Tab completes
    // the highlighted row.  Enter still submits: a sentence that merely
    // mentions a path must not need a second Enter.
    if (fileVisible.length > 0) {
      if (key.upArrow) {
        setFileIndex((current) => (current - 1 + fileVisible.length) % fileVisible.length);
        return;
      }
      if (key.downArrow) {
        setFileIndex((current) => (current + 1) % fileVisible.length);
        return;
      }
      if (key.tab) {
        const chosen = fileVisible[fileIndex] ?? fileVisible[0];
        if (chosen) completeFile(chosen);
        return;
      }
    }

    // Completion navigation wins over typing while the menu is open — but only
    // while the input is still a *partial* command.  Once the buffer is exactly
    // a command name (or has arguments), Enter must submit it: otherwise the
    // menu keeps matching the command itself and Enter re-completes forever.
    // "Exact" checks the whole menu, not just the top row: an abbreviation can
    // rank another command first, and the typed name still wins.
    const typed = buffer.trim().toLowerCase();
    const exact = matches.some((match) => match.name.toLowerCase() === typed);
    const hasArgs = buffer.trim().includes(' ');
    const menuOpen = matches.length > 0 && !exact && !hasArgs;
    if (menuOpen && key.tab) {
      setCompletionIndex((current) => (current + 1) % matches.length);
      return;
    }
    if (menuOpen && key.return) {
      // Enter completes the highlighted row; the next Enter runs it.  Running
      // it here looks convenient until "/e" + Enter executes `/exit` — the
      // first catalog entry that matches — and quits with the session open.
      const chosen = matches[completionIndex] ?? matches[0];
      if (chosen) {
        setBuffer(`${chosen.name} `);
        setCompletionIndex(0);
        return;
      }
    }
    if (key.return) {
      submit(buffer);
      setBuffer('');
      setCompletionIndex(0);
      return;
    }
    if (key.backspace || key.delete) {
      setBuffer((current) => current.slice(0, -1));
      return;
    }
    if (!key.ctrl && !key.meta && input) {
      setBuffer((current) => current + input);
    }
  });

  const { turn } = state;
  // The live area is only for a turn in flight.  Once `turn.end` lands the
  // same steps are already in the transcript block; keeping the live area up
  // printed every step twice and pushed the evidence footer below a duplicate.
  const showTurn = turn.running;

  return (
    <Box flexDirection="column">
      <Text bold>
        Xenon{state.ready ? `  ·  ${state.ready.cwd}` : '  ·  启动中…'}
      </Text>
      <StatusBar status={state.status} />
      {state.ready?.setupNeeded && (
        <Text color="yellow">尚未配置 API Key · 输入 /setup 开始配置</Text>
      )}

      <Transcript blocks={state.transcript} />

      {showTurn && (
        <Box flexDirection="column" marginTop={1}>
          <Text bold>{`❯ ${turn.objective}`}</Text>
          <Timeline entries={turn.entries} />
          <StatusLine
            running={turn.running}
            elapsedSeconds={turn.elapsedSeconds}
            chars={turn.chars}
            providerNote={turn.providerNote}
          />
        </Box>
      )}

      {turn.end && !turn.running && <EvidenceFooter end={turn.end} />}

      {turn.approval && <ApprovalPanel request={turn.approval} />}
      {state.prompt && (
        <PromptPanel request={state.prompt} value={promptValue} />
      )}

      {state.error ? <Text color="red">{state.error}</Text> : null}

      {!turn.approval && (
        <>
          {fileVisible.length > 0 && (
            <Box flexDirection="column">
              {fileVisible.map((path, index) => (
                <Text key={path} color={index === fileIndex ? 'cyan' : 'gray'}>
                  {`${index === fileIndex ? '❯' : ' '} ${path}`}
                </Text>
              ))}
            </Box>
          )}
          <Completer matches={menuVisible} active={completionIndex} />
          <Box marginTop={1}>
            <Text color={turn.running ? 'gray' : 'green'}>{turn.running ? '… ' : '❯ '}</Text>
            <Text>{buffer}</Text>
            {!turn.running && <Text color="gray">▏</Text>}
          </Box>
        </>
      )}
    </Box>
  );
}
