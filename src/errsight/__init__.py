from __future__ import annotations

import atexit
import logging
import sys
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

from errsight import backtrace as _backtrace
from errsight import hub
from errsight import source_context as _source_context
from errsight.configuration import Configuration
from errsight.hub import with_scope
from errsight.scope import Scope
from errsight.transport import Transport
from errsight.version import __version__

__all__ = [
    "__version__",
    "Configuration",
    "Scope",
    "LoggingHandler",
    "init",
    "capture_exception",
    "log",
    "close",
    "flush",
    "set_user",
    "clear_user",
    "set_tag",
    "set_tags",
    "clear_tags",
    "add_breadcrumb",
    "clear_breadcrumbs",
    "with_scope",
    "attach_logging_handler",
]

_LEVELS = ("debug", "info", "warning", "error", "fatal")

_config: Optional[Configuration] = None
_transport: Optional[Transport] = None
_atexit_registered = False
_auto_logging_handler: Optional["LoggingHandler"] = None


def init(**kwargs: Any) -> Configuration:
    """Configure ErrSight and start the background flush worker.

    Subsequent calls replace the prior client; the old transport is closed
    before the new one starts.
    """
    global _config, _transport, _atexit_registered, _auto_logging_handler

    if _transport is not None:
        _transport.close()
        _transport = None

    # Drop any previously auto-attached handler before re-init so the root
    # logger doesn't accumulate handlers across repeated init() calls.
    if _auto_logging_handler is not None:
        logging.getLogger().removeHandler(_auto_logging_handler)
        _auto_logging_handler = None

    _config = Configuration(**kwargs)
    if _config.is_enabled():
        _transport = Transport(_config)
        if not _atexit_registered:
            atexit.register(close)
            _atexit_registered = True
        if _config.attach_to_logging:
            _auto_logging_handler = attach_logging_handler(level=logging.WARNING)
    else:
        sys.stderr.write(
            "[errsight] api_key not set; SDK is disabled until configured\n"
        )
    return _config


def log(
    *,
    level: str,
    message: str,
    backtrace: Optional[str] = None,
    metadata: Optional[Mapping[str, Any]] = None,
    tags: Optional[Mapping[str, Any]] = None,
    user: Optional[Mapping[str, Any]] = None,
    fingerprint: Optional[str] = None,
    occurred_at: Optional[datetime] = None,
) -> None:
    if _config is None or _transport is None or not _config.is_enabled():
        return
    if _level_index(level) < _level_index(_config.min_level):
        return

    scope = hub.current_scope()

    ts = (occurred_at or datetime.now(timezone.utc)).isoformat(timespec="milliseconds")
    if ts.endswith("+00:00"):
        ts = ts[:-6] + "Z"

    event: Dict[str, Any] = {
        "ingestion_id": str(uuid.uuid4()),
        "level": level.lower(),
        "message": str(message),
        "environment": _config.environment,
        "occurred_at": ts,
    }
    if backtrace:
        event["backtrace"] = backtrace
    if metadata:
        event["metadata"] = dict(metadata)

    # Tags: scope is the base, per-call overrides on collision.
    merged_tags: Dict[str, str] = dict(scope.tags)
    if tags:
        merged_tags.update({str(k): str(v) for k, v in tags.items()})
    if merged_tags:
        event["tags"] = merged_tags

    # User: per-call wins; fall back to scope.
    if user is not None:
        event["user"] = dict(user)
    elif scope.user:
        event["user"] = dict(scope.user)

    crumbs = scope.breadcrumbs
    if crumbs:
        event["breadcrumbs"] = crumbs

    if fingerprint:
        event["fingerprint"] = fingerprint
    if _config.release:
        event["release"] = _config.release

    final = _run_before_send(event)
    if final is None:
        return
    _transport.enqueue(final)


def capture_exception(
    exc: BaseException,
    *,
    metadata: Optional[Mapping[str, Any]] = None,
    tags: Optional[Mapping[str, Any]] = None,
    user: Optional[Mapping[str, Any]] = None,
    fingerprint: Optional[str] = None,
) -> None:
    if not isinstance(exc, BaseException):
        return

    enriched: Dict[str, Any] = dict(metadata or {})
    enriched["exception_class"] = type(exc).__name__

    # Cause chain. Surface the inner cause so the issue detail page can show
    # "rescued from <inner>" — the outer message is usually the wrapper; the
    # inner is what actually broke.
    try:
        causes = _backtrace.walk_causes(exc)
    except Exception:
        causes = []
    if causes:
        enriched["exception_causes"] = causes

    # Structured frames + source context. Frame parsing must never break
    # capture; on any error, fall back to the legacy backtrace string only.
    try:
        frames = _backtrace.parse_traceback(exc.__traceback__)
        for frame in frames:
            if not frame.get("in_app"):
                continue
            abs_path = frame.get("abs_path")
            lineno = frame.get("lineno")
            if not isinstance(abs_path, str) or not isinstance(lineno, int):
                continue
            ctx = _source_context.fetch(abs_path, lineno)
            if ctx:
                frame.update(ctx)
    except Exception:
        frames = []
    if frames:
        enriched["exception_frames"] = frames

    backtrace = "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    )

    log(
        level="error",
        message=f"{type(exc).__name__}: {exc}",
        backtrace=backtrace,
        metadata=enriched,
        tags=tags,
        user=user,
        fingerprint=fingerprint,
    )


def close() -> None:
    """Drain the queue, stop the flush thread, and detach the auto-attached
    LoggingHandler if one was installed."""
    global _transport, _auto_logging_handler
    if _transport is not None:
        _transport.close()
        _transport = None
    if _auto_logging_handler is not None:
        logging.getLogger().removeHandler(_auto_logging_handler)
        _auto_logging_handler = None


def flush(timeout: float = 5.0) -> bool:
    """Synchronously drain queued events. Returns ``True`` if the queue
    was emptied within ``timeout``, ``False`` otherwise.

    Unlike :func:`close`, this keeps the background worker running.
    Designed for environments where the host process can be frozen
    between invocations (AWS Lambda, Cloud Functions) and the worker
    thread isn't guaranteed to run before the next freeze.
    """
    if _transport is None:
        return True
    return _transport.flush(timeout=timeout)


def set_user(user: Optional[Mapping[str, Any]]) -> None:
    hub.current_scope().set_user(user)


def clear_user() -> None:
    hub.current_scope().clear_user()


def set_tag(key: Any, value: Any) -> None:
    hub.current_scope().set_tag(key, value)


def set_tags(tags: Optional[Mapping[str, Any]]) -> None:
    hub.current_scope().set_tags(tags)


def clear_tags() -> None:
    hub.current_scope().clear_tags()


def add_breadcrumb(
    *,
    category: str,
    message: str,
    level: str = "info",
    data: Optional[Mapping[str, Any]] = None,
) -> None:
    hub.current_scope().add_breadcrumb(
        category=category, message=message, level=level, data=data
    )


def clear_breadcrumbs() -> None:
    hub.current_scope().clear_breadcrumbs()


def _level_index(level: str) -> int:
    try:
        return _LEVELS.index(level.lower())
    except ValueError:
        return 0


def _run_before_send(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    assert _config is not None
    hook = _config.before_send
    if hook is None:
        return event
    try:
        result = hook(event)
    except Exception as e:
        sys.stderr.write(
            f"[errsight] before_send raised {type(e).__name__}: {e} — passing event through\n"
        )
        return event
    return result if isinstance(result, dict) else None


# Imported at the bottom so logging_handler.py's deferred `from errsight
# import log, capture_exception` sees fully-defined module attributes.
from errsight.logging_handler import LoggingHandler, attach_logging_handler  # noqa: E402
