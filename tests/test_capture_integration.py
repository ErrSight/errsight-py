from __future__ import annotations

import json
from typing import Dict, List

import errsight


def _init(httpserver, **overrides):
    httpserver.expect_request("/api/v1/events", method="POST").respond_with_data(
        "", status=202
    )
    kwargs = dict(
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


def test_capture_exception_attaches_structured_frames(httpserver):
    _init(httpserver)
    try:
        x = 1
        y = 0
        _ = x / y  # noqa: F841
    except ZeroDivisionError as exc:
        errsight.capture_exception(exc)
    errsight.close()

    event = _events(httpserver)[0]
    frames = event["metadata"]["exception_frames"]
    assert isinstance(frames, list) and len(frames) >= 1
    last = frames[-1]
    assert last["function"] == "test_capture_exception_attaches_structured_frames"
    assert last["filename"].endswith("test_capture_integration.py")
    assert isinstance(last["lineno"], int) and last["lineno"] > 0
    assert last["in_app"] is True


def test_in_app_frame_gets_source_context(httpserver):
    _init(httpserver)
    try:
        raise RuntimeError("look at the source")
    except RuntimeError as exc:
        errsight.capture_exception(exc)
    errsight.close()

    event = _events(httpserver)[0]
    frames = event["metadata"]["exception_frames"]
    in_app_frames = [f for f in frames if f.get("in_app")]
    assert in_app_frames, "expected at least one in_app frame"
    last = in_app_frames[-1]
    assert "context_line" in last
    assert "look at the source" in last["context_line"]
    assert isinstance(last["pre_context"], list)
    assert isinstance(last["post_context"], list)


def test_capture_exception_attaches_cause_chain(httpserver):
    _init(httpserver)
    try:
        try:
            raise KeyError("inner-key")
        except KeyError as inner:
            raise ValueError("outer-value") from inner
    except ValueError as outer:
        errsight.capture_exception(outer)
    errsight.close()

    causes = _events(httpserver)[0]["metadata"]["exception_causes"]
    assert isinstance(causes, list)
    assert causes[0]["class"] == "KeyError"
    assert "inner-key" in causes[0]["message"]


def test_capture_exception_without_cause_omits_causes_field(httpserver):
    _init(httpserver)
    try:
        raise ValueError("standalone")
    except ValueError as exc:
        errsight.capture_exception(exc)
    errsight.close()

    metadata = _events(httpserver)[0]["metadata"]
    assert "exception_causes" not in metadata


def test_legacy_backtrace_string_still_shipped(httpserver):
    """The structured frames are additive; the original `backtrace` string
    field is still sent so the server can fall back to it.
    """
    _init(httpserver)
    try:
        raise ValueError("dual format")
    except ValueError as exc:
        errsight.capture_exception(exc)
    errsight.close()

    event = _events(httpserver)[0]
    assert "backtrace" in event
    assert "ValueError" in event["backtrace"]
    assert "exception_frames" in event["metadata"]
