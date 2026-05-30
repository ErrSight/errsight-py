"""AWS Lambda integration for ErrSight.

Wrap your handler::

    import errsight
    from errsight.aws_lambda import errsight_lambda

    errsight.init(api_key=...)

    @errsight_lambda
    def lambda_handler(event, context):
        ...

Or with options::

    @errsight_lambda(flush_timeout=10.0, include_event=True)
    def lambda_handler(event, context):
        ...

The decorator:

- Pushes a per-invocation scope tagged with the function name, version,
  and AWS request id.
- Captures unhandled exceptions with full Lambda context (function
  metadata, remaining time).
- **Synchronously flushes the transport before returning.** Lambda
  freezes the process between invocations, so the background flush
  thread isn't guaranteed to run before the next freeze — queued events
  could be lost. The sync flush adds latency (typically <100ms) but
  guarantees delivery.

For long-running Lambda handlers (15-min max), the background flush
worker still ticks during execution, so most events ship asynchronously.
The synchronous flush at the boundary catches anything queued in the
final ``flush_interval`` window.
"""
from __future__ import annotations

import functools
import json
from typing import Any, Callable, Dict, Optional

import errsight

# Upper bound on event JSON we'll ship in metadata when include_event=True.
# Lambda event payloads can be huge (S3 batch events, etc.); a 4KB cap
# keeps the event under the 512KB ingestion limit even with deep traces.
_MAX_EVENT_BYTES = 4096

LambdaHandler = Callable[[Any, Any], Any]


def errsight_lambda(
    handler: Optional[LambdaHandler] = None,
    *,
    flush_timeout: float = 5.0,
    include_event: bool = False,
) -> Any:
    """Decorator that wraps an AWS Lambda handler with ErrSight capture
    + a synchronous flush before returning.

    Two calling conventions::

        @errsight_lambda                       # bare
        def lambda_handler(event, context): ...

        @errsight_lambda(flush_timeout=10.0)   # with options
        def lambda_handler(event, context): ...

    Args:
        flush_timeout: Seconds to wait for the queue to drain on return.
            Tune up for high-volume handlers; tune down for latency-
            sensitive ones (events still go through if the background
            worker runs).
        include_event: When True, attach a truncated form of the Lambda
            event payload to capture metadata. Off by default because
            events frequently contain PII (user data, request bodies).
    """
    def decorator(fn: LambdaHandler) -> LambdaHandler:
        @functools.wraps(fn)
        def wrapper(event: Any, context: Any) -> Any:
            with errsight.with_scope():
                _populate_scope_from_context(context)
                try:
                    return fn(event, context)
                except BaseException as exc:
                    try:
                        errsight.capture_exception(
                            exc,
                            metadata=_lambda_metadata(event, context, include_event),
                        )
                    except Exception:
                        pass
                    raise
                finally:
                    # Sync flush before Lambda freezes the process. The
                    # boolean return is ignored — if the flush times out
                    # we'd rather return on time than block the response.
                    try:
                        errsight.flush(timeout=flush_timeout)
                    except Exception:
                        pass
        return wrapper

    # Bare usage: @errsight_lambda  (decorator called with the handler)
    if handler is not None and callable(handler):
        return decorator(handler)
    # Parameterized usage: @errsight_lambda(...) returns the decorator
    return decorator


def _populate_scope_from_context(context: Any) -> None:
    if context is None:
        return
    try:
        tags: Dict[str, str] = {}
        for attr, tag_key in (
            ("function_name", "lambda_function"),
            ("function_version", "lambda_version"),
            ("aws_request_id", "aws_request_id"),
        ):
            val = getattr(context, attr, None)
            if val:
                tags[tag_key] = str(val)
        if tags:
            errsight.set_tags(tags)
    except Exception:
        pass


def _lambda_metadata(
    event: Any, context: Any, include_event: bool
) -> Dict[str, Any]:
    md: Dict[str, Any] = {}
    if context is not None:
        try:
            for attr in (
                "function_name",
                "function_version",
                "invoked_function_arn",
                "memory_limit_in_mb",
                "aws_request_id",
                "log_group_name",
                "log_stream_name",
            ):
                val = getattr(context, attr, None)
                if val is not None:
                    md[attr] = val
            try:
                # get_remaining_time_in_millis is a callable, not an attr.
                # Useful for diagnosing "out of time" Lambda timeouts —
                # close to 0 at exception time strongly suggests timeout.
                md["remaining_time_ms"] = context.get_remaining_time_in_millis()
            except Exception:
                pass
        except Exception:
            pass

    if include_event:
        try:
            serialized = json.dumps(event, default=str)
            if len(serialized.encode("utf-8")) > _MAX_EVENT_BYTES:
                md["event_truncated"] = True
                # Truncate by bytes, then re-decode with errors='ignore'
                # so a multi-byte split doesn't yield invalid UTF-8.
                truncated = serialized.encode("utf-8")[:_MAX_EVENT_BYTES]
                md["event"] = truncated.decode("utf-8", errors="ignore")
            else:
                md["event"] = event
        except Exception:
            pass
    return md
