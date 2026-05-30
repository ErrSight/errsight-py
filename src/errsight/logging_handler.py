from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

# Attributes set by the logging machinery itself; anything in
# record.__dict__ NOT in this set is user-supplied via the `extra=` kwarg
# and gets folded into the event's metadata.
_RESERVED_RECORD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


def _py_level_to_errsight(levelno: int) -> str:
    # Custom intermediate levels (e.g. logging.NOTICE = 25) collapse to the
    # nearest standard tier. Mirrors how Python's getLevelName picks names.
    if levelno >= logging.CRITICAL:
        return "fatal"
    if levelno >= logging.ERROR:
        return "error"
    if levelno >= logging.WARNING:
        return "warning"
    if levelno >= logging.INFO:
        return "info"
    return "debug"


class LoggingHandler(logging.Handler):
    """``logging.Handler`` subclass that forwards records to ErrSight.

    Records with ``exc_info`` (including ``logger.exception(...)``) route
    through :func:`errsight.capture_exception` so structured frames and the
    cause chain ship just like a direct ``errsight.capture_exception(exc)``
    call.

    Pass overrides via ``extra``::

        logger.error("payment failed", extra={
            "order_id": 42,                  # → metadata.order_id
            "errsight": {                    # → top-level event fields
                "user": {"id": "alice"},
                "tags": {"gateway": "stripe"},
                "fingerprint": "checkout-stripe",
            },
        })
    """

    # Per-thread recursion guard. If SDK internals ever route through
    # logging (e.g. a future contributor adds `logger.warning(...)`), the
    # second emit on the same thread short-circuits instead of looping.
    # Class-level so all handler instances share one flag — recursion is a
    # thread property, not a handler property.
    _processing: "threading.local" = threading.local()

    def __init__(self, level: int = logging.WARNING) -> None:
        super().__init__(level)

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._processing, "active", False):
            return
        try:
            self._processing.active = True
            self._emit(record)
        except Exception:
            # Per logging.Handler convention: errors inside emit() flow
            # through handleError so they end up on stderr but don't kill
            # the calling thread's log call.
            self.handleError(record)
        finally:
            self._processing.active = False

    def _emit(self, record: logging.LogRecord) -> None:
        # Deferred imports: at module-load time the `errsight` package is
        # mid-initialization and these names don't exist on it yet. By the
        # time _emit is invoked (user code → logger.error → handler) the
        # package is fully loaded.
        from errsight import capture_exception as _capture
        from errsight import log as _log

        message = record.getMessage()
        level = _py_level_to_errsight(record.levelno)

        metadata: Dict[str, Any] = {"logger": record.name}
        if record.funcName:
            metadata["function"] = record.funcName
        if record.module:
            metadata["module"] = record.module
        if record.lineno:
            metadata["lineno"] = record.lineno
        if record.stack_info:
            metadata["stack_info"] = record.stack_info

        # Fold user-supplied `extra=` attrs into metadata. setdefault, not
        # assignment: a key we've already populated (e.g. `logger`) wins
        # over a user override of the same name — preserves the canonical
        # meaning of those metadata fields.
        for attr, value in record.__dict__.items():
            if attr in _RESERVED_RECORD_ATTRS or attr.startswith("_"):
                continue
            if attr == "errsight":
                continue
            metadata.setdefault(attr, value)

        overrides = record.__dict__.get("errsight")
        user: Optional[Dict[str, Any]] = None
        tags: Optional[Dict[str, Any]] = None
        fingerprint: Optional[str] = None
        if isinstance(overrides, dict):
            user_override = overrides.get("user")
            tags_override = overrides.get("tags")
            fp_override = overrides.get("fingerprint")
            if isinstance(user_override, dict):
                user = user_override
            if isinstance(tags_override, dict):
                tags = tags_override
            if isinstance(fp_override, str):
                fingerprint = fp_override

        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            _capture(
                exc,
                metadata={**metadata, "log_message": message},
                tags=tags,
                user=user,
                fingerprint=fingerprint,
            )
            return

        _log(
            level=level,
            message=message,
            metadata=metadata,
            tags=tags,
            user=user,
            fingerprint=fingerprint,
        )


def attach_logging_handler(
    level: int = logging.WARNING,
    logger: Optional[logging.Logger] = None,
) -> LoggingHandler:
    """Add a :class:`LoggingHandler` to ``logger`` (or the root logger) and
    return the handler so it can be detached later via
    ``logger.removeHandler(handler)``.
    """
    target = logger if logger is not None else logging.getLogger()
    handler = LoggingHandler(level=level)
    target.addHandler(handler)
    return handler
