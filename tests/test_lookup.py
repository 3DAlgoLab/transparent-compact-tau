"""Tests for the bounded `context_lookup` subagent.

The provider is scripted, so these assert the harness rather than model
behaviour: that the loop stops at its budgets, that tool results actually flow
back, that the tool set is exactly the three recovery tools, and that a provider
that cannot be built surfaces as a tool error instead of a traceback.
"""

from tau_agent.messages import (
    AssistantMessage,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from tau_agent.provider_events import AssistantDoneEvent, AssistantStartEvent
from tau_agent.session.entries import CustomEntry, MessageEntry
from tau_ai import FakeProvider

from transparent_compact.lookup import lookup_tool, run_lookup
from transparent_compact.recovery import recovery_tools

_counter = 0


def _uid() -> str:
    global _counter
    _counter += 1
    return f"lookup-test-{_counter:04d}"


def _answer(text: str) -> list[object]:
    return [
        AssistantStartEvent(partial=AssistantMessage(model="fake")),
        AssistantDoneEvent(
            reason="stop", message=AssistantMessage(content=text, stop_reason="stop")
        ),
    ]


def _call(name: str, args: dict[str, object]) -> list[object]:
    call = ToolCall(id=f"call-{name}", name=name, arguments=args)
    message = AssistantMessage(model="fake", content=[call], stop_reason="toolUse")
    return [
        AssistantStartEvent(partial=AssistantMessage(model="fake")),
        AssistantDoneEvent(reason="toolUse", message=message),
    ]


def _entry(kind: str, message: object) -> MessageEntry | CustomEntry:
    if kind == "custom":
        return CustomEntry(
            id=_uid(),
            parent_id=None,
            timestamp=1_700_000_000,
            namespace="x",
            data={},
        )
    return MessageEntry(id=_uid(), parent_id=None, timestamp=1_700_000_000, message=message)


BRANCH = [
    _entry("message", UserMessage(content="Why did the build fail?")),
    _entry("message", AssistantMessage(content="Compiling.")),
    _entry(
        "message",
        ToolResultMessage(
            tool_name="bash",
            tool_call_id="c1",
            content="FAILED tests/test_x.py::test_y",
            is_error=True,
        ),
    ),
]


def _provider(scripted: list[list[object]]) -> FakeProvider:
    return FakeProvider(scripted)


def test_run_lookup_feeds_tool_results_back_and_returns_the_final_answer() -> None:
    import asyncio

    provider = _provider(
        [
            _call("context_grep", {"pattern": "FAILED"}),
            _answer(
                "The build failed in tests/test_x.py::test_y [entry cited from the raw branch]."
            ),
        ]
    )
    answer = asyncio.run(
        run_lookup(
            "Why did the build fail?",
            recovery_tools(lambda: list(BRANCH)),
            provider=provider,
            model="fake",
        )
    )

    assert "The build failed in tests/test_x.py::test_y" in answer
    # Two provider exchanges: one tool round, then the answer.
    assert len(provider.calls) == 2
    # The grep result reached the second request: the subagent really read the
    # raw branch, it did not just repeat the question.
    second_messages = provider.calls[1][2]
    results = [message for message in second_messages if isinstance(message, ToolResultMessage)]
    assert len(results) == 1
    assert "FAILED tests/test_x.py::test_y" in results[0].text


def test_the_subagent_sees_only_the_three_recovery_tools() -> None:
    import asyncio

    provider = _provider([_answer("Nothing to add.")])
    asyncio.run(
        run_lookup(
            "What happened?", recovery_tools(lambda: list(BRANCH)), provider=provider, model="fake"
        )
    )

    names = {tool.name for tool in provider.calls[0][3]}
    assert names == {"context_list_entries", "context_grep", "context_show_entry"}


def test_the_turn_budget_stops_a_model_that_keeps_calling_tools() -> None:
    import asyncio

    scripted = [_call("context_grep", {"pattern": "no-such-thing"}) for _ in range(20)]
    provider = _provider(scripted)
    answer = asyncio.run(
        run_lookup(
            "loop forever",
            recovery_tools(lambda: list(BRANCH)),
            provider=provider,
            model="fake",
            max_turns=3,
        )
    )

    assert len(provider.calls) == 3
    assert "turn budget (3) reached" in answer


def test_the_tool_call_budget_tells_the_agent_to_answer_now() -> None:
    import asyncio

    scripted = [_call("context_grep", {"pattern": "no-such-thing"}) for _ in range(10)]
    provider = _provider(scripted)
    asyncio.run(
        run_lookup(
            "spend the budget",
            recovery_tools(lambda: list(BRANCH)),
            provider=provider,
            model="fake",
            max_turns=10,
            max_tool_calls=2,
        )
    )

    # Every turn after the budget still gets a tool result, so the transcript
    # stays well-formed and the agent is told to answer.
    last_messages = provider.calls[-1][2]
    errors = [
        message
        for message in last_messages
        if isinstance(message, ToolResultMessage) and message.is_error
    ]
    assert any("Tool-call budget reached" in str(message.content) for message in errors)


def test_lookup_tool_reports_a_provider_it_cannot_build() -> None:
    import asyncio

    def _broken_factory(_context: object) -> tuple[object, str]:
        raise RuntimeError("no credentials configured")

    tool = lookup_tool(lambda: list(BRANCH), lambda: None, factory=_broken_factory)
    result = asyncio.run(tool.execute("t1", {"question": "what happened?"}))

    assert "Error:" in result.text
    assert "cannot start the lookup subagent" in result.text
    assert "no credentials configured" in result.text


def test_lookup_tool_requires_a_question() -> None:
    import asyncio

    tool = lookup_tool(lambda: list(BRANCH), lambda: None, factory=lambda _c: (object(), "fake"))
    result = asyncio.run(tool.execute("t1", {}))
    assert "`question` is required" in result.text
