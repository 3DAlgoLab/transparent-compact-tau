"""Mechanical message compression (algorithm Step 2).

Every signature here is *display notation*, never replayable: paths become
relative to the working directory, requested read ranges print as `start-end`
(no claim about what was returned), write bodies collapse to a character count,
`edits` arrays to `×N`. The stored tool-call arguments are never rewritten —
these are derived records next to the untouched originals.

`bash_output_label` is the only place anything is inferred, and it is
deliberately paranoid: conservative lexical hints classify the command, and only
a short list of shapes may be quoted out (reported nonzero status, empty output,
recognized test/build summaries, HTTP status for `curl`/`wget`, one error-text
line for recognized *execution* commands, and git's own bracketed commit hash).
Unrecognized shapes yield nothing, so a fixture file that happens to contain
"Error:" cannot be laundered into a fake failure.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Mapping

from tau_agent.messages import TextContent, ToolResultMessage

from .cap import (
    NORMAL_PREVIEW_CHARS,
    CommandPreview,
    ToolStub,
    command_preview,
    render_tool_stub,
)

ARG_SNIPPET_CHARS = NORMAL_PREVIEW_CHARS
FILE_TOOLS = ("read", "write", "edit")
SIGNATURE_TOOLS = (*FILE_TOOLS, "bash")

_TRUNCATION_NOTICE = re.compile(
    # Tau's read/bash notices differ from Pi's: "[N more lines in file. …]" and
    # "[Full output: …]" are Tau's markers, so the pattern is widened to catch
    # an incomplete result rather than pretending Pi's wording applies here.
    r"\[(?:Output truncated|Showing last |Showing lines |\d+ more lines in file|Full output:)",
    re.I,
)
_EXIT_FOOTER = re.compile(r"(?:^|\n)Command exited with code (-?\d+)\s*$")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_TEST_SUMMARY = re.compile(
    r"^=+ .*\b\d+ (?:passed|failed|errors?|skipped|xfailed)\b.*=+$"
    r"|^(?:\d+ (?:passed|failed|errors?|skipped|xfailed|deselected|warnings?)(?:, )?)+ in [\d.]+s$"
    r"|^(?:ℹ|#) (?:pass|fail) \d+$"
    r"|^BUILD (?:SUCCESSFUL|FAILED)\b"
    r"|^(?:PASS|FAIL): \d+/\d+$"
)
_ERROR_TEXT = re.compile(
    r"^(?:\s*\w*(?:Error|Exception):|Traceback \(most recent call last\):|error(?:\[\w+\])?:|fatal:)"
    r"|command not found"
)
_HTTP_STATUS = re.compile(
    r"(?:^|\n)(?:HTTP/[\d.]+\s+([1-5]\d\d)\b|http(?:_code)?[=: ]\s*([1-5]\d\d)\b)", re.I
)
_GIT_COMMIT = re.compile(r"^\[[^\]\n]+ ([0-9a-f]{7,40})\]", re.M)
_TEST_COMMAND = re.compile(
    r"^(?:(?:uv run |(?:\S*\/)?python[\d.]* -m )?pytest\b|(?:\S*\/)?pytest\b"
    r"|(?:npm|pnpm|yarn) (?:run )?(?:test|build)\b|node --test\b|(?:\.\/)?gradlew\b"
    r"|cargo (?:test|build)\b|go (?:test|build)\b|dotnet (?:test|build)\b|mvn (?:test|verify|package)\b)"
)
_READ_ONLY_COMMAND = re.compile(r"^(?:cat|sed|awk|rg|grep|head|tail|find|ls|man|git)\b")
_HISTORICAL_TMUX = re.compile(r"^(?:timeout\s+\S+\s+)?tmux\b")
_LEADING_CD = re.compile(r"^\s*cd\s+(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s;&|]+)\s*&&\s*")


def checkpoint_path(value: str, cwd: str) -> str:
    """Make a path relative for display only; never resolve or rewrite it."""
    prefix = cwd if cwd.endswith(posixpath.sep) else cwd + posixpath.sep
    if value == cwd:
        return "."
    if value.startswith(prefix):
        return value[len(prefix) :]
    return value


def checkpoint_command(command: str, cwd: str) -> str:
    """Shorten a command for display: drop a leading cd to the root, then shorten paths.

    A session that shells out through `cd <project> && …` says nothing worth
    keeping, so that prefix is removed outright; every other complete occurrence
    of the working directory becomes `.`. Only whole path boundaries are
    replaced, so a sibling like `/repo-other` and a URL containing the root stay
    intact.
    """
    escaped = re.escape(cwd)
    stripped = re.sub(rf'^cd\s+(?:"{escaped}"|{escaped})\s*&&\s*', "", command, count=1)
    return re.sub(
        rf"""(^|[\s"'=<>|;&(])({escaped})(?=$|[/\s"'<>|;&)])""",
        lambda match: f"{match.group(1)}.",
        stripped,
    )


def argument_snippet(value: object) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value)
    if text is None:
        text = "null"
    return text if len(text) <= ARG_SNIPPET_CHARS else f"{text[:ARG_SNIPPET_CHARS]}…"


def _is_safe_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and abs(value) < 2**53


def _positive_int(value: object) -> int | None:
    """A usable 1-based line/limit number, or None if the argument is not one.

    Providers occasionally send `10.0` where they mean `10`; a fractional value
    with an integral value is accepted, anything else is treated as an unknown
    range so the display stays honest.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float) and value.is_integer():
        return int(value) if value > 0 else None
    return None


def tool_signature(name: str, arguments: Mapping[str, object] | None, cwd: str) -> ToolStub:
    """Render one call as a `ToolStub` display record."""
    args = arguments or {}
    if name not in SIGNATURE_TOOLS:
        rendered = ", ".join(f"{key}={argument_snippet(value)}" for key, value in args.items())
        return ToolStub(prefix=f"{name}({rendered})")

    parts: list[str] = []
    command: CommandPreview | None = None
    file_tool = name in FILE_TOOLS and isinstance(args.get("path"), str)
    base = f"{name} {json.dumps(checkpoint_path(str(args['path']), cwd))}" if file_tool else name

    offset, limit = args.get("offset"), args.get("limit")
    first_line, line_count = _positive_int(offset), _positive_int(limit)
    range_ok = (
        name == "read"
        and file_tool
        and (offset is None or first_line is not None)
        and (limit is None or line_count is not None)
    )
    if range_ok and (first_line is not None or line_count is not None):
        start = first_line if first_line is not None else 1
        end = "" if line_count is None else start + line_count - 1
        base += f":{start}-{end}"

    for key, value in args.items():
        if file_tool and key == "path":
            continue
        if range_ok and key in ("offset", "limit"):
            continue
        if name == "bash" and key == "timeout":
            continue
        if name == "bash" and key == "command" and isinstance(value, str):
            command = command_preview(
                json.dumps(checkpoint_command(value, cwd), ensure_ascii=False)
            )
        elif name == "write" and key == "content" and isinstance(value, str):
            parts.append(f"({len(value)} chars)")
        elif name == "edit" and key == "edits" and isinstance(value, list):
            parts.append(f"×{len(value)}")
        elif name == "edit" and key in ("oldText", "newText") and isinstance(value, str):
            parts.append(f"{key}=<{len(value)} chars>")
        else:
            parts.append(f"{key}={argument_snippet(value)}")

    if command is not None:
        suffix = f" {' '.join(parts)}" if parts else ""
        return ToolStub(prefix=f"{base} ", command=command, suffix=suffix)
    return ToolStub(prefix=" ".join([base, *parts]))


def tool_call_signature(name: str, arguments: dict[str, object] | None, cwd: str) -> str:
    return render_tool_stub(tool_signature(name, arguments, cwd))


def command_hints(command: str) -> dict[str, bool]:
    """Conservative lexical categories; this parses no shell syntax."""
    core = _LEADING_CD.sub("", command).lstrip()
    tests = bool(_TEST_COMMAND.match(core))
    network = bool(re.match(r"^(?:curl|wget)\b", core))
    read_only = bool(_READ_ONLY_COMMAND.match(core))
    historical = not read_only and (
        network
        or bool(re.match(r"^(?:timeout\s+\S+\s+)?tmux\b", core))
        or bool(
            re.match(
                r"^(?:\S*\/)?(?:python[\d.]*|node|ruby|perl)\b[^\n]*(?:\s-[ce]\b|\s-\s*<<)", core
            )
        )
        or bool(re.match(r"^(?:uv run )?(?:\S*\/)?(?:python[\d.]*|node)\b", core))
        or bool(re.match(r"^(?:time|hyperfine)\b", core))
        or bool(re.search(r"\b(?:bench(?:mark)?|timeit)\b", core))
    )
    return {"tests": tests, "network": network, "historical": historical}


def _quote(body: str) -> str:
    """Wrap an observation in the checkpoint's single-quoted display form.

    JSON escaping supplies the guarantee that the quote can never be opened or
    closed by the text itself; the doubled quotes JSON would add are unnecessary
    inside single quotes.
    """
    inner = json.dumps(body, ensure_ascii=False)[1:-1].replace('\\"', '"')
    return f"'{inner}'"


def observation_quote(text: str) -> str:
    """Quote an observation, keeping it closed and escaped even with control chars."""
    if len(_quote(text)) <= ARG_SNIPPET_CHARS:
        return _quote(text)
    end = min(len(text), ARG_SNIPPET_CHARS)
    while len(_quote(text[:end] + "…")) > ARG_SNIPPET_CHARS:
        end -= 1
    return _quote(text[:end] + "…")


def _result_text(message: ToolResultMessage) -> tuple[str, bool]:
    """Return `(text, text_only)`; media-only results are never observations."""
    blocks = message.content
    if not blocks:
        return "", False
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, TextContent):
            return "", False
        parts.append(block.text)
    return "".join(parts), True


def bash_output_label(command: str, message: ToolResultMessage) -> str:
    """Select the few safe observations from one tool result, or nothing."""
    raw, text_only = _result_text(message)
    if not text_only:
        return ""

    details = message.details if isinstance(message.details, dict) else {}
    structured_exit = details.get("exit_code")
    has_structured_exit = _is_safe_integer(structured_exit)
    footer = _EXIT_FOOTER.search(raw)
    # A successful read can print a fixture containing the wrapper's own text.
    # Prefer explicit metadata; trust a legacy nonzero footer only on tool error.
    exit_match: re.Match[str] | None = footer
    if footer is not None:
        if has_structured_exit:
            if int(footer.group(1)) != int(structured_exit):  # type: ignore[arg-type]
                exit_match = None
        elif message.is_error is not True:
            exit_match = None

    body_text = raw[: exit_match.start()] if exit_match else raw
    body = body_text.strip()

    truncation = details.get("truncation")
    truncated_flag = isinstance(truncation, dict) and truncation.get("truncated") is True
    partial = truncated_flag or bool(_TRUNCATION_NOTICE.search(body))

    labels: list[str] = []
    reported_exit: int | None = None
    if has_structured_exit:
        reported_exit = int(structured_exit)  # type: ignore[arg-type]
    elif exit_match is not None:
        reported_exit = int(exit_match.group(1))
    if reported_exit not in (None, 0):
        labels.append(f"exit {reported_exit}")  # Reported shell status, not test status.

    if not partial and (body_text == "" or body == "(no output)"):
        return "; ".join([*labels, "no output"])

    hints = command_hints(command)
    lines = _ANSI.sub("", body).split("\n")
    observed: list[str] = []
    if hints["tests"]:
        observed = [line for line in lines if _TEST_SUMMARY.match(line.strip())][-3:]
    if hints["network"]:
        codes = [a or b for a, b in _HTTP_STATUS.findall(body)]
        if codes:
            shown = ",".join(codes[:4]) + (",…" if len(codes) > 4 else "")
            labels.append(f"output: http {shown}")
    # Only recognized execution commands get error-text scanning; reads, searches,
    # and git views never contribute test/error labels.
    if not observed and (hints["tests"] or hints["historical"]):
        errors = [
            line
            for line in [line for line in lines if line.strip()][-6:]
            if _ERROR_TEXT.match(line)
        ]
        observed = errors[-1:]

    if observed:
        labels.append(f"output: {' / '.join(observation_quote(line.strip()) for line in observed)}")
    elif (
        not any(label.startswith("output:") for label in labels)
        and hints["historical"]
        and not partial
        and len(body_text) <= 100
        and len(_quote(body_text)) <= ARG_SNIPPET_CHARS
    ):
        labels.append(f"output: {_quote(body_text)}")

    if re.match(r"^git\b|.*&&\s*git\b", command) and re.search(r"\bgit commit\b", command):
        found = _GIT_COMMIT.search(body)
        if found:
            labels.append(f"commit {found.group(1)}")
    return "; ".join(labels)
