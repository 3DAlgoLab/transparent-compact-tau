"""Mechanical compression, the size cap, and span tiling (write path)."""

from collections.abc import Sequence

from tau_agent.messages import (
    AssistantMessage,
    BashExecutionMessage,
    BranchSummaryMessage,
    CustomMessage,
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
    MessageEntry,
    SessionEntry,
)

from transparent_compact.cap import (
    TOOL_PREFIX,
    CommandPreview,
    SpanContent,
    ToolLine,
    ToolStub,
    cap_spans,
    command_preview,
    render_tool_line,
)
from transparent_compact.checkpoint import (
    StoredSpan,
    build_checkpoint,
    conversation_size,
    file_operations,
    previous_file_lists,
    summarize_entries,
)
from transparent_compact.signature import (
    bash_output_label,
    checkpoint_command,
    checkpoint_path,
    command_hints,
    observation_quote,
    tool_signature,
)
from transparent_compact.spans import (
    MECH_KIND,
    collect_base_and_spans,
    mech_details,
    new_span_entries,
    parse_span,
    span_to_json,
    turn_owner_ids,
)

CWD = "/repo"


def user(text: str, *, entry_id: str) -> MessageEntry:
    return MessageEntry(id=entry_id, timestamp=1_700_000_000.0, message=UserMessage(content=text))


def assistant(blocks: list[object], *, entry_id: str) -> MessageEntry:
    return MessageEntry(
        id=entry_id,
        timestamp=1_700_000_001.0,
        message=AssistantMessage(content=blocks),  # type: ignore[arg-type]
    )


def result_msg(
    *,
    call_id: str,
    tool: str,
    text: str,
    is_error: bool = False,
    details: object = None,
) -> ToolResultMessage:
    return ToolResultMessage(
        tool_call_id=call_id,
        tool_name=tool,
        content=[TextContent(text=text)],
        is_error=is_error,
        details=details,
    )


def result(
    *,
    entry_id: str,
    call_id: str,
    tool: str,
    text: str,
    is_error: bool = False,
    details: object = None,
) -> MessageEntry:
    return MessageEntry(
        id=entry_id,
        timestamp=1_700_000_002.0,
        message=result_msg(
            call_id=call_id,
            tool=tool,
            text=text,
            is_error=is_error,
            details=details,
        ),
    )


def call(call_id: str, name: str, arguments: dict[str, object]) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


def owners_for(entries: Sequence[SessionEntry]) -> dict[str, str | None]:
    return turn_owner_ids(list(entries))


# ---------------------------------------------------------------------------
# tool signatures (display notation, never replayable)
# ---------------------------------------------------------------------------


def test_read_signature_prints_the_requested_range_and_relative_path() -> None:
    stub = tool_signature("read", {"path": "/repo/src/x.py", "offset": 12, "limit": 30}, CWD)
    assert stub.prefix == 'read "src/x.py":12-41'
    assert stub.command is None
    assert stub.suffix == ""

    head_only = tool_signature("read", {"path": "/repo/src/x.py", "limit": 5}, CWD)
    assert head_only.prefix == 'read "src/x.py":1-5'

    outside = tool_signature("read", {"path": "/elsewhere/file.py"}, CWD)
    assert outside.prefix == 'read "/elsewhere/file.py"'


def test_write_and_edit_payloads_collapse_to_sizes_and_counts() -> None:
    write = tool_signature("write", {"path": "/repo/s.py", "content": "x" * 2144}, CWD)
    assert write.prefix == 'write "s.py" (2144 chars)'

    edit = tool_signature(
        "edit",
        {
            "path": "/repo/a.ts",
            "edits": [{"oldText": "a", "newText": "b"}, {"oldText": "c", "newText": "d"}],
        },
        CWD,
    )
    assert edit.prefix == 'edit "a.ts" ×2'

    single = tool_signature(
        "edit", {"path": "/repo/a.ts", "oldText": "a" * 40, "newText": "b"}, CWD
    )
    assert "oldText=<40 chars>" in single.prefix and "newText=<1 chars>" in single.prefix


def test_bash_signature_keeps_an_encoded_command_prefix_not_the_whole_command() -> None:
    long_command = "echo " + "y" * 400
    stub = tool_signature(
        "bash", {"command": f"cd {CWD} && {long_command}", "timeout": 30, "description": "x"}, CWD
    )
    assert stub.prefix == "bash "
    assert stub.command is not None and stub.command.truncated is True
    assert len(stub.command.head) <= 160
    assert "timeout=" not in stub.suffix
    assert 'description="x"' in stub.suffix


def test_other_tools_render_named_argument_snippets() -> None:
    stub = tool_signature("web_search", {"query": "tau ext", "count": 5}, CWD)
    assert stub.prefix == 'web_search(query="tau ext", count=5)'


def test_checkpoint_command_replaces_only_complete_path_prefixes() -> None:
    assert checkpoint_command("uv run pytest /repo/tests", CWD) == "uv run pytest ./tests"
    assert checkpoint_command("ls /repo-other", CWD) == "ls /repo-other"
    assert checkpoint_command("cat http://repo/repo/x", CWD) == "cat http://repo/repo/x"
    assert checkpoint_path("/repo/a/b.py", CWD) == "a/b.py"
    assert checkpoint_path(CWD, CWD) == "."


# ---------------------------------------------------------------------------
# bash output labels (the only inference, deliberately paranoid)
# ---------------------------------------------------------------------------


def test_exit_status_is_reported_from_metadata_not_from_text() -> None:
    failed = result_msg(
        call_id="c",
        tool="bash",
        text="1 failed in 0.2s\n\nCommand exited with code 1",
        is_error=True,
        details={"exit_code": 1},
    )
    assert bash_output_label("pytest -q", failed) == "exit 1; output: '1 failed in 0.2s'"

    mismatched = result_msg(
        call_id="c2",
        tool="bash",
        text="Command exited with code 1",
        details={"exit_code": 0},
    )
    # Metadata contradicts the footer, so no status may be inferred.
    assert bash_output_label("pytest -q", mismatched) == ""


def test_empty_output_and_test_summaries_are_quoted_but_no_verdict_is_implied() -> None:
    empty = result_msg(call_id="c", tool="bash", text="", details={"exit_code": 0})
    assert bash_output_label("true", empty) == "no output"

    passing = result_msg(
        call_id="c2",
        tool="bash",
        text="................\n19 passed in 0.16s",
        details={"exit_code": 0},
    )
    assert bash_output_label("pytest -q", passing) == "output: '19 passed in 0.16s'"

    unrecognised = result_msg(
        call_id="c3", tool="bash", text="made up output", details={"exit_code": 0}
    )
    # `made up output` is not an execution command, so nothing is inferred at all.
    assert bash_output_label("./weird-tool run", unrecognised) == ""


def test_file_viewing_commands_never_produce_error_or_test_labels() -> None:
    fixture = result_msg(
        call_id="c",
        tool="bash",
        text="tests/fixtures/err.txt:ValueError: this text is only a fixture",
        details={"exit_code": 0},
    )
    assert bash_output_label("cat tests/fixtures/err.txt", fixture) == ""
    # A test command whose output holds no recognised summary claims nothing at all.
    assert bash_output_label("uv run pytest -q", fixture) == ""


def test_truncated_output_is_never_called_a_complete_observation() -> None:
    truncated = result_msg(
        call_id="c",
        tool="bash",
        text="2 failed\n[Showing lines 400-500 of 900]",
        details={"exit_code": 0, "truncation": {"truncated": True}},
    )
    assert bash_output_label("pytest -q", truncated) == ""


def test_http_status_is_extracted_only_for_fetch_commands() -> None:
    body = "HTTP/1.1 503 Service Unavailable"
    fetched = result_msg(call_id="c", tool="bash", text=body, details={"exit_code": 0})
    assert bash_output_label("curl -s https://example.test", fetched) == "output: http 503"
    assert bash_output_label("cat server.log", fetched) == ""


def test_git_commit_hash_is_taken_only_from_git_own_bracketed_form() -> None:
    commit = result_msg(
        call_id="c",
        tool="bash",
        text="[main 9c4c99b] Let extensions own the compaction summary",
        details={"exit_code": 0},
    )
    assert bash_output_label("git commit -m msg", commit) == "commit 9c4c99b"
    lookalike = result_msg(
        call_id="c2", tool="bash", text="abc1234567 mentioned", details={"exit_code": 0}
    )
    assert bash_output_label("git log", lookalike) == ""


def test_command_hints_strip_a_leading_cd_and_classify_conservatively() -> None:
    assert command_hints("cd /repo && pytest -q")["tests"] is True
    assert command_hints("grep -r Error src")["historical"] is False
    assert command_hints("python -m pytest")["historical"] is True
    assert command_hints("curl -s https://x")["network"] is True


def test_observation_quote_stays_closed_and_escaped() -> None:
    assert observation_quote('he said "hi"\nbye') == "'he said \"hi\"\\nbye'"
    long_text = "z" * 400
    quoted = observation_quote(long_text)
    assert quoted.startswith("'") and quoted.endswith("…'") and len(quoted) <= 160


# ---------------------------------------------------------------------------
# the size ladder
# ---------------------------------------------------------------------------


def _tool_line_span(
    *calls: ToolStub, mergeable: bool = True, prefix_lines: tuple[str, ...] = ()
) -> SpanContent:
    line = render_tool_line(list(calls))
    lines = (*prefix_lines, line)
    return SpanContent(
        lines=lines, tools=(ToolLine(index=len(lines) - 1, calls=calls, mergeable=mergeable),)
    )


def test_ladder_shortens_previews_from_the_canonical_prefix_never_from_a_cut_view() -> None:
    stub = tool_signature("bash", {"command": "echo " + "y" * 400}, CWD)
    span = _tool_line_span(stub)
    canonical = span.lines[0]

    uncapped = cap_spans([span])
    at_150 = cap_spans([span], 150)
    at_110 = cap_spans([span], 110)

    assert uncapped.lines[0] == (canonical,)
    assert (uncapped.tightened[0], uncapped.trimmed[0]) == (False, False)
    assert (at_150.tightened[0], at_150.trimmed[0]) == (True, False)
    assert (at_110.tightened[0], at_110.trimmed[0]) == (True, False)

    middle, narrow = at_150.lines[0][0], at_110.lines[0][0]
    assert len(narrow) < len(middle) < len(canonical)
    # Every deeper cut is a prefix of the canonical preview, never a re-cut of an
    # already shortened view.
    assert canonical.startswith(middle.rstrip("…"))
    assert middle.startswith(narrow.rstrip("…"))


def test_prefix_folding_only_joins_adjacent_tool_only_lines() -> None:
    first = ToolStub(prefix='read "a"')
    second = ToolStub(prefix='read "b"')
    span = SpanContent(
        lines=(render_tool_line([first]), "[Assistant]: explaining", render_tool_line([second])),
        tools=(
            ToolLine(index=0, calls=(first,), mergeable=True),
            ToolLine(index=2, calls=(second,), mergeable=True),
        ),
    )

    capped = cap_spans([span], 30)

    # Rung 3 cannot fold across the prose line; rung 4 then drops the tool lines
    # and stops before the retained prose is ever at risk.
    assert capped.trimmed[0] is True
    assert capped.lines[0] == ("[Assistant]: explaining",)


def test_folding_merges_only_mergeable_adjacent_records() -> None:
    first = ToolStub(prefix='read "a"')
    second = ToolStub(prefix='read "b"')
    span = SpanContent(
        lines=(render_tool_line([first]), render_tool_line([second])),
        tools=(
            ToolLine(index=0, calls=(first,), mergeable=True),
            ToolLine(index=1, calls=(second,), mergeable=True),
        ),
    )

    total = sum(len(line) + 1 for line in span.lines)
    folded = cap_spans([span], total - (len(TOOL_PREFIX) - 1))

    assert (folded.tightened[0], folded.trimmed[0]) == (True, False)
    assert folded.lines[0] == ('[Tools]: read "a"; read "b"',)


def test_user_lines_survive_call_removal_then_get_stubbed_once() -> None:
    long_user = "[User]: " + "u" * 400
    calls = (
        ToolStub(prefix="bash ", command=command_preview("pytest " + "-q " * 200)),
        ToolStub(prefix='read "f"'),
    )
    span = SpanContent(
        lines=(long_user, render_tool_line(list(calls)), "[Assistant]: prose"),
        tools=(ToolLine(index=1, calls=calls, mergeable=False),),
    )

    capped = cap_spans([span], 300)

    # The preview rungs shorten the tool line first, but that row is then removed
    # whole, so the span's only surviving damage is the user-line stub: content
    # loss is reported as trimmed, never as a bare tightening.
    assert (capped.tightened[0], capped.trimmed[0]) == (False, True)
    survivors = [line for line in capped.lines[0] if line.startswith("[User]:")]
    assert survivors and "…[truncated +288 chars — use context_lookup]" in survivors[0]
    assert len(survivors[0]) < len(long_user)
    assert span.lines[0] == long_user  # the canonical line is never rewritten
    assert "[Tools]:" not in "\n".join(capped.lines[0])  # the loss is the tool line


def test_a_record_with_an_overlong_preview_head_is_not_trusted() -> None:
    # A preview head longer than the canonical display means the record cannot
    # reproduce the stored line, so the ladder may only omit the line — never
    # rewrite it from an untrustworthy record.
    fake = ToolStub(prefix="bash ", command=CommandPreview(head="z" * 400))
    span = SpanContent(
        lines=(render_tool_line([fake]),),
        tools=(ToolLine(index=0, calls=(fake,), mergeable=False),),
    )

    capped = cap_spans([span], 100)

    assert capped.lines[0] == ()
    assert capped.trimmed[0] is True
    assert span.lines[0] == render_tool_line([fake])


def test_stored_records_are_used_only_when_they_round_trip_exactly() -> None:
    mismatched = ToolStub(prefix="bash ", command=CommandPreview(head='"old text"'))
    span = SpanContent(
        lines=('[Tools]: read "something else"',),
        tools=(ToolLine(index=0, calls=(mismatched,), mergeable=True),),
    )

    capped = cap_spans([span], 1)

    # The record disagrees with the line, so the line stays opaque and can only
    # be dropped whole — never regex-repaired.
    assert capped.lines[0] == ()
    assert capped.trimmed[0] is True


def test_no_budget_means_no_damage_at_all() -> None:
    span = _tool_line_span(ToolStub(prefix='bash "short command"'))
    capped = cap_spans([span])
    assert capped.lines[0] == span.lines
    assert (capped.tightened[0], capped.trimmed[0]) == (False, False)


def test_oldest_span_is_cut_first_across_spans() -> None:
    first = SpanContent(lines=("[User]: old question", '[Tools]: read "a"'))
    second = SpanContent(lines=("[User]: new question",))

    capped = cap_spans([first, second], 40)

    assert (capped.trimmed[0], capped.trimmed[1]) == (True, False)
    assert capped.lines[1] == ("[User]: new question",)


# ---------------------------------------------------------------------------
# turn identity and span tiling
# ---------------------------------------------------------------------------


def test_turn_owners_follow_user_prompts_and_end_at_summary_boundaries() -> None:
    entries: list[SessionEntry] = [
        user("first", entry_id="u1"),
        assistant([call("c", "read", {})], entry_id="a1"),
        result(entry_id="r1", call_id="c", tool="read", text="x"),
        user("second", entry_id="u2"),
        CustomMessageEntry(id="cm", custom_type="subagents:record", content="note", timestamp=1.0),
        MessageEntry(
            id="bs",
            timestamp=1.0,
            message=BranchSummaryMessage(summary="side branch", from_id="u1"),
        ),
        assistant([TextContent(text="after")], entry_id="a2"),
    ]

    owners = owners_for(entries)
    assert owners["u1"] == "u1" and owners["a1"] == "u1" and owners["r1"] == "u1"
    assert owners["u2"] == "u2" and owners["cm"] == "u2"
    assert owners["bs"] is None and owners["a2"] is None


def test_spans_are_stored_per_compaction_and_re_rendered_not_recopied() -> None:
    first = CompactionEntry(
        id="k1",
        summary="checkpoint one",
        first_kept_entry_id="u2",
        details={
            "kind": MECH_KIND,
            "span": {
                "at": "T1",
                "reason": "threshold",
                "lines": ["[User]: old"],
                "turnIds": ["u1"],
            },
        },
    )
    second = CompactionEntry(
        id="k2",
        summary="checkpoint two",
        first_kept_entry_id="u5",
        details={
            "kind": MECH_KIND,
            "span": {"at": "T2", "reason": "manual", "lines": ["[User]: newer"], "turnIds": ["u3"]},
        },
    )

    base, spans = collect_base_and_spans([first, second])

    assert base is None
    assert [span.at for span in spans] == ["T1", "T2"]
    assert [span.lines for span in spans] == [(("[User]: old"),), (("[User]: newer"),)]


def test_a_model_summary_becomes_an_opaque_base_and_hides_older_spans() -> None:
    older = CompactionEntry(
        id="k1",
        summary="mechanical one",
        details={
            "kind": MECH_KIND,
            "span": {"at": "T1", "reason": "threshold", "lines": ["[User]: old"]},
        },
    )
    model = CompactionEntry(id="k2", summary="model wrote this", first_kept_entry_id="u9")
    newer = CompactionEntry(
        id="k3",
        summary="mechanical two",
        details={
            "kind": MECH_KIND,
            "span": {"at": "T3", "reason": "threshold", "lines": ["[User]: new"]},
        },
    )

    base, spans = collect_base_and_spans([older, model, newer])

    assert base is not None and base.kind == "model-summary" and base.summary == "model wrote this"
    assert [span.at for span in spans] == ["T3"]


def test_a_malformed_record_is_treated_as_opaque_instof_reparsed() -> None:
    broken = CompactionEntry(
        id="k1",
        summary="checkpoint",
        details={"kind": MECH_KIND, "span": {"lines": "not a list"}},
    )

    base, spans = collect_base_and_spans([broken])

    assert spans == []
    assert base is not None and base.kind == "legacy-checkpoint"


def test_span_records_round_trip_through_json_shapes() -> None:
    span = StoredSpan(
        at="T",
        reason="threshold",
        lines=("[User]: q", '[Tools]: read "a"'),
        turn_ids=("u1", "u1"),
        tools=(ToolLine(index=1, calls=(ToolStub(prefix='read "a"'),), mergeable=True),),
        from_ts="F",
        to_ts="T2",
        tokens_before=1234,
    )

    payload = span_to_json(span)
    restored = parse_span(payload)

    assert restored == span
    record = {"kind": MECH_KIND, **payload}
    assert mech_details(CompactionEntry(id="k", summary="s", details=record)) == record


def test_new_span_entries_tile_after_the_base_and_skip_summary_rows() -> None:
    branch: list[SessionEntry] = [
        user("u1", entry_id="u1"),
        CompactionEntry(id="k1", summary="old checkpoint", first_kept_entry_id="u2"),
        user("u2", entry_id="u2"),
        MessageEntry(
            id="cs", timestamp=1.0, message=BranchSummaryMessage(summary="folded", from_id="u2")
        ),
        CustomMessageEntry(id="cm", custom_type="x", content="kept as stub", timestamp=1.0),
        user("u3", entry_id="u3"),
        user("kept", entry_id="u4"),
    ]

    selected = new_span_entries(
        branch,
        replaced_entry_ids=("k1", "u2", "cs", "cm", "u3"),
        base_entry_id="k1",
    )

    assert [entry.id for entry in selected] == ["u2", "cm", "u3"]


# ---------------------------------------------------------------------------
# summarize_entries
# ---------------------------------------------------------------------------


def test_prose_survives_verbatim_while_thinking_and_output_do_not() -> None:
    entries = [
        user("run the tests, please", entry_id="u1"),
        assistant(
            [
                ThinkingContent(thinking="secret deliberation"),
                TextContent(text="running now"),
                call("c1", "bash", {"command": "cd /repo && pytest -q"}),
            ],
            entry_id="a1",
        ),
        result(
            entry_id="r1",
            call_id="c1",
            tool="bash",
            text="19 passed in 0.16s",
            details={"exit_code": 0},
        ),
    ]

    span = summarize_entries(entries, cwd=CWD, owners=owners_for(entries))

    assert span.lines == (
        "[User]: run the tests, please",
        "[Assistant]: running now",
        "[Tools]: bash \"pytest -q\" → output: '19 passed in 0.16s'",
    )
    assert span.turn_ids == ("u1", "u1", "u1")
    assert "secret deliberation" not in "\n".join(span.lines)
    assert "19 passed" not in span.lines[2].split("→")[0]
    assert span.tools[0].calls[0].suffix == " → output: '19 passed in 0.16s'"


def test_missing_and_erroring_results_are_labelled_honestly() -> None:
    entries = [
        user("q", entry_id="u1"),
        assistant(
            [call("c1", "read", {"path": "/repo/a"}), call("c2", "read", {"path": "/repo/b"})],
            entry_id="a1",
        ),
        result(
            entry_id="r1",
            call_id="c1",
            tool="read",
            text="boom",
            is_error=True,
            details={"exit_code": None},
        ),
    ]

    span = summarize_entries(entries, cwd=CWD, owners=owners_for(entries))

    assert "[tool error]" in span.lines[1] and "[unknown]" in span.lines[1]
    assert span.lines[1].count("[Tools]: ") == 1


def test_thinking_only_and_bash_execution_lines_are_stubs() -> None:
    entries = [
        user("q", entry_id="u1"),
        assistant([ThinkingContent(thinking="hmm")], entry_id="a1"),
        MessageEntry(
            id="b1",
            timestamp=1.0,
            message=BashExecutionMessage(command="ls /repo", output="a\nb", exit_code=0),
        ),
        CustomMessageEntry(
            id="cm", custom_type="subagents:record", content="long payload", timestamp=1.0
        ),
        MessageEntry(
            id="c1",
            timestamp=1.0,
            message=CustomMessage(
                custom_type="transparent-compact:reorient", content="old checklist"
            ),
        ),
    ]

    span = summarize_entries(
        entries,
        cwd=CWD,
        owners=owners_for(entries),
        skipped_custom_types=("transparent-compact:reorient",),
    )

    assert span.lines[0] == "[User]: q"
    assert '[BashExecution]: bash "ls ." [output removed]' == span.lines[1]
    assert span.lines[2].startswith("[Custom message]: custom(type=subagents:record)")
    assert "long payload" not in span.lines[2]
    assert not any("old checklist" in line for line in span.lines)
    assert all(owner == "u1" for owner in span.turn_ids)


# ---------------------------------------------------------------------------
# the envelope and tiling across compactions
# ---------------------------------------------------------------------------


def _branch(*extra: SessionEntry) -> list[SessionEntry]:
    return [user("u1", entry_id="u1"), *extra]


def test_checkpoint_envelope_keeps_a_fixed_section_order() -> None:
    branch = _branch()
    span = summarize_entries([user("old question", entry_id="u0")], cwd=CWD, owners={"u0": "u0"})
    stored = span.span_content(at="2026-11-03T00:00:00Z", reason="threshold")

    result_one = build_checkpoint(
        reason="threshold",
        tokens_before=41_000,
        now="2026-11-03T00:00:00Z",
        branch=branch,
        base=None,
        spans=[],
        new_span=stored,
        first_kept_entry_id="u1",
        read_files=["src/x.py"],
        modified_files=["src/y.py"],
    )

    summary = result_one.summary
    order = [
        summary.index("## Verbatim compaction checkpoint (compaction 1 on this branch)"),
        summary.index("**Compacted conversation follows.**"),
        summary.index("**Recovering hidden details.**"),
        summary.index("### Compacted conversation (oldest first)"),
        summary.index('<compacted-span n="1"'),
        summary.index("<read-files"),
        summary.index("<modified-files"),
        summary.index("**Evidence boundary:**"),
        summary.index("continues verbatim below"),
    ]
    assert order == sorted(order)
    assert summary.rstrip().endswith("continues verbatim below — it was not compacted.")
    assert "41,000 tokens before compaction" in summary
    assert result_one.details["kind"] == MECH_KIND
    assert result_one.details["span"]["lines"] == list(span.lines)
    assert result_one.capped is None  # cap off keeps the view identical to canon


def test_second_checkpoint_re_renders_earlier_spans_instead_of_summarizing_them() -> None:
    first_span = summarize_entries(
        [user("first question", entry_id="u1")], cwd=CWD, owners={"u1": "u1"}
    ).span_content(at="T1", reason="threshold")
    first_entry = CompactionEntry(
        id="k1",
        summary="rendered once",
        first_kept_entry_id="u2",
        details={
            "kind": MECH_KIND,
            "span": span_to_json(first_span),
            "readFiles": ["a.py"],
            "modifiedFiles": [],
        },
    )
    branch = [first_entry, user("u2", entry_id="u2"), user("u3", entry_id="u3")]
    _, prior_spans = collect_base_and_spans(branch)
    new_span = summarize_entries(
        [user("second question", entry_id="u2")], cwd=CWD, owners={"u2": "u2"}
    ).span_content(at="T2", reason="manual")

    second = build_checkpoint(
        reason="manual",
        tokens_before=52_000,
        now="T2",
        branch=branch,
        base=None,
        spans=prior_spans,
        new_span=new_span,
        first_kept_entry_id="u3",
        read_files=[],
        modified_files=[],
    )

    summary = second.summary
    assert '<compacted-span n="1" compacted-at="T1" trigger="threshold">' in summary
    assert '<compacted-span n="2" compacted-at="T2" trigger="manual">' in summary
    assert "[User]: first question" in summary  # the same original characters
    assert "[User]: second question" in summary
    assert "rendered once" not in summary  # never re-summarized from a rendered view
    assert second.details["span"]["lines"] == ["[User]: second question"]
    assert "(compaction 2 on this branch)" in summary


def test_a_model_base_is_kept_opaque_and_flagged_as_mixed_context() -> None:
    branch = [CompactionEntry(id="k1", summary="model summary text", first_kept_entry_id="u1")]
    _, spans = collect_base_and_spans(branch)
    base, _ = collect_base_and_spans(branch)
    new_span = summarize_entries(
        [user("q", entry_id="u0")], cwd=CWD, owners={"u0": "u0"}
    ).span_content(at="T", reason="manual")

    result_two = build_checkpoint(
        reason="manual",
        tokens_before=10,
        now="T",
        branch=branch,
        base=base,
        spans=spans,
        new_span=new_span,
        first_kept_entry_id="u1",
        read_files=[],
        modified_files=[],
    )

    assert '<compacted-base kind="model-summary"' in result_two.summary
    assert "Mixed context" in result_two.summary
    assert "model summary text" in result_two.summary


def test_cap_trims_the_conversation_section_and_says_so() -> None:
    branch = _branch()
    long_span = summarize_entries(
        [
            user("question " + "w" * 4000, entry_id="u1"),
            assistant([TextContent(text="answer " + "e" * 4000)], entry_id="a1"),
        ],
        cwd=CWD,
        owners={"u1": "u1", "a1": "u1"},
    ).span_content(at="T", reason="threshold")

    overhead, line_chars = conversation_size(
        None, [long_span], first_kept_entry_id="u1", branch=branch
    )
    capped = build_checkpoint(
        reason="threshold",
        tokens_before=10,
        now="T",
        branch=branch,
        base=None,
        spans=[],
        new_span=long_span,
        first_kept_entry_id="u1",
        read_files=[],
        modified_files=[],
        cap_limit_chars=overhead + 200,
    )

    assert capped.capped is not None and capped.capped.any_trimmed
    assert (
        'trimmed="true"' in capped.summary
        and "(trimmed for size — use context_lookup)" in capped.summary
    )
    assert capped.details["span"]["lines"] == list(long_span.lines)  # canon stays uncapped
    assert line_chars > 200


def test_file_lists_carry_forward_from_the_previous_record() -> None:
    entries = [
        assistant(
            [
                call("c1", "read", {"path": "/repo/a.py"}),
                call("c2", "edit", {"path": "/repo/b.py", "edits": []}),
            ],
            entry_id="a1",
        ),
        result(entry_id="r1", call_id="c1", tool="read", text="x"),
    ]
    carried = (["carried.py"], ["previously.py"])

    reads, modified = file_operations(entries, previous=carried, cwd=CWD)

    assert reads == ["a.py", "carried.py"]
    assert modified == ["b.py", "previously.py"]

    stored = CompactionEntry(
        id="k", summary="s", details={"kind": MECH_KIND, "readFiles": ["z.py"], "modifiedFiles": []}
    )
    assert previous_file_lists([stored]) == (["z.py"], [])
    assert previous_file_lists([CustomEntry(id="c", namespace="x", timestamp=1.0)]) is None
