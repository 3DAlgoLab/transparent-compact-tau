# transparent-compact (Tau port)

Deterministic, losslessly recoverable context compaction for
[Tau](https://github.com/rian-dolphin/tau) — a port of
[`transparent-compact` for pi](../transparent-compact-pi).

Tau's built-in compaction asks a **model** to rewrite history and keeps only
that rewrite: the original turns become unreachable, and the summary's accuracy
is a model's opinion. This extension replaces that summary with a
**mechanically constructed** one assembled from the untouched transcript, and
makes the raw branch permanently reachable.

## Status

Work in progress. `TODO.md` holds the milestone plan and the dated progress log;
`transparent-compact-core-algorithm.md` is the algorithm being ported.

| Milestone | State |
| --- | --- |
| `M0` Tau core seams (`session_before_compact`, `CompactionEntry.details`, `context.entries`) | landed on `tau` branch `feat/session-before-compact` |
| `M1` Store A raw-branch transcript (`transparent_compact.transcript`) | implemented |
| `M2` Mechanical checkpoint: span records, message compression, envelope | implemented |
| `M3` Cap ladder and cap policy (`transparent_compact.cap`, `.state`) | implemented; `warn` notifies instead of prompting |
| `M4` Recovery | `context_list_entries` / `context_grep` / `context_show_entry` implemented; `context_lookup` pending |
| `M5` Commands (`/transparent-compact`, `/compaction-method`, `/cap-compaction`) | implemented |
| `M6` Turn filtering, fidelity ledger, aside-turns, auto-filter | not started |

## What you get once it is loaded

- Compaction (manual, threshold, or overflow) writes a deterministic checkpoint
  into the session instead of asking a model to summarize: per-span
  `<compacted-span>` blocks, `[User]:`/`[Agent]` prose verbatim, tool calls as
  signatures with a few conservative observations, and carried read/modified file
  lists.
- The raw branch survives untouched, and three tools read it:
  `context_list_entries`, `context_grep`, `context_show_entry`.
- `/transparent-compact` prints the live state — method, cap policy, branch vs
  projected entry counts, how many checkpoints are in the branch and how many
  spans can be re-rendered from their own records.
- `/compaction-method summary` hands compaction back to Tau for this branch;
  `/cap-compaction off|warn|on|25%|80c|4t` sets the ceiling.

## Checks

```bash
uv run pytest -q                      # 60 tests, incl. real CodingSession end-to-end
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy                           # strict
```

## The two stores

| | Contents | Lossy? | Model? | Rebuildable? |
| --- | --- | --- | --- | --- |
| **Store A** — raw session | append-only Tau session entries (`context.entries`) | **No** | No | No — it is the record |
| **Store B** — active context | the injected mechanical checkpoint + recent entries | Yes | **No** | Yes, from Store A |

The invariant the whole design protects: *content is never silently lost — it
may be visibly hidden.* Compaction never writes to or edits the raw history, and
every recovery path reads the raw branch, never the lossy projection.

## Development

```bash
uv run pytest -q
uv run ruff check src tests
```

The `tau-ai` dependency resolves to the local `../tau` checkout (editable path
source), because the compaction seam is not published yet. Run Tau with this
extension from that checkout:

```bash
cd ../tau && uv run tau -e ../transparent-compact-tau
```
