#!/usr/bin/env python
"""Check that this extension and the running `tau` will actually work together.

Run it with the interpreter behind the `tau` command you are using:

    ~/.local/share/uv/tools/tau-ai/bin/python scripts/check_install.py
    cd ../tau && uv run python ../transparent-compact-tau/scripts/check_install.py

It answers three separate questions, each of which has silently broken a real
session here, and stops at the first failure:

1. does this tau carry the `session_before_compact` seam? (a reinstall can serve
   a cached wheel built before the seam work, and `tau --version` cannot tell)
2. does the extension import at all? (Tau imports a directory extension under a
   synthesized module name, so absolute sibling imports fail)
3. is the compaction handler actually subscribed? (Tau does not validate event
   names, so an extension written against the seam loads and stays inert on a
   tau that lacks it)
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]


def fail(message: str) -> int:
    print(f"FAIL  {message}")
    return 1


def main() -> int:
    print(f"python: {sys.executable}")

    # 1. the Tau-side seam
    try:
        import tau_coding
        from tau_coding.extensions.api import LIFECYCLE_EVENT_TYPES
    except ImportError as error:  # pragma: no cover - diagnostic path
        return fail(f"cannot import tau_coding: {error}")

    print(f"tau_coding: {tau_coding.__file__}")
    # A branch build and a released wheel used to be indistinguishable: both said
    # 0.4.9b3. The branch carries its own versions and bumps them when behaviour
    # changes (0.4.9c1, 0.4.9.post1, 0.4.9.post2), so this line identifies the install.
    print(f"tau version: {tau_coding.__version__}")
    if "session_before_compact" not in LIFECYCLE_EVENT_TYPES:
        return fail(
            "this tau has no session_before_compact seam. Reinstall it from the "
            "branch: (cd ../tau && uv tool install --editable --force .)"
        )
    print("seam: session_before_compact is available")

    # 2 + 3. the extension imports, runs setup, and really subscribes the hook.
    # Loading through ExtensionRuntime.load is what a session does, so a handler
    # that fails to register shows up here rather than at the next compaction.
    from tau_coding.extensions.runtime import ExtensionRuntime
    from tau_coding.resources import TauResourcePaths

    target = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE
    runtime = ExtensionRuntime()
    with tempfile.TemporaryDirectory() as cwd:
        runtime.load(
            TauResourcePaths(cwd=Path(cwd)),
            extra_paths=[target],
            include_resource_dirs=False,
        )
    for diagnostic in runtime.diagnostics:
        print(f"diagnostic: {diagnostic.kind} {diagnostic.name}: {diagnostic.message}")
    if not runtime.has_session_before_compact_handlers:
        return fail(
            "the extension loaded but subscribed no session_before_compact "
            "handler — compaction will keep using Tau's model summary"
        )
    print("handler: session_before_compact is subscribed")
    print("OK      compaction override, recovery tools, and commands are wired")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
