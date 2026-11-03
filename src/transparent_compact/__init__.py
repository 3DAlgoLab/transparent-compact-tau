"""Deterministic, losslessly recoverable compaction for Tau.

The public surface is deliberately small: the transcript renderer (Store A's
view of the raw branch) and the version string. Everything else is reached
through the extension entry point in :mod:`transparent_compact.extension`.
"""

from transparent_compact.transcript import (
    HR,
    GrepHit,
    Transcript,
    TranscriptEntry,
    TranscriptSection,
    build_transcript,
    paginate_lines,
)

__version__ = "0.1.0"

__all__ = [
    "HR",
    "GrepHit",
    "Transcript",
    "TranscriptEntry",
    "TranscriptSection",
    "__version__",
    "build_transcript",
    "paginate_lines",
]
