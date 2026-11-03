"""Assembling the checkpoint with zero model calls (algorithm Steps 2-4, 6-7).

Two halves live here:

* **`summarize_entries`** compresses raw entries into span lines mechanically —
  a switch on message role, no judgment. Prose survives verbatim, thinking and
  most tool output do not, and each line remembers the user turn that owns it.
* **`build_checkpoint`** assembles the envelope: an inherited base block (if any)
  followed by every stored span re-rendered side by side, then this compaction's
  new span. The canonical record in `details` holds the **uncapped** lines; the
  rendered summary is a derived view under the current budget, so a later
  checkpoint can restore what an earlier cap hid.

Nothing in this module calls a model, opens a file, or edits stored history.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from tau_agent.messages import (
    AssistantMessage,
    BashExecutionMessage,
    BranchSummaryMessage,
    CompactionSummaryMessage,
    CustomMessage,
    ImageContent,
    TextContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from tau_agent.session.entries import (
    CompactionEntry,
    CustomMessageEntry,
    MessageEntry,
    SessionEntry,
)
from tau_agent.types import JSONValue

from transparent_compact.cap import (
    MIN_LINE_BUDGET_CHARS,
    SPAN_TRIMMED_ATTR,
    SPAN_TRIMMED_NOTE,
    TIGHTENED_ATTR,
    CappedSpans,
    SpanContent,
    ToolLine,
    ToolStub,
    cap_spans,
    render_tool_line,
)
from transparent_compact.signature import (
    bash_output_label,
    checkpoint_path,
    tool_call_signature,
    tool_signature,
)
from transparent_compact.spans import (
    MECH_KIND,
    SPAN_DETAILS_VERSION,
    BaseBlock,
    StoredSpan,
    is_tool_only_assistant,
    mech_details,
    span_to_json,
)
from transparent_compact.transcript import format_timestamp

RECOVERY_HISTORY_NOTE = (
    "Compaction hides earlier details from your current view. The original conversation "
    "history, including recorded tool outputs and your reasoning, is preserved in the "
    "session and can be retrieved."
)
CHECKPOINT_EXPLANATION = (
    "**Compacted conversation follows.** The earlier part of this conversation has been "
    "compacted into the checkpoint below. This is not the full transcript. " + RECOVERY_HISTORY_NOTE
)
RECOVERY_INSTRUCTIONS = (
    "**Recovering hidden details.** Use `context_lookup` with a specific historical question: "
    "a subagent searches this conversation's full session history and returns relevant findings. "
    "Use the recovery tools for history established by this conversation or its checkpoint, not "
    "for project discovery or exploration of other sessions. If its findings are incomplete, use "
    "the bounded fallback tools `context_list_entries`, `context_grep`, and `context_show_entry`."
)
EVIDENCE_BOUNDARY_RULE = (
    "**Evidence boundary:** Tool stubs and file lists omit supporting details. Most output and "
    "detailed payloads are omitted. Inline observations are historical, not verification of "
    "unshown details or current state. Use `context_lookup` for omitted observations or "
    "provenance; inspect sources for current state."
)
FILE_LIST_EXPLANATION = (
    "File lists record earlier operations. Read/write annotations describe retained evidence as "
    "of this checkpoint, not live filesystem state; unannotated paths do not establish content "
    "visibility."
)
MIXED_CONTEXT_WARNING = (
    "**Mixed context: earlier material is model-summarized, not verbatim.** The verbatim "
    "guarantee applies only to the labelled verbatim spans and uncompacted tail. The "
    "model-summary base may omit or reinterpret details; use `context_lookup` to recover "
    "original history."
)
SECTION_HEADER = "### Compacted conversation (oldest first)"
SPAN_CLOSE = "</compacted-span>"
BASE_CLOSE = "</compacted-base>"
CUSTOM_STUB_NOTE = (
    "- Newly compacted custom messages are kept only as type stubs; their content is removed and "
    "remains recoverable in the session through context_lookup. Stored canonical spans are "
    "unchanged; their presentation may tighten under the cap."
)
PROSE_NOTE = (
    "- In the compacted spans, user and assistant prose is kept verbatim, subject to budget "
    "trimming. Retained prose is a record of what was said, not verification of its claims. Tool "
    "signatures are display notation, not replayable shell commands: paths under the working "
    "directory become relative (`.` in commands); read ranges are requested lines; write/edit "
    "payloads become sizes/counts. New bash previews keep the first 160 encoded characters, "
    "followed by … when truncated, with escaped newlines. Thinking and most output are removed; "
    "selected output observations remain after →."
)
STATUS_NOTE = (
    "- [tool error] means the result was flagged as an error; [unknown] means no safely matched "
    "result/status. Unlabelled does NOT establish exit 0 or passing tests. → exit N is a reported "
    "shell status; output: quotes/extracts selected output, not an inferred verdict. Everything "
    "shown is historical."
)
TAIL_NOTE = (
    "- Tau keeps the most recent part of the conversation verbatim (its "
    "`keep_recent_tokens` setting). The spans below cover only what came *before* that. The "
    "retained verbatim conversation begins after the explicit boundary marker at the end of this "
    "checkpoint."
)
TRANSCRIPT_NOTE = (
    "- The full transcript before this point is kept in the session; use `context_lookup` to "
    "recover details (including thinking and tool outputs)."
)
EMPTY_SPANS_TEXT = "(no user or assistant prose in these spans — use context_lookup)"


@dataclass(frozen=True)
class SpanLines:
    """One span's canonical lines, each tagged with its owning user turn."""

    lines: tuple[str, ...] = ()
    turn_ids: tuple[str | None, ...] = ()
    tools: tuple[ToolLine, ...] = ()

    def span_content(
        self, at: str, reason: str, *, from_ts: str | None = None, to_ts: str | None = None
    ) -> StoredSpan:
        return StoredSpan(
            at=at,
            reason=reason,
            lines=self.lines,
            turn_ids=self.turn_ids,
            tools=self.tools,
            from_ts=from_ts,
            to_ts=to_ts,
        )


@dataclass(frozen=True)
class CheckpointResult:
    """The rendered summary plus the canonical record to store with it."""

    summary: str
    details: dict[str, JSONValue]
    capped: CappedSpans | None = None
    warnings: tuple[str, ...] = ()


BranchEntry = MessageEntry | CustomMessageEntry


def _text_with_images(message: UserMessage) -> tuple[str, int]:
    if isinstance(message.content, str):
        return message.content, 0
    text = "".join(block.text for block in message.content if isinstance(block, TextContent))
    images = sum(1 for block in message.content if isinstance(block, ImageContent))
    return text, images


def _pair_results(entries: Sequence[BranchEntry]) -> dict[tuple[str, str], ToolResultMessage]:
    """Match tool calls to results by call id and tool name, never by text.

    Mirrors the transcript renderer: a call id reused twice without a consumed
    result in between is ambiguous and stays `[unknown]`.
    """
    pending: dict[str, tuple[str, str] | None] = {}
    pairs: dict[tuple[str, str], ToolResultMessage] = {}
    for entry in entries:
        if not isinstance(entry, MessageEntry):
            continue
        message = entry.message
        if isinstance(message, AssistantMessage):
            for call in message.tool_calls:
                if not call.id:
                    continue
                pending[call.id] = None if call.id in pending else (entry.id, call.name)
        elif isinstance(message, ToolResultMessage):
            owner = pending.get(message.tool_call_id)
            if owner is None or owner[1] != message.tool_name:
                continue
            pairs[(owner[0], message.tool_call_id)] = message
            del pending[message.tool_call_id]
    return pairs


def summarize_entries(
    entries: Sequence[BranchEntry],
    *,
    cwd: str,
    owners: Mapping[str, str | None],
    skipped_custom_types: Iterable[str] = (),
) -> SpanLines:
    """Compress raw entries into span lines and turn tags (no model, no judgment).

    Thinking and tool results are dropped here on purpose: they stay in the raw
    branch, which is what makes the checkpoint small without making the session
    incomplete.
    """
    skipped = set(skipped_custom_types)
    results = _pair_results(entries)
    lines: list[str] = []
    turn_ids: list[str | None] = []
    tools: list[ToolLine] = []

    def emit(text: str, owner: str | None, record: ToolLine | None = None) -> None:
        lines.append(text)
        turn_ids.append(owner)
        if record is not None:
            tools.append(record)

    for entry in entries:
        owner = owners.get(entry.id)
        if isinstance(entry, CustomMessageEntry):
            if entry.custom_type in skipped:
                continue
            emit(
                f"[Custom message]: custom(type={entry.custom_type}) [content removed — use context_lookup]",
                owner,
            )
            continue
        message = entry.message
        if isinstance(message, UserMessage):
            text, images = _text_with_images(message)
            if text.strip() or images:
                emit(f"[User]: {text}", owner)
        elif isinstance(message, AssistantMessage):
            prose = message.text.strip()
            stubs: list[ToolStub] = []
            for call in message.content:
                if not isinstance(call, ToolCall):
                    continue
                result = results.get((entry.id, call.id))
                status = (
                    " [unknown]" if result is None else (" [tool error]" if result.is_error else "")
                )
                command = call.arguments.get("command") if call.name == "bash" else None
                label = ""
                if result is not None and isinstance(command, str):
                    label = bash_output_label(command, result)
                stub = tool_signature(call.name, call.arguments, cwd)
                suffix = stub.suffix + status + (f" → {label}" if label else "")
                stubs.append(ToolStub(prefix=stub.prefix, command=stub.command, suffix=suffix))
            if prose:
                emit(f"[Assistant]: {prose}", owner)
            if stubs:
                index = len(lines)
                emit(
                    render_tool_line(stubs),
                    owner,
                    ToolLine(
                        index=index,
                        calls=tuple(stubs),
                        mergeable=not prose and is_tool_only_assistant(message),
                    ),
                )
        elif isinstance(message, BashExecutionMessage):
            signature = tool_call_signature("bash", {"command": message.command}, cwd)
            emit(f"[BashExecution]: {signature} [output removed]", owner)
        elif isinstance(message, CustomMessage):
            if message.custom_type in skipped:
                continue
            emit(
                f"[Custom message]: custom(type={message.custom_type}) [content removed — use context_lookup]",
                owner,
            )
        elif isinstance(
            message, (BranchSummaryMessage, CompactionSummaryMessage, ToolResultMessage)
        ):
            continue
    return SpanLines(lines=tuple(lines), turn_ids=tuple(turn_ids), tools=tuple(tools))


def file_operations(
    entries: Sequence[BranchEntry],
    *,
    previous: tuple[Sequence[str], Sequence[str]] | None = None,
    cwd: str = "",
) -> tuple[list[str], list[str]]:
    """Cumulative read/modified file lists for the compacted spans.

    Tau's own compaction carries file lists only from its own runs, so this
    extension carries its own forward from the previous checkpoint's record and
    adds what the new span touched.
    """
    read_files: set[str] = set()
    modified_files: set[str] = set()
    if previous is not None:
        read_files.update(previous[0])
        modified_files.update(previous[1])
    for entry in entries:
        if not isinstance(entry, MessageEntry) or not isinstance(entry.message, AssistantMessage):
            continue
        for call in entry.message.tool_calls:
            path = call.arguments.get("path")
            if not isinstance(path, str) or not path:
                continue
            shown = checkpoint_path(path, cwd) if cwd else path
            if call.name == "read":
                read_files.add(shown)
            elif call.name in ("write", "edit"):
                modified_files.add(shown)
    modified = {file for file in modified_files}
    return sorted(read_files - modified), sorted(modified)


def span_open(index: int, at: str, reason: str, covers: str | None) -> str:
    covers_attr = f' covers="{covers}"' if covers else ""
    return f'<compacted-span n="{index}"{covers_attr} compacted-at="{at}" trigger="{reason}">'


def _covers(span: StoredSpan) -> str | None:
    if span.from_ts and span.to_ts:
        return f"{span.from_ts} → {span.to_ts}"
    return None


def closing_line(entries: Sequence[SessionEntry], first_kept_entry_id: str | None) -> str:
    """Name the start of the retained verbatim tail.

    Spans end at the cut point, which is earlier than the compaction itself, so
    the tail is named by the first kept entry's timestamp.
    """
    kept = next((entry for entry in entries if entry.id == first_kept_entry_id), None)
    if kept is None:
        return "Conversation continues verbatim below — it was not compacted."
    return (
        f"Conversation from {format_timestamp(kept.timestamp)} onward continues verbatim below — "
        "it was not compacted."
    )


def conversation_size(
    base: BaseBlock | None,
    spans: Sequence[StoredSpan],
    *,
    first_kept_entry_id: str | None,
    branch: Sequence[SessionEntry],
) -> tuple[int, int]:
    """Return `(fixed overhead chars, span line chars)` for the budget decision."""
    close = closing_line(branch, first_kept_entry_id)
    overhead = len(SECTION_HEADER) + 2 + len(close) + 2
    if base is not None:
        base_open = f'<compacted-base kind="{base.kind}" compacted-at="{base.at}">'
        overhead += len(base_open) + 1 + len(BASE_CLOSE) + 2 + len(base.summary)
    for position, span in enumerate(spans):
        overhead += (
            len(span_open(position + 1, span.at, span.reason, _covers(span)))
            + len(SPAN_TRIMMED_ATTR)
            + len(TIGHTENED_ATTR)
            + 2
            + len(SPAN_TRIMMED_NOTE)
            + 1
            + len(SPAN_CLOSE)
            + 2
        )
    lines = sum(sum(len(line) + 1 for line in span.lines) for span in spans)
    return overhead, lines


def render_span_block(
    index: int, span: StoredSpan, lines: Sequence[str], *, tightened: bool, trimmed: bool
) -> str:
    opener = span_open(index, span.at, span.reason, _covers(span))
    attributes = (TIGHTENED_ATTR if tightened else "") + (SPAN_TRIMMED_ATTR if trimmed else "")
    opening = opener[:-1] + attributes + ">" if attributes else opener
    body = [opening]
    if trimmed:
        body.append(SPAN_TRIMMED_NOTE)
    body.extend(lines)
    body.append(SPAN_CLOSE)
    return "\n".join(body)


def build_checkpoint(
    *,
    reason: str,
    tokens_before: int,
    now: str,
    branch: Sequence[SessionEntry],
    base: BaseBlock | None,
    spans: Sequence[StoredSpan],
    new_span: StoredSpan,
    first_kept_entry_id: str | None,
    read_files: Sequence[str],
    modified_files: Sequence[str],
    cap_limit_chars: int | None = None,
    operator_note: str | None = None,
) -> CheckpointResult:
    """Assemble the envelope, then render it under `cap_limit_chars` (None = off)."""
    warnings: list[str] = []
    all_spans = [*spans, new_span]
    # Protected lines would be filter markers and full-fidelity slots (later
    # milestones); with neither feature in play, no line is protected yet.
    contents = [SpanContent(lines=span.lines, tools=span.tools) for span in all_spans]
    overhead, line_chars = conversation_size(
        base, all_spans, first_kept_entry_id=first_kept_entry_id, branch=branch
    )

    capped: CappedSpans | None = None
    if cap_limit_chars is None:
        rendered_lines = [span.lines for span in all_spans]
    else:
        if base is not None and len(base.summary) > cap_limit_chars:
            warnings.append(
                f"the base block alone is {len(base.summary):,} chars (budget {cap_limit_chars:,}); it is kept un-capped"
            )
        budget = max(MIN_LINE_BUDGET_CHARS, cap_limit_chars - overhead)
        capped = cap_spans(contents, budget)
        rendered_lines = list(capped.lines)
        if capped.any_trimmed:
            warnings.append(
                f"conversation spans were trimmed to fit {cap_limit_chars:,} chars "
                f"(~{overhead + line_chars:,} chars of conversation); removed content is recoverable with context_lookup"
            )

    compaction_count = sum(1 for entry in branch if isinstance(entry, CompactionEntry)) + 1
    blocks: list[str] = []
    if base is not None:
        base_open = f'<compacted-base kind="{base.kind}" compacted-at="{base.at}">'
        blocks.append(f"{base_open}\n{base.summary.rstrip()}\n{BASE_CLOSE}")
    for position, span in enumerate(all_spans):
        tightened = bool(capped.any_tightened and capped.tightened[position]) if capped else False
        trimmed = bool(capped.any_trimmed and capped.trimmed[position]) if capped else False
        blocks.append(
            render_span_block(
                position + 1, span, rendered_lines[position], tightened=tightened, trimmed=trimmed
            )
        )

    parts: list[str] = []
    parts.append(
        f"## Verbatim compaction checkpoint (compaction {compaction_count} on this branch)"
    )
    parts.append(CHECKPOINT_EXPLANATION)
    if base is not None and base.kind == "model-summary":
        parts.append(MIXED_CONTEXT_WARNING)
        warnings.append(MIXED_CONTEXT_WARNING)
    parts.append(
        f"- Latest: {now} (trigger: {reason}, ~{tokens_before:,} tokens before compaction)"
    )
    parts.append(PROSE_NOTE)
    parts.append(STATUS_NOTE)
    parts.append(CUSTOM_STUB_NOTE)
    parts.append(TAIL_NOTE)
    parts.append(TRANSCRIPT_NOTE)
    if operator_note and operator_note.strip():
        parts.append(f"**Requested focus for this checkpoint:** {operator_note.strip()}")
    parts.append("")
    parts.append(RECOVERY_INSTRUCTIONS)
    parts.append("")
    parts.append(SECTION_HEADER)
    parts.append("")
    parts.append("\n\n".join(blocks) if blocks else EMPTY_SPANS_TEXT)
    if read_files or modified_files:
        parts.extend(["", FILE_LIST_EXPLANATION])
        if read_files:
            parts.append(
                '<read-files note="reads in compacted history; unless annotated, no retained read-tool output identified">\n'
                + "\n".join(read_files)
                + "\n</read-files>"
            )
        if modified_files:
            parts.append(
                '<modified-files note="modified in compacted history">\n'
                + "\n".join(modified_files)
                + "\n</modified-files>"
            )
    parts.extend(["", EVIDENCE_BOUNDARY_RULE, "", closing_line(branch, first_kept_entry_id)])

    details: dict[str, JSONValue] = {
        "kind": MECH_KIND,
        "version": SPAN_DETAILS_VERSION,
        "readFiles": list(read_files),
        "modifiedFiles": list(modified_files),
        # Only this compaction's own span, uncapped. Earlier spans are re-rendered
        # from their own entries' details — never nested, never copied forward.
        "span": span_to_json(new_span),
    }
    return CheckpointResult(
        summary="\n".join(parts), details=details, capped=capped, warnings=tuple(warnings)
    )


def previous_file_lists(entries: Sequence[SessionEntry]) -> tuple[list[str], list[str]] | None:
    """Return the carried file lists from the newest checkpoint record."""
    for entry in reversed(list(entries)):
        details = mech_details(entry)
        if details is None:
            continue
        reads = details.get("readFiles")
        modified = details.get("modifiedFiles")
        return (
            [item for item in reads if isinstance(item, str)] if isinstance(reads, list) else [],
            [item for item in modified if isinstance(item, str)]
            if isinstance(modified, list)
            else [],
        )
    return None


__all__ = [
    "BaseBlock",
    "CheckpointResult",
    "SpanLines",
    "StoredSpan",
    "build_checkpoint",
    "conversation_size",
    "file_operations",
    "previous_file_lists",
    "summarize_entries",
]
