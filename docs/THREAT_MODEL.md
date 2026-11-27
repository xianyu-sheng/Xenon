# Tool-output threat model

Xenon treats text returned by tools as data, not as instructions. Tool output
can contain prompt-injection text because files, websites, repositories,
documents, command output, MCP servers, and plugins may be controlled by an
attacker. The provenance and advisory mechanism makes that boundary visible;
it is a bounded, non-blocking warning system rather than a security sandbox.

## Data flow

Every tool execution produces a schema `1.0` `ToolResult` with:

- `provenance`: `source_type`, a source reference capped at 200 characters,
  and `untrusted`;
- `security_advisories`: deterministic heuristic matches, each with `kind`,
  `rule_id`, and severity `warning`.

The detector scans only the text that `ToolExecutor` will show to the model.
It scans at most 32 KiB per result, deduplicates matches, sorts rule IDs for
stable output, and emits at most five advisories. The original tool text is
never modified. NFKC normalization and removal of zero-width and bidirectional
control characters happen only on the temporary matching copy.

The warning is prepended to the model-visible observation and recorded in the
session `tool/result` event. Conversation turns retain only compact provenance
metadata. Advisories are accumulated for the current turn and cleared when the
next turn starts. If a later action already requires approval, the existing
approval panel names the untrusted sources and matched rules. An advisory never
creates a new approval gate and never changes a successful tool result into a
failure.

## Provenance mapping

All tool results are untrusted by default. Unknown and runtime-registered tools
fail safe to `tool` provenance. User-authored turns are marked `user` and
trusted. Installed skills and memory enter through separate trusted context
paths rather than the tool-result path.

| Source type | Action types |
| --- | --- |
| `file` | `write_file`, `read_file`, `list_files`, `search_files`, `edit_file`, `create_directory`, `batch_write`, `batch_edit`, `code_index`, `ast_analyze`, `refactor`, `diff_preview`, `lsp_goto_def`, `lsp_find_refs`, `lsp_hover`, `lsp_diagnostics`, `lsp_symbols` |
| `document` | `read_document` |
| `web` | `web_fetch`, `docs_fetch`, `weather` |
| `github` | `github_fetch` |
| `mcp` | `mcp_call` |
| `command` | `command` |
| `repo` | `git`, `clone_repo` |
| `tool` | `datetime`, `register_tool`, `spawn_agent`, `submit_plan`, documented `sqlite_query` extension, and every unknown/plugin action |
| `user` | user-authored conversation turns (trusted) |

## Detection scope

The deterministic rules warn about common instruction-override, forged-role,
priority-override, concealment, credential-access, secret-exfiltration, and
tool-execution phrases. Expressions use bounded wildcards and avoid nested
unbounded quantifiers so adversarial text cannot trigger catastrophic regex
backtracking.

This mechanism is deliberately limited:

- it is heuristic and does not promise to detect or prevent prompt injection;
- it does not decode Base64, hexadecimal, or other encoded payloads;
- it does not detect arbitrary Unicode homoglyph substitutions;
- it does not assemble clues split across multiple results;
- it does not use a model-based classifier;
- it examines only model-visible tool text, not hidden receipt metadata;
- it warns but does not block, sanitize, or rewrite content.

Use OS- or container-level isolation for untrusted code and treat approvals as
human decisions, not as proof that preceding content was safe.

## Sensitive paths: one list, three consumers

"What counts as a sensitive path" has a single source of truth,
`xenon/utils/sensitive.py` (#449). It is a pure string classifier — no file
I/O, standard library only — exposing `classify_path(path, *, workspace=None)`
(returns `""`, `"credential"`, `"system"` or `"outside"`) and
`matches_credential_name(name)`. Three places use the idea of a sensitive path:

| Consumer | What it is for | Relation to `xenon/utils/sensitive.py` |
| --- | --- | --- |
| `xenon/nodes/tool_node.py::_SENSITIVE_PATHS` | **Write blacklist** — a security boundary: writes under these prefixes are refused | References `SYSTEM_WRITE_BLACKLIST`. The matching logic in `ToolNode._validate_path` is unchanged. The list may only grow; it is never loosened. |
| `xenon/frontend/queries.py::_is_sensitive_path` | **Dashboard attention** (#446): a credential read, a system-path read or a path outside the workspace is promoted instead of folded away | Calls `classify_path`. `SYSTEM_PATHS` is the write blacklist plus `/root` (reading there deserves attention; refusing every write there would break containers that work in `/root`). |
| `xenon/nodes/tool_security.py` | **Prompt-injection advisories** on tool output | Not derived. Its `credential_access` / `secret_exfiltration` rules match natural-language *requests* ("read the .env", "send the credentials"), not paths, so reusing the path patterns would change their meaning. |

`xenon/utils/sensitive.py` is authoritative for the lists. The security
boundary is still the check in `tool_node`: `classify_path` is for attention
and audit, and nothing it returns may be used to allow a write. The separate
credential read/write guard (`_USER_SENSITIVE` in `tool_node`) is a security
check with its own substring semantics and is likewise left as it is.
