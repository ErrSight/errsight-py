from __future__ import annotations

import linecache
from typing import Any, Dict, Optional

# 5 lines before, 5 lines after — sentry's default and a comfortable
# debugging window. Larger values bloat events without adding value.
PRE_CONTEXT_LINES = 5
POST_CONTEXT_LINES = 5

# Cap each emitted line to keep events bounded. A 1MB minified JS line
# accidentally landing in a Python backtrace (it happens via Jinja template
# errors over inline JS, asset-pipeline failures, etc.) shouldn't blow our
# 512KB ingestion limit.
MAX_LINE_BYTES = 256
_TRUNCATION_MARKER = "…[truncated]"


def fetch(filename: Optional[str], lineno: Optional[int]) -> Optional[Dict[str, Any]]:
    """Return ``{pre_context, context_line, post_context}`` for the given
    frame, or ``None`` when the file can't be read (missing, synthetic,
    permission denied, malformed encoding, …).

    Never raises. Source-context failure must not cascade into a failed
    event capture.
    """
    if not isinstance(filename, str) or not filename:
        return None
    if not isinstance(lineno, int) or lineno < 1:
        return None
    # Synthetic frames (<frozen ...>, <string>, <stdin>, (eval)) have no
    # readable source file on disk.
    if filename.startswith("<") or filename.startswith("("):
        return None

    try:
        # linecache caches by filename — the same path queried by Python's
        # own traceback module shares this cache, which is fine; we never
        # write to it. Cache stays for the process lifetime (unlike Ruby's
        # bounded LRU). In practice an app reads from O(100) source files
        # for backtraces — bounded enough.
        lines = linecache.getlines(filename)
    except Exception:
        return None
    if not lines:
        return None

    idx = lineno - 1
    if idx < 0 or idx >= len(lines):
        return None

    pre_start = max(0, idx - PRE_CONTEXT_LINES)
    post_end = min(len(lines), idx + 1 + POST_CONTEXT_LINES)

    return {
        "pre_context": [_truncate(_strip_newline(l)) for l in lines[pre_start:idx]],
        "context_line": _truncate(_strip_newline(lines[idx])),
        "post_context": [_truncate(_strip_newline(l)) for l in lines[idx + 1 : post_end]],
    }


def reset_cache() -> None:
    """Drop the linecache. Test-only — also clears caches used by Python's
    own traceback module, so use it sparingly outside tests.
    """
    linecache.clearcache()


def _strip_newline(line: str) -> str:
    if line.endswith("\r\n"):
        return line[:-2]
    if line.endswith("\n") or line.endswith("\r"):
        return line[:-1]
    return line


def _truncate(line: str) -> str:
    encoded = line.encode("utf-8")
    if len(encoded) <= MAX_LINE_BYTES:
        return line
    # Slice on bytes but decode with errors='ignore' so a multi-byte codepoint
    # split mid-byte doesn't surface as invalid UTF-8 in the event payload.
    return encoded[:MAX_LINE_BYTES].decode("utf-8", errors="ignore") + _TRUNCATION_MARKER


def _all_truncation_overhead_bytes() -> int:
    return len(_TRUNCATION_MARKER.encode("utf-8"))
