"""Store A: the lossless raw-branch transcript (algorithm Part 3).

Tau's *projected* context (`context.transcript`, and what the provider sees)
loses anything compaction folded away. The append-only branch behind it
(`context.entries`) never does: every message, thinking block, tool result, and
`CompactionEntry` is still there. This module renders that branch into physical
lines plus an authoritative index, so recovery tools and mechanical checkpoints
can quote raw history without ever writing a second copy of it.

Three properties carry the design:

* **Physical lines.** No serialized element contains a newline, so an element's
  array position *is* its line number, and the index can address content by
  `(entry_id, line)`.
* **Attribution comes only from the index.** Callers resolve an entry from a
  line number (`entry_for_line`), never by scanning text — a tool output that
  prints a fake `ENTRY …` header inside itself cannot spoof ownership.
* **Nothing is truncated here.** Trimming is a rendering concern owned by the cap
  ladder; the transcript stays complete so a later setting change can restore
  what an earlier view hid.
"""

from __future__ import annotations

import bisect
import json
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from tau_agent.messages import (
    AgentMessage,
    AssistantMessage,
    BashExecutionMessage,
    BranchSummaryMessage,
    CompactionSummaryMessage,
    CustomMessage,
    ImageContent,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from tau_agent.session.entries import (
    BranchSummaryEntry,
    CompactionEntry,
    CustomEntry,
    CustomMessageEntry,
    LabelEntry,
    LeafEntry,
    MessageEntry,
    ModelChangeEntry,
    SessionEntry,
    SessionInfoEntry,
    ThinkingLevelChangeEntry,
)

HR = "=" * 78
"""Separator line that opens every entry block."""

PREAMBLE: tuple[str, ...] = (
    "# tau session transcript — full raw branch",
    "# Each block starts with an ENTRY header; entry ids match the session JSONL.",
    "# Thinking blocks and tool outputs are complete (not truncated).",
    "",
)

IMAGE_NOTE = "image(s) attached — binary, not inlined in transcript"


@dataclass(frozen=True)
class TranscriptSection:
    """One labelled region inside an entry block (thinking / text / toolCall)."""

    kind: str
    start_line: int


@dataclass(frozen=True)
class TranscriptEntry:
    """An authoritative index record for one session entry.

    `start_line` is the 1-based position of the ENTRY header (the HR line sits
    immediately above it) and `end_line` is inclusive, covering the trailing
    blank separator — matching the layout Pi's renderer produces.
    """

    entry_id: str
    entry_type: str
    role: str | None
    start_line: int
    end_line: int
    sections: tuple[TranscriptSection, ...] = ()
    related_ids: tuple[str, ...] = ()

    def contains_line(self, line: int) -> bool:
        return self.start_line <= line <= self.end_line


@dataclass(frozen=True)
class GrepHit:
    """One matching line, attributed to the entry that physically holds it."""

    line: int
    entry_id: str | None
    role: str | None
    text: str


@dataclass(frozen=True)
class _Block:
    """Rendered form of one entry, before line numbers are assigned."""

    header: str
    body: tuple[str, ...] = ()
    role: str | None = None
    section_offsets: tuple[tuple[str, int], ...] = ()


def format_timestamp(seconds: float) -> str:
    """Render an entry timestamp deterministically (UTC, whole seconds)."""
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text_and_image_note(content: object) -> tuple[tuple[str, ...], int]:
    """Return visible text lines plus the count of non-inlined images."""
    if isinstance(content, str):
        return tuple(content.split("\n")), 0
    if not isinstance(content, Sequence):
        return (), 0
    parts: list[str] = []
    images = 0
    for block in content:
        if isinstance(block, TextContent):
            parts.append(block.text)
        elif isinstance(block, ImageContent):
            images += 1
    text = "".join(parts)
    if images:
        text = f"{text}\n[{images} {IMAGE_NOTE}]" if text else f"[{images} {IMAGE_NOTE}]"
    return tuple(text.split("\n")), images


def _json_block(value: object) -> tuple[str, ...]:
    return tuple(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True).split("\n"))


def _serialize_message(message: AgentMessage) -> _Block:
    """Serialize one message to physical lines plus labelled section offsets.

    Offsets are relative to the first body line; the caller turns them into
    absolute line numbers. Assistant content keeps provider order, so a turn's
    thinking, prose, and calls appear exactly as the model emitted them.
    """
    if isinstance(message, UserMessage):
        lines, _ = _text_and_image_note(message.content)
        return _Block(header="user", body=lines)
    if isinstance(message, ToolResultMessage):
        lines, _ = _text_and_image_note(message.content)
        return _Block(header="toolResult", body=lines)
    if isinstance(message, AssistantMessage):
        body: list[str] = []
        offsets: list[tuple[str, int]] = []
        for block in message.content:
            if isinstance(block, ThinkingContent):
                offsets.append(("thinking", len(body)))
                body.append("[thinking]")
                body.extend(block.thinking.split("\n"))
                if block.redacted:
                    body.append("[redacted thinking — content withheld by the provider]")
            elif isinstance(block, TextContent):
                offsets.append(("text", len(body)))
                body.append("[text]")
                body.extend(block.text.split("\n"))
            elif isinstance(block, ToolCall):
                offsets.append(("toolCall", len(body)))
                body.append(f"[toolCall] {block.name}")
                body.extend(_json_block(block.arguments))
        return _Block(header="assistant", body=tuple(body), section_offsets=tuple(offsets))
    if isinstance(message, BashExecutionMessage):
        body = [f"$ {message.command}", "[output]"]
        body.extend(message.output.split("\n"))
        return _Block(header="bashExecution", body=tuple(body))
    if isinstance(message, CustomMessage):
        lines, _ = _text_and_image_note(message.content)
        return _Block(header="custom", body=lines)
    if isinstance(message, (BranchSummaryMessage, CompactionSummaryMessage)):
        return _Block(header="summary", body=tuple(message.summary.split("\n")))
    return _Block(header="message", body=_json_block(message.model_dump(mode="json")))


def _entry_block(entry: SessionEntry) -> _Block | None:
    """Render one session entry, or `None` when it carries no block header."""
    stamp = format_timestamp(entry.timestamp)
    if isinstance(entry, MessageEntry):
        message = entry.message
        block = _serialize_message(message)
        extra = ""
        if isinstance(message, ToolResultMessage):
            extra = f" ({message.tool_name}, isError={'true' if message.is_error else 'false'})"
        elif isinstance(message, AssistantMessage):
            extra = f" (model={message.provider}/{message.model}, stop={message.stop_reason})"
        elif isinstance(message, BashExecutionMessage):
            flags = [f"exit={message.exit_code}"]
            if message.cancelled:
                flags.append("cancelled")
            if message.truncated:
                flags.append("truncated")
            if message.full_output_path:
                flags.append(f"fullOutput={message.full_output_path}")
            extra = f" ({', '.join(flags)})"
        elif isinstance(message, CustomMessage):
            extra = f" (customType={message.custom_type})"
        header = f"ENTRY {entry.id}  {message.role}{extra}  {stamp}"
        header_block = _Block(
            header=header,
            body=block.body,
            role=message.role,
            section_offsets=block.section_offsets,
        )
        return header_block
    if isinstance(entry, CustomMessageEntry):
        lines, _ = _text_and_image_note(entry.content)
        return _Block(
            header=f"ENTRY {entry.id}  custom_message ({entry.custom_type})  {stamp}",
            body=lines,
            role="custom_message",
        )
    if isinstance(entry, CompactionEntry):
        tokens = "?" if entry.tokens_before is None else entry.tokens_before
        kept = "?" if entry.first_kept_entry_id is None else entry.first_kept_entry_id
        body = ["[summary]", *entry.summary.split("\n")]
        return _Block(
            header=f"ENTRY {entry.id}  compaction  {stamp}  tokensBefore={tokens}  firstKeptEntryId={kept}",
            body=tuple(body),
        )
    if isinstance(entry, BranchSummaryEntry):
        root = "?" if entry.branch_root_id is None else entry.branch_root_id
        return _Block(
            header=f"ENTRY {entry.id}  branch_summary  {stamp}  branchRootId={root}",
            body=tuple(entry.summary.split("\n")),
        )
    if isinstance(entry, ModelChangeEntry):
        provider = "" if entry.provider is None else f"{entry.provider}/"
        return _Block(
            header=f"ENTRY {entry.id}  model_change  {stamp}  -> {provider}{entry.model}  (not in LLM context)"
        )
    if isinstance(entry, ThinkingLevelChangeEntry):
        level = "(unset)" if entry.thinking_level is None else entry.thinking_level
        return _Block(
            header=f"ENTRY {entry.id}  thinking_level_change  {stamp}  -> {level}  (not in LLM context)"
        )
    if isinstance(entry, SessionInfoEntry):
        title = "" if entry.title is None else f"  name={entry.title}"
        return _Block(
            header=f"ENTRY {entry.id}  session_info  {stamp}{title}  (not in LLM context)",
        )
    if isinstance(entry, LabelEntry):
        label = "(cleared)" if not entry.label else entry.label
        return _Block(
            header=f"ENTRY {entry.id}  label  {stamp}  target={entry.target_id}  label={label}  (not in LLM context)"
        )
    if isinstance(entry, LeafEntry):
        pointer = "(none)" if entry.entry_id is None else entry.entry_id
        return _Block(
            header=f"ENTRY {entry.id}  leaf  {stamp}  entryId={pointer}  (not in LLM context)",
        )
    if isinstance(entry, CustomEntry):
        return _Block(
            header=f"ENTRY {entry.id}  custom ({entry.namespace})  {stamp}  (not in LLM context)"
        )
    return _Block(
        header=f"ENTRY {entry.id}  {getattr(entry, 'type', 'unknown')}  {stamp}  (unhandled entry type)"
    )


def _pair_tool_results(entries: Sequence[SessionEntry]) -> dict[str, tuple[str, ...]]:
    """Match each tool-result entry to the assistant entry that requested it.

    Ports Pi's `pairedToolResults`: pairing is by call id *and* tool name over
    entry ids, never by scanning rendered text. A call id seen twice with no
    consumed result in between is ambiguous and stays unmatched; sequential
    reuse after a result was consumed pairs again.
    """
    pending: dict[str, tuple[str, str] | None] = {}
    related: dict[str, list[str]] = {}
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
            related.setdefault(owner[0], []).append(entry.id)
            related[entry.id] = [owner[0]]
            del pending[message.tool_call_id]
    return {entry_id: tuple(ids) for entry_id, ids in related.items()}


class Transcript:
    """The rendered raw branch: physical lines plus an authoritative index."""

    def __init__(self, lines: Sequence[str], entries: Sequence[TranscriptEntry]) -> None:
        self.lines: tuple[str, ...] = tuple(lines)
        self.entries: tuple[TranscriptEntry, ...] = tuple(entries)
        self._by_id = {entry.entry_id: entry for entry in self.entries}
        self._starts = [entry.start_line for entry in self.entries]

    @property
    def line_count(self) -> int:
        return len(self.lines)

    @property
    def entry_ids(self) -> tuple[str, ...]:
        return tuple(entry.entry_id for entry in self.entries)

    def entry(self, entry_id: str) -> TranscriptEntry | None:
        """Return the index record for `entry_id`, or `None`."""
        return self._by_id.get(entry_id)

    def entry_for_line(self, line: int) -> TranscriptEntry | None:
        """Attribute a 1-based line to its entry using only the index."""
        if not self._starts:
            return None
        position = bisect.bisect_right(self._starts, line) - 1
        if position < 0:
            return None
        candidate = self.entries[position]
        return candidate if candidate.contains_line(line) else None

    def lines_for_entry(self, entry_id: str) -> tuple[int, int, int]:
        """Return `(first_line, last_line, block_start)` for an entry.

        `block_start` is the HR line above the ENTRY header, so a caller can
        slice `self.lines` and still get a self-describing block.
        """
        found = self.entry(entry_id)
        if found is None:
            return (0, 0, 0)
        return (found.start_line, found.end_line, found.start_line - 1)

    def render_lines(self, start_line: int = 1, end_line: int | None = None) -> str:
        """Render a 1-based inclusive line range as text."""
        start = max(1, start_line)
        end = self.line_count if end_line is None else min(self.line_count, end_line)
        if start > end:
            return ""
        return "\n".join(self.lines[start - 1 : end])

    def render_entries(self, entry_ids: Iterable[str]) -> str:
        """Render whole entry blocks, in the order requested."""
        chunks: list[str] = []
        for entry_id in entry_ids:
            found = self.entry(entry_id)
            if found is None:
                continue
            chunks.append(self.render_lines(found.start_line - 1, found.end_line))
        return "\n".join(chunks)

    def entry_range(self, from_id: str, to_id: str) -> tuple[str, ...] | None:
        """Return the entry ids from `from_id` to `to_id` inclusive.

        Unknown ids, or a reversed range, resolve by branch order rather than by
        string comparison, so an agent can quote any window it has seen.
        """
        first = self.entry(from_id)
        last = self.entry(to_id)
        if first is None or last is None:
            return None
        positions = {entry.entry_id: index for index, entry in enumerate(self.entries)}
        low, high = positions[from_id], positions[to_id]
        if low > high:
            low, high = high, low
        return tuple(entry.entry_id for entry in self.entries[low : high + 1])

    def grep(
        self,
        pattern: str,
        *,
        limit: int = 200,
        case_insensitive: bool = True,
        literal: bool = False,
        context_lines: int = 0,
    ) -> tuple[list[GrepHit], bool]:
        """Search rendered lines, attributing every hit through the index.

        Returns the hits plus whether the limit cut the search short; callers
        must present truncation rather than imply the branch was exhausted.
        """
        if not pattern:
            return ([], False)
        expression = re.escape(pattern) if literal else pattern
        flags = re.IGNORECASE if case_insensitive else 0
        try:
            matcher = re.compile(expression, flags)
        except re.error:
            return ([], False)
        hits: list[GrepHit] = []
        for index, line in enumerate(self.lines, start=1):
            if not matcher.search(line):
                continue
            owner = self.entry_for_line(index)
            hits.append(
                GrepHit(
                    line=index,
                    entry_id=None if owner is None else owner.entry_id,
                    role=None if owner is None else owner.role,
                    text=line,
                )
            )
            for offset in range(1, context_lines + 1):
                below = index + offset
                if below > self.line_count:
                    break
                owner_below = self.entry_for_line(below)
                hits.append(
                    GrepHit(
                        line=below,
                        entry_id=None if owner_below is None else owner_below.entry_id,
                        role=None if owner_below is None else owner_below.role,
                        text=self.lines[below - 1],
                    )
                )
            if len(hits) >= limit:
                return (hits, True)
        return (hits, False)

    def table_of_contents(self, *, limit: int = 60) -> list[str]:
        """Return a compact `(line, entry, kind, preview)` listing.

        A lookup subagent gets this instead of the whole branch: it can then ask
        for the windows it actually needs.
        """
        rows: list[str] = []
        for entry in self.entries:
            preview = ""
            for line in self.lines[entry.start_line : entry.end_line]:
                if line and line != HR:
                    preview = line
                    break
            kind = entry.role or entry.entry_type
            rows.append(f"line {entry.start_line}  {entry.entry_id}  {kind}  {preview[:80]}")
        if len(rows) <= limit:
            return rows
        head = max(1, limit // 2)
        tail = limit - head - 1
        omitted = len(rows) - head - tail
        tail_rows = rows[-tail:] if tail else []
        return [*rows[:head], f"… {omitted} entries omitted …", *tail_rows]


def build_transcript(entries: Sequence[SessionEntry]) -> Transcript:
    """Render the active branch (Store A) into lines plus an index."""
    lines: list[str] = list(PREAMBLE)
    blocks: list[tuple[SessionEntry, _Block, int]] = []
    for entry in entries:
        block = _entry_block(entry)
        if block is None:
            continue
        hr_index = len(lines)
        lines.append(HR)
        lines.append(block.header)
        lines.extend(block.body)
        lines.append("")
        blocks.append((entry, block, hr_index))

    related = _pair_tool_results(entries)
    records: list[TranscriptEntry] = []
    for position, (entry, block, hr_index) in enumerate(blocks):
        next_hr = blocks[position + 1][2] if position + 1 < len(blocks) else len(lines)
        sections = tuple(
            TranscriptSection(kind=kind, start_line=hr_index + offset + 3)
            for kind, offset in block.section_offsets
        )
        records.append(
            TranscriptEntry(
                entry_id=entry.id,
                entry_type=str(getattr(entry, "type", "unknown")),
                role=block.role,
                start_line=hr_index + 2,
                end_line=next_hr,
                sections=sections,
                related_ids=tuple(related.get(entry.id, ())),
            )
        )
    return Transcript(lines, records)


def paginate_lines(
    lines: Sequence[str],
    *,
    limit: int,
    marker: Callable[[int, int], str] | None = None,
) -> tuple[list[str], int]:
    """Take the largest prefix of `lines` that fits `limit` characters.

    Returns the chosen lines and how many were used, so the caller can emit its
    own continuation note (`marker(used, total)`) without this helper inventing a
    tool-specific cursor format. A single line longer than `limit` is still
    returned on its own rather than silently dropped.
    """
    chosen: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + (1 if chosen else 0)
        if chosen and used + cost > limit:
            break
        chosen.append(line)
        used += cost
    if marker is not None and len(chosen) < len(lines):
        note = marker(len(chosen), len(lines))
        if note:
            chosen.append(note)
    return (chosen, len(chosen))
