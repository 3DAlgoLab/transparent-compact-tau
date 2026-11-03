"""The sidebar lines are a pure function over branch entries.

Kept separate from the end-to-end tests because the TUI is not involved: the
point is that the always-visible summary reports the *live* policy, including a
policy that a slash command changed mid-branch.
"""

from tau_agent.messages import UserMessage
from tau_agent.session.entries import CustomEntry, MessageEntry

from transparent_compact.extension import sidebar_lines

_counter = 0


def _uid() -> str:
    global _counter
    _counter += 1
    return f"sidebar-test-{_counter:04d}"


def _user(text: str) -> MessageEntry:
    return MessageEntry(
        id=_uid(), parent_id=None, timestamp=1_700_000_000, message=UserMessage(content=text)
    )


def _setting(namespace: str, data: dict[str, object]) -> CustomEntry:
    return CustomEntry(
        id=_uid(),
        parent_id=None,
        timestamp=1_700_000_001,
        namespace=namespace,
        data=data,
    )


def test_default_policy_is_visible_without_any_setting() -> None:
    lines = sidebar_lines([_user("hello")], 60_000, 1)

    assert lines[0] == "method: verbatim"
    assert lines[1] == "cap: warn 25% · window 60k"
    assert lines[2] == "checkpoints: 0 (0 mechanical, 0 span(s))"
    assert lines[3] == "branch: 1 entries → 1 in context"


def test_a_branch_setting_changes_what_the_sidebar_claims() -> None:
    from transparent_compact.spans import CAP_NAMESPACE, METHOD_NAMESPACE

    branch = [
        _user("hello"),
        _setting(METHOD_NAMESPACE, {"method": "summary"}),
        _setting(CAP_NAMESPACE, {"mode": "off", "budget": {"unit": "percent", "value": 25}}),
    ]
    lines = sidebar_lines(branch, 60_000, 4)

    assert lines[0] == "method: summary"
    assert lines[1] == "cap: off · window 60k"


def test_an_unknown_context_window_is_not_invented() -> None:
    lines = sidebar_lines([], None, 0)
    assert lines[1] == "cap: warn 25%"
