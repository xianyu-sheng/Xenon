# Headless harness (`xenon exec`)

`xenon exec` is the deterministic, non-interactive entry into Xenon's kernel.
It runs in a real workspace, uses the real `ToolExecutor`, writes a durable
ledger, applies the permission policy, and creates a delivery commit through
`GitFinalizer`.

## Natural-language task

```bash
xenon exec "add an add() function to calc/add.py and test it" \
  -m deepseek/deepseek-v4-pro \
  --workspace . \
  --output result.json
```

`xenon exec --task "..."` is the explicit equivalent. Without `-m`, the
harness resolves a model from `~/.xenon/models.yaml`
(preferring the `planner` role), then falls back to the first provider model in
`~/.xenon/credentials.yaml`. Use `--config path/to/models.yaml` to point at a
different model file. A missing model or API key is a configuration error
(exit code 2), not a crash.

The model returns one capability action per turn:

- `call_tool` with `capability` (`read`, `search`, `edit`, `verify`, `shell`,
  `vcs`, `remote`), `params` and optional `targets`;
- `think`, `ask_user`, `abort`, or `finish` with optional claims.

The model never sees or invokes a concrete tool. The catalog binds the
capability; the runtime applies policy, records a receipt, and verifies claims.
Malformed model actions become a visible `think` turn and are bounded by
`--max-steps`.

The model must not commit or push. When it finishes, the finalizer stages only
files with write/delete receipts from this run and creates the delivery commit.
A model claim that cannot be resolved from facts blocks delivery.

## Deterministic plan task

```bash
xenon exec --plan plan.json --workspace . --output result.json
```

Plan steps use `kind` + `capability` + `params` + `expects` + optional
`depends_on`; no step names a concrete tool. This path is intended for
regression tests, benchmarks, and reproducible delivery checks.

Example plan shape:

```json
{
  "objective": "add and test add()",
  "steps": [
    {
      "id": 1,
      "kind": "edit",
      "capability": "edit",
      "params": {"files": [{"path": "calc/add.py", "content": "..."}]},
      "expects": [{"id": "edit-1", "kind": "file_changed", "target": "calc/add.py"}]
    },
    {
      "id": 2,
      "kind": "verify",
      "capability": "verify",
      "params": {"command": "python -m pytest tests/test_add.py -q"},
      "depends_on": [1],
      "expects": [{"id": "test-1", "kind": "test_passed", "target": "tests/test_add.py"}]
    }
  ]
}
```

## Output and exit codes

- `--jsonl` (default): one canonical ledger event per stdout line.
- `--no-jsonl`: suppress the event stream.
- `--output result.json`: write the final `TaskResult` JSON.
- Exit `0`: accepted and delivered.
- Exit `1`: blocked, aborted, or evidence/debt incomplete.
- Exit `2`: plan/model/usage configuration error.
- Exit `3`: harness or provider failure.

Keep the ledger directory outside the workspace. The finalizer treats unknown
worktree paths as unverified and will reject the run, which is intentional.

## Interactive REPL (kernel default; legacy freeze)

Tool-requiring turns use the kernel by default.  For one release the legacy
host is available only as an explicit frozen fallback:

```bash
# kernel default
xenon

# frozen legacy fallback (removal target: #202)
XENON_RUNTIME=legacy xenon
```

Only turns that require tools and are not a skill, plan-mode turn, pending
continuation, atomic recipe, or a mode without kernel parity are routed to the
kernel. `direct`/`react` use ActFirst; `plan-execute` uses the kernel's LLM
PlanFirst planner. After the last plan step, PlanFirst asks the model once for
a final answer from the deterministic working set; the finisher only returns a
`Finish` action and never executes a tool. `reflection`, `plan-react`, `plan-reflection` and
`react-reflection` remain on the frozen legacy scheduler until they have
equivalent strategies. They must not silently fall through to ActFirst, and an
unknown mode name fails closed instead of being treated as the default.
External queries stay legacy unless an MCP registry is available; then the
kernel uses the `core+mcp` profile and the remote call goes through approval.
Plain chat, `/plan`, `/resume`, `/evidence`, and skills still use their existing
paths. `/mode` metadata comes from the neutral `xenon/modes.py` catalog and no
longer imports the legacy engine registry.

The REPL adapter:

- builds a `TaskSpec` from the turn prompt and the selected model;
- uses the workspace root from `ProjectContext`;
- writes its durable ledger and `result.json` sidecar under
  `~/.xenon/ledger/repl/`;
- maps `READ_ONLY` to `PermissionMode.READ_ONLY` and
  `WRITE`/`EXECUTE` to `PermissionMode.AUTO_EDIT` (no automatic bypass);
- runs `GitFinalizer` only when the workspace has `.git`;
- projects kernel events into the existing ThinkingPanel and writes the answer
  back to `ContextManager` and the turn tree.

If the kernel blocks a turn, the REPL prints the open obligations and the
ledger path. `XENON_RUNTIME=legacy` is a frozen fallback, not a second
permanent runtime; it will be removed after parity (#202).

## Approval and scoped grants

The kernel can produce a `require_approval` policy decision, but it never
prompts by itself. Approval is only active when an `ApprovalProvider` is
injected:

- the kernel-default REPL bridges the decision to the existing
  `_confirm_tool`/approval panel for `EXECUTE` turns;
- an approval is converted into a **one-shot `ScopedGrant`** for the exact
  command/target and is revoked after the gateway call;
- no provider, non-TTY, user denial, or a grant that cannot be scoped all fail
  closed with a `USER_APPROVAL` ledger event;
- destructive commands (`reset --hard`, force push, `rm -rf`, ...) and any
  `git push` remain `VETO`, not approvable until an explicit pre-execution
  `TaskSpec.allow_push` gate exists;
- `xenon exec` does not inject an approval provider, so headless runs keep the
  conservative veto behavior.

`READ_ONLY` never becomes an approval prompt. `WRITE` turns do not gain shell
approval; only `EXECUTE` maps to the approval-capable policy.

## MCP read-only slice

When the REPL has an MCP registry (connected or lazily configured), the kernel
runtime uses the `core+mcp` catalog profile:

- the gateway receives the REPL `AgentContext`, so `mcp_call` can reach
  `_mcp_registry`;
- available MCP tool names are appended to the task constraints when already
  discovered;
- `mcp_call` uses `tool_name` + `tool_args`;
- every remote call still requires approval;
- the result is recorded as `NetworkFact(url="mcp://<tool>", content=...)` and
  the bounded content is visible in the model working set.

External queries are routed to the kernel only when an MCP registry is
available; otherwise they stay on the legacy path. MCP servers are not sandboxed
by Xenon's in-process sandbox; approval is the boundary. Dynamic per-tool
`ToolSpec` discovery and real GitHub MCP transcripts are follow-ups.

## Kernel evidence in the REPL

After a kernel turn, `/evidence` prefers the kernel ledger for the current
session:

- `/evidence` — human-readable kernel projection (phase, tools, tests,
  artifacts, claims, debt, delivery);
- `/evidence raw` — seq/kind/hash/prev ledger lines;
- `/evidence kernel` — force the current kernel ledger;
- `/evidence <task-id>` — read a specific ledger under
  `~/.xenon/ledger/repl/`;
- `/evidence all` — legacy session events plus kernel ledger files.

`/save`, auto-save, `/load`, and `/resume` now persist only a pointer to the
kernel ledger (task id + path), not a copy. Resume restores the pointer and
never replays a task or resurrects an approval. If the ledger is missing or the
chain is invalid, the view says so and does not turn the projection into a
trusted delivery.

### `/goal` and `/plan` after a kernel turn

`/goal` reads the kernel ledger when the current turn has a kernel pointer.
It shows the ledger objective, phase, plan steps, open debt, artifacts and the
finalize commit. `/plan status` keeps showing the legacy plan-mode toggle and
adds the read-only kernel plan below it. `/plan on|off` still controls only
legacy soft guidance; it does not pretend to start a kernel plan.

If the pointer exists but the ledger is missing or outside the kernel root,
both commands fail closed and do not fall back to the legacy goal projection.
`/mode` is intentionally not part of this view: it selects a model thinking
mode, not task state, so it stays backed by `ModelRegistry`.

### Per-tool MCP specs

When the REPL has discovered MCP tools, the kernel builds one `ToolSpec` per
canonical `server:tool` name. The model selects an exact name; the catalog
rejects unknown names instead of silently binding the generic `mcp_call` spec.
The gateway translates the exact call back to the internal `mcp_call` executor,
but the receipt and `NetworkFact` keep the real tool name.

This is still a read-only MCP slice: the MCP server process is outside Xenon's
filesystem sandbox, and approval is the security boundary. The generic
`mcp_call` spec remains the fallback only when no per-tool specs are present.

### MCP tool budget

The REPL does not inject every discovered MCP tool into the model. It builds
one spec per canonical `server:tool`, then selects a deterministic subset:

- `XENON_MCP_TOOLS` (comma/space separated) is an explicit allowlist; it wins
  over automatic selection;
- otherwise tool-name tokens are weighted above description tokens and ties
  sort by canonical name;
- `XENON_MCP_TOOL_LIMIT` caps the injected set (default 16);
- unselected tools are not in the prompt and cannot be exactly bound; this is
  a budget boundary, not a sandbox.

A real GitHub MCP run discovered 46 tools, injected 16, and still selected
`github:search_repositories` correctly.

### Objective vs strategy prompt

A kernel task records both `objective` (the strategy-facing prompt) and
`user_objective` (the user's own words). The REPL passes the raw turn input as
`user_objective`; `/goal` and `/evidence` prefer it for display. Older ledgers
without the field fall back to `objective`, so the projection remains
backward-compatible. The model still sees only the strategy-facing `objective`.

### Delivery transaction

The finalizer is not called blindly at `Finish`. The runtime now:

1. preflights every non-`commit_created` claim, open debt and current verdict;
2. appends an `AUDIT_REPORTED` event with `scope=preflight`;
3. appends a `DELIVERY_ATTEMPTED` intent event;
4. lets the finalizer commit;
5. appends the finalizer `TOOL_RECEIPT`;
6. runs the final audit/finalize guard and control decision;
7. if the decision is not `DELIVER`, rolls back the commit created by this
   finalizer and records `DELIVERY_ATTEMPTED status=rolled_back`;
8. if rollback fails, records a `delivery-rollback` debt and blocks.

The commit is still a delivery-layer side effect, not a model tool call. The
intent event makes the crash window between `git commit` and its receipt
visible; full crash recovery remains a follow-up.

### V2 coverage audit

`AuditCoordinator` builds a deterministic coverage matrix for `TaskSpec`
obligations, plan step expectations and open debt. Each row is `bound`,
`unresolved`, `ambiguous` or `abstained`; only bound rows satisfy V2. An
`AuditProposal` may fill a semantic `requirement_met` gap only when its artifact
paths and test targets bind to real ledger facts; proposals never override
deterministic kinds and never emit a pass verdict.

Negative facts are explicit: `pushed` with `expect={"absent": true}` is bound by
the absence of push receipts and becomes unresolved as soon as a push exists.
Unresolved/abstained rows open debt before the finalizer; `/evidence` prints the
latest final coverage matrix. LLM-driven proposal generation, bounded audit
rounds and user acceptance of debt remain follow-ups for #200.

`LoopRuntime` accepts an optional `AuditProposalProvider`. The provider only
returns an `AuditProposal`; it is recorded as `AUDIT_PROPOSED` and passed to the
deterministic binder. A provider exception or malformed value is recorded and
treated as "no proposal", never as evidence. `Budget.max_audit_rounds`
(default 1) caps repeated `Finish` audits; exhausting it returns a blocked
result with `audit budget exhausted`.

A model-backed AuditProposalProvider is not wired yet; this slice defines the
seam and tests it with a fake provider.
