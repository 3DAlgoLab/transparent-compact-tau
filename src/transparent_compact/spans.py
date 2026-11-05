"""Canonical checkpoint records and turn tiling (algorithm Steps 3-4, 7).

Every verbatim compaction entry stores **only its own new span** in
`CompactionEntry.details`. At the next compaction the extension walks the branch
and re-renders all earlier spans side by side, so prose that survives five
compactions is the same original characters every time — history is assembled,
never re-summarized, and no entry id bookkeeping is needed to tile spans.

A *turn* is one user message plus everything up to the next one, even across
several compaction spans. Tagging every span line with its owning user entry is
what later makes whole turns individually selectable without touching files.

Everything here reads like untrusted input: `details` came off disk and may have
been written by an older version, so parsing is validated rather than assumed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from tau_agent.messages import (
    AssistantMessage,
    BranchSummaryMessage,
    CompactionSummaryMessage,
    TextContent,
    UserMessage,
)
from tau_agent.session.entries import (
    CompactionEntry,
    CustomEntry,
    CustomMessageEntry,
    MessageEntry,
    SessionEntry,
)
from tau_agent.types import JSONValue

from .cap import CommandPreview, ToolLine, ToolStub
from .transcript import format_timestamp

MECH_KIND = "mech-compact"
SPAN_DETAILS_VERSION = 1
METHOD_NAMESPACE = "transparent-compact:compaction-method"
CAP_NAMESPACE = "transparent-compact:cap-compaction"
DECISION_NAMESPACE = "transparent-compact:compaction-decision"


@dataclass(frozen=True)
class StoredSpan:
    """One compaction's own span, exactly as stored in `details`."""

    at: str
    reason: str
    lines: tuple[str, ...]
    turn_ids: tuple[str | None, ...] = ()
    tools: tuple[ToolLine, ...] = ()
    from_ts: str | None = None
    to_ts: str | None = None
    tokens_before: int | None = None
    # Raw entry ids this span compressed. The branch order plus the base
    # boundary already tile the history; recording the ids makes that tiling
    # checkable after a reload instead than re-derived from assumptions.
    covered_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class BaseBlock:
    """An opaque inherited summary: a model summary or a legacy checkpoint."""

    kind: str
    at: str
    summary: str
    entry_id: str = ""


CONTEXT_ROLES = (BranchSummaryMessage, CompactionSummaryMessage)
NON_TURN_ROLES = (BranchSummaryMessage, CompactionSummaryMessage)


def _is_non_empty_string(value: object) -> bool:
    return isinstance(value, str) and value != ""


def _coerce_timestamp(value: object) -> str:
    return value if isinstance(value, str) else ""


def mech_details(entry: SessionEntry) -> dict[str, Any] | None:
    """Return this extension's canonical record for a compaction entry."""
    if not isinstance(entry, CompactionEntry) or not isinstance(entry.details, dict):
        return None
    if entry.details.get("kind") != MECH_KIND:
        return None
    return entry.details


def parse_span(raw: object) -> StoredSpan | None:
    """Validate one stored span record, returning `None` if it is unusable."""
    if not isinstance(raw, dict):
        return None
    lines = raw.get("lines")
    if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
        return None
    turn_ids = raw.get("turnIds")
    owners: tuple[str | None, ...] = ()
    if isinstance(turn_ids, list):
        owners = tuple(
            None if item is None else item if isinstance(item, str) else None for item in turn_ids
        )
    raw_reason = raw.get("reason")
    reason = raw_reason if isinstance(raw_reason, str) else "unknown"
    return StoredSpan(
        at=_coerce_timestamp(raw.get("at")),
        reason=reason,
        lines=tuple(lines),
        turn_ids=owners,
        tools=_parse_tools(raw.get("tools"), len(lines)),
        from_ts=raw.get("from") if _is_non_empty_string(raw.get("from")) else None,
        to_ts=raw.get("to") if _is_non_empty_string(raw.get("to")) else None,
        tokens_before=raw.get("tokensBefore") if isinstance(raw.get("tokensBefore"), int) else None,
        covered_ids=tuple(item for item in raw.get("covered", []) if isinstance(item, str)),
    )


def _parse_tools(raw: object, line_count: int) -> tuple[ToolLine, ...]:
    """Accept only well-shaped display records; anything else stays opaque."""
    if (
        not isinstance(raw, dict)
        or raw.get("version") != 2
        or not isinstance(raw.get("lines"), list)
    ):
        return ()
    records: list[ToolLine] = []
    for item in raw["lines"]:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        mergeable = item.get("mergeable")
        calls = item.get("calls")
        if not isinstance(index, int) or not 0 <= index < line_count:
            continue
        if not isinstance(mergeable, bool) or not isinstance(calls, list) or not calls:
            continue
        stubs: list[ToolStub] = []
        valid = True
        for call in calls:
            if not isinstance(call, dict):
                valid = False
                break
            prefix, suffix = call.get("prefix"), call.get("suffix")
            if not isinstance(prefix, str) or not isinstance(suffix, str):
                valid = False
                break
            preview = call.get("command")
            command: CommandPreview | None = None
            if preview is not None:
                if not isinstance(preview, dict) or not isinstance(preview.get("head"), str):
                    valid = False
                    break
                truncated = preview.get("truncated")
                command = CommandPreview(
                    head=preview["head"],
                    truncated=False if truncated is None else bool(truncated),
                )
            stubs.append(ToolStub(prefix=prefix, command=command, suffix=suffix))
        if valid:
            records.append(ToolLine(index=index, calls=tuple(stubs), mergeable=mergeable))
    return tuple(records)


def _command_json(stub: ToolStub) -> dict[str, JSONValue]:
    """The command half of one display record, empty for prose-only rows."""
    if stub.command is None:
        return {}
    return {"command": {"head": stub.command.head, "truncated": stub.command.truncated}}


def _call_json(stub: ToolStub) -> dict[str, JSONValue]:
    return {"prefix": stub.prefix, "suffix": stub.suffix, **_command_json(stub)}


def _tool_line_json(record: ToolLine) -> dict[str, JSONValue]:
    return {
        "index": record.index,
        "mergeable": record.mergeable,
        "calls": [_call_json(stub) for stub in record.calls],
    }


def tools_to_json(tools: tuple[ToolLine, ...]) -> dict[str, JSONValue]:
    """Serialize the per-line display records the cap ladder tightens against."""
    return {"version": 2, "lines": [_tool_line_json(record) for record in tools]}


def span_to_json(span: StoredSpan) -> dict[str, JSONValue]:
    """Serialize one span canonically (uncapped lines plus display records)."""
    payload: dict[str, JSONValue] = {
        "at": span.at,
        "reason": span.reason,
        "lines": list(span.lines),
        "turnIds": list(span.turn_ids),
    }
    if span.from_ts:
        payload["from"] = span.from_ts
    if span.to_ts:
        payload["to"] = span.to_ts
    if span.tokens_before is not None:
        payload["tokensBefore"] = span.tokens_before
    if span.covered_ids:
        payload["covered"] = list(span.covered_ids)
    if span.tools:
        payload["tools"] = tools_to_json(span.tools)
    return payload


def collect_base_and_spans(
    entries: list[SessionEntry],
) -> tuple[BaseBlock | None, list[StoredSpan]]:
    """Split the branch into an opaque base plus independently stored spans.

    The base is the most recent compaction entry that is *not* a mechanical
    checkpoint with a stored span — i.e. a model-generated summary or a legacy
    checkpoint whose internals we cannot reproduce. Spans are the mechanical
    entries after it, oldest first. Spans before the base are deliberately not
    re-rendered: the base is opaque and the session still holds the raw history.
    """
    base_index = -1
    for index in range(len(entries) - 1, -1, -1):
        entry = entries[index]
        if not isinstance(entry, CompactionEntry):
            continue
        details = mech_details(entry)
        if details is None or parse_span(details.get("span")) is None:
            base_index = index
            break
    base_entry = entries[base_index] if base_index >= 0 else None
    base: BaseBlock | None = None
    if isinstance(base_entry, CompactionEntry):
        details = mech_details(base_entry)
        kind = "legacy-checkpoint" if details is not None else "model-summary"
        base = BaseBlock(
            kind=kind,
            at=format_timestamp(base_entry.timestamp),
            summary=base_entry.summary,
            entry_id=base_entry.id,
        )

    spans: list[StoredSpan] = []
    for entry in entries[max(base_index + 1, 0) :]:
        if not isinstance(entry, CompactionEntry):
            continue
        details = mech_details(entry)
        if details is None:
            continue
        span = parse_span(details.get("span"))
        if span is None:
            continue
        if not span.at:
            span = replace(span, at=format_timestamp(entry.timestamp))
        spans.append(span)
    return base, spans


def latest_mech_compaction(entries: list[SessionEntry]) -> CompactionEntry | None:
    for entry in reversed(entries):
        if isinstance(entry, CompactionEntry) and mech_details(entry) is not None:
            return entry
    return None


def custom_state(entries: list[SessionEntry], namespace: str) -> dict[str, Any] | None:
    """Return the most recent session state for `namespace` on this branch."""
    for entry in reversed(entries):
        if isinstance(entry, CustomEntry) and entry.namespace == namespace:
            return entry.data if isinstance(entry.data, dict) else {}
    return None


def turn_owner_ids(entries: list[SessionEntry]) -> dict[str, str | None]:
    """Map every entry to the user entry that opened its turn.

    A compaction boundary ends the current turn: material before the cut belongs
    to an earlier span and gets no owner here, which keeps turn identity tied to
    raw entries rather than to any projection.
    """
    owners: dict[str, str | None] = {}
    current: str | None = None
    for entry in entries:
        if not isinstance(entry, MessageEntry):
            owners[entry.id] = current
            continue
        message = entry.message
        if isinstance(message, UserMessage):
            current = entry.id
            owners[entry.id] = current
        elif isinstance(message, NON_TURN_ROLES):
            current = None
            owners[entry.id] = None
        else:
            owners[entry.id] = current
    return owners


def new_span_entries(
    entries: list[SessionEntry],
    *,
    replaced_entry_ids: tuple[str, ...],
    base_entry_id: str | None,
) -> list[MessageEntry | CustomMessageEntry]:
    """Choose the raw entries this compaction must compress into a new span.

    Everything at or before the base compaction is already covered by an opaque
    base or by earlier stored spans, so it is re-rendered instead of re-summarized.
    That is what makes consecutive compactions tile exactly: nothing is
    summarized twice and nothing is skipped. Injected `custom_message` entries are
    kept as stubs because they do occupy model context; summary messages are not,
    because their content is rebuilt from stored spans.
    """
    replaced = set(replaced_entry_ids)
    start = 0
    if base_entry_id is not None:
        for index, entry in enumerate(entries):
            if entry.id == base_entry_id:
                start = index + 1
                break
    selected: list[MessageEntry | CustomMessageEntry] = []
    for entry in entries[start:]:
        if entry.id not in replaced:
            continue
        if isinstance(entry, CustomMessageEntry):
            selected.append(entry)
        elif isinstance(entry, MessageEntry) and not isinstance(entry.message, CONTEXT_ROLES):
            selected.append(entry)
    return selected


def is_tool_only_assistant(message: AssistantMessage) -> bool:
    """True when an assistant message contains only tool calls and whitespace."""
    return bool(message.tool_calls) and not any(
        block.text.strip() for block in message.content if isinstance(block, TextContent)
    )


__all__ = [
    "CAP_NAMESPACE",
    "DECISION_NAMESPACE",
    "MECH_KIND",
    "METHOD_NAMESPACE",
    "SPAN_DETAILS_VERSION",
    "BaseBlock",
    "StoredSpan",
    "collect_base_and_spans",
    "custom_state",
    "is_tool_only_assistant",
    "latest_mech_compaction",
    "mech_details",
    "new_span_entries",
    "parse_span",
    "span_to_json",
    "turn_owner_ids",
]
