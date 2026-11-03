"""End-to-end tests: the extension inside a real Tau session.

These are the tests that decide whether the port is real. Each one drives
`CodingSession` with the actual extension file loaded, a scripted provider, and
a JSONL session on disk, then asks the questions the design cares about:

* did compaction produce a deterministic checkpoint **without a model call**?
* is the raw branch still complete, and do the recovery tools read it?
* does a second checkpoint re-render the first span instead of copying it?
* does branch state (method, cap) survive a reload?
* does Tau's own summarizer still work when the extension is told to stand down?
"""

import asyncio
import time
from pathlib import Path

import pytest
from tau_agent import AssistantMessage
from tau_agent.messages import ToolCall
from tau_agent.provider_events import AssistantDoneEvent, AssistantStartEvent, ToolCallEndEvent
from tau_agent.session import CompactionEntry, CustomEntry, JsonlSessionStorage
from tau_ai import FakeProvider
from tau_coding import SUMMARIZATION_SYSTEM_PROMPT, CodingSession, CodingSessionConfig
from tau_coding import session as coding_session_module

from transparent_compact.spans import CAP_NAMESPACE, MECH_KIND, METHOD_NAMESPACE

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "src" / "transparent_compact" / "extension.py"

SECRET = "first-secret-output"
# Distinct from the command text: `printf '%s-%s\n' alpha omega` prints
# `alpha-omega`, which appears nowhere in the command line itself. That makes
# "the output left the context, the call did not" an unambiguous assertion.
CMD = "printf '%s-%s\\n' alpha omega"
CMD_OUTPUT = "alpha-omega"
LONG_USER_TEXT = "please remember this exact sentence " * 60


def _answer(text: str) -> list[object]:
    return [
        AssistantStartEvent(partial=AssistantMessage(model="fake")),
        AssistantDoneEvent(
            reason="stop", message=AssistantMessage(content=text, stop_reason="stop")
        ),
    ]


def _bash_call(command: str) -> list[object]:
    call = ToolCall(id="call-1", name="bash", arguments={"command": command})
    message = AssistantMessage(model="fake", content=[call], stop_reason="toolUse")
    return [
        AssistantStartEvent(partial=AssistantMessage(model="fake")),
        ToolCallEndEvent(content_index=0, tool_call=call, partial=AssistantMessage(content=[call])),
        AssistantDoneEvent(reason="toolUse", message=message),
    ]


def _config(
    tmp_path: Path,
    provider: FakeProvider,
    storage: JsonlSessionStorage,
    **overrides: object,
) -> CodingSessionConfig:
    return CodingSessionConfig(
        provider=provider,
        model="fake",
        system="You are Tau.",
        storage=storage,
        cwd=tmp_path,
        extension_paths=(EXTENSION,),
        extensions_enabled=False,
        auto_compact_enabled=False,
        **overrides,
    )


async def _drain(stream: object) -> list[object]:
    return [event async for event in stream]  # type: ignore[attr-defined]


async def _compactions(storage: JsonlSessionStorage) -> list[CompactionEntry]:
    return [entry for entry in await storage.read_all() if isinstance(entry, CompactionEntry)]


async def _settings(storage: JsonlSessionStorage, namespace: str) -> list[dict[str, object]]:
    return [
        entry.data
        for entry in await storage.read_all()
        if isinstance(entry, CustomEntry) and entry.namespace == namespace
    ]


async def _await_setting(storage: JsonlSessionStorage, namespace: str) -> dict[str, object]:
    """Wait for a command handler's fire-and-forget entry to reach storage."""
    deadline = time.time() + 5.0
    while time.time() < deadline:
        records = await _settings(storage, namespace)
        if records:
            return records[-1]
        await asyncio.sleep(0.01)
    raise AssertionError(f"no {namespace!r} entry was written")


@pytest.fixture
def tiny_keep_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force a compaction boundary that keeps almost nothing, so spans are large."""
    monkeypatch.setattr(coding_session_module, "DEFAULT_COMPACTION_KEEP_RECENT_TOKENS", 1)


@pytest.mark.anyio
async def test_manual_compaction_writes_a_checkpoint_without_calling_the_model(
    tiny_keep_window: None,
    tmp_path: Path,
) -> None:
    storage = JsonlSessionStorage(tmp_path / "session.jsonl")
    # Exactly two scripted turns: the tool-call turn and its follow-up. A third
    # provider call (a summarization request) would find an exhausted provider.
    provider = FakeProvider([_bash_call(CMD), _answer("Done.")])
    session = await CodingSession.load(_config(tmp_path, provider, storage))

    await _drain(session.prompt("Run that command and report."))
    assert len(provider.calls) == 2

    result = await session.compact_detailed()

    assert len(provider.calls) == 2, "a mechanical checkpoint must not spend a model call"
    assert all(call[1] != SUMMARIZATION_SYSTEM_PROMPT for call in provider.calls), (
        "no summarization request may be made"
    )
    assert CMD_OUTPUT not in "\n".join(message.text for message in session.messages)
    assert "Run that command and report." in result.summary
    assert "printf" in result.summary
    assert "### Compacted conversation" in result.summary

    compactions = await _compactions(storage)
    assert len(compactions) == 1
    entry = compactions[0]
    assert entry.summary == result.summary
    assert isinstance(entry.details, dict)
    assert entry.details["kind"] == MECH_KIND
    # Tau's own summarizer attributes are absent: this summary is not model output.
    assert entry.provider is None and entry.model is None and entry.usage is None
    assert entry.first_kept_entry_id

    # The raw branch is untouched: the tool output still exists losslessly, even
    # though it has left the model-facing projection.
    raw = _raw_text(session.entries)
    assert CMD_OUTPUT in raw
    assert CMD_OUTPUT not in result.summary
    await session.aclose()


def _raw_text(entries: object) -> str:
    """Flatten every stored message body on the branch, for lossless checks."""
    chunks: list[str] = []
    for record in entries:  # type: ignore[attr-defined]
        message = getattr(record, "message", None)
        if message is not None:
            chunks.append(str(getattr(message, "text", "")))
    return "\n".join(chunks)


@pytest.mark.anyio
async def test_recovery_tools_read_the_raw_branch_after_compaction(
    tiny_keep_window: None,
    tmp_path: Path,
) -> None:
    storage = JsonlSessionStorage(tmp_path / "session.jsonl")
    provider = FakeProvider([_bash_call(CMD), _answer("Done.")])
    session = await CodingSession.load(_config(tmp_path, provider, storage))
    await _drain(session.prompt("Run that command."))
    await session.compact_detailed()

    tools = {tool.name: tool for tool in session.tools}
    assert {"context_list_entries", "context_grep", "context_show_entry"} <= set(tools)

    listed = await tools["context_list_entries"].execute("t1", {})
    assert "raw branch" in listed.text

    found = await tools["context_grep"].execute("t2", {"pattern": CMD_OUTPUT})
    assert CMD_OUTPUT in found.text
    assert "match" in found.text

    shown = await tools["context_show_entry"].execute(
        "t3", {"entry_id": _entry_id_of(found.text, CMD_OUTPUT)}
    )
    assert CMD_OUTPUT in shown.text

    missing = await tools["context_grep"].execute("t4", {"pattern": "no-such-token-anywhere"})
    assert "No match" in missing.text
    await session.aclose()


def _entry_id_of(grep_output: str, needle: str) -> str:
    """Pull the entry id out of a `context_grep` row: `L12 <id> <role>: text`."""
    for line in grep_output.splitlines():
        if needle in line:
            parts = line.split()
            if len(parts) >= 3 and parts[0].startswith("L"):
                return parts[1]
    raise AssertionError(f"no grep row for {needle!r}")


@pytest.mark.anyio
async def test_a_second_checkpoint_re_renders_the_first_span_and_stores_only_its_own(
    tiny_keep_window: None,
    tmp_path: Path,
) -> None:
    storage = JsonlSessionStorage(tmp_path / "session.jsonl")
    provider = FakeProvider(
        [
            _bash_call(CMD),
            _answer("First answer."),
            _answer("Second answer."),
        ]
    )
    session = await CodingSession.load(_config(tmp_path, provider, storage))
    await _drain(session.prompt("First question."))
    await _drain(session.prompt("Second question."))

    first = await session.compact_detailed()
    await _drain(session.prompt("Third question."))
    second = await session.compact_detailed()

    assert first.summary.count("<compacted-span") == 1
    assert second.summary.count("<compacted-span") >= 2
    # The first span is re-rendered from its stored record, character for character.
    first_line = next(
        line for line in _span_lines(await _compactions(storage)) if "First question." in line
    )
    assert first_line in second.summary

    compactions = await _compactions(storage)
    newest = compactions[-1]
    assert isinstance(newest.details, dict)
    stored = newest.details["span"]
    assert isinstance(stored, dict)
    assert "First question." not in "\n".join(stored["lines"]), (
        "a span record holds only its own lines"
    )
    await session.aclose()


def _span_lines(compactions: list[CompactionEntry]) -> list[str]:
    lines: list[str] = []
    for entry in compactions:
        details = entry.details
        if isinstance(details, dict) and isinstance(details.get("span"), dict):
            span_lines = details["span"].get("lines")
            if isinstance(span_lines, list):
                lines.extend(str(line) for line in span_lines)
    return lines


@pytest.mark.anyio
async def test_the_cap_trims_the_view_and_leaves_the_canonical_span_intact(
    tiny_keep_window: None,
    tmp_path: Path,
) -> None:
    storage = JsonlSessionStorage(tmp_path / "session.jsonl")
    provider = FakeProvider([_answer("Understood.")])
    session = await CodingSession.load(_config(tmp_path, provider, storage))
    await _drain(session.prompt(LONG_USER_TEXT))

    handled = session.handle_command("/cap-compaction 500c")
    assert handled.handled is True
    await _await_setting(storage, CAP_NAMESPACE)

    result = await session.compact_detailed()

    assert 'trimmed="true"' in result.summary
    assert "use context_lookup" in result.summary
    assert LONG_USER_TEXT[:40] in result.summary
    assert LONG_USER_TEXT not in result.summary, "the capped view must be shorter than the canon"

    compaction = (await _compactions(storage))[-1]
    assert isinstance(compaction.details, dict)
    span = compaction.details["span"]
    assert isinstance(span, dict)
    assert LONG_USER_TEXT in "\n".join(str(line) for line in span["lines"]), "canon stays complete"
    await session.aclose()


@pytest.mark.anyio
async def test_method_and_cap_are_branch_state_that_a_reload_rereads(
    tiny_keep_window: None,
    tmp_path: Path,
) -> None:
    storage = JsonlSessionStorage(tmp_path / "session.jsonl")
    provider = FakeProvider([_answer("Answer.")])
    session = await CodingSession.load(_config(tmp_path, provider, storage))
    await _drain(session.prompt("Earlier work."))

    assert session.handle_command("/compaction-method summary").message is None
    await _await_setting(storage, METHOD_NAMESPACE)
    assert session.handle_command("/cap-compaction off").message is None
    await _await_setting(storage, CAP_NAMESPACE)
    await session.aclose()

    reopened = await CodingSession.load(_config(tmp_path, FakeProvider([]), storage))
    status = reopened.handle_command("/transparent-compact")
    assert status.handled is True
    assert status.message is not None
    assert "Compaction method: summary" in status.message
    assert "cap-compaction" in status.message
    assert "Verbatim compaction cap: off" in status.message
    await reopened.aclose()


@pytest.mark.anyio
async def test_summary_method_hands_compaction_back_to_tau(
    tiny_keep_window: None,
    tmp_path: Path,
) -> None:
    storage = JsonlSessionStorage(tmp_path / "session.jsonl")
    provider = FakeProvider([_answer("Answer."), _answer("MODEL SUMMARY TEXT")])
    session = await CodingSession.load(_config(tmp_path, provider, storage))
    await _drain(session.prompt("Earlier work."))

    session.handle_command("/compaction-method summary")
    await _await_setting(storage, METHOD_NAMESPACE)

    result = await session.compact_detailed()

    # Prompt turn + one summarization request: the model is back in charge.
    assert len(provider.calls) == 2
    assert provider.calls[1][1] == SUMMARIZATION_SYSTEM_PROMPT
    assert "MODEL SUMMARY TEXT" in result.summary
    compaction = (await _compactions(storage))[-1]
    assert compaction.details is None
    await session.aclose()
