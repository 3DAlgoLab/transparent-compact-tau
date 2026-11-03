# transparent-compact-tau — port TODO

Port `../transparent-compact-pi` (TypeScript Pi extension) to a Tau extension in this
repo, keeping the algorithm in `transparent-compact-core-algorithm.md` intact.
Tau is pre-1.0: where a needed Pi seam does not exist in `../tau`, add it to Tau core
(section 2) and flag that Tau must be reinstalled/rebuilt.

## 0. Invariants carried over from the algorithm

These are non-negotiable; every milestone is checked against them.

1. **Canonical history is never edited.** Session entries stay append-only; compaction
   never rewrites stored messages, and a rendered/shortened checkpoint never becomes
   the source for the next checkpoint (Part 4 step 4 of the algorithm doc).
2. **Two stores.** Store A = raw active branch (lossless ground truth, no disk mirror).
   Store B = active context = one mechanically assembled checkpoint + retained tail.
3. **Zero model calls in mechanical compaction.** Success depends only on data already
   in the transcript.
4. **State lives in session entries**, never in memory flags, so reload/resume/fork and
   context overflow recovery work.
5. **Recovery tools and the lookup subagent see Store A**, not the compacted projection.
6. **Cap is presentation-only until the last resort**; presentation loss must be stated
   in the checkpoint, content loss must never be silent.
7. **Deterministic rendering:** one compaction N chars long always renders the same way;
   re-running the cap on unchanged inputs is idempotent.

## 1. Current state

- [x] Read `transparent-compact-core-algorithm.md` (all 10 parts).
- [x] Read Pi implementation: `extensions/index.ts` (~2.2k lines),
  `extensions/lib/checkpoint-cap.ts` (pure cap ladder), `extensions/lib/turn-context.ts`
  (fidelity/slots/estimates), `extensions/lib/turn-filter.ts` (filters + TUI picker).
- [x] Read Tau's compaction path and extension runtime (`../tau`).
- [x] Read the reference port idioms in `../tau-subagents` (how a real Tau extension
  registers tools/commands/handlers, builds providers, appends entries).
- [x] **M0 landed** in `../tau` on branch `feat/session-before-compact`
  (`9c4c99b` seam + tests, `39a9cd6` docs/dev-note). `uv run mypy` clean, full
  `uv run pytest tests/` = 2291 passed (one pre-existing, timezone-dependent
  TUI failure: `test_tui_tree_labels_filter_timestamps_and_clear`, fails on clean
  `main` too).
- [x] **M1–M5 built in this repo** (log in §7): `transcript.py`, `spans.py`,
      `signature.py`, `cap.py`, `state.py`, `checkpoint.py`, `recovery.py`,
      `lookup.py`, `extension.py`; 66 tests (`tests/test_transcript.py`,
      `tests/test_checkpoint.py`, `tests/test_lookup.py`,
      `tests/test_extension.py`), `ruff` clean, `mypy --strict` clean.
- [ ] M6 (filters/fidelity) and the M2 verbatim self-test are not written.
- [ ] Nothing has run in the real TUI yet: the end-to-end tests drive `CodingSession`
      headlessly, and the installed `tau` tool still lacks M0.

## 2. Tau gaps → core changes (landed in `../tau`, branch `feat/session-before-compact`)

Pi gives the extension three things Tau did not have. Each maps to one surgical
Tau change. **The installed `tau` tool is a non-editable install of `tau-ai
0.4.9b3`**, so it does not see these seams: either `cd ../tau && uv tool install
--force .` (from this branch) or run the repo directly with `uv run tau`.

### G1. Pre-compaction override hook (the keystone) — *landed*

- Pi: `session_before_compact` → `{ cancel } | { compaction: { summary, details, ... } }`.
- Tau today: only `compaction_start` / `compaction_end` **observational** events
  (`tau_coding/events.py`), and four internal call sites that always spend a model
  request: `compact_detailed` (manual `/compact`), `_maybe_auto_compact` (threshold),
  `_try_overflow_compact` (overflow), `_recover_from_truncated_turn` (truncation, which
  reuses the overflow path). They all build a plan with
  `_recent_preserving_compaction_plan()` and then
  `_generate_compaction_summary(plan.messages_to_summarize)` + `_append_compaction(...)`.
- **Change:** add a value-returning hook, dispatched like the existing `tool_call` hook
  (`ExtensionRuntime._run_tool_call_hooks`):
  - `SessionBeforeCompactEvent(reason, first_kept_entry_id, replaced_entry_ids,
    messages_to_summarize, tokens_before, context_window_tokens, custom_instructions)`
  - `SessionBeforeCompactResult(cancel=False, summary=None, details=None)`
  - runtime methods `ExtensionRuntime.has_session_before_compact_handlers` (skip
    payload construction entirely when nobody subscribed) and
    `run_session_before_compact(event)`; the first *decisive* handler wins
    (`cancel=True` or a non-blank `summary`), a non-result is a diagnostic, and an
    exception is recorded as a runtime diagnostic and falls through to model compaction
    (never block compaction — blocking would break overflow recovery). Chose the
    opposite fail-safe from `tool_call` deliberately.
  - new `CodingSession` helper `_compaction_summary_for_plan(plan, reason, instructions)`
    used by **all** compaction call sites — `compact_detailed` (`manual`),
    `_maybe_auto_compact` (`threshold`), `_try_overflow_compact` (`overflow`) — so a
    mechanical override cannot be bypassed by any path. Truncation recovery reuses the
    overflow path, so it is covered by the same three funnels.
  - manual compaction reports a cancellation as `ValueError("Compaction cancelled by
    an extension")`; automatic paths return `False` and write nothing.
  - `CompactionPlan` gained `replaced_entry_ids` (ids behind the existing
    `messages_to_summarize`); the persisted `CompactionEntry` still relies on
    `first_kept_entry_id` for replay, so JSONL stays compact and replay semantics
    are unchanged.
- Notes: `details` must be persisted (G2); `custom_instructions` (`/compact <text>`) is
  passed through so the extension can still honour it in `summary` mode.

### G2. `CompactionEntry.details` — *landed*

- Pi compaction entries carry `details` (our canonical span records live there).
- Tau `CompactionEntry` (`tau_agent/session/entries.py`) has no `details`; it keeps
  `replaces_entry_ids` (legacy, always empty) + `first_kept_entry_id` + `tokens_before`.
- **Change:** add `details: JSONValue = None`, thread it through
  `_append_compaction(...)` and `_GeneratedSummary`, and let replay ignore it
  (`_apply_compaction` only reads `summary`). Old JSONL stays valid.

### G3. Raw branch access + context-window size for extensions — *landed*

- Pi: `ctx.sessionManager.getBranch()` (raw entries, pre-compaction) and
  `ctx.model.contextWindow`.
- Tau: `context.transcript` is the **projected active context** (compaction collapsed),
  so Store A is invisible to extensions; no context-window accessor.
- **Change:** `ExtensionContext.entries` → `session._state.entries`, which
  `SessionState.from_entries` already narrows to the active parent path
  (`path_to_entry`), i.e. exactly Pi's `getBranch()` semantics; plus
  `ExtensionContext.context_window_tokens`.
- Landed shape: both are deep-copied properties (`entries` returns
  `tuple[SessionEntry, …]`, so a handler cannot mutate live state); `BoundSession`
  protocol gained `entries` / `context_window_tokens`; `CodingSession.entries` is the
  public accessor. Verified against reload/replay: a reopened session reproduces the
  identical projected context from an extension-authored summary.

### G4. Out-of-band UI updates while the main thread is blocked (deferred need)

- Pi: `pi.appendEntry(...)` from the turn picker while `compact()` is in flight
  (algorithm Part 4 "concurrency"). Tau's `append_entry` is async and the main thread is
  not blocked the same way; revisit when the picker lands (M6). Not needed for M0–M5.

### Explicitly *not* changing in Tau core

- No `compaction_method` config, no core checkpoint builder, no `tau_agent` edits — all
  policy stays in the extension (project rule: extension behaviour lives in
  `tau_coding` + extension packages, never in `tau_agent`, except the data-model field
  in G2 which is a storage concern).

## 3. Extension architecture (this repo)

```
src/transparent_compact/
  extension.py     # activate(): hooks, tools, commands; wiring only
  state.py         # entry-namespace readers: method, cap, filters, settings
  transcript.py    # Store A → line-addressable transcript (G6/G7 of the algorithm)
  checkpoint.py    # canonical span records + deterministic checkpoint assembly
  cap.py           # port of checkpoint-cap.ts: presentation ladder then destructive cap
  lookup.py        # context_lookup subagent loop + fallback recovery tools
  prompts.py       # lookup system prompt, recovery guidance text
  tests/           # pytest, deterministic, no network
```

- **Namespaces (Store A entries, via `api.append_entry`)** — mirrors Pi's customTypes:
  - `transparent-compact:method` → `{method: "verbatim"|"summary"}`
  - `transparent-compact:cap` → `{mode: "off"|"enforce"|"warn", percent: int|null}`
  - later: `transparent-compact:filter-revision`, `transparent-compact:aside-turn`,
    `transparent-compact:auto-filter`, `transparent-compact:checkpoint-retrofit`.
- **Checkpoint metadata** lives in `CompactionEntry.details`:
  `{kind: "mech-compact", span: {at, reason, tokens_before, lines[], turn_ids[],
  tools: {version, lines: [{index, mergeable, calls: [{prefix, command, suffix}]}]}},
  read_files, modified_files}`. Base = older mech entries re-rendered (no nesting),
  native Tau compactions kept verbatim as their own base part.
- **Cap default:** `off` (Pi parity) — `/cap-compaction` enables it; budget is
  `percent/100 × context_window_tokens` (needs G3).
- **Method default:** `verbatim` (Pi parity: the extension owns compaction once loaded).

## 4. Milestones

### M0 — Tau core seams (G1–G3) — *done*
- [x] `CompactionEntry.details` + `_append_compaction(details=...)` + `_GeneratedSummary.details`.
- [x] `SessionBeforeCompactEvent` / `SessionBeforeCompactResult` in
      `tau_coding/extensions/api.py`, exported from `tau_coding.extensions`.
- [x] `ExtensionRuntime.has_session_before_compact_handlers` +
      `run_session_before_compact(...)` (first decisive wins, crash falls through).
- [x] `CodingSession._compaction_summary_for_plan(...)` wired into manual, threshold,
      and overflow (truncation recovery shares the overflow path).
- [x] `ExtensionContext.entries` + `.context_window_tokens`.
- [x] Tau tests: `tests/test_session_before_compact.py` (override with zero summarization
      requests, persisted `details` + reopen/replay, cancel, raising-handler fallback,
      threshold tiling with base carry-forward) and `tests/test_extensions.py`
      (dispatch order, cancel short-circuit, crash diagnostic, bad-result diagnostic,
      `entries`/`context_window_tokens` copy semantics).
- [x] Docs: `website/content/guides/extensions.md` (hook table + context block),
      `src/tau_coding/data/docs/extensions.md` (agent-readable summary), and
      `dev-notes/extension-compaction-override.md` (indexed in `dev-notes/README.md`).
- [ ] Not merged to `main` yet; the extension is developed against the branch.

### M1 — Store A transcript (algorithm Part 1–2) — *done, as `transcript.py`*

As built: `build_transcript(entries)` renders the raw branch into physical lines
with an authoritative index (`entry_for_line`, `lines_for_entry`, `entry_range`,
`render_lines`, `render_entries`, `table_of_contents`, `grep`). Headers are
`ENTRY <id> (2026-11-03T09:12:04Z)` + `  [User]: …` — Tau entry ids are strings,
so the address *is* the entry id rather than pi's `[N]`. Images are counted, never
inlined, and nothing in Store A truncates.

- [x] Branch → ordered entry rendering with a per-entry line index.
- [ ] Per-entry renderer: `[N] (from B …) [User]: …`, `[Agent] …`,
      `[Tools]: read(...); edit(...)` stubs with prefix/command/suffix records,
      `[Thinking omitted]` for thinking-only turns, `[Custom]: …`.
- [ ] Preamble block, `render_entries` windowing, `paginate` (12000 chars,
      `[Showing entries X–Y of Z …]`), `grep` (200 matches, case-insensitive,
      line-per-hit), `show_entries`.

### M2 — Mechanical checkpoint (algorithm Part 3) — *done, as `checkpoint.py` + `spans.py`*

As built: `collect_base_and_spans` rebuilds the base (opaque model summary, or
re-rendered mech spans) plus every stored span; `summarize_entries` compresses the
new span; `build_checkpoint` assembles the envelope. Call/result pairing uses
`tool_call_id` + `tool_name`, and observation extraction (`signature.py`) was
re-tuned for Tau's tool output shapes (`details["exit_code"]`, Tau's read/bash
truncation markers, the `Command exited with code N` status block).

**Gap still owed:** the verbatim self-test below is not implemented. The cap ladder
today trusts only records that round-trip (`render_tool_line(record) == line`) and
otherwise carries a line verbatim or omits it whole — never rewrites it — which
covers the destructive case but not the explicit re-serialize-and-assert check.

- [x] Span records for the replaced range only; base rebuilt from earlier mech entries
      (`CompactionEntry.details`); native Tau compaction entries stay opaque.
- [x] Hook returns `{summary, details}`; cancels on a build failure for
      manual/threshold and defers to Tau's summarizer on overflow.
- [ ] `[User]:` lines joined with `\n` like Pi; `[Agent]` prose verbatim; tool results
      and thinking dropped from Store B but never from Store A.
- [ ] Header `**`-joined segments + `**Retained recent context below** — do not answer
      from this alone; use \`context_lookup\`.`
- [ ] Verbatim self-test: re-serialize the span's raw entries and assert every retained
      checkpoint line occurs; on mismatch fall back to Tau's model summary (never
      silently truncate the checkpoint itself).
- [ ] Hook: return `{summary, details}`; `cancel` when the cap is aborting (M3).

### M3 — Cap (algorithm Part 4) — *done, as `cap.py` + `state.py`*

As built: all six rungs in order (160→120→80→fold `[Tools]: `→remove non-user
content→stub oversized user lines→drop oldest content), stopping at the first rung
that fits, with presentation loss (`tightened`) and content loss (`trimmed`) tracked
separately and reported separately. Budget comes from `context.context_window_tokens`;
`off` disables it. Budgets parse as `25%` / `80c` / `4t`.

- [ ] `warn` **notifies** rather than prompting. The `ExtensionUi.select` call path
      exists but has never run against a TUI; headless behaviour is "apply the cap
      and say so", which keeps compaction working.
- [ ] Pi's distinct `enforce` mode is not modelled (`on` is the silent variant).
- [ ] `off` / `enforce` (silent + notification) / `warn` (confirm; refusal cancels
      compaction so the model summarizer can run instead).
- [ ] Shortening note `**Note:** …` and presentation-loss accounting.
- [ ] Budget from `context.context_window_tokens`; `off` = no cap.

### M4 — Recovery tools (algorithm Part 5) — *done*

As built (`recovery.py`, registered with `tau.register_tool`; parameters use Tau's
snake_case): three deterministic tools over the **raw branch**, each bounded at
12 000 chars with an explicit pagination note, all reporting "N of M entries".

- [x] `context_list_entries(around_entry_id, window, include_tools)`.
- [x] `context_grep(pattern, context_lines, max_matches, entry_id, literal, case_sensitive)`.
- [x] `context_show_entry(entry_id, to_id, include_tools)` — whole raw entries, uncapped.
- [x] `context_lookup(question)` (`lookup.py`) — a bounded, read-only subagent over
      Store A. It runs **its own provider loop** (`stream_response` per turn), not a
      child `CodingSession`, so it never writes a session file; its tool set is
      exactly the three recovery tools, which also makes recursion impossible;
      budgets are `MAX_LOOKUP_TURNS = 6` and `MAX_LOOKUP_TOOL_CALLS = 12`, and a
      budget that runs out says so in the answer instead of posing as complete.
      Provider comes from Tau's idiom (`load_provider_settings` →
      `resolve_provider_selection` → `create_model_provider`) and is closed after
      the answer. `MECH_COMPACT_LOOKUP_MODEL` overrides the model — the lookup reads
      a lot of history, so a cheap large-window model is the right tool.
- [x] Descriptions state: source = raw branch, entry-id provenance, "not a file",
      pagination note, and "prefer your own tools for the working tree".

### M5 — Commands + status (algorithm Part 6/7 minimal set) — *done, as `extension.py`*

- [x] `/compaction-method [verbatim|summary]` (entry-backed, printed status; `mech` /
      `model` / `tau` as aliases).
- [x] `/cap-compaction [on|warn|off|<n>%|<n>c|<n>t]`.
- [x] `/transparent-compact` status dump (version, method, cap, branch vs projected
      entry counts, checkpoint count, re-renderable spans, base state, tools, usage).
- [x] Notifications: checkpoint size + share of the window, presentation vs content
      loss, mixed-context warning, base-over-capacity warning, build-failure error.
- [ ] No `session_start` replay pass. Every command and hook call re-reads the
      branch, so correctness does not depend on it; the cost is that pi's startup
      mixed-context notice does not fire until a compaction happens.

### M6 — Filters + fidelity (algorithm Part 7–8) — *after M0–M5 are green*
- [ ] `filterSpans` / `fidelitySpans` ports, filter-revision entries, markers
      (`[Turn manually filtered]`, `[Full-fidelity turn …: MODE; slot uuid]`).
- [ ] Slots + `expandCheckpoint` equivalent: re-inject original branch messages at slot
      offsets in the active context (Tau needs its own seam because the checkpoint is one
      `UserMessage` after `_apply_compaction`).
- [ ] Aside preflags (`Ctrl+C` then `Escape`), `--filter-asides` flag, prefilter,
      auto-filter, retrofits, context-change notices, budget estimator,
      `/filter-turns` + `/compact-filtered` (TUI picker → likely a Textual component,
      see `../tau-subagents/ui`).

### M7 — Small-window end-to-end test
- [ ] Reproduce a `dpi2/coder2`-style profile with context lowered to ≤ 60k
      (`provider.contextWindows` override or `/model` + settings), drive
      user → tools → user → `/compact`, then assert: no summarization request was made
      (fake/scripted provider counts), checkpoint content is verbatim, and a follow-up
      `context_lookup`/`context_grep` recovers a tool output that is gone from context.
- [ ] Reload/resume check: reopen the session file, confirm method/cap/checkpoint state
      replays and a second compaction tiles (no double-summary, no gap).

## 5. Open design questions

1. **Slot re-injection in Tau (M6).** Tau replays a compaction as a single
   `UserMessage(COMPACTION_SUMMARY_PREFIX + summary)`. Faithful full-fidelity
   re-injection needs either a core `context` hook (Pi's `context` event) or an
   extension that rewrites context per request. Decide then whether to add a core seam.
2. **Store A cost.** Pi's transcript is live-rendered; Tau's is too. Keep it live — do
   not copy bodies into checkpoint metadata (algorithm Part 4 anti-pattern).
3. **`context_lookup` provider:** reuse `load_provider_settings` +
   `resolve_provider_selection` + `create_model_provider` (the `tau-subagents` pattern)
   so a different lookup model is possible, instead of reaching into the session's
   provider.
4. **Do we need `replaces_entry_ids`?** No — resolved by reading the code and probing.
   The field is `Field(default_factory=list, exclude_if=lambda ids: not ids)`, Tau never
   passes it, so fresh JSONL omits it and `_apply_compaction` resolves the cut from
   `first_kept_entry_id` on the active path (verified: reopen reproduces the identical
   projected context). Tile spans by **branch order + `first_kept_entry_id`**, and keep
   the extension's own span records in `details`.
   Caveat found while probing: with a degenerate `keep_recent_tokens=1`, consecutive
   compactions can each re-ingest the previous `CompactionEntry` (the hook's
   `replaced_entry_ids` sets overlap across runs). That is expected — the checkpoint
   carries forward its base rather than re-reading raw rows — but it means the extension
   must key carry-forward off `details` of the *specific* prior compaction entry, never
   assume persisted spans are pairwise disjoint.

## 6. Build / run notes

- Work on a branch in `../tau` (e.g. `feat/session-before-compact`); keep commits small,
  run `uv run pytest tests/` there.
- This repo: `pyproject.toml` with `[tool.tau] extensions = ["src/transparent_compact/extension.py"]`
  (same trick as `../tau-subagents`), dev deps `pytest`, `anyio`.
- Smoke test: `cd ../tau && uv run tau -e ../transparent-compact-tau -p "…"`.
- The live `tau` command is a **non-editable** `uv tool` install of `tau-ai 0.4.9b3`
  (`~/.local/share/uv/tools/tau-ai/…`, no `.pth` back to this repo), so it will not
  see the `session_before_compact` seam until rebuilt:
  `cd ../tau && uv tool install --force .` (run from `feat/session-before-compact`),
  or use `uv run tau` from the repo for all development work.

---

## 7. Progress log

### 2026-11-03 (later) — `context_lookup`, and M4 is closed

```
uv run pytest -q                       66 passed
uv run ruff check src tests            all checks passed
uv run ruff format --check src tests   all formatted
uv run mypy                            success, 10 files (strict)
```

`lookup.py` is the only place in this extension that talks to a model. Design
points worth keeping:

- **No child session.** A `CodingSession` child would write its own JSONL file for
  a run the user never asked to keep, and the algorithm forbids a second history.
  The subagent is a `stream_response` loop: assemble a message, run its tool
  calls locally, append the results, repeat.
- **Its tool set is the three recovery tools** — which both confines it to this
  session's branch and makes recursion structurally impossible.
- **Budgets are reported, not hidden.** Hitting the turn cap returns the partial
  answer with `(turn budget (6) reached)`; hitting the tool-call cap feeds the
  subagent an explicit "answer now" error result so the transcript stays
  well-formed.
- **Its answer is a claim.** The prompt demands entry ids; the tool description
  demands the caller verify them with `context_show_entry` before repeating
  anything as fact.

Tau facts learned while wiring it (all cost a failing test):

1. **`AgentTool.execute_fn` takes four arguments** —
   `(tool_call_id, arguments, signal, on_update)`. A two-argument handler is a
   `TypeError` at call time, not at registration.
2. **`AgentToolResult` has no `is_error` field.** Tau flags a tool as failed only
   when it *raises* (`loop._run_tool`), so a failure we want the main agent to
   reason about ("the subagent could not start, use the deterministic tools") is
   returned as `Error: …` text, deliberately not raised.
3. **`AgentToolResult.content` must be built from `TextContent` blocks** for type
   checking; the pydantic validator accepts a bare string at runtime, which hides
   the mismatch until `mypy`.
4. **Entry timestamps are seconds; message timestamps are milliseconds**
   (`message.timestamp / 1000` on write, `* 1000` on read). A fixture using
   milliseconds makes `format_timestamp` raise `year 55840 is out of range`.

Next, in priority order: the M2 verbatim self-test, `warn` prompting through
`ExtensionUi` (M3), a real small-window run (M7), then M6.

### 2026-11-03 — M1–M5 built, M4 partial

Checks run against this tree:

```
uv run pytest -q                       60 passed
uv run ruff check src tests            all checks passed
uv run ruff format --check src tests   all formatted
uv run mypy                            success, 9 files (strict)
```

`tests/test_extension.py` is the part worth trusting. It loads **this** extension
file into a real `CodingSession` (JSONL storage, `FakeProvider`,
`DEFAULT_COMPACTION_KEEP_RECENT_TOKENS` patched to 1 so the boundary is a real
span) and asserts the design claims end to end:

- manual compaction produces a checkpoint and issues **no** request carrying
  `SUMMARIZATION_SYSTEM_PROMPT`; `CompactionEntry.details.kind == "mech-compact"`
  and Tau's own `provider`/`model`/`usage` attribution fields stay empty;
- the bash output is gone from `session.messages`, still present in
  `session.entries`, and `context_grep` → `context_show_entry` recovers it;
- a second compaction re-renders the first span from its stored record and
  persists only its own (`details.span`);
- a 500-char cap emits `trimmed="true"` plus the recovery note while
  `details.span.lines` keeps the canonical line byte for byte;
- `/compaction-method summary` hands compaction back to Tau (a summarization
  request is made and `details` is `None`);
- method and cap written by slash commands are re-read after the session file is
  closed and reopened.

What the build taught us:

1. **Entry types vs message types.** `MessageEntry`/`CustomEntry`/`CompactionEntry`
   live in `tau_agent.session.entries`; the wire union lives in
   `tau_agent.messages`. The first import failure mixed the two.
2. **`notify` is `ExtensionAPI.notify`, not `ExtensionContext.notify`.** Fixing it
   exposed a hole in the seam's public surface: `NotifyLevel` was not re-exported
   from `tau_coding.extensions`, so an extension could not name the level —
   patched in `tau@a1e5cfc`.
3. **Slash handlers are synchronous, `append_entry` is not.** The settings commands
   schedule the write on the running loop and keep a strong reference to the task
   until it finishes (`_persist`); the tests poll storage briefly rather than
   asserting immediately.
4. **Tau's tool metadata differs from pi's and the observation parser must know
   it.** `read` truncates with `[N more lines in file…]` / `[Showing lines …]`;
   `bash` reports `details["exit_code"]` *and* appends `Command exited with code N`.
   `signature.py` trusts the structured exit code and treats the text footer as
   corroborating only when `is_error` or the structured code agrees, so a fixture
   containing that sentence stays a fixture.
5. **Cap rung 4 must not damage prose, and damage must be reported honestly.**
   Removing non-user content can delete a whole `[Tools]:` row — content loss,
   `trimmed`. A line that was stubbed and then dropped whole reports `trimmed`, not
   `tightened`: the surviving damage, not the most invasive intermediate step.
6. **Tiling is only provable with the extension's own record.** Tau omits
   `replaces_entry_ids` when it is empty, and with a degenerate keep-recent budget
   two compactions can overlap. Each checkpoint persists `covered`: the exact entry
   ids its own span compressed.

Next, in priority order: `context_lookup` (M4), the M2 verbatim self-test, `warn`
prompting through `ExtensionUi` (M3), a real small-window run (M7), then M6.
