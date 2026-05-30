from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

import errsight
from errsight import hub
from errsight.aws_lambda import errsight_lambda


def _init(httpserver, **overrides):
    httpserver.expect_request("/api/v1/events", method="POST").respond_with_data(
        "", status=202
    )
    kwargs: Dict[str, Any] = dict(
        api_key="elp_" + "a" * 48,
        host=httpserver.url_for("").rstrip("/"),
        environment="test",
        min_level="debug",
        flush_interval=0.05,
        shutdown_timeout=2.0,
    )
    kwargs.update(overrides)
    errsight.init(**kwargs)


def _events(httpserver) -> List[Dict[str, object]]:
    out: List[Dict[str, object]] = []
    for req, _resp in httpserver.log:
        out.extend(json.loads(req.get_data()))
    return out


class FakeLambdaContext:
    """Shape-compatible stand-in for ``LambdaContext`` so we don't need
    AWS infrastructure to test the decorator."""

    function_name = "errsight-test-fn"
    function_version = "$LATEST"
    invoked_function_arn = "arn:aws:lambda:us-east-1:123:function:errsight-test-fn"
    memory_limit_in_mb = 256
    aws_request_id = "req-abc-123"
    log_group_name = "/aws/lambda/errsight-test-fn"
    log_stream_name = "2026/05/12/[$LATEST]abc"

    def __init__(self, remaining_ms: int = 3000):
        self._remaining = remaining_ms

    def get_remaining_time_in_millis(self) -> int:
        return self._remaining


def test_bare_decorator_returns_handler_result(httpserver):
    _init(httpserver)

    @errsight_lambda
    def handler(event, context):
        return {"statusCode": 200, "body": "ok"}

    result = handler({"path": "/hello"}, FakeLambdaContext())
    assert result == {"statusCode": 200, "body": "ok"}


def test_decorator_captures_exception_and_re_raises(httpserver):
    _init(httpserver)

    @errsight_lambda
    def handler(event, context):
        raise ValueError("lambda boom")

    with pytest.raises(ValueError):
        handler({"key": "value"}, FakeLambdaContext())

    # No need to call close — the decorator flushed synchronously.
    events = _events(httpserver)
    assert len(events) == 1
    event = events[0]
    assert event["tags"]["lambda_function"] == "errsight-test-fn"
    assert event["tags"]["lambda_version"] == "$LATEST"
    assert event["tags"]["aws_request_id"] == "req-abc-123"

    md = event["metadata"]
    assert md["function_name"] == "errsight-test-fn"
    assert md["invoked_function_arn"].endswith("errsight-test-fn")
    assert md["memory_limit_in_mb"] == 256
    assert md["aws_request_id"] == "req-abc-123"
    assert md["remaining_time_ms"] == 3000
    assert md["exception_class"] == "ValueError"


def test_parameterized_decorator_with_flush_timeout(httpserver):
    _init(httpserver)

    @errsight_lambda(flush_timeout=2.0)
    def handler(event, context):
        raise RuntimeError("with options")

    with pytest.raises(RuntimeError):
        handler({}, FakeLambdaContext())

    events = _events(httpserver)
    assert len(events) == 1


def test_include_event_attaches_truncated_payload(httpserver):
    _init(httpserver)

    @errsight_lambda(include_event=True)
    def handler(event, context):
        raise ValueError("event-included failure")

    with pytest.raises(ValueError):
        handler({"order_id": 42, "user": "alice"}, FakeLambdaContext())

    md = _events(httpserver)[0]["metadata"]
    assert "event" in md
    # event was small; not truncated
    assert "event_truncated" not in md
    assert md["event"] == {"order_id": 42, "user": "alice"}


def test_include_event_truncates_huge_payloads(httpserver):
    _init(httpserver)

    huge_event = {"data": "x" * 10000}

    @errsight_lambda(include_event=True)
    def handler(event, context):
        raise ValueError("huge")

    with pytest.raises(ValueError):
        handler(huge_event, FakeLambdaContext())

    md = _events(httpserver)[0]["metadata"]
    assert md["event_truncated"] is True
    assert isinstance(md["event"], str)
    assert len(md["event"].encode("utf-8")) <= 4096


def test_scope_pushed_per_invocation_is_isolated(httpserver):
    """Each invocation should see its own scope — set_user in one
    invocation must not leak to the next."""
    _init(httpserver)
    leaked: List[Any] = []

    @errsight_lambda
    def handler(event, context):
        # Before mutation: scope should be clean (tags from context only).
        leaked.append(hub.current_scope().user)
        errsight.set_user({"id": event["who"]})
        return event["who"]

    handler({"who": "alice"}, FakeLambdaContext())
    handler({"who": "bob"}, FakeLambdaContext())

    # First call: user was None at entry.
    # Second call: user from first call must NOT have leaked.
    assert all(u is None or u.get("id") not in {"alice", "bob"} for u in leaked)


def test_no_init_does_not_raise():
    """If errsight.init wasn't called, the decorator must still be safe."""
    @errsight_lambda
    def handler(event, context):
        return "ok"

    assert handler({}, FakeLambdaContext()) == "ok"


def test_no_init_capture_is_silent():
    @errsight_lambda
    def handler(event, context):
        raise ValueError("uninit")

    with pytest.raises(ValueError):
        handler({}, FakeLambdaContext())


def test_handler_with_no_context_still_works(httpserver):
    """Some test harnesses pass None for context. The decorator should
    still run the handler and capture exceptions, just without context
    tags."""
    _init(httpserver)

    @errsight_lambda
    def handler(event, context):
        raise ValueError("no context")

    with pytest.raises(ValueError):
        handler({}, None)

    events = _events(httpserver)
    assert len(events) == 1
    # No lambda_function tag because no context to read from.
    assert "lambda_function" not in events[0].get("tags", {})
