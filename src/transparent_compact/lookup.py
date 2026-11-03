"""`context_lookup` — a bounded, read-only search agent over the raw branch.

The three deterministic tools answer "show me this entry". This one answers
"what did we decide about the rate limiter, and where is it written down", which
needs an agent to decide what to search for next. It is deliberately the only
model call this extension makes, and it is confined:

* tools: the three recovery tools only — no filesystem, no writes, and no
  `context_lookup` itself, so it cannot recurse;
* corpus: this session's raw branch, nothing else (never another session, never
  an on-disk transcript that may have been edited);
* budget: a turn cap and a tool-call cap, both reported in the answer when hit;
* storage: **no session file**. This is a provider request loop, not a child
  `CodingSession` — a child session would write its own history and pollute the
  session directory with a run the user never asked to keep.

The answer is prose with entry-id citations. It is the agent's *claim*; the ids
are what make it checkable, and the deterministic tools are how the main agent
verifies it.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence

from tau_agent.messages import (
    AgentMessage,
    AssistantMessage,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from tau_agent.provider import ModelProvider
from tau_agent.provider_events import AssistantDoneEvent, TextDeltaEvent, ToolCallEndEvent
from tau_agent.tools import AgentTool, AgentToolResult
from tau_agent.types import JSONValue
from tau_coding import load_provider_settings, resolve_provider_selection
from tau_coding.extensions import ExtensionContext
from tau_coding.provider_runtime import ClosableModelProvider, create_model_provider

from .recovery import ReadEntries, recovery_tools

MAX_LOOKUP_TURNS = 6
MAX_LOOKUP_TOOL_CALLS = 12

LOOKUP_SYSTEM_PROMPT = """\
You are a read-only lookup agent inside one Tau coding session. Your only job is
to find something in that session's own raw history and report it with evidence.

The raw branch is the complete, never-edited record of the conversation: every
user turn, assistant reply, thinking block, tool call, and full tool output. The
main agent may be working from a compacted checkpoint of it, so information you
find here may be information it can no longer see.

You have exactly three tools, and they read only this session's branch:
- context_list_entries — a table of contents (entry ids, roles, previews)
- context_grep — regex or literal search over the rendered branch, with entry ids
- context_show_entry — one entry, or an inclusive range, rendered in full

Rules:
- Cite entry ids for everything you report. The caller follows them up.
- Quote exact text when the wording matters (commands, paths, error messages).
- You have no filesystem, web, or project tools. If the question is about the
  current state of files, say that you cannot see it and name the historical
  evidence instead.
- Do not speculate. If the history does not contain the answer, say so plainly
  and say what you searched.
- Be brief. Findings with entry ids, not a narrative.
"""

ANSWER_INSTRUCTION = """\
Search the raw branch and answer the question below with entry-id citations.
"""

TURN_BUDGET_NOTE = "(lookup turn budget reached — this is the last partial answer)"


def settings_provider_factory(context: ExtensionContext) -> tuple[ClosableModelProvider, str]:
    """Build a provider for the session's current selection.

    Returns `(provider, model)`. The caller owns the provider and must close it;
    a lookup run is short-lived and must not keep credentials or connections
    alive after it answers.

    `MECH_COMPACT_LOOKUP_MODEL` overrides the model, which is how you keep the
    search cheap: the lookup reads a lot of history, and a cheap model with a
    big window does that better than an expensive one with a small one. The
    session's own model is the default because it already has credentials and a
    window that must fit this session's branch.
    """
    settings = load_provider_settings()
    override = os.environ.get("MECH_COMPACT_LOOKUP_MODEL", "").strip() or None
    selection = resolve_provider_selection(
        settings,
        provider_name=context.provider_name,
        model=override or context.model,
    )
    return create_model_provider(selection.provider, model=selection.model), selection.model


async def _stream_one_response(
    provider: ModelProvider,
    model: str,
    system: str,
    messages: list[AgentMessage],
    tools: Sequence[AgentTool],
) -> AssistantMessage:
    """Drive one provider stream into a single assistant message.

    `AssistantDoneEvent.message` is the provider's own assembled message, so it
    wins. The delta/call accumulation is the fallback for a stream that ends
    without one, and it keeps the transcript well-formed either way: a turn that
    requested a tool must produce a tool call, and every turn must be answered.
    """
    final: AssistantMessage | None = None
    text_parts: list[str] = []
    calls: list[ToolCall] = []
    async for event in provider.stream_response(
        model=model, system=system, messages=messages, tools=list(tools)
    ):
        if isinstance(event, TextDeltaEvent):
            text_parts.append(event.delta)
        elif isinstance(event, ToolCallEndEvent):
            calls.append(event.tool_call)
        elif isinstance(event, AssistantDoneEvent):
            final = event.message
    if final is not None:
        return final
    content: list[TextContent | ThinkingContent | ToolCall] = []
    if text_parts:
        content.append(TextContent(text="".join(text_parts)))
    content.extend(calls)
    return AssistantMessage(content=content, stop_reason="toolUse" if calls else "stop")


async def run_lookup(
    question: str,
    tools: Sequence[AgentTool],
    *,
    provider: ModelProvider,
    model: str,
    max_turns: int = MAX_LOOKUP_TURNS,
    max_tool_calls: int = MAX_LOOKUP_TOOL_CALLS,
) -> str:
    """Run the bounded lookup loop and return the agent's final text.

    Tool execution is local and deterministic; only the *choice* of what to
    search next comes from the model. When either budget runs out the answer says
    so rather than presenting a partial search as a complete one.
    """
    schemas = [tool for tool in tools]
    messages: list[AgentMessage] = [UserMessage(content=f"{ANSWER_INSTRUCTION}\n{question}")]
    calls_used = 0
    notes: list[str] = []

    for _turn in range(max_turns):
        assistant = await _stream_one_response(
            provider, model, LOOKUP_SYSTEM_PROMPT, messages, schemas
        )
        messages.append(assistant)
        if not assistant.tool_calls:
            return assistant.text or "(the lookup agent returned no text)"
        for call in assistant.tool_calls:
            if calls_used >= max_tool_calls:
                result = AgentToolResult(
                    content=[
                        TextContent(
                            text="Tool-call budget reached. Answer now with what you have, "
                            "citing the entry ids you have already seen."
                        )
                    ]
                )
                forced_error = True
                notes.append(f"tool-call budget ({max_tool_calls}) reached")
            else:
                tool = next(
                    (candidate for candidate in schemas if candidate.name == call.name), None
                )
                if tool is None:
                    result = AgentToolResult(
                        content=[TextContent(text=f"Unknown tool {call.name!r}.")]
                    )
                    forced_error = True
                else:
                    result = await tool.execute(call.id, call.arguments)
                    forced_error = False
                    calls_used += 1
            messages.append(
                ToolResultMessage(
                    tool_call_id=call.id,
                    tool_name=call.name,
                    content=list(result.content),
                    details=result.details,
                    is_error=forced_error,
                )
            )

    notes.append(f"turn budget ({max_turns}) reached")
    tail = next(
        (message.text for message in reversed(messages) if isinstance(message, AssistantMessage)),
        "",
    )
    suffix = f"\n\n({' ; '.join(notes)})" if notes else ""
    return f"{tail or '(no final answer)'}{suffix}"


def lookup_tool(
    read_entries: ReadEntries,
    get_context: Callable[[], ExtensionContext],
    *,
    factory: Callable[[ExtensionContext], tuple[ModelProvider, str]] = settings_provider_factory,
) -> AgentTool:
    """Registerable `context_lookup`, bound to the live session's branch."""

    async def _execute(
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal: object = None,
        on_update: object = None,
    ) -> AgentToolResult:
        del tool_call_id, signal, on_update
        question = str(arguments.get("question") or "").strip()
        if not question:
            return _fail("`question` is required.")
        context = get_context()
        try:
            provider, model = factory(context)
        except Exception as error:
            return _fail(f"cannot start the lookup subagent: {type(error).__name__}: {error}")
        try:
            answer = await run_lookup(
                question,
                recovery_tools(read_entries),
                provider=provider,
                model=model,
            )
        except Exception as error:
            return _fail(f"lookup failed: {type(error).__name__}: {error}")
        finally:
            aclose = getattr(provider, "aclose", None)
            if callable(aclose):
                await aclose()
        return AgentToolResult(content=[TextContent(text=answer)])

    return AgentTool(
        name="context_lookup",
        label="Lookup session history",
        description=(
            "Answer a question about THIS Tau session's own raw history by running a "
            "bounded read-only subagent over it. It costs one model exchange and sees "
            "only this session's branch through context_list_entries, context_grep, and "
            "context_show_entry, so it can search iteratively where a single grep cannot. "
            "Use it for questions like 'what did we change and why', 'where is X handled', "
            "or 'what did the failing test output' after compaction hid the details. Its "
            "answer cites raw entry ids — follow them with context_show_entry before "
            "repeating anything as fact. Not for the filesystem, the web, or other sessions "
            "(for another session, run its file through `tau export` and use your own tools)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question to answer from this session's raw history.",
                }
            },
            "required": ["question"],
        },
        execute_fn=_execute,
        execution_mode="sequential",
    )


def _fail(message: str) -> AgentToolResult:
    """Report a lookup failure as text rather than as an exception.

    Tau flags a tool result as an error only when the tool raises. Raising would
    tell the main agent "the tool crashed"; what it needs to know is that the
    subagent could not run, so the deterministic tools are the way forward. The
    text says which, and the extension never retries on its own.
    """
    return AgentToolResult(content=[TextContent(text=f"Error: {message}")])
