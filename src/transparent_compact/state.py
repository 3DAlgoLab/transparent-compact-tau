"""Session-scoped settings read from the branch (Pi parity).

Method and cap policy live in `CustomEntry` records, not in process memory:
reload, resume, fork, and tree navigation must inherit only the choices made on
their own branch path, and a compaction must never depend on a flag that a
restart would drop.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Literal

from tau_agent.session.entries import SessionEntry

from transparent_compact.spans import CAP_NAMESPACE, METHOD_NAMESPACE, custom_state

CompactionMethod = Literal["verbatim", "summary"]
CapMode = Literal["on", "off", "warn"]
CapUnit = Literal["chars", "tokens", "percent"]

DEFAULT_CAP_PERCENT = 25
MAX_PERCENT = 100
CHARS_PER_TOKEN = 4


@dataclass(frozen=True)
class CapBudget:
    unit: CapUnit
    value: float

    def text(self) -> str:
        symbol = {"chars": "c", "tokens": "t", "percent": "%"}[self.unit]
        number = f"{self.value:g}"
        return f"{number}{symbol}"


@dataclass(frozen=True)
class CapSettings:
    mode: CapMode
    budget: CapBudget


def compaction_method(entries: list[SessionEntry]) -> CompactionMethod:
    """Return the method in force on this branch (`verbatim` unless changed)."""
    data = custom_state(entries, METHOD_NAMESPACE)
    method = data.get("method") if isinstance(data, dict) else None
    return method if method in ("verbatim", "summary") else "verbatim"


def _env_int(name: str, fallback: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return fallback
    try:
        value = int(raw)
    except ValueError:
        return fallback
    return value if value > 0 else fallback


def _env_percent(name: str, fallback: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return fallback
    try:
        value = float(raw)
    except ValueError:
        return fallback
    return int(value) if 0 < value <= MAX_PERCENT else fallback


def default_cap_budget() -> CapBudget:
    chars = os.environ.get("MECH_COMPACT_MAX_SUMMARY_CHARS")
    if chars:
        return CapBudget(unit="chars", value=_env_int("MECH_COMPACT_MAX_SUMMARY_CHARS", 80_000))
    return CapBudget(
        unit="percent", value=_env_percent("MECH_COMPACT_MAX_SUMMARY_PERCENT", DEFAULT_CAP_PERCENT)
    )


def valid_cap_budget(value: object) -> CapBudget | None:
    """Accept only a sane stored budget; anything else falls back to the default."""
    if not isinstance(value, dict):
        return None
    unit, number = value.get("unit"), value.get("value")
    if not isinstance(unit, str) or unit not in ("chars", "tokens", "percent"):
        return None
    if isinstance(number, bool) or not isinstance(number, (int, float)):
        return None
    budget = CapBudget(unit=unit, value=float(number))  # type: ignore[arg-type]
    return budget if budget_is_valid(budget) else None


def parse_cap_budget(text: str) -> CapBudget | None:
    """Parse `80000c`, `20000t`, or `25%`."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(c|t|%)", text.strip())
    if not match:
        return None
    unit: CapUnit = {"c": "chars", "t": "tokens", "%": "percent"}[match.group(2)]  # type: ignore[assignment]
    value = float(match.group(1))
    budget = CapBudget(unit=unit, value=value)
    return budget if budget_is_valid(budget) else None


def budget_is_valid(budget: CapBudget) -> bool:
    if budget.value <= 0 or budget.value != budget.value:  # NaN
        return False
    if budget.unit == "percent":
        return budget.value <= MAX_PERCENT
    return float(budget.value).is_integer() and (
        budget.unit == "chars" or budget.value <= (2**53 - 1) // CHARS_PER_TOKEN
    )


def cap_settings(entries: list[SessionEntry]) -> CapSettings:
    """Return the cap policy in force, defaulting to `warn` at 25% of context."""
    data = custom_state(entries, CAP_NAMESPACE)
    if isinstance(data, dict):
        mode = data.get("mode")
        budget = valid_cap_budget(data.get("budget"))
        if mode in ("on", "off", "warn") and budget is not None:
            return CapSettings(mode=mode, budget=budget)
    return CapSettings(mode="warn", budget=default_cap_budget())


def budget_chars(budget: CapBudget, context_window_tokens: int | None) -> int | None:
    """Resolve a budget to characters, or `None` when percent has no window."""
    if budget.unit == "chars":
        return int(budget.value)
    if budget.unit == "tokens":
        return int(budget.value) * CHARS_PER_TOKEN
    if not context_window_tokens or context_window_tokens <= 0:
        return None
    return max(1, context_window_tokens * int(budget.value) // 100) * CHARS_PER_TOKEN


def cap_description(settings: CapSettings, context_window_tokens: int | None) -> str:
    resolved = budget_chars(settings.budget, context_window_tokens)
    if resolved is None:
        detail = "model context window unavailable"
    elif settings.budget.unit == "chars":
        detail = f"{resolved:,} chars"
    else:
        detail = (
            f"{resolved // CHARS_PER_TOKEN:,} estimated tokens at 4 chars/token; {resolved:,} chars"
        )
    return f"Verbatim compaction cap: {settings.mode}; budget {settings.budget.text()} ({detail})."
