/**
 * xenon-ui — the terminal front-end.
 *
 * This is the whole UI now: input, slash commands, completion, status bar,
 * approval panel, evidence footer.  The Python side is a kernel host that only
 * answers questions and runs turns — it renders nothing.
 */

import { Box, Text, useApp, useInput } from 'ink';
import React, { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';

import {
  ANIMATION_DURATION_MS,
  ANIMATION_FPS,
  animationEnabled,
  renderFrame,
  settledMark,
} from './brand.js';
import { DashboardServer } from './dashboard/server.js';
import { KernelHost } from './host.js';
import type { ApprovalChoice } from './protocol.js';
import { SessionController } from './session.js';
import {
  activeFileQuery,
  applyFileCompletion,
  completeCommand,
  showWelcomeCard,
} from './state.js';
import { TerminalTitle } from './terminalTitle.js';
import {
  ApprovalPanel,
  BrandLogo,
  Completer,
  PromptPanel,
  StartupSummary,
  StatusBar,
  StatusLine,
  Timeline,
  Transcript,
  WelcomeCard,
} from './ui.js';

export interface CliArgs {
  models: string[];
  level: string;
  python: string;
  cwd?: string;
  resume?: string;
  /** Start the local dashboard with the session (`xenon --dashboard`). */
  dashboard?: boolean;
}

export function App({ args }: { args: CliArgs }): React.ReactElement {
  const { exit } = useApp();
  const [buffer, setBuffer] = useState('');
  const [promptValue, setPromptValue] = useState('');
  const [completionIndex, setCompletionIndex] = useState(0);
  const [fileIndex, setFileIndex] = useState(0);
  const [detailsExpanded, setDetailsExpanded] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const [logoFrame, setLogoFrame] = useState<number | null>(
    animationEnabled() ? 0 : null,
  );
  const fileSeq = useRef(0);
  const pendingFile = useRef<{ id: string; query: string } | null>(null);

  // ── startup identity ───────────────────────────────────────────────
  // The frames are the same generated storyboard the Python player used;
  // only the player moved.  Any key skips the 3 s intro, and a non-TTY
  // (pipe, CI, TERM=dumb) starts directly on the settled mark.
  useEffect(() => {
    if (logoFrame === null) return;
    const frameMs = Math.round(1000 / ANIMATION_FPS);
    const timer = setInterval(() => {
      setLogoFrame((current) => {
        if (current === null) return null;
        const next = current + 1;
        const frames = Math.ceil((ANIMATION_DURATION_MS / 1000) * ANIMATION_FPS);
        if (next >= frames) {
          clearInterval(timer);
          return null;
        }
        return next;
      });
    }, frameMs);
    return () => clearInterval(timer);
    // Runs once: the intro either finishes or is skipped; it never restarts.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // The controller owns the host, the frame reduction and the "what does this
  // keystroke mean" rules.  The terminal is one renderer of its state; the
  // local dashboard (Part E) is another.
  const controller = useMemo(() => {
    let instance: SessionController | null = null;
    let dashboard: DashboardServer | null = null;
    let dashboardUrl: string | null = null;
    let closed = false;
    const openDashboard = async (): Promise<string | null> => {
      if (dashboardUrl) return dashboardUrl;
      if (!instance || closed) return null;
      const server = new DashboardServer({ controller: instance });
      const url = await server.start();
      if (closed) {
        // The user quit while the server was binding; do not leak it.
        await server.close();
        return null;
      }
      dashboard = server;
      dashboardUrl = url;
      return url;
    };
    const closeDashboard = () => {
      closed = true;
      void dashboard?.close();
      dashboard = null;
      dashboardUrl = null;
    };
    const host = new KernelHost({
      models: args.models,
      level: args.level,
      python: args.python,
      cwd: args.cwd,
      resume: args.resume,
      onFrame: (frame) => instance?.handleFrame(frame),
      onStderr: () => {
        /* Diagnostics stay out of the UI; the frames carry what matters. */
      },
      onExit: (code) => instance?.handleHostExit(code),
    });
    instance = new SessionController(host, {
      onExit: () => exit(),
      dashboard: openDashboard,
      closeDashboard,
    });
    return instance;
  }, [
    args.models,
    args.level,
    args.python,
    args.cwd,
    args.resume,
    args.dashboard,
    exit,
  ]);

  const snapshot = useSyncExternalStore(
    controller.subscribe,
    controller.getSnapshot,
    controller.getSnapshot,
  );
  const state = snapshot.state;
  const commandRunning = snapshot.commandRunning;
  const fileResults = snapshot.fileResults;

  useEffect(() => {
    // Ask for the command catalog once: it powers completion and /help hints.
    const timer = setTimeout(() => {
      controller.fetchCatalog();
      controller.fetchStatus();
      if (args.dashboard) void controller.openDashboard();
    }, 300);
    return () => {
      clearTimeout(timer);
      controller.shutdown();
    };
  }, [controller, args.dashboard]);

  // ── terminal tab title ─────────────────────────────────────────────
  const title = useMemo(() => new TerminalTitle(), []);
  useEffect(() => {
    title.start();
    return () => title.close();
  }, [title]);

  useEffect(() => {
    if ((state.turn.approval && !state.turn.cancelling) || state.prompt) {
      title.setState('waiting', state.prompt ? '等待输入' : '等待确认');
      return;
    }
    if (state.turn.running) {
      title.setState('running');
      return;
    }
    // A stopped turn is a state of its own, not "idle": the tab says the
    // operator interrupted something until the next turn starts.
    title.setState(state.turn.end?.cancelled ? 'interrupted' : 'idle');
  }, [
    title,
    state.turn.running,
    state.turn.approval,
    state.turn.cancelling,
    state.prompt,
    state.turn.end,
  ]);

  // ── live clocks ────────────────────────────────────────────────────
  // The kernel only ticks `progress` during model calls, so tool runs and
  // approvals would freeze the clock.  The front-end keeps its own second
  // counter and uses the kernel's number when it is larger.
  useEffect(() => {
    if (!state.turn.running) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [state.turn.running]);

  // Live kernel facts (phase / steps / tools / stall).  These only change
  // while a turn runs and the kernel answers a `status` request, so poll
  // while one is in flight — the alternative is a bar that jumps only when
  // the turn ends.
  useEffect(() => {
    if (!state.turn.running) return;
    controller.fetchStatus();
    const timer = setInterval(() => controller.fetchStatus(), 500);
    return () => clearInterval(timer);
  }, [controller, state.turn.running]);

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
      controller.clearFileResults();
      setFileIndex(0);
      return;
    }
    const id = `f-${(fileSeq.current += 1)}`;
    pendingFile.current = { id, query: fileQueryText };
    setFileIndex(0);
    controller.requestFiles(id, fileQueryText);
  }, [fileQueryText, controller]);

  const fileVisible =
    fileQuery && fileResults && pendingFile.current?.id === fileResults.id
      ? fileResults.paths
      : [];
  useEffect(() => {
    setFileIndex((current) => (current < fileVisible.length ? current : 0));
  }, [fileVisible.length]);

  const completeFile = useCallback(
    (path: string) => {
      setBuffer((current) => applyFileCompletion(current, path));
      pendingFile.current = null;
      controller.clearFileResults();
    },
    [controller],
  );

  const shutdown = useCallback(() => {
    controller.shutdown();
  }, [controller]);

  const submit = useCallback(
    (text: string) => {
      controller.submit(text);
    },
    [controller],
  );

  const answer = useCallback(
    (choice: ApprovalChoice) => {
      controller.answerApproval(choice);
    },
    [controller],
  );

  useInput((input, key) => {
    // The intro is skippable: the first keystroke settles it — but it keeps its
    // normal meaning.  Consuming it used to drop whatever was typed during the
    // ~3 s animation, so the first slash command after a `--resume` boot (whose
    // restored card appears while the intro still plays) silently vanished, and
    // a fast typist on a fresh boot lost the leading "/" of their first command.
    if (logoFrame !== null) {
      setLogoFrame(null);
    }

    if (key.ctrl && input === 'o') {
      setDetailsExpanded((value) => !value);
      return;
    }

    if (key.ctrl && input === 'c') {
      // First Ctrl+C stops the run; a second one leaves.  The first press is
      // cooperative (the kernel closes its ledger before answering); once a
      // cancel is already pending, pressing again means "I will not wait".
      if (state.turn.running && !state.turn.cancelling) {
        controller.cancelTurn();
        return;
      }
      shutdown();
      return;
    }

    if (state.prompt) {
      if (key.return) {
        controller.answerPrompt(promptValue);
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

    if (state.turn.approval && !state.turn.cancelling) {
      const index = Number.parseInt(input, 10);
      if (!Number.isNaN(index) && index >= 1 && index <= state.turn.approval.options.length) {
        const choice = state.turn.approval.options[index - 1];
        if (choice) answer(choice);
      }
      return;
    }

    // Shift+Tab used to cycle modes.  There is nothing to cycle now, so the
    // key lands on the same explanation the command prints instead of silently
    // doing nothing — old muscle memory gets an answer, not a dead key.
    if (key.tab && key.shift) {
      controller.runCommand('/mode');
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
  const elapsedSeconds = turn.running
    ? Math.max(turn.elapsedSeconds, turn.startedAt ? (now - turn.startedAt) / 1000 : 0)
    : turn.elapsedSeconds;
  const showWelcome = showWelcomeCard(state);

  return (
    <Box flexDirection="column">
      {logoFrame === null ? (
        <BrandLogo rows={settledMark()} />
      ) : (
        <BrandLogo rows={renderFrame(logoFrame)} />
      )}

      <Box flexDirection="column" marginTop={1}>
        <Text bold>
          Xenon{state.ready ? `  ·  ${state.ready.cwd}` : '  ·  启动中…'}
        </Text>
        <StatusBar status={state.status} />
      </Box>

      {showWelcome && state.ready ? (
        <>
          <WelcomeCard ready={state.ready} />
          <StartupSummary ready={state.ready} />
        </>
      ) : null}

      <Transcript blocks={state.transcript} expanded={detailsExpanded} />

      {showTurn && (
        <Box flexDirection="column" marginTop={1}>
          <Text bold>{`❯ ${turn.objective}`}</Text>
          <Timeline entries={turn.entries} expanded={detailsExpanded} />
          <StatusLine
            running={turn.running}
            phase={state.status?.phase}
            steps={state.status?.steps}
            toolCalls={state.status?.tool_calls}
            elapsedSeconds={elapsedSeconds}
            chars={turn.chars}
            providerNote={turn.providerNote}
            stalledSeconds={state.status?.stalled_seconds}
            cancelling={turn.cancelling}
          />
        </Box>
      )}

      {commandRunning && !state.prompt && !turn.approval && (
        <Box marginTop={1}>
          <Text color="gray">{`… 运行中 ${commandRunning}`}</Text>
        </Box>
      )}

      {/* A cancel answers the pending request; keeping the card up would
          invite a second, meaningless choice. */}
      {turn.approval && !turn.cancelling && <ApprovalPanel request={turn.approval} />}
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
