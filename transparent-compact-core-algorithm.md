# transparent-compact: the core algorithm

> Written 2026-11-03 by Ava, from the source at `~/dev-pi/transparent-compact-pi` (v0.10.0),
> read end-to-end: `extensions/index.ts` (2399 lines) + `extensions/lib/checkpoint-cap.ts`,
> `turn-context.ts`, `turn-filter.ts`. Code references are `file:symbol` with line numbers as
> of that checkout. I read the code; I did not run a live compaction trace, so runtime claims
> are inference from the code path.
> Package: `npm:@foliox/transparent-compact-pi` · `git:github.com/felixwoolford/transparent-compact-pi`

## The core idea in one line

**Compaction normally destroys information at write time. This refuses to — it moves the loss to read time instead.**

Pi's built-in compaction asks a model to *rewrite* history into a summary. That rewrite is lossy,
permanent, and re-summarized every round: a photocopy of a photocopy, worst with sub-frontier models.
This extension never rewrites prose. It **deletes nothing**, builds a *pointer* into the untouched
original, and pays for detail only when it is actually needed.

Two stores, kept permanently separate:

```
STORE A — THE SESSION FILE (append-only JSONL, never edited)
   every user message, assistant text, thinking block,
   full tool arguments, full tool outputs — forever
        ▲
        │ read only at lookup time
        │
STORE B — THE ACTIVE CONTEXT (what the model sees)
   a mechanically generated CHECKPOINT = table of contents
   + verbatim prose + Pi's retained verbatim tail
```

The checkpoint is a **lossy index**; the session is the **lossless source**. Everything else in the
codebase follows from keeping those two apart. Related survey note: [[deterministic_compaction]].

---

# Part 1 — Write path: a checkpoint with zero model calls

Entered via Pi's `session_before_compact` hook (`extensions/index.ts:2200`). Pi has already decided
*where* to cut; the extension only decides *what to write down*.

### Step 1 — Accept Pi's boundary, never move it

Pi hands over `preparation`: `messagesToSummarize`, `firstKeptEntryId` (start of the recent verbatim
tail), `tokensBefore`, and the file-op sets. The extension never moves that boundary. Key structural
fact: Pi's preparation span is `[previous cut, current cut)` — so consecutive compactions **tile
perfectly**, nothing is summarized twice or skipped, and no entry-id bookkeeping is needed. The next
preparation is authoritative.

### Step 2 — Compress each message mechanically (`summarizeMessages`, `index.ts:616`)

A pure switch on message role. No judgment, no model:

| What it is | What survives in the checkpoint |
|---|---|
| User message | **verbatim** |
| Assistant prose (`text` blocks) | **verbatim** |
| Assistant `thinking` | **dropped** (still in the session) |
| Tool call | one **signature line**: `read "src/x.ts":12-41`, `write "s.py" (2144 chars)`, `edit "a.ts" ×2` |
| Tool result | **dropped**, except a tiny curated observation after `→` |
| Custom message | `[Custom message]: type(...) [content removed — use context_lookup]` |

All calls from one assistant message collapse into a single `[Tools]:` line.

Two sub-algorithms worth noting:

**`toolSignature` (`index.ts:498`)** — display notation only, never replayable. Paths go relative to
cwd (`.` inside commands), read ranges print as `start-end` **requested** lines (not a claim about
coverage returned), write bodies collapse to char count, `edits` arrays to `×N`, individual
`oldText`/`newText` to `oldText=<N chars>`, bash drops `timeout` but keeps `background` etc.
The *original arguments are never rewritten* — the signature is a separate derived record.

**`bashOutputLabel` (`index.ts:560`)** — the only place anything is *inferred*, and deliberately
paranoid. Conservative lexical regexes (`commandHints`, `index.ts:538`) classify the command, then
only these get quoted out:

- reported nonzero shell status → `exit 1`
- empty output → `no output`
- recognized test/build summary lines (`N passed in 0.21s`, `BUILD FAILED`)
- HTTP status codes, only for leading `curl`/`wget`
- one error-text line, only for recognized *execution* commands — never for `cat`/`grep`/`git`, so a
  fixture file containing the string "Error:" cannot be laundered into a fake failure
- a git commit hash, only in git's own bracketed `[main abc1234]` form

Unrecognized shapes get nothing. Media-only, structured-only, or truncated output never qualifies as a
"tiny complete observation". The checkpoint text itself states the semantics: `[tool error]` = a
matched result flagged as error; `[unknown]` = no safely matched result; **unlabelled does NOT
establish exit 0 or passing tests**.

### Step 3 — Tag every line with its owning user turn

While emitting lines, each is stamped `turnIds[i]` = the id of the raw **user** message that opened
that turn (`turnOwners`, `turn-context.ts:16`). A *turn* = one user message plus everything until the
next one, **even across several compaction spans**. This is what later makes whole turns individually
selectable without touching file state.

### Step 4 — Assemble history instead of overwriting it (`collectBaseAndSpans`, `index.ts:920`)

The anti-photocopy mechanism. Every verbatim compaction entry stores **only its own new span** in
`details.span`. At the next compaction the extension walks the branch, collects *all* prior spans,
and re-renders them side by side:

```
compaction #1  →  span A (lines frozen, never re-summarized)
compaction #2  →  span B
compaction #3  →  span C

checkpoint #3 = [ base? ] + span A + span B + span C   ← re-rendered, not re-summarized
```

Prose that survives five compactions is the **same original characters** every time. If an earlier
model summary exists (someone ran `/compaction-method summary`), it is preserved as an opaque
`<compacted-base kind="model-summary">` block and flagged: *"Mixed context: earlier material is
model-summarized, not verbatim."*

### Step 5 — The size guard: a ladder of diminishing damage (`capSpans`, `lib/checkpoint-cap.ts:89`)

Over budget ≠ start cutting prose. It walks a ladder, oldest-first across **all** spans, stopping the
instant it fits:

```
1. bash previews 160 → 120 encoded chars
2. bash previews 120 →  80 encoded chars
3. fold repeated "[Tools]: " prefixes on adjacent tool-only lines (never across prose/custom/user/span)
4. remove non-user content, whole calls independently removable  ← first real loss
5. stub long user messages to a 120-char prefix
6. remove the oldest remaining content, user messages included   ← last resort
```

- Rungs 1–3 are **presentation only** → span attribute `tools-tightened="true"`, no history lost.
- Rungs 4–6 set `trimmed="true"` plus a recovery note.

The trick that keeps tightening from compounding: each span also stores versioned `details.tools`
**display records** (signature fragments + the *original* encoded command prefix, never unbounded
payloads). A 120-char preview is always derived from the canonical prefix, never from an already
shortened 160-char rendering. And a record is trusted only if it **round-trips exactly**:
`renderToolLine(record.calls) === span.lines[index]` (`toolRecords`, `checkpoint-cap.ts:48`);
otherwise the line stays opaque and falls back to conservative omission. Legacy text-only spans and
opaque inherited bases are never regex-repaired. Protected lines (fidelity slots, filter markers) are
untouchable at every rung.

Budget default: `warn 25%` of the model's context window, estimated at `ceil(chars/4)` — not a
tokenizer. Precedence: session setting → `MECH_COMPACT_MAX_SUMMARY_CHARS` →
`MECH_COMPACT_MAX_SUMMARY_PERCENT`. Retained startup reads and an inherited base count as **fixed
overhead** and are never trimmed — if they alone exceed the budget, it warns and keeps them.

### Step 6 — Assemble the envelope (`buildMechanicalSummary`, `index.ts:1204`)

Fixed, mechanical section order:

```
## Verbatim compaction checkpoint (compaction N on this branch)
explanation + tool-status label semantics + recovery instructions
────────────── conversation section (the budgeted part) ──────────────
<compacted-base>             (only if a model summary exists)
<compaction-span n=1 …>      oldest first, each with compacted-at / trigger / covers
<compaction-span n=2 …>
retained startup reads
<read-files> / <modified-files>  with evidence annotations
───────────────────────────────────────────────────────────────────────
evidence-boundary warning
"Conversation from <timestamp> onward continues verbatim below — it was not compacted."
════════ Pi's retained verbatim tail follows ════════
then AFTER the tail + any pending user input:
   task-relevant recovery reminders + recovery recap
```

Placing the reminders *after* the recent tail rather than before it is deliberate instruction
delivery: the checklist goes where generation resumes. It is a saved custom message
(`transparent-compact:reorient`) tagged with the compaction/revision id, re-placed by
`context_with_system` (`index.ts:1926`) on the first post-compaction request, with obsolete managed
checklists deduplicated — including ones Pi kept inside its tail. Verbatim compaction later skips its
own generated checklists when building spans, so they never accumulate. User and assistant prose is
never stripped by matching instruction text.

### Step 7 — Store canon, render lossily

`details` on the compaction entry hold the **uncapped** canonical span lines, turn ids, tool records,
startup reads, and cumulative file lists. The rendered `summary` string is a *derived view*. Stored
entries are never edited; a later checkpoint re-renders its earlier spans under the current budget,
so raising the budget or `/cap-compaction off` restores full previews.

### Failure policy

If the checkpoint build throws, it does **not** silently fall back to a model summary when filtering
or fidelity choices exist — it cancels the compaction instead, because a summary would silently drop
the user's explicit choices (catch block, `index.ts:2351`).

---

# Part 2 — The user-controlled loss dial

Four fidelities per **whole turn**, edited in `/filter-turns` (existing checkpoint, no compaction) or
`/compact-filtered` (compact first, then the same picker). `[-] LOCKED` is an availability state, not
a fifth mode — turns overlapping Pi's retained tail can't be half-removed.

```
[X] PRUNED       → one marker: "[Turn manually filtered]"
[ ] PROSE        → default mechanical compaction
[○] PROSE+TOOLS  → original messages, full tool args/results, thinking → "[Thinking omitted]"
[■] FULL         → original messages incl. all recorded thinking and images
```

## How PROSE+TOOLS / FULL avoid storing a second copy

The checkpoint stores only a **slot**: a generated random `uuidv7` marker at a checked UTF-16 offset,
plus `{turnId, mode, offset, marker}` (`fidelitySpans` / `locateSlots`, `turn-context.ts:80,101`).
No raw payload is ever copied into checkpoint metadata.

At *request* time, `expandCheckpoint` (`turn-context.ts:116`) splits the checkpoint at those offsets
and splices in the original messages from the raw branch:

```
"...prose... [Full-fidelity turn abc: FULL; slot 018f…] ...more prose..."
                       ↓ replace marker with the real messages
"...prose..." + [user, assistant+thinking, toolCall, toolResult] + "...more prose..."
```

Safety properties enforced in code, not by convention:

- Markers are **generated**, never pattern-matched from historical text — a transcript cannot spoof one
  (`locateSlots` also requires exactly one occurrence).
- Slot offsets are re-validated against the exact string, mode, and duplicate-turn rules.
- Tool-call/result groups must be complete and unambiguous or it throws *"choose PROSE instead"*
  (`validateTools`, `turn-context.ts:36`).
- Native messages are `structuredClone`d — later provider transforms cannot mutate session records.
- Original projection disables *only* compaction, preserving branch topology, explicit context
  omissions/replacements, and custom-message conversion (`originalProjection`, `turn-context.ts:29`).
  It never resurrects content the user explicitly omitted.
- Full-fidelity turns count as **fixed overhead** in the cap; only PROSE is trimmable. Budget is
  re-checked before saving *and* on every actual request (`checkContextBudget`,
  `turn-context.ts:161`), with output/framing reserve. Oversize → hard abort with instructions to
  downgrade, never silent downgrade. A hook error alone doesn't cancel a Pi request, so it calls
  `ctx.abort()` explicitly (`index.ts:1934`).

Standalone edits append a non-context `transparent-compact:filter-revision` entry (source compaction
id + rendered summary + exclusion snapshot + fidelity + slots) rather than rewriting the compaction
entry; latest revision for the active compaction wins. Exclusions are stored as a **snapshot**, not an
append-only union, so unchecking actually restores.

## Asides and automatic filtering

`/filter-later <msg>` (alias `/fl`) submits an ordinary user turn and writes a non-context
`transparent-compact:aside-turn` bridge entry just before it — a short-lived flag, no content
heuristic. `/filter-auto on` makes threshold/overflow compaction apply eligible pre-flags without
opening a picker; manual `/compact` never applies new pre-flags. Explicit keep/restore overrides are
recorded in `keptAsideTurnIds` so auto mode doesn't re-exclude what the user restored.

---

# Part 3 — Read path: `context_lookup`

The other half of the algorithm, where the deferred cost gets paid.

## Render the raw branch in memory, never to disk

`sessionTranscript` (`index.ts:1775`) calls `ctx.sessionManager.getBranch()` — **raw** history, not
the compacted projection: compaction and context edits never remove earlier thinking or tool results
here. `branchToTranscript` (`index.ts:340`) serializes it to physical lines (no line may contain `\n`,
so array position maps 1:1 to a line), one block per entry:

```
==============================================================================
ENTRY 018f3a…  assistant (model=anthropic/…, stop=toolUse)  2026-…
[thinking]
   …complete, untruncated…
[text]
   …
[toolCall] bash
{ "command": "…" }
```

Plus an **authoritative index**: `entryId → {startLine, endLine, sections[], relatedIds[]}`.
Attribution comes *only* from that renderer index (`entryAt`, `index.ts:1367`), never from scanning
text — so a tool output that literally prints `ENTRY fake-id  user` inside itself cannot spoof
ownership. Call/result pairing uses object identity plus tool-name matching (`pairedToolResults`,
`index.ts:307`): ambiguous duplicate ids stay unknown, sequential reuse is allowed after consumption,
and relations are one-hop and chronological (a result pulls its originating assistant entry, not
sibling outputs).

## A subagent searches it in its own context

Three internal tools over those lines — `list_entries`, `grep`, `show_entry` — paginated at
12k/12k/24k payload chars. The subagent gets the question, a 60-line table of contents, and iterates
with its own system prompt. Its raw search output **never enters the main conversation**; only the
final findings (capped at 12k chars) come back.

Budget accounting is itself an algorithm (`runLookupSubagent`, `index.ts:1625`):

```
25 "substantive search turns"   — new queries, changed params, replayed/invented cursors
3× = 75 total model calls       — hard ceiling on ALL search-phase calls
continuation pages = free       — but only if the cursor was actually ISSUED to this subagent,
                                  held in an ephemeral Set, single-use
when either budget ends         → one tool-free write-up call of partial findings
```

Each issued cursor is a key of `(toolName, args)`; a batch is free only if **every** call in it
consumes a still-available cursor, and cursors are deleted *before* dispatch so a sibling call cannot
double-spend one. Stale/paging-only answers carry an explicit incomplete-findings marker. Both the
normal answer and the forced write-up must distinguish *unreached evidence* from *absence*: an
incomplete search does not establish that the answer was never recorded.

## Rate-limiting the main agent

`consecutiveLookups` (`index.ts:1805`) walks the raw branch backwards, stopping at a user message, a
bash execution, or any non-recovery tool result. Parallel lookups in one batch all count, so they
can't both claim the first slot. Fallback tools (`context_list_entries`, `context_grep`,
`context_show_entry`) neither count nor reset; their output *does* enter main context — that's the
trade-off, and they exist only as fallback when lookup findings are incomplete.

---

# Invariants that hold the whole thing together

1. **No model call inside compaction.** `/compact` is pure string manipulation.
2. **Stored canon is never edited.** Only rendered views change; restoring a setting restores content.
3. **Presentation loss ≠ content loss**, and they are labelled differently (`tools-tightened` vs `trimmed`).
4. **Derived tokens are generated, never pattern-matched from history.** Slots, boundaries, and entry
   attribution all come from structures model text cannot influence.
5. **All state lives in session entries read off the active branch** — method, cap, filters, fidelity,
   startup reads. No in-memory flags, so reload / resume / fork / tree navigation inherit exactly the
   choices on their own path.
6. **Every claim is hedged at the source.** The checkpoint text itself says retained prose records
   *what was said*, not what is true; historical output never establishes current file or build state.

# Where it hurts

- **Recovery depends on model compliance.** Instructions are delivered, not obeyed. Weak models may
  still confabulate instead of calling `context_lookup`.
- **The raw branch grows unbounded.** Compaction shrinks the *view*, not the file; lookups get slower
  and larger over very long sessions.
- **`FULL` pins can deadlock compaction**: they are uncappable fixed overhead, so a large pinned turn
  can stop automatic compaction from freeing enough room, and requests abort until it is downgraded.
- **`bashOutputLabel` and startup-read retention are lexical heuristics.** Startup retention keeps
  successful text reads between the 1st and 2nd user prompt only if the path/basename literally
  appears in AGENTS.md / CLAUDE.md — indirect references, shell reads, and nested calls miss.
- Token math is `estimateTokens` ≈ `ceil(chars/4)` + 8/message framing, not provider tokenization.
- Doc drift: `MECH_COMPACT_LOOKUP_MAX_CALLS` defaults to **3** in code (`lookupMaxCallsFromEnv`,
  `index.ts:80`) while the README table says `1`.
- The README's own framing: the cap is a fallback for running compaction rounds well past intended
  use — "wrap up and hand off to a new session". If normal work constantly blows the cap, a different
  compaction tool is probably the better fit.

---

**One-sentence summary:** *keep the lossless transcript as ground truth, replace the summary with a
mechanically re-rendered index of verbatim fragments bounded by a damage-minimizing ladder, and buy
back detail on demand through a budgeted subagent over the untouched original.*

## Related

- [[deterministic_compaction]] — general survey of zero-LLM compaction approaches
- [[pi-fabric-compaction]] — Pi Fabric's pure-TypeScript compaction, same "fire the LLM" move
- [[tau-extensions]] — "Deterministic Compaction: essential for local LLMs, small context windows"

## Refs

- Repo (from `package.json` `repository`): https://github.com/felixwoolford/transparent-compact-pi
- Install: `pi install npm:@foliox/transparent-compact-pi` · also `pi install git:github.com/felixwoolford/transparent-compact-pi`
  (no `pi.dev/packages/…` page exists for it — that URL 404s, it is npm/git only)
- README's cited study on minimal retrieval-over-summarization: https://arxiv.org/abs/2610.20116
- Local inference note worth remembering: on a single-slot llama.cpp server (`-np 1`) each lookup
  evicts the main session's KV cache — enable `--cache-ram <MiB>` sized for one full main-session state.
