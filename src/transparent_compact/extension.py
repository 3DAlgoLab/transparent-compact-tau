"""The Tau extension: hooks compaction, owns the checkpoint, and serves recovery.

`setup(tau)` is the entry point Tau calls. Everything the extension persists
goes through session entries — settings as `CustomEntry` records, checkpoint
metadata as `CompactionEntry.details` — so a reload, resume, or fork picks the
state up from the branch instead of from this object's memory.

The one rule worth stating twice: this module never edits stored history. The
hook supplies a *replacement view* of old entries; the entries themselves stay.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import replace

from tau_agent.session.entries import CompactionEntry, SessionEntry
from tau_agent.types import JSONValue
from tau_coding.context_window import auto_compaction_threshold_for_context_window
from tau_coding.extensions import (
    CustomMessageView,
    ExtensionAPI,
    ExtensionCommandContext,
    ExtensionContext,
    ExtensionError,
    MessageRenderOptions,
    NotifyLevel,
    SessionBeforeCompactEvent,
    SessionBeforeCompactResult,
)

from .checkpoint import (
    CheckpointResult,
    build_checkpoint,
    conversation_size,
    file_operations,
    previous_file_lists,
    summarize_entries,
)
from .lookup import lookup_tool
from .recovery import recovery_tools
from .spans import (
    CAP_NAMESPACE,
    DECISION_NAMESPACE,
    METHOD_NAMESPACE,
    collect_base_and_spans,
    mech_details,
    new_span_entries,
    turn_owner_ids,
)
from .state import (
    CapMode,
    CapSettings,
    budget_chars,
    cap_description,
    cap_settings,
    compaction_method,
    parse_cap_budget,
)
from .transcript import format_timestamp
from .version import __version__

EXTENSION_NAME = "transparent-compact"
NOTICE_TYPE = "transparent-compact:notice"

METHOD_USAGE = "/compaction-method [verbatim|summary]"
CAP_USAGE = "/cap-compaction [on|warn|off|<n>%|<n>c|<n>t]"
SIDEBAR_KEY = "state"


def sidebar_lines(
    entries: Sequence[SessionEntry],
    context_window_tokens: int | None,
    projected_entries: int,
) -> list[str]:
    """What compaction will do and what it has done, in four short lines.

    A pure function over branch entries, so it is testable without a frontend and
    narrow enough for the sidebar column. `/transparent-compact` is the full
    picture; this is the always-visible summary that tells the reader which of the
    two compaction behaviours is live right now.
    """
    branch = list(entries)
    method = compaction_method(branch)
    settings = cap_settings(branch)
    compactions = [entry for entry in branch if isinstance(entry, CompactionEntry)]
    mechanical = [entry for entry in compactions if mech_details(entry) is not None]
    base, spans = collect_base_and_spans(branch)

    if settings.mode == "off":
        cap_line = "cap: off"
    else:
        unit = {"chars": "c", "tokens": "t", "percent": "%"}[settings.budget.unit]
        cap_line = f"cap: {settings.mode} {settings.budget.value}{unit}"
    if context_window_tokens:
        cap_line += f" · window {context_window_tokens // 1000}k"

    lines = [
        f"method: {method}",
        cap_line,
        f"checkpoints: {len(compactions)} ({len(mechanical)} mechanical, {len(spans)} span(s))",
        f"branch: {len(branch)} entries → {projected_entries} in context",
    ]
    if base is not None:
        lines.append(f"base: {base.kind} summary carried forward")
    return lines


def render_notice(message: CustomMessageView, options: MessageRenderOptions) -> str:
    """Render this extension's notices as a single labelled line."""
    del options
    return f"[transparent-compact] {message.content}"


class NothingToFold(Exception):
    """The span Tau offered holds nothing this extension has not already checkpointed.

    A kept boundary that has not moved since the last compaction leaves a replaced
    span of exactly one row: the previous checkpoint. Stored spans are re-rendered
    from their covered entries and a checkpoint is never re-summarized, so a
    checkpoint built here would copy text without giving anything back. This is an
    expected condition, not a build failure, and `_before_compact` decides what it
    means rather than letting it fall into the failure path.
    """


class TransparentCompact:
    """Wires the deterministic checkpoint into a running Tau session."""

    def __init__(self, api: ExtensionAPI) -> None:
        self._api = api
        # Strong references: a fire-and-forget task with no reference can be
        # garbage collected before it writes its entry.
        self._pending: set[asyncio.Task[None]] = set()
        self._sidebar_warned = False

        api.register_message_renderer(NOTICE_TYPE, render_notice)
        for tool in recovery_tools(self._branch):
            api.register_tool(tool)
        # The only model call this extension makes. The subagent behind it can
        # itself only use the three recovery tools above, over this branch.
        api.register_tool(lookup_tool(self._branch, lambda: self._api.context))
        api.register_command(
            "transparent-compact",
            self._status_command,
            description="Show compaction mode, cap, and checkpoint state.",
            usage="/transparent-compact",
        )
        api.register_command(
            "compaction-method",
            self._method_command,
            description="Choose verbatim checkpoints or Tau's model summary.",
            usage=METHOD_USAGE,
        )
        api.register_command(
            "cap-compaction",
            self._cap_command,
            description="Set the checkpoint size cap: on, warn, off, or a budget.",
            usage=CAP_USAGE,
        )
        api.on("session_before_compact", self._before_compact)
        api.on("session_start", self._on_session_start)

    # -- sidebar ------------------------------------------------------------

    async def _on_session_start(self, event: object, context: ExtensionContext) -> None:
        del event, context
        self._refresh_sidebar()

    def _refresh_sidebar(self) -> None:
        """Repaint the sidebar section when the frontend has one.

        The whole thing is guarded: a sidebar is decoration, so nothing in it may
        be able to break a session. `ExtensionError` covers the stale-generation
        case, which is exactly when a refresh tends to fire (right after a reload).
        """
        try:
            context = self._api.context
            if not context.ui.sidebar.supported:
                return
            context.ui.sidebar.set_section(
                SIDEBAR_KEY,
                title="transparent-compact",
                content=sidebar_lines(
                    list(context.entries),
                    context.context_window_tokens,
                    len(context.transcript),
                ),
            )
        except ExtensionError as error:
            if not self._sidebar_warned:
                self._sidebar_warned = True
                self._notify(f"could not update the sidebar: {error}", "warning")

    # -- state access -------------------------------------------------------

    def _branch(self) -> tuple[SessionEntry, ...]:
        """Return the raw active branch, the lossless store behind everything."""
        return self._api.context.entries

    def _notify(self, text: str, level: NotifyLevel = "info") -> None:
        self._api.notify(f"[transparent-compact] {text}", level)

    def _persist(self, namespace: str, data: dict[str, JSONValue]) -> None:
        """Append a settings entry without blocking a synchronous command handler."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._notify("no running event loop: the setting was not saved", "error")
            return
        task = loop.create_task(self._api.append_entry(namespace, data))
        self._pending.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._pending.discard(task)
        failure = task.exception()
        if failure is not None:
            self._notify(f"could not save the setting: {failure}", "error")
            return
        # The setting only becomes visible once its entry is appended, so this is
        # the first moment a repaint can show the new value.
        self._refresh_sidebar()

    # -- the hook -----------------------------------------------------------

    async def _before_compact(
        self,
        event: SessionBeforeCompactEvent,
        context: ExtensionContext,
    ) -> SessionBeforeCompactResult | None:
        """Replace Tau's model-written summary with a mechanical checkpoint.

        Returning `None` leaves Tau in charge; that is deliberate for the
        `summary` method and for a build failure where refusing to compact would
        strand a session that is already too large. A build failure on an ordinary
        threshold compaction is refused instead — a verbatim checkpoint is the
        point of this extension, and quietly writing a model summary instead would
        be a silent downgrade of what the user asked for.

        "Too large" is the exception that outranks that preference, and it is
        spelled out in `_is_window_critical`: at a full context window a refused
        compaction can end the session, so the downgrade is the cheaper loss. That
        exception only buys anything when something can be summarized at all, so a
        span with nothing left to fold (`NothingToFold`) is cancelled below the
        window, and handed to Tau's summarizer only once the window is full.
        """
        branch = context.entries
        if compaction_method(list(branch)) == "summary":
            return None
        if not event.replaced_entry_ids:
            self._notify(
                "compaction reported no replaced entries; using Tau's summarizer", "warning"
            )
            return None

        critical = self._is_window_critical(event)
        try:
            built = await self._assemble(event, context, branch)
        except NothingToFold as exc:
            detail = f"{type(exc).__name__}: {exc}"
            if critical:
                # At a full window the lossy option still beats the fatal one: Tau's
                # summarizer can rewrite the older checkpoint shorter, and a session
                # that refuses to shrink at this point can end on a rejected request.
                self._notify(
                    "nothing left to checkpoint; falling back to Tau's model summary", "warning"
                )
                await self._record_decision(
                    event, outcome="delegated", detail=detail, cause="nothing-to-fold"
                )
                return None
            # Below the window there is nothing to gain: the recent entries this
            # compaction kept are the whole context, and they are kept by design.
            # Delegating would spend a summarizer call to rewrite an older
            # checkpoint, which is what a session on 2026-11-05 did twice — 24,045
            # tokens and then 56,240 — for a context that was not near its window.
            self._notify(
                f"nothing left to checkpoint ({event.tokens_before} of "
                f"{event.context_window_tokens} tokens sits in the kept recent window); "
                "compaction cancelled",
                "warning",
            )
            await self._record_decision(
                event, outcome="cancelled", detail=detail, cause="nothing-to-fold"
            )
            return SessionBeforeCompactResult(cancel=True)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            if event.reason == "overflow" or critical:
                self._notify(
                    f"checkpoint build failed ({exc}); falling back to Tau's model summary", "error"
                )
                await self._record_decision(event, outcome="delegated", detail=detail)
                return None
            # For a manual or threshold compaction the user asked for a
            # verbatim checkpoint. Writing a model summary instead would be a
            # silent downgrade, so the compaction is refused instead.
            self._notify(f"checkpoint build failed ({exc}); compaction cancelled", "error")
            await self._record_decision(event, outcome="cancelled", detail=detail)
            return SessionBeforeCompactResult(cancel=True)

        self._report(built, context.context_window_tokens)
        self._refresh_sidebar()
        return SessionBeforeCompactResult(summary=built.summary, details=built.details)

    def _is_window_critical(self, event: SessionBeforeCompactEvent) -> bool:
        """Return whether refusing this compaction risks ending the session.

        Tau asks for a compaction once the context passes `window - reserve`, and
        its recovery for an already-rejected request is capped. Past that line a
        compaction is no longer a matter of taste: the next request is at risk, and
        a refusal that leaves the context where it was can be the last thing a
        session does. A verbatim checkpoint is worth insisting on right up to that
        point, and not one byte past it.
        """
        threshold = auto_compaction_threshold_for_context_window(event.context_window_tokens)
        return threshold is not None and event.tokens_before >= threshold

    async def _record_decision(
        self,
        event: SessionBeforeCompactEvent,
        *,
        outcome: str,
        detail: str,
        cause: str = "build-failure",
    ) -> None:
        """Record in the session why a compaction was refused or handed back.

        A refused compaction is otherwise invisible: the notification exists only
        in the UI, and the boundary stays exactly where it was, so a session that
        runs out of context later leaves nothing in its file to explain why no
        compaction happened. Awaited rather than fired off in a task, because the
        record is only worth having if it is durable before the next request.
        """
        try:
            await self._api.append_entry(
                DECISION_NAMESPACE,
                {
                    "outcome": outcome,
                    "cause": cause,
                    "reason": event.reason,
                    "tokens_before": event.tokens_before,
                    "context_window_tokens": event.context_window_tokens,
                    "detail": detail,
                    "at": format_timestamp(time.time()),
                },
            )
        except Exception as exc:  # a lost audit note must not cost a compaction
            self._notify(f"could not record the compaction decision: {exc}", "warning")

    async def _assemble(
        self,
        event: SessionBeforeCompactEvent,
        context: ExtensionContext,
        branch: tuple[SessionEntry, ...],
    ) -> CheckpointResult:
        """Build the checkpoint: inherited base, re-rendered spans, one new span.

        Raises `NothingToFold` when the offered span contains only entries earlier
        checkpoints already cover, and propagates any build failure as-is.
        """
        entries = list(branch)
        base, prior_spans = collect_base_and_spans(entries)
        covered = new_span_entries(
            entries,
            replaced_entry_ids=event.replaced_entry_ids,
            base_entry_id=base.entry_id if base is not None else None,
        )
        if not covered:
            msg = "no uncompacted entries inside the replaced span"
            raise NothingToFold(msg)

        now = format_timestamp(time.time())
        lines = summarize_entries(covered, cwd=str(context.cwd), owners=turn_owner_ids(entries))
        new_span = replace(
            lines.span_content(
                at=now,
                reason=event.reason,
                from_ts=format_timestamp(covered[0].timestamp),
                to_ts=format_timestamp(covered[-1].timestamp),
            ),
            tokens_before=event.tokens_before,
            covered_ids=tuple(entry.id for entry in covered),
        )

        reads, modified = file_operations(
            covered,
            previous=previous_file_lists(entries),
            cwd=str(context.cwd),
        )

        settings = cap_settings(entries)
        limit = (
            None
            if settings.mode == "off"
            else budget_chars(settings.budget, context.context_window_tokens)
        )
        overhead, line_chars = conversation_size(
            base,
            [*prior_spans, new_span],
            first_kept_entry_id=event.first_kept_entry_id,
            branch=entries,
        )
        if limit is not None and settings.mode == "warn" and overhead + line_chars > limit:
            if self._is_window_critical(event):
                # A modal at this point parks a provider request behind a
                # question, and the request it is protecting is the one that may
                # already be rejected. Trim, and say so out loud instead.
                self._notify(
                    f"cap trimming the checkpoint to {limit:,} chars without asking: "
                    "the context is at the window",
                    "warning",
                )
            else:
                limit = await self._confirm_cap(context, settings, limit, overhead + line_chars)

        return build_checkpoint(
            reason=event.reason,
            tokens_before=event.tokens_before,
            now=now,
            branch=entries,
            base=base,
            spans=prior_spans,
            new_span=new_span,
            first_kept_entry_id=event.first_kept_entry_id,
            read_files=reads,
            modified_files=modified,
            cap_limit_chars=limit,
            operator_note=event.custom_instructions,
        )

    async def _confirm_cap(
        self,
        context: ExtensionContext,
        settings: CapSettings,
        limit: int,
        needed: int,
    ) -> int | None:
        """Ask before the cap damages a checkpoint, when a UI is available.

        `warn` means *visible* trimming, not silent trimming. Without a UI the
        cap still applies — protecting the context window is the point — but the
        notification says exactly what was lost and that it is recoverable.
        """
        detail = f"{needed:,} chars of history against a {limit:,} char budget"
        if not context.has_ui:
            self._notify(f"cap will trim the checkpoint: {detail}", "warning")
            return limit
        choice = await context.ui.select(
            "Compaction cap would trim history",
            [
                f"Apply the cap ({limit:,} chars)",
                "Skip the cap this time",
                f"Raise the cap to {limit * 2:,} chars",
            ],
        )
        if choice is None or choice.startswith("Apply"):
            return limit
        if choice.startswith("Skip"):
            self._notify(
                "cap skipped for this compaction; the checkpoint keeps every span line", "warning"
            )
            return None
        doubled = limit * 2
        self._notify(f"cap raised for this compaction to {doubled:,} chars", "warning")
        return doubled

    def _report(self, built: CheckpointResult, context_window_tokens: int | None) -> None:
        """State out loud what the checkpoint did, in the order it matters."""
        summary = built.summary
        facts: list[str] = [f"checkpoint written ({len(summary):,} chars"]
        if context_window_tokens:
            facts[0] += f", ~{len(summary) // 4:,} of {context_window_tokens:,} tokens"
        facts[0] += ")"
        if built.capped is not None and built.capped.any_trimmed:
            facts.append("spans trimmed to fit — content is recoverable with context_lookup")
        elif built.capped is not None and built.capped.any_tightened:
            facts.append("previews tightened (presentation only)")
        for warning in built.warnings:
            facts.append(warning)
        self._notify(
            "; ".join(facts), "warning" if built.capped and built.capped.any_trimmed else "info"
        )

    # -- commands -----------------------------------------------------------

    def _status_command(self, args: str, command: ExtensionCommandContext) -> str | None:
        del args
        entries = list(command.api.context.entries)
        method = compaction_method(entries)
        settings = cap_settings(entries)
        window = command.api.context.context_window_tokens
        compactions = [entry for entry in entries if isinstance(entry, CompactionEntry)]
        mechanical = [entry for entry in compactions if mech_details(entry) is not None]
        base, spans = collect_base_and_spans(entries)
        projected = len(command.api.context.transcript)
        carried = f", base carried: {base.kind}" if base is not None else ", no base carried"
        lines = [
            f"transparent-compact {__version__}",
            f"Compaction method: {method} "
            f"({'deterministic checkpoint, no model call' if method == 'verbatim' else 'Tau model summary'}).",
            cap_description(settings, window),
            f"Branch: {len(entries)} entries ({projected} in the model-facing projection).",
            f"Checkpoints: {len(compactions)} total, {len(mechanical)} written by this extension, "
            f"{len(spans)} span(s) re-renderable{carried}.",
            "Recovery: context_lookup, context_list_entries, context_grep, context_show_entry.",
            METHOD_USAGE + " · " + CAP_USAGE,
        ]
        return "\n".join(lines)

    def _method_command(self, args: str, command: ExtensionCommandContext) -> str | None:
        entries = list(command.api.context.entries)
        requested = args.strip().lower()
        aliases = {
            "mech": "verbatim",
            "model": "summary",
            "tau": "summary",
            "": compaction_method(entries),
        }
        current = compaction_method(entries)
        if requested in ("", "status", "show"):
            return f"Compaction method: {current}. {METHOD_USAGE}"
        method = aliases.get(requested, requested)
        if method not in ("verbatim", "summary"):
            return f"Unknown method {requested!r}. {METHOD_USAGE}"
        self._persist(METHOD_NAMESPACE, {"method": method, "at": format_timestamp(time.time())})
        detail = (
            "deterministic checkpoint from raw history, no model call"
            if method == "verbatim"
            else "Tau's own model-generated compaction summary"
        )
        self._notify(f"compaction method set to {method} ({detail})", "info")
        return None

    def _cap_command(self, args: str, command: ExtensionCommandContext) -> str | None:
        entries = list(command.api.context.entries)
        window = command.api.context.context_window_tokens
        settings = cap_settings(entries)
        requested = args.strip().lower()
        if requested in ("", "status", "show"):
            return cap_description(settings, window) + f"\n{CAP_USAGE}"
        budget = parse_cap_budget(requested)
        mode: CapMode = settings.mode
        if budget is not None:
            mode = "on"
        elif requested == "on":
            mode = "on"
        elif requested == "warn":
            mode = "warn"
        elif requested == "off":
            mode = "off"
        else:
            return f"Unknown cap {requested!r}. {CAP_USAGE}"
        self._persist(
            CAP_NAMESPACE,
            {"mode": mode, "budget": {"unit": settings.budget.unit, "value": settings.budget.value}}
            if budget is None
            else {"mode": mode, "budget": {"unit": budget.unit, "value": budget.value}},
        )
        shown = budget or settings.budget
        self._notify(f"cap set to {mode} at {shown.text()}", "info")
        return None


def setup(tau: ExtensionAPI) -> None:
    """Register the compaction hook, recovery tools, and status commands."""
    TransparentCompact(tau)


__all__ = [
    "CAP_NAMESPACE",
    "DECISION_NAMESPACE",
    "EXTENSION_NAME",
    "METHOD_NAMESPACE",
    "NOTICE_TYPE",
    "TransparentCompact",
    "render_notice",
    "setup",
]
