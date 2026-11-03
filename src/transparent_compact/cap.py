"""Checkpoint presentation and the progressive size guard (algorithm Step 5).

Pure functions: no Tau session access, no filesystem, no model calls, and no
editing of canonical records. A span's stored lines and tool *display records*
are canonical; this module only ever derives a smaller *view* of them, which is
why turning the cap down and later back up restores full previews instead of
compounding an old cut.

The ladder stops the instant the budget fits, oldest span first:

1. bash previews 160 → 120 encoded characters
2. bash previews 120 → 80 encoded characters
3. fold repeated ``[Tools]: `` prefixes on adjacent tool-only lines
4. remove non-user content, whole calls independently removable  ← first real loss
5. stub long user messages to a 120-character prefix
6. remove the oldest remaining content, user messages included   ← last resort

Rungs 1-3 are presentation only (``tools-tightened="true"``); rungs 4-6 set
``trimmed="true"`` plus a recovery note.
"""

from __future__ import annotations

from dataclasses import dataclass

TOOL_PREFIX = "[Tools]: "
FILTER_MARKER = "[Turn manually filtered]"
NORMAL_PREVIEW_CHARS = 160
TIGHTENED_ATTR = ' tools-tightened="true"'
SPAN_TRIMMED_ATTR = ' trimmed="true"'
SPAN_TRIMMED_NOTE = "(trimmed for size — use context_lookup)"
USER_LINE_PREFIX = "[User]:"
USER_STUB_CHARS = 120
# Floor for the span budget. If the fixed envelope alone exceeds the cap, the
# checkpoint still keeps *something* rather than deleting the whole
# conversation; the warning says the budget was exceeded.
MIN_LINE_BUDGET_CHARS = 500


@dataclass(frozen=True)
class CommandPreview:
    """An encoded display fragment, never a full or executable command."""

    head: str
    truncated: bool = False


@dataclass(frozen=True)
class ToolStub:
    """One call rendered as display notation: prefix + optional command + suffix."""

    prefix: str
    command: CommandPreview | None = None
    suffix: str = ""


@dataclass(frozen=True)
class ToolLine:
    """A display record for one tool line, addressable by line index.

    `mergeable` requires both adjacent messages to be tool-only before prefixes
    may be folded.
    """

    index: int
    calls: tuple[ToolStub, ...]
    mergeable: bool


@dataclass(frozen=True)
class SpanContent:
    """Canonical span lines plus the optional versioned display records."""

    lines: tuple[str, ...]
    tools: tuple[ToolLine, ...] = ()
    protected_lines: tuple[int, ...] = ()


@dataclass(frozen=True)
class CappedSpans:
    """The lossy view: rendered lines and which kind of damage each span took."""

    lines: tuple[tuple[str, ...], ...]
    tightened: tuple[bool, ...]
    trimmed: tuple[bool, ...]

    @property
    def any_trimmed(self) -> bool:
        return any(self.trimmed)

    @property
    def any_tightened(self) -> bool:
        return any(self.tightened)


def head_prefix(text: str, maximum: int) -> str:
    """Cut at `maximum` characters.

    JavaScript needs surrogate-pair care here; Python strings are code points, so
    a slice can never split one character in half.
    """
    return text[: max(0, maximum)]


def command_preview(encoded: str) -> CommandPreview:
    if len(encoded) <= NORMAL_PREVIEW_CHARS:
        return CommandPreview(head=encoded)
    return CommandPreview(head=head_prefix(encoded, NORMAL_PREVIEW_CHARS), truncated=True)


def render_command(preview: CommandPreview, maximum: int = NORMAL_PREVIEW_CHARS) -> str:
    head = head_prefix(preview.head, maximum)
    return head + ("…" if preview.truncated or len(preview.head) > maximum else "")


def render_tool_stub(stub: ToolStub, maximum: int = NORMAL_PREVIEW_CHARS) -> str:
    command = render_command(stub.command, maximum) if stub.command is not None else ""
    return f"{stub.prefix}{command}{stub.suffix}"


def render_tool_line(
    calls: tuple[ToolStub, ...] | list[ToolStub], maximum: int = NORMAL_PREVIEW_CHARS
) -> str:
    return TOOL_PREFIX + "; ".join(render_tool_stub(call, maximum) for call in calls)


def _valid_record(record: ToolLine) -> bool:
    if not record.calls:
        return False
    for call in record.calls:
        if not isinstance(call.prefix, str) or not isinstance(call.suffix, str):
            return False
        preview = call.command
        if preview is None:
            continue
        if not isinstance(preview, CommandPreview) or len(preview.head) > NORMAL_PREVIEW_CHARS:
            return False
        if len(preview.head) < NORMAL_PREVIEW_CHARS - 1 and preview.truncated:
            return False
    return True


def _tool_records(span: SpanContent) -> dict[int, ToolLine]:
    """Accept a display record only when it round-trips the canonical line exactly.

    Legacy text-only spans and opaque inherited bases stay opaque: they fall back
    to conservative omission rather than regex repair.
    """
    records: dict[int, ToolLine] = {}
    seen: set[int] = set()
    for record in span.tools:
        if not isinstance(record.index, int) or not 0 <= record.index < len(span.lines):
            continue
        if record.index in seen:
            records.pop(record.index, None)
            seen.add(record.index)
            continue
        seen.add(record.index)
        if not _valid_record(record):
            continue
        if render_tool_line(record.calls) == span.lines[record.index]:
            records[record.index] = record
    return records


@dataclass
class _Row:
    index: int
    protected: bool
    text: str
    calls: list[ToolStub] | None = None
    mergeable: bool = False
    preview: int = NORMAL_PREVIEW_CHARS
    fold_prefix: bool = False
    tightened: bool = False


def cap_spans(spans: list[SpanContent], budget: int | float = float("inf")) -> CappedSpans:
    """Return the largest loss-free view that fits `budget` characters."""
    tightened = [False] * len(spans)
    trimmed = [False] * len(spans)
    size = sum(sum(len(line) + 1 for line in span.lines) for span in spans)
    if size <= budget:
        return CappedSpans(tuple(span.lines for span in spans), tuple(tightened), tuple(trimmed))

    states: list[list[_Row]] = []
    for span in spans:
        records = _tool_records(span)
        rows: list[_Row] = []
        for index, text in enumerate(span.lines):
            record = records.get(index)
            rows.append(
                _Row(
                    index=index,
                    protected=index in span.protected_lines,
                    text=text,
                    calls=None if record is None else list(record.calls),
                    mergeable=False if record is None else record.mergeable,
                )
            )
        states.append(rows)

    def folded(rows: list[_Row], i: int) -> bool:
        if i <= 0 or not rows[i].fold_prefix:
            return False
        return (
            rows[i].calls is not None
            and rows[i - 1].calls is not None
            and rows[i].mergeable
            and rows[i - 1].mergeable
            and rows[i].index == rows[i - 1].index + 1
        )

    def measure() -> int:
        total = 0
        for rows in states:
            for i, row in enumerate(rows):
                saving = len(TOOL_PREFIX) - 1 if folded(rows, i) else 0
                total += len(row.text) + 1 - saving
        return total

    def finish() -> CappedSpans:
        rendered: list[tuple[str, ...]] = []
        for span_index, rows in enumerate(states):
            lines: list[str] = []
            for i, row in enumerate(rows):
                if folded(rows, i):
                    lines[-1] = f"{lines[-1]}; {row.text[len(TOOL_PREFIX) :]}"
                else:
                    lines.append(row.text)
                if row.tightened or folded(rows, i):
                    tightened[span_index] = True
            rendered.append(tuple(lines))
        return CappedSpans(tuple(rendered), tuple(tightened), tuple(trimmed))

    def render(row: _Row) -> str:
        assert row.calls is not None  # guarded by callers
        return render_tool_line(row.calls, row.preview)

    def update(row: _Row) -> None:
        text = render(row)
        if len(text) < len(row.text):
            nonlocal size
            size += len(text) - len(row.text)
            row.text = text
            row.tightened = True

    for maximum in (120, 80):
        for rows in states:
            for row in rows:
                if not row.calls or not any(call.command is not None for call in row.calls):
                    continue
                row.preview = maximum
                update(row)
                if size <= budget:
                    return finish()

    for rows in states:
        for i in range(1, len(rows)):
            rows[i].fold_prefix = True
            if folded(rows, i):
                size -= len(TOOL_PREFIX) - 1
            if size <= budget:
                return finish()

    # No presentation-only rung fits. Keep calls independently removable even
    # when prefixes are folded, and never cap one giant concatenated tool line.
    for span_index, rows in enumerate(states):
        if size <= budget:
            break
        i = 0
        while i < len(rows) and size > budget:
            row = rows[i]
            if row.protected or row.text.startswith(USER_LINE_PREFIX) or row.text == FILTER_MARKER:
                i += 1
                continue
            if row.calls and len(row.calls) > 1:
                row.calls.pop(0)
                row.text = render(row)
            elif row.calls:
                # Dropping the last call drops the line, and that is a real loss,
                # not a tightening — it must be reported so the span carries the
                # recovery note.
                trimmed[span_index] = True
                rows.pop(i)
                size = measure()
                continue
            else:
                # Prose has no calls to remove: only the last-resort rung, which
                # goes oldest first, may take it.
                i += 1
                continue
            trimmed[span_index] = True
            size = measure()

    # Stub each oversized user line exactly once. Canonical lines stay intact, so
    # a later checkpoint never shortens an already shortened presentation.
    for span_index, rows in enumerate(states):
        if size <= budget:
            break
        for row in rows:
            if size <= budget:
                break
            line = row.text
            if (
                row.protected
                or not line.startswith(USER_LINE_PREFIX)
                or len(line) <= USER_STUB_CHARS
            ):
                continue
            end = USER_STUB_CHARS
            stub = f"{line[:end]}…[truncated +{len(line) - end} chars — use context_lookup]"
            if len(stub) >= len(line):
                continue
            row.text = stub
            trimmed[span_index] = True
            size = measure()

    for span_index, rows in enumerate(states):
        while size > budget:
            index = next(
                (
                    i
                    for i, row in enumerate(rows)
                    if not row.protected and row.text != FILTER_MARKER
                ),
                -1,
            )
            if index < 0:
                break
            rows.pop(index)
            trimmed[span_index] = True
            size = measure()

    return finish()
