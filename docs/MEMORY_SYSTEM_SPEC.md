# Xenon Memory System v2

Status: production-oriented local backend implemented in Xenon 0.6.x.

## Product promise

Xenon memory is local-first, transparent, bounded, and user-governed. The model may
propose a memory, but it may not silently persist one. Every persistent write tells
the user what was stored, why, in which scope, and at which path.

The differentiator is the complete interaction loop rather than a new database:

1. discover a reusable fact or preference;
2. show the proposed content, reason, scope, and destination;
3. obtain explicit consent unless the user already gave an unambiguous save command;
4. maintain small hierarchical views automatically;
5. retrieve only relevant entries with provenance;
6. preserve an audit trail and make removal reversible through archiving.
7. remain correct when multiple Xenon processes write the same project.

## Scope hierarchy

| Scope | Default destination | Write authority | Intended use |
|---|---|---|---|
| session | memory only | explicit or confirmed | temporary task state |
| project-local | `.xenon/memory/local/` | default for suggestions | private project facts/preferences |
| project-shared | `.xenon/memory/shared/` | explicit user choice only | team conventions and decisions |
| user | `~/.local/share/xenon/memory/` | explicit or confirmed | preferences reused across projects |

When Xenon starts at the account home directory, HOME is treated as an unscoped
privacy boundary even if it contains `.git`, `package.json`, or `pyproject.toml`.
Only `user` and `session` scopes are active. An unqualified remember request
defaults to `user`; an explicit project-local/shared request is rejected until
the user enters a bounded project directory. Xenon never creates project memory
or scans a project tree under HOME implicitly.

`XENON.local.md` and `XENON.md` contain compact index imports, not an unbounded
memory dump. `AGENTS.md` is a fallback project instruction filename when
`XENON.md` is absent. The global instruction entry point is
`~/.config/xenon/XENON.md`.

## Files and source of truth

Each persistent scope has this shape:

```text
INDEX.md                 # compact generated navigation
preferences.md           # generated human-readable views
project.md
decisions.md
conventions.md
lessons.md
metadata.json            # authoritative active state + metadata
archive.jsonl            # append-like reversible archive journal
.metadata.lock           # short-lived cross-process transaction lock
```

Markdown views carry stable `xenon-memory-id` comments. They are intentionally
small and readable. `metadata.json` owns timestamps, counters, scores, provenance,
status, and checksums so maintenance data never pollutes the model prompt.

## Record contract

Every record includes: `id`, `scope`, `kind`, `content`, `tags`, creation/update
time, last retrieval/use time, retrieval/use counts, importance, confidence,
pinning, optional expiry, source/evidence, supersession, status, and checksum.

Kinds are `preference`, `fact`, `decision`, `constraint`, and `lesson`. Status is
`active`, `archived`, or `superseded`.

## Write state machine

```text
user text
  ├─ unambiguous save command ── validate/redact check ── persist ── receipt
  └─ reusable-information signal ── candidate preview ── user decision
                                      ├─ save/edit/change scope ── persist ── receipt
                                      └─ ignore ── no write
```

Automatic discovery produces at most one proposal per turn and defaults to
`project-local`. It never selects `project-shared`. Secret-like values, questions,
oversized entries, and low-confidence statements are not proposed.

## Capacity and lifecycle policy

- one entry: at most 300 estimated tokens;
- one generated leaf: target at most 1,500 tokens;
- one active persistent scope: target at most 10,000 tokens;
- one context injection: at most `min(4,000, 8% of the model context window)`.

Before adding storage, Xenon performs exact normalized deduplication. When a
private scope exceeds a threshold, it archives the weakest non-pinned records by
a score combining importance, confidence, age, retrieval frequency, and confirmed
use. Confirmed use is weighted more heavily than retrieval. Shared and pinned
records are never automatically archived. Threshold maintenance never physically
deletes a record.

The complete `read → decide → write → regenerate views` operation runs under an
ownership-token inter-process lock. Atomic file replacement prevents torn JSON;
the transaction lock additionally prevents lost updates from two terminals. A
dead owner's stale lock can be reclaimed, while a live slow writer is not stolen.

## Retrieval and conflict policy

Retrieval searches all applicable scopes, boosts the current project and session,
and injects only bounded, relevant records. Each injected line exposes scope,
kind, and stable ID. Current user instructions always override remembered content.

The default retriever is deterministic mixed Chinese/English lexical ranking. It
returns a score and human-readable reasons, exposed by `/memory search --explain`.
`MemoryRetriever` is a separate interface so hybrid embedding retrieval can be
added without changing lifecycle or consent policy.

Retrieval and successful use are separate signals. `retrieval_count` increments
when Xenon selects a memory for context; `use_count` increments only after that
memory was injected and the turn produced a successful assistant answer.

Conflict detection is intentionally conservative. Assignment changes such as
`Python 3.12 → Python 3.13` and opposite deterministic constraints are reported,
but never silently resolved. `/memory replace <old-id> <new-content>` creates a
new record, marks the old record `superseded`, and links the version chain.
`/memory rollback <new-id>` reverses that transition.

## Inspection and operations

The supported operational surface is:

```text
/memory status                  paths, budgets, and storage contract
/memory list [--all]            active or complete lifecycle listing
/memory search [--explain] Q    bounded results and optional rank evidence
/memory inspect ID              content, path, counters, timestamps, provenance
/memory doctor                  schema, checksum, link, permission, capacity checks
/memory add ...                 explicit write with scope and kind
/memory replace OLD NEW         explicit supersession
/memory rollback NEW            reversible supersession
/memory archive ID / restore ID reversible lifecycle changes
/memory pin ID / unpin ID       retention protection
/memory migrate                 non-destructive v1 import
```

`doctor` is read-only and never repairs or overwrites malformed metadata. A
checksum mismatch, broken supersession link, duplicate active item, or unreadable
scope is surfaced as an error so the authoritative file remains inspectable.

## Instruction loading and imports

Instruction precedence is general to specific:

1. `~/.config/xenon/XENON.md`;
2. project `XENON.md`, or `AGENTS.md` as fallback;
3. legacy `.xenon/rules.md`;
4. project `XENON.local.md`.

A line containing only `@path` imports another instruction file. Imports must stay
inside their owning root, do not follow escaping symlinks, reject cycles, and have
depth and byte budgets. Imported memory indexes are navigation hints; query-time
retrieval remains the authoritative injection mechanism.

## Security and failure behavior

- private JSON/Markdown files are written with mode `0600`; shared views use `0644`;
- writes are atomic and read-modify-write mutations are inter-process locked;
- candidate detection rejects common credential/key formats;
- storage failure must not prevent the answer from being shown;
- a candidate is never considered consent;
- physical deletion requires a separate explicit destructive command.

## Extension boundary

`MemoryBackend` is the persistence interface. `MemoryBackendRegistry` maps scopes
to implementations. `MemoryService` owns lifecycle policy, retrieval, and receipts.
`MemoryCandidateDetector` owns rule-first authorization/candidate detection, while
`MemoryContextCompiler` owns injection budgets. SQLite, vector indexes, encrypted
stores, or remote team backends can therefore be added without changing REPL policy.
