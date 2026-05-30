from __future__ import annotations

import os
import sys
import sysconfig
import traceback
from types import TracebackType
from typing import Any, Dict, List, Optional

# Cap on frames per event. A pathological infinite-recursion crash can produce
# 10k+ frames; without a cap the event blows past the 512 KB ingestion limit
# and gets rejected. Match sentry-python at 50.
MAX_FRAMES = 50

# Hard cap on cause-chain depth. Python doesn't normally loop __cause__, but
# user code can construct pathological chains by setting __cause__ manually.
# The seen-set guards against that; depth cap is the load-bearing protection.
MAX_CAUSE_DEPTH = 5
MAX_CAUSE_BACKTRACE_FRAMES = 20

_THIRDPARTY_DIR_NAMES = {"site-packages", "dist-packages"}

_excluded_prefixes_cache: Optional[List[str]] = None


def _compute_excluded_prefixes() -> List[str]:
    """Paths under which frames are framework/stdlib, not customer code."""
    prefixes: List[str] = []
    for attr in ("prefix", "base_prefix", "exec_prefix", "base_exec_prefix"):
        path = getattr(sys, attr, None)
        if path:
            try:
                prefixes.append(os.path.realpath(path))
            except OSError:
                prefixes.append(path)
    # sysconfig covers stdlib, purelib, platlib (site-packages), include, scripts.
    for path in sysconfig.get_paths().values():
        if path:
            try:
                prefixes.append(os.path.realpath(path))
            except OSError:
                prefixes.append(path)
    # De-dup, preserve order. Memoization is per-process; these paths don't
    # move at runtime, so re-deriving on every frame would just burn CPU.
    return list(dict.fromkeys(prefixes))


def _excluded_prefixes() -> List[str]:
    global _excluded_prefixes_cache
    if _excluded_prefixes_cache is None:
        _excluded_prefixes_cache = _compute_excluded_prefixes()
    return _excluded_prefixes_cache


def reset_defaults() -> None:
    """Test helper: drop the memoized exclusion prefixes."""
    global _excluded_prefixes_cache
    _excluded_prefixes_cache = None


def is_in_app(abs_path: Optional[str]) -> bool:
    """Customer-code vs framework classification. Mirrors the in_app concept
    from the Ruby SDK — the UI uses this to decide which frames to expand
    and attach source context to.
    """
    if not abs_path:
        return False
    # Synthetic frames: <frozen importlib._bootstrap>, <string>, <stdin>,
    # (eval), etc. No file to read; not customer code.
    if abs_path.startswith("<") or abs_path.startswith("("):
        return False

    try:
        real = os.path.realpath(abs_path)
    except OSError:
        real = abs_path

    # site-packages / dist-packages anywhere in the path means third-party.
    # Bundler-style vendoring (vendor/bundle) doesn't really exist in Python
    # but pip --target=vendor/ does; the site-packages directory name is the
    # universal signal.
    parts = real.split(os.sep)
    if _THIRDPARTY_DIR_NAMES.intersection(parts):
        return False

    for prefix in _excluded_prefixes():
        if real.startswith(prefix):
            return False
    return True


def parse_traceback(tb: Optional[TracebackType]) -> List[Dict[str, Any]]:
    """Walk a traceback into structured frames, oldest-first (matching
    Python's stack-trace print order — outermost call first, raise site last).

    Capped at MAX_FRAMES from the most-recent end; deep recursion drops the
    redundant middle, not the actual failure point.
    """
    if tb is None:
        return []
    try:
        summaries = traceback.extract_tb(tb, limit=MAX_FRAMES)
    except Exception:
        return []

    frames: List[Dict[str, Any]] = []
    for s in summaries:
        filename = s.filename or ""
        lineno = int(s.lineno) if s.lineno is not None else 0
        frame: Dict[str, Any] = {
            "filename": filename,
            "abs_path": filename,
            "lineno": lineno,
            "function": s.name or "",
            "in_app": is_in_app(filename),
        }
        frames.append(frame)
    return frames


def walk_causes(exc: BaseException) -> List[Dict[str, Any]]:
    """Walk the exception's __cause__ / __context__ chain.

    Python's cause-chain semantics:
      - `raise X from Y` sets __cause__ = Y and __suppress_context__ = True.
      - An exception raised while handling another sets __context__ implicitly.
      - `raise X from None` suppresses the implicit context.

    Returns up to MAX_CAUSE_DEPTH causes, each {class, message, backtrace}.
    backtrace is a list of pre-formatted lines (compatible with Ruby's shape).
    """
    causes: List[Dict[str, Any]] = []
    seen: set[int] = {id(exc)}
    current = _next_in_chain(exc)
    while current is not None and len(causes) < MAX_CAUSE_DEPTH:
        if id(current) in seen:
            break
        seen.add(id(current))
        bt: Optional[List[str]] = None
        if current.__traceback__ is not None:
            try:
                lines = traceback.format_tb(
                    current.__traceback__, limit=MAX_CAUSE_BACKTRACE_FRAMES
                )
                bt = [line.rstrip("\n") for line in lines] or None
            except Exception:
                bt = None
        cause: Dict[str, Any] = {
            "class": type(current).__name__,
            "message": str(current),
        }
        if bt is not None:
            cause["backtrace"] = bt
        causes.append(cause)
        current = _next_in_chain(current)
    return causes


def _next_in_chain(exc: BaseException) -> Optional[BaseException]:
    if exc.__cause__ is not None:
        return exc.__cause__
    if not exc.__suppress_context__ and exc.__context__ is not None:
        return exc.__context__
    return None
