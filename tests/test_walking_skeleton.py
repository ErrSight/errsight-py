from __future__ import annotations

import json

import errsight


def test_capture_exception_posts_event_to_ingest(httpserver):
    httpserver.expect_request("/api/v1/events", method="POST").respond_with_data(
        "", status=202
    )

    errsight.init(
        api_key="elp_" + "a" * 48,
        host=httpserver.url_for("").rstrip("/"),
        environment="test",
        min_level="debug",
        flush_interval=0.05,
        shutdown_timeout=2.0,
    )

    try:
        raise ValueError("boom")
    except ValueError as exc:
        errsight.capture_exception(exc, metadata={"order_id": 42})

    errsight.close()

    assert len(httpserver.log) == 1
    request, _response = httpserver.log[0]

    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["X-API-Key"].startswith("elp_")
    assert request.headers["User-Agent"].startswith("errsight-py/")

    body = json.loads(request.get_data())
    assert isinstance(body, list)
    assert len(body) == 1

    event = body[0]
    assert event["level"] == "error"
    assert "ValueError" in event["message"]
    assert "boom" in event["message"]
    assert event["environment"] == "test"
    assert event["occurred_at"].endswith("Z")
    assert "backtrace" in event and "ValueError: boom" in event["backtrace"]
    assert event["metadata"]["exception_class"] == "ValueError"
    assert event["metadata"]["order_id"] == 42
    assert "ingestion_id" in event


def test_log_below_min_level_is_dropped(httpserver):
    httpserver.expect_request("/api/v1/events", method="POST").respond_with_data(
        "", status=202
    )

    errsight.init(
        api_key="elp_" + "a" * 48,
        host=httpserver.url_for("").rstrip("/"),
        min_level="error",
        flush_interval=0.05,
        shutdown_timeout=2.0,
    )

    errsight.log(level="info", message="should be dropped")
    errsight.log(level="error", message="should ship")

    errsight.close()

    assert len(httpserver.log) == 1
    request, _response = httpserver.log[0]
    body = json.loads(request.get_data())
    assert len(body) == 1
    assert body[0]["message"] == "should ship"


def test_no_init_call_is_a_noop():
    # No init() — these must not raise.
    errsight.log(level="error", message="ignored")
    try:
        raise RuntimeError("ignored")
    except RuntimeError as exc:
        errsight.capture_exception(exc)
    errsight.close()


def test_before_send_can_drop(httpserver):
    httpserver.expect_request("/api/v1/events", method="POST").respond_with_data(
        "", status=202
    )

    def drop_all(event):
        return None

    errsight.init(
        api_key="elp_" + "a" * 48,
        host=httpserver.url_for("").rstrip("/"),
        min_level="debug",
        flush_interval=0.05,
        shutdown_timeout=2.0,
        before_send=drop_all,
    )

    errsight.log(level="error", message="should be dropped by before_send")
    errsight.close()

    assert len(httpserver.log) == 0
