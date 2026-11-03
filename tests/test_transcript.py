"""Store A transcript: line addresses, attribution, and pairing."""

from tau_agent.messages import (
    AgentMessage,
    AssistantMessage,
    BashExecutionMessage,
    CustomMessage,
    ImageContent,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from tau_agent.session.entries import (
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

from transparent_compact.transcript import (
    HR,
    PREAMBLE,
    build_transcript,
    paginate_lines,
)


def _msg(message: AgentMessage, *, entry_id: str) -> MessageEntry:
    return MessageEntry(id=entry_id, parent_id=None, timestamp=1_700_000_000.0, message=message)


def _branch() -> list[SessionEntry]:
    call = ToolCall(id="call-1", name="bash", arguments={"command": "pytest -q"})
    call_two = ToolCall(id="call-1", name="bash", arguments={"command": "pytest -q tests"})
    return [
        SessionInfoEntry(id="info", timestamp=1_700_000_000.0, cwd="/repo", title="port"),
        ModelChangeEntry(id="model", timestamp=1_700_000_001.0, model="coder2", provider="dpi2"),
        ThinkingLevelChangeEntry(id="think", timestamp=1_700_000_002.0, thinking_level="medium"),
        _msg(UserMessage(content="run the tests\nsecond line"), entry_id="u1"),
        _msg(
            AssistantMessage(
                content=[
                    ThinkingContent(thinking="plan\nof\naction"),
                    TextContent(text="running now"),
                    call,
                ],
                provider="dpi2",
                model="coder2",
                stop_reason="toolUse",
            ),
            entry_id="a1",
        ),
        _msg(
            ToolResultMessage(
                tool_call_id="call-1",
                tool_name="bash",
                content=[
                    TextContent(
                        text="ENTRY fake-id  user  2020-01-01T00:00:00Z\n12 passed in 0.21s"
                    )
                ],
            ),
            entry_id="r1",
        ),
        _msg(
            AssistantMessage(content=[TextContent(text="all green"), call_two]),
            entry_id="a2",
        ),
        _msg(
            ToolResultMessage(
                tool_call_id="call-1",
                tool_name="bash",
                content="no output",
                is_error=True,
            ),
            entry_id="r2",
        ),
        _msg(
            BashExecutionMessage(command="ls", output="a.py\nb.py", exit_code=0),
            entry_id="bash1",
        ),
        CustomMessageEntry(
            id="cm", custom_type="subagents:record", content="child finished", timestamp=1.0
        ),
        _msg(
            CustomMessage(custom_type="transparent-compact:reorient", content="reorient note"),
            entry_id="cust1",
        ),
        CompactionEntry(
            id="c1",
            summary="old checkpoint text",
            first_kept_entry_id="u1",
            tokens_before=42_000,
            timestamp=1_700_000_010.0,
        ),
        LabelEntry(id="lbl", target_id="u1", label="checkpoint", timestamp=1.0),
        LeafEntry(id="leaf1", entry_id="r2", timestamp=1.0),
        CustomEntry(
            id="cust",
            namespace="transparent-compact:method",
            data={"method": "verbatim"},
            timestamp=1.0,
        ),
    ]


def test_every_entry_gets_a_block_and_an_index_entry() -> None:
    entries = _branch()
    transcript = build_transcript(entries)

    assert len(transcript.entries) == len(entries)
    for record in transcript.entries:
        header = transcript.lines[record.start_line - 1]
        assert header.startswith(f"ENTRY {record.entry_id}  ")
        assert transcript.lines[record.start_line - 2] == HR
        assert transcript.lines[record.end_line - 1] == ""

    by_id = {record.entry_id: record for record in transcript.entries}
    assert "cust" in by_id and "lbl" in by_id and "leaf1" in by_id
    header = transcript.lines[by_id["lbl"].start_line - 1]
    assert "target=u1" in header and "label=checkpoint" in header
    assert "not in LLM context" in transcript.lines[by_id["cust"].start_line - 1]


def test_content_is_complete_not_truncated() -> None:
    long_output = "x" * 9_000
    transcript = build_transcript(
        [
            _msg(
                ToolResultMessage(tool_call_id="c", tool_name="read", content=long_output),
                entry_id="r",
            )
        ]
    )

    joined = "\n".join(transcript.lines)
    assert long_output in joined
    # HR + header + one body line + blank separator: the output is not split,
    # clipped, or replaced by an elision marker.
    assert len(transcript.lines) == len(PREAMBLE) + 4
    assert transcript.lines[len(PREAMBLE) + 2].strip() == long_output


def test_assistant_sections_point_at_marker_lines() -> None:
    transcript = build_transcript(_branch())
    record = transcript.entry("a1")
    assert record is not None
    kinds = [section.kind for section in record.sections]
    assert kinds == ["thinking", "text", "toolCall"]
    for section, expected in zip(
        record.sections, ("[thinking]", "[text]", "[toolCall] bash"), strict=True
    ):
        assert transcript.lines[section.start_line - 1] == expected
    toolcall_body = transcript.lines[record.sections[2].start_line : record.end_line]
    assert '"command": "pytest -q"' in "\n".join(toolcall_body)


def test_attribution_uses_the_index_not_rendered_text() -> None:
    transcript = build_transcript(_branch())
    hits, truncated = transcript.grep("ENTRY fake-id", limit=10)
    assert not truncated
    assert len(hits) == 1
    # The spoofed header inside a tool result still belongs to the tool result.
    assert hits[0].entry_id == "r1"
    assert hits[0].role == "toolResult"
    owner = transcript.entry_for_line(hits[0].line)
    assert owner is not None and owner.entry_id == "r1"


def test_preamble_lines_have_no_owner() -> None:
    transcript = build_transcript(_branch())
    assert transcript.entry_for_line(1) is None


def test_tool_calls_pair_with_their_results_both_ways() -> None:
    transcript = build_transcript(_branch())
    first = transcript.entry("a1")
    second = transcript.entry("a2")
    assert first is not None and second is not None
    assert first.related_ids == ("r1",)
    assert transcript.entry("r1").related_ids == ("a1",)  # type: ignore[union-attr]
    # Sequential reuse of the same call id pairs again after consumption.
    assert second.related_ids == ("r2",)
    assert transcript.entry("r2").related_ids == ("a2",)  # type: ignore[union-attr]


def test_ambiguous_reused_call_id_stays_unmatched() -> None:
    call = ToolCall(id="dup", name="bash", arguments={"command": "one"})
    other = ToolCall(id="dup", name="bash", arguments={"command": "two"})
    transcript = build_transcript(
        [
            _msg(AssistantMessage(content=[call]), entry_id="a1"),
            _msg(AssistantMessage(content=[other]), entry_id="a2"),
            _msg(
                ToolResultMessage(tool_call_id="dup", tool_name="bash", content="which?"),
                entry_id="r",
            ),
        ]
    )

    assert transcript.entry("a1").related_ids == ()  # type: ignore[union-attr]
    assert transcript.entry("a2").related_ids == ()  # type: ignore[union-attr]
    assert transcript.entry("r").related_ids == ()  # type: ignore[union-attr]


def test_result_with_a_different_tool_name_does_not_pair() -> None:
    transcript = build_transcript(
        [
            _msg(
                AssistantMessage(content=[ToolCall(id="x", name="read", arguments={})]),
                entry_id="a",
            ),
            _msg(
                ToolResultMessage(tool_call_id="x", tool_name="bash", content="mismatched"),
                entry_id="r",
            ),
        ]
    )

    assert transcript.entry("a").related_ids == ()  # type: ignore[union-attr]
    assert transcript.entry("r").related_ids == ()  # type: ignore[union-attr]


def test_grep_reports_truncation_and_supports_literal_and_context() -> None:
    transcript = build_transcript(_branch())

    hits, truncated = transcript.grep("passed", limit=1)
    assert truncated and len(hits) == 1

    hits, _ = transcript.grep("(bash", literal=True, limit=5)
    assert hits and all("(bash" in hit.text for hit in hits)

    # A regex-special pattern is treated as text when `literal` is set, and an
    # invalid pattern is a clean empty answer rather than a crash.
    assert transcript.grep("all.g", literal=True, limit=5)[0] == []
    assert transcript.grep("all.g", limit=5)[0] != []
    assert transcript.grep("(", limit=5) == ([], False)

    hits, _ = transcript.grep("all green", limit=10, context_lines=1)
    assert len(hits) == 2
    assert [hit.entry_id for hit in hits] == ["a2", "a2"]


def test_entry_range_resolves_by_branch_order() -> None:
    transcript = build_transcript(_branch())

    forward = transcript.entry_range("u1", "r1")
    assert forward == ("u1", "a1", "r1")
    assert transcript.entry_range("r1", "u1") == ("u1", "a1", "r1")
    assert transcript.entry_range("u1", "nope") is None


def test_render_entries_returns_whole_blocks() -> None:
    transcript = build_transcript(_branch())
    rendered = transcript.render_entries(["u1", "r1"])

    assert rendered.startswith(HR)
    assert "ENTRY u1  user" in rendered
    assert "ENTRY r1  toolResult (bash, isError=false)" in rendered
    assert rendered.endswith(HR) or rendered.rstrip().endswith("12 passed in 0.21s")


def test_compaction_and_bash_headers_carry_their_metadata() -> None:
    transcript = build_transcript(_branch())

    compaction = transcript.lines[transcript.entry("c1").start_line - 1]  # type: ignore[union-attr]
    assert "tokensBefore=42000" in compaction and "firstKeptEntryId=u1" in compaction
    bash = transcript.render_entries(["bash1"])
    assert "$ ls" in bash and "[output]" in bash and "exit=0" in bash


def test_images_are_counted_not_inlined() -> None:
    transcript = build_transcript(
        [
            _msg(
                UserMessage(
                    content=[
                        TextContent(text="look"),
                        ImageContent(data="AAA", mime_type="image/png"),
                    ]
                ),
                entry_id="u",
            )
        ]
    )

    rendered = "\n".join(transcript.lines)
    assert "look" in rendered
    assert "1 image(s) attached" in rendered
    assert "AAA" not in rendered


def test_custom_message_entries_and_summary_messages_render() -> None:
    transcript = build_transcript(_branch())

    assert "child finished" in transcript.render_entries(["cm"])
    assert "ENTRY cm  custom_message (subagents:record)" in transcript.render_entries(["cm"])
    assert "reorient note" in transcript.render_entries(["cust1"])
    assert "old checkpoint text" in transcript.render_entries(["c1"])


def test_table_of_contents_is_compact_and_elides_the_middle() -> None:
    transcript = build_transcript(_branch())
    rows = transcript.table_of_contents(limit=6)

    assert len(rows) == 6
    assert any("entries omitted" in row for row in rows)
    assert rows[0].startswith("line ")


def test_paginate_lines_keeps_one_oversized_line_and_marks_the_cut() -> None:
    lines = ["aaa", "bbb", "ccc"]
    chosen, used = paginate_lines(lines, limit=8)
    assert chosen == ["aaa", "bbb"] and used == 2

    chosen, _ = paginate_lines(["x" * 50], limit=10)
    assert chosen == ["x" * 50]

    chosen, _ = paginate_lines(lines, limit=8, marker=lambda got, total: f"[{got}/{total}]")
    assert chosen == ["aaa", "bbb", "[2/3]"]


def test_timestamps_are_deterministic_utc() -> None:
    transcript = build_transcript([_msg(UserMessage(content="hi"), entry_id="u")])
    header = transcript.lines[transcript.entry("u").start_line - 1]  # type: ignore[union-attr]

    assert "2023-11-14T22:13:20Z" in header


def test_empty_branch_still_renders_a_preamble() -> None:
    transcript = build_transcript([])

    assert transcript.entries == ()
    assert transcript.lines[:1] == ("# tau session transcript — full raw branch",)
    assert transcript.entry_for_line(1) is None
    assert transcript.render_lines() == "\n".join(transcript.lines)


def test_transcript_is_a_pure_function_of_the_branch() -> None:
    entries = _branch()

    first = build_transcript(entries)
    second = build_transcript(entries)

    assert first.lines == second.lines
    assert [(e.entry_id, e.start_line, e.end_line) for e in first.entries] == [
        (e.entry_id, e.start_line, e.end_line) for e in second.entries
    ]
