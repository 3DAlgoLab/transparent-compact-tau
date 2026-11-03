"""The bounded recovery tools over the untouched branch (algorithm Step 8).

These are the fallback path: the model has `context_lookup` for a question, and
these three tools for the case where it already knows what it wants to see.
Every one of them reads the **raw branch** (`context.entries`), never the
compacted projection, and every one states its own truncation instead of
letting a cut-off answer look complete.

Nothing here edits history. Store A is append-only; these tools only read it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from tau_agent.messages import TextContent
from tau_agent.session.entries import SessionEntry
from tau_agent.tools import AgentTool, AgentToolResult
from tau_agent.types import JSONValue

from .transcript import Transcript, build_transcript, paginate_lines

# A recovery answer is a means to an answer, not a way to re-import the whole
# session into context. Anything larger is reported as cut.
OUTPUT_CHAR_LIMIT = 12_000
LINE_PREVIEW_CHARS = 400
DEFAULT_GREP_MATCHES = 40
MAX_GREP_MATCHES = 200
DEFAULT_LIST_LIMIT = 60
MAX_LIST_LIMIT = 300

ReadEntries = Callable[[], Sequence[SessionEntry]]


def _result(text: str, **details: JSONValue) -> AgentToolResult:
    return AgentToolResult(
        content=[TextContent(text=text)], details=dict(details) if details else None
    )


def _render(text: str) -> AgentToolResult:
    """Wrap tool output, cutting at a character budget and saying so."""
    lines = text.split("\n")
    kept, used = paginate_lines(lines, limit=OUTPUT_CHAR_LIMIT)
    body = "\n".join(kept)
    if used < len(lines):
        body += f"\n[output cut after {used} of {len(lines)} lines — narrow the query or show one entry at a time]"
    return _result(body)


def _error(message: str) -> AgentToolResult:
    return _result(message, error=True)


def _transcript(read_entries: ReadEntries) -> Transcript:
    return build_transcript(read_entries())


def _bounded_int(
    arguments: Mapping[str, JSONValue], key: str, default: int, minimum: int, maximum: int
) -> int:
    value = arguments.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(minimum, min(maximum, value))


def _optional_str(arguments: Mapping[str, JSONValue], key: str) -> str | None:
    value = arguments.get(key)
    return value if isinstance(value, str) and value else None


def _preview(line: str) -> str:
    return line if len(line) <= LINE_PREVIEW_CHARS else line[:LINE_PREVIEW_CHARS] + "…"


def _list_tool(read_entries: ReadEntries) -> AgentTool:
    async def execute(
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal: object = None,
        on_update: object = None,
    ) -> AgentToolResult:
        del tool_call_id, signal, on_update
        limit = _bounded_int(arguments, "limit", DEFAULT_LIST_LIMIT, 5, MAX_LIST_LIMIT)
        branch = read_entries()
        if not branch:
            return _error("No session history is available to list.")
        transcript = _transcript(read_entries)
        rows = transcript.table_of_contents(limit=limit)
        header = (
            f"Session history: {len(transcript.entries)} entries over {transcript.line_count} lines "
            f"(oldest first). This is the raw branch, not the compacted view."
        )
        return _render("\n".join([header, *rows]))

    return AgentTool(
        name="context_list_entries",
        label="List history entries",
        description=(
            "List this conversation's raw session history (oldest first) as one line per entry: "
            "line number, entry id, kind, and a short preview. Use it to find the entry id you want, "
            "then `context_show_entry` to read it. This reads the untouched history, so it still "
            "works after compaction has hidden details from the conversation."
        ),
        parameters={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": f"Maximum rows to return (default {DEFAULT_LIST_LIMIT}, max {MAX_LIST_LIMIT}).",
                }
            },
        },
        execute_fn=execute,
        execution_mode="sequential",
    )


def _grep_tool(read_entries: ReadEntries) -> AgentTool:
    async def execute(
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal: object = None,
        on_update: object = None,
    ) -> AgentToolResult:
        del tool_call_id, signal, on_update
        pattern = _optional_str(arguments, "pattern")
        if pattern is None:
            return _error("`pattern` is required.")
        limit = _bounded_int(arguments, "max_matches", DEFAULT_GREP_MATCHES, 1, MAX_GREP_MATCHES)
        context_lines = _bounded_int(arguments, "context_lines", 0, 0, 3)
        ignore_case = arguments.get("ignore_case") is not False
        literal = arguments.get("literal") is not False
        entry_id = _optional_str(arguments, "entry_id")
        transcript = _transcript(read_entries)
        if entry_id is not None and transcript.entry(entry_id) is None:
            return _error(
                f"Unknown entry id {entry_id!r}. Run context_list_entries to see the ids."
            )

        hits, cut = transcript.grep(
            pattern,
            limit=limit,
            case_insensitive=ignore_case,
            literal=literal,
            context_lines=context_lines,
        )
        if entry_id is not None:
            hits = [hit for hit in hits if hit.entry_id == entry_id]
        if not hits:
            kind = "literal" if literal else "regex"
            return _render(
                f"No match for {kind} {pattern!r} in {transcript.line_count} lines of session history."
            )
        rows = [
            f"L{hit.line}  {hit.entry_id or '(preamble)'}  {hit.role or 'note'}: {_preview(hit.text)}"
            for hit in hits
        ]
        status = f"{len(hits)} match(es)" + (" (more exist — raise max_matches)" if cut else "")
        return _render("\n".join([f"Search of session history: {status}", *rows]))

    return AgentTool(
        name="context_grep",
        label="Search history",
        description=(
            "Search this conversation's raw session history for a string (default) or regex, and "
            "return the matching lines with their entry ids. Use it after `context_lookup` when you "
            "need an exhaustive sweep, or to confirm exact text. Matches are attributed by the "
            "history index, so text inside a tool output cannot impersonate another entry."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Text to search for (literal by default).",
                },
                "literal": {
                    "type": "boolean",
                    "description": "Treat the pattern as literal text (default true).",
                },
                "ignore_case": {
                    "type": "boolean",
                    "description": "Case-insensitive search (default true).",
                },
                "max_matches": {
                    "type": "integer",
                    "description": f"Maximum matches to return (default {DEFAULT_GREP_MATCHES}, max {MAX_GREP_MATCHES}).",
                },
                "context_lines": {
                    "type": "integer",
                    "description": "Extra lines to include after each match (0-3).",
                },
                "entry_id": {"type": "string", "description": "Restrict the search to one entry."},
            },
            "required": ["pattern"],
        },
        execute_fn=execute,
        execution_mode="sequential",
    )


def _show_tool(read_entries: ReadEntries) -> AgentTool:
    async def execute(
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal: object = None,
        on_update: object = None,
    ) -> AgentToolResult:
        del tool_call_id, signal, on_update
        entry_id = _optional_str(arguments, "entry_id")
        if entry_id is None:
            return _error("`entry_id` is required.")
        to_id = _optional_str(arguments, "to_id")
        transcript = _transcript(read_entries)
        if transcript.entry(entry_id) is None:
            return _error(
                f"Unknown entry id {entry_id!r}. Run context_list_entries to see the ids."
            )
        ids: Sequence[str] = (entry_id,)
        if to_id is not None:
            window = transcript.entry_range(entry_id, to_id)
            if window is None:
                return _error(f"Unknown end of range: {to_id!r}.")
            ids = window
        return _render(transcript.render_entries(ids))

    return AgentTool(
        name="context_show_entry",
        label="Show history entry",
        description=(
            "Show one entry (or an inclusive window with `to_id`) from this conversation's raw "
            "session history, complete: full tool output, thinking, and arguments as they were "
            "recorded. Entry ids come from `context_list_entries`, `context_grep`, or the ids "
            "quoted inside a compaction checkpoint."
        ),
        parameters={
            "type": "object",
            "properties": {
                "entry_id": {"type": "string", "description": "The entry to show."},
                "to_id": {
                    "type": "string",
                    "description": "Optional inclusive end of a window of entries.",
                },
            },
            "required": ["entry_id"],
        },
        execute_fn=execute,
        execution_mode="sequential",
    )


def recovery_tools(read_entries: ReadEntries) -> list[AgentTool]:
    """Build the three deterministic fallback tools."""
    return [_list_tool(read_entries), _grep_tool(read_entries), _show_tool(read_entries)]


__all__ = ["OUTPUT_CHAR_LIMIT", "build_transcript", "recovery_tools"]
