from __future__ import annotations

import json
import logging
import uuid
from typing import Dict, List

import pytest

import errsight
from errsight.logging_handler import LoggingHandler


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


@pytest.fixture
def isolated_logger():
    """A fresh logger per test with no inherited handlers; doesn't propagate
    to the root logger to avoid polluting pytest's capture machinery.
    """
    name = f"errsight_test_{uuid.uuid4().hex}"
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.handlers.clear()
    yield logger
    logger.handlers.clear()


def test_logger_error_ships_event(httpserver, isolated_logger):
    _init(httpserver)
    isolated_logger.addHandler(LoggingHandler(level=logging.WARNING))

    isolated_logger.error("payment failed")
    errsight.close()

    event = _events(httpserver)[0]
    assert event["level"] == "error"
    assert event["message"] == "payment failed"
    assert event["metadata"]["logger"] == isolated_logger.name


def test_log_level_mapping(httpserver, isolated_logger):
    _init(httpserver)
    isolated_logger.addHandler(LoggingHandler(level=logging.DEBUG))

    isolated_logger.debug("d")
    isolated_logger.info("i")
    isolated_logger.warning("w")
    isolated_logger.error("e")
    isolated_logger.critical("c")
    errsight.close()

    levels = [e["level"] for e in _events(httpserver)]
    assert levels == ["debug", "info", "warning", "error", "fatal"]


def test_handler_level_filter_drops_records_below_threshold(httpserver, isolated_logger):
    _init(httpserver)
    isolated_logger.addHandler(LoggingHandler(level=logging.WARNING))

    isolated_logger.debug("dropped")
    isolated_logger.info("dropped")
    isolated_logger.warning("kept")
    isolated_logger.error("kept")
    errsight.close()

    messages = [e["message"] for e in _events(httpserver)]
    assert messages == ["kept", "kept"]


def test_record_args_are_formatted_into_message(httpserver, isolated_logger):
    _init(httpserver)
    isolated_logger.addHandler(LoggingHandler(level=logging.DEBUG))

    isolated_logger.error("order %d failed for %s", 42, "alice")
    errsight.close()

    assert _events(httpserver)[0]["message"] == "order 42 failed for alice"


def test_extra_kwargs_become_metadata(httpserver, isolated_logger):
    _init(httpserver)
    isolated_logger.addHandler(LoggingHandler(level=logging.DEBUG))

    isolated_logger.error(
        "payment failed", extra={"order_id": 42, "user_id": "alice"}
    )
    errsight.close()

    metadata = _events(httpserver)[0]["metadata"]
    assert metadata["order_id"] == 42
    assert metadata["user_id"] == "alice"


def test_errsight_override_sets_user_tags_fingerprint(httpserver, isolated_logger):
    _init(httpserver)
    isolated_logger.addHandler(LoggingHandler(level=logging.DEBUG))

    isolated_logger.error(
        "checkout failed",
        extra={
            "errsight": {
                "user": {"id": "alice"},
                "tags": {"gateway": "stripe"},
                "fingerprint": "checkout-stripe",
            },
        },
    )
    errsight.close()

    event = _events(httpserver)[0]
    assert event["user"] == {"id": "alice"}
    assert event["tags"]["gateway"] == "stripe"
    assert event["fingerprint"] == "checkout-stripe"
    # The override key itself doesn't leak into metadata.
    assert "errsight" not in event["metadata"]


def test_logger_exception_routes_through_capture(httpserver, isolated_logger):
    _init(httpserver)
    isolated_logger.addHandler(LoggingHandler(level=logging.DEBUG))

    try:
        raise ValueError("boom")
    except ValueError:
        isolated_logger.exception("explaining the boom")
    errsight.close()

    event = _events(httpserver)[0]
    assert event["level"] == "error"
    # capture_exception sets message to "<Cls>: <msg>"; the log call's
    # message lives in metadata.log_message so neither is lost.
    assert "ValueError" in event["message"]
    assert "boom" in event["message"]
    assert event["metadata"]["log_message"] == "explaining the boom"
    # Structured frames + source context come for free via capture_exception.
    assert "exception_frames" in event["metadata"]
    in_app = [f for f in event["metadata"]["exception_frames"] if f.get("in_app")]
    assert in_app, "expected an in_app frame for source context"
    assert "context_line" in in_app[-1]


def test_recursion_guard_breaks_infinite_loop(httpserver, isolated_logger):
    """If the SDK re-enters emit() mid-flight, the guard must short-circuit.

    Simulate by monkey-patching errsight.log to itself log via the handler.
    Without the guard, this would be an unbounded loop.
    """
    _init(httpserver)
    isolated_logger.addHandler(LoggingHandler(level=logging.DEBUG))

    original_log = errsight.log

    def re_entering(**kwargs):
        # Inside emit(), trigger another emit() — the guard returns early.
        isolated_logger.error("re-entry attempt")
        return original_log(**kwargs)

    errsight.log = re_entering  # type: ignore[assignment]
    try:
        isolated_logger.error("original")
    finally:
        errsight.log = original_log  # type: ignore[assignment]

    errsight.close()

    messages = [e["message"] for e in _events(httpserver)]
    assert "original" in messages
    assert "re-entry attempt" not in messages


def test_attach_logging_handler_returns_handler():
    handler = errsight.attach_logging_handler(level=logging.WARNING)
    try:
        assert isinstance(handler, LoggingHandler)
        assert handler in logging.getLogger().handlers
    finally:
        logging.getLogger().removeHandler(handler)


def test_attach_logging_handler_to_specific_logger(isolated_logger):
    handler = errsight.attach_logging_handler(
        level=logging.WARNING, logger=isolated_logger
    )
    assert handler in isolated_logger.handlers
    assert handler not in logging.getLogger().handlers


def test_attach_to_logging_config_auto_attaches_on_init(httpserver):
    _init(httpserver, attach_to_logging=True)

    root_handlers = [
        h for h in logging.getLogger().handlers if isinstance(h, LoggingHandler)
    ]
    assert len(root_handlers) == 1

    logging.getLogger().error("from root")
    errsight.close()

    # close() must detach the auto-attached handler so the root logger
    # doesn't keep an orphaned handler around after shutdown.
    remaining = [
        h for h in logging.getLogger().handlers if isinstance(h, LoggingHandler)
    ]
    assert remaining == []

    messages = [e["message"] for e in _events(httpserver)]
    assert "from root" in messages


def test_re_init_replaces_auto_handler_without_accumulation(httpserver):
    _init(httpserver, attach_to_logging=True)
    first = [h for h in logging.getLogger().handlers if isinstance(h, LoggingHandler)]
    assert len(first) == 1

    _init(httpserver, attach_to_logging=True)
    second = [h for h in logging.getLogger().handlers if isinstance(h, LoggingHandler)]
    assert len(second) == 1
    assert second[0] is not first[0]

    errsight.close()


def test_attach_to_logging_false_does_not_auto_attach(httpserver):
    _init(httpserver, attach_to_logging=False)
    attached = [
        h for h in logging.getLogger().handlers if isinstance(h, LoggingHandler)
    ]
    assert attached == []
    errsight.close()


def test_handler_without_init_is_a_noop(isolated_logger):
    """Adding the handler before init() must not raise. Records just drop
    (errsight.log is a no-op when transport isn't configured).
    """
    isolated_logger.addHandler(LoggingHandler(level=logging.DEBUG))
    isolated_logger.error("ignored")
    isolated_logger.exception("ignored", exc_info=False)
