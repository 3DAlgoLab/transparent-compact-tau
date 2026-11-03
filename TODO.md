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
- [ ] No extension code written yet in this repo.

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

### M1 — Store A transcript (algorithm Part 1–2)
- [ ] Branch → ordered entry list (skip label/leaf/custom bookkeeping), entry index for
      `[N]` addresses, turn-owner map (raw user boundaries).
- [ ] Per-entry renderer: `[N] (from B …) [User]: …`, `[Agent] …`,
      `[Tools]: read(...); edit(...)` stubs with prefix/command/suffix records,
      `[Thinking omitted]` for thinking-only turns, `[Custom]: …`.
- [ ] Preamble block, `render_entries` windowing, `paginate` (12000 chars,
      `[Showing entries X–Y of Z …]`), `grep` (200 matches, case-insensitive,
      line-per-hit), `show_entries`.

### M2 — Mechanical checkpoint (algorithm Part 3)
- [ ] Span records for the replaced range only; base rebuilt from earlier mech entries
      (`CompactionEntry.details`) + native Tau compaction entries kept verbatim.
- [ ] `[User]:` lines joined with `\n` like Pi; `[Agent]` prose verbatim; tool results
      and thinking dropped from Store B but never from Store A.
- [ ] Header `**`-joined segments + `**Retained recent context below** — do not answer
      from this alone; use \`context_lookup\`.`
- [ ] Verbatim self-test: re-serialize the span's raw entries and assert every retained
      checkpoint line occurs; on mismatch fall back to Tau's model summary (never
      silently truncate the checkpoint itself).
- [ ] Hook: return `{summary, details}`; `cancel` when the cap is aborting (M3).

### M3 — Cap (algorithm Part 4)
- [ ] Port `capSpans` faithfully: 160-char default previews → 120 → 80 → fold
      `[Tools]: ` prefixes (only when both adjacent lines are tool-only and mergeable)
      → destructive (drop non-user content oldest-first, stub user lines
      `…[truncated +N chars — use context_lookup]`, drop protected-marker-safe rows).
      Stop at the first rung that fits; never measure the raw history.
- [ ] `off` / `enforce` (silent + notification) / `warn` (confirm; refusal cancels
      compaction so the model summarizer can run instead).
- [ ] Shortening note `**Note:** …` and presentation-loss accounting.
- [ ] Budget from `context.context_window_tokens`; `off` = no cap.

### M4 — Recovery tools (algorithm Part 5)
- [ ] `context_list_entries(around_entry, span, include_tools)`
- [ ] `context_grep(pattern, limit, flags)`
- [ ] `context_show_entry(entry_from, entry_to)`
- [ ] `context_lookup(question)` — subagent over Store A with **only** those three tools,
      its own provider request loop (no CodingSession, no session file), max turns,
      abort-aware, consecutive-call cap, `lookupModel` override setting.
- [ ] Descriptions carry the Pi guidance: source = raw branch, `entry N` provenance,
      "not a file", filesystem rule, pagination note.

### M5 — Commands + status (algorithm Part 6/7 minimal set)
- [ ] `/compaction-method [verbatim|summary]` (entry-backed, printed status).
- [ ] `/cap-compaction [off|percent NN|enforce|warn]`.
- [ ] `/transparent-compact` status dump (method, cap, spans, checkpoint chars, entries).
- [ ] Notifications mirroring Pi's strings (compaction summary note, cap warning,
      mixed-context warning).
- [ ] session_start: state replay + mixed-context warning.

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
