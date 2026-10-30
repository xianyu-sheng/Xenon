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

Only turns that require tools and are not a skill, pending continuation, atomic
recipe, or a mode without kernel parity are routed to the kernel. `direct`/`react` use ActFirst; `plan-execute` uses the kernel's LLM
PlanFirst planner. After the last plan step, PlanFirst asks the model once for
a final answer from the deterministic working set; the finisher only returns a
`Finish` action and never executes a tool. `reflection`, `plan-react`, `plan-reflection` and
`react-reflection` remain on the frozen legacy scheduler until they have
equivalent strategies. They must not silently fall through to ActFirst, and an
unknown mode name fails closed instead of being treated as the default.
External queries stay legacy unless an MCP registry is available; then the
kernel uses the `core+mcp` profile and the remote call goes through approval.
Plain chat, write-plan approval, `/resume`, `/evidence`, and skills still use
their existing paths. `/plan` is a constraint, not a runtime switch: read-only
plan turns use ActFirst/PlanFirst; deferred-write plan turns use the kernel's
plan-only mode, then wait for `/plan approve`. `/mode` metadata comes from the neutral `xenon/modes.py` catalog and no
longer imports the legacy engine registry.

The REPL adapter:

- builds a `TaskSpec` from the turn prompt and the selected model;
- uses the workspace root from `ProjectContext`;
- writes its durable ledger and `result.json` sidecar under
  `~/.xenon/ledger/repl/`;
- maps `READ_ONLY` to `PermissionMode.READ_ONLY` and
  `WRITE`/`EXECUTE` to `PermissionMode.AUTO_EDIT` (no automatic bypass);
- runs `GitFinalizer` only when the workspace has `.git`;
- projects kernel events into the existing ThinkingPanel, including write
  effect paths and created-path facts, and writes the answer back to
  `ContextManager` and the turn tree;
- decides the kernel route before materializing legacy task state, and clears
  `_task_state` on every kernel exit (including blocked and adapter-error
  turns), so the ledger remains the only task-state source for kernel turns.
- writes kernel metric counts into the turn verdict instead of inferring them
  from the rendering panel, and only advertises continuation for resumable
  tree statuses.

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

`/save`, auto-save, `/load`, and `/resume` persist only a pointer to the kernel
ledger (task id + path) plus the plan decision for that task, never a copy of
the ledger. The decision is not authority: restoring it requires the same task
id and the same accepted-plan digest in the ledger, so a stale or hand-edited
snapshot cannot make a plan look approved. Resume never replays a task. If the
ledger is missing or the chain is invalid, the view says so and does not turn
the projection into a trusted delivery.

### `/goal` and `/plan` after a kernel turn

`/goal` reads the kernel ledger when the current turn has a kernel pointer.
It shows the ledger objective, phase, plan steps, open debt, artifacts and the
finalize commit. `/plan status` keeps showing the plan-mode toggle and adds the
read-only kernel plan below it. Read-only plan turns stay on the kernel; a plan
that deferred a write produces a `PLANNED` result and waits for `/plan approve`.
After execution, `/plan status` reports the execution phase and finalize commit;
a rejected or blocked plan is never shown as delivered.

The kernel now has a plan-only primitive for the remaining `/plan` migration:
`TaskMode.PLAN` accepts one plan and stops in `TaskPhase.PLANNED` without
starting a step or executing a tool. The plan may include write/execute steps
(the planning prompt is not clamped to the read-only execution boundary), but
empty or invalid plans fail closed instead of becoming pending approvals. It
refuses any non-`SubmitPlan` action, so a misbehaving strategy cannot turn
plan-only into execution. The REPL
adapter exposes `plan_only=True`, renders the result as `Kernel · plan`, and
marks the turn `pending-approval` instead of blocking it. `/plan approve` and
`/plan reject` bind a decision to the current planned ledger and record it in
session events; the kernel ledger stays immutable. Approve exits plan mode and
calls `resume_kernel_plan()` to execute the accepted plan in the same ledger;
reject keeps plan mode and writes nothing. Deferred-write `/plan` now uses
kernel plan-only; the real TTY transcript is in
`docs/evidence/tty-plan-approval/`.

The runtime also has a same-ledger resume primitive, `resume_planned()`: it
requires the accepted plan digest and an approval id, appends `plan_approved`,
moves `PLANNED -> RUNNING`, starts the accepted plan's first ready step, and
never appends a second `task_started` or asks the strategy to replan. The REPL
`/plan approve` command calls it through `resume_kernel_plan()`; a failed start
is reported as a failure, not as a delivered task.

If the pointer exists but the ledger is missing or outside the kernel root,
both commands fail closed and do not fall back to the legacy goal projection.
`/mode` is intentionally not part of this view: it selects a model thinking
mode, not task state, so it stays backed by `ModelRegistry`.

### PlanReact and step release

`/mode plan-react` submits one plan, then runs a bounded sub-loop on the active
step. Only the active step is ever targeted, and every iteration costs step
budget, so a confused actor cannot spin.

When a step's sub-loop is done — or its budget is spent — the strategy emits
`ReleaseStep`. That action is a strategy signal only: `parse_action` cannot
produce it, so no model can release a step. The runtime closes the step
deterministically; unmet expectations become debt, exhaust the bounded repair
budget and end as `BLOCKED`, never as a delivery. Each step keeps its own
receipts, verdicts and obligation bindings, and `step_explored` records the
budget decisions in the ledger.

### Reflection is a proposal source

`/mode reflection` runs on the kernel as `ReflectFirst`: ActFirst executes, then
one bounded reflection pass may add a single corrective tool call. The reflector
receives the deterministic receipts, not the chat history, and its output is
parsed into an action of which only `CallTool` survives. It cannot finish a
task, close a debt, or decide pass/fail — an unverified claim stays debt after
reflection, and the controller still owns delivery.

Every review is appended to the ledger as `reflection_proposed`
(`decision=correction|declined`, plus the capability and round), so "the
reflector ran" is an auditable fact rather than a claim. The strategy only hands
the runtime a short notice list; the runtime appends the event, and the event can
never satisfy an obligation or close a debt.

`RuntimeAdapter` fail-closes on unknown strategy names instead of falling back to
ActFirst, so a half-migrated mode cannot silently inherit another mode's
semantics.

### Scoped write negations

A negation that names a concrete path ("不要修改 probe.txt") protects only that
path: the turn may still create other files, and the tool boundary rejects
writes to the named path even at `EXECUTE` level. A general negation
("不要写任何文件") remains a global write ban. The contract carries the protected
paths, and `execution_policy_denial` enforces them before the tool runs.

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
