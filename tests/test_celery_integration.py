from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

celery = pytest.importorskip("celery")

from celery import Celery  # noqa: E402

import errsight  # noqa: E402
from errsight import hub  # noqa: E402
from errsight.celery import (  # noqa: E402
    HEADER_KEY,
    _before_task_publish,
    _task_failure,
    _task_postrun,
    _task_prerun,
    install,
    uninstall,
)
from errsight.scope import Scope  # noqa: E402


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


@pytest.fixture
def fake_task():
    """A minimal Celery-Task-shaped object for direct signal-handler calls."""

    class _FakeRequest:
        def __init__(self):
            self.headers: Dict[str, Any] = {}
            self.args = ()
            self.kwargs = {}
            self.retries = 0

    class _FakeTask:
        def __init__(self, name="tests.fake_task"):
            self.name = name
            self.request = _FakeRequest()

    return _FakeTask()


# ---- Unit tests: signal handlers as plain functions -----------------------


def test_before_task_publish_stashes_scope_in_headers():
    with errsight.with_scope():
        errsight.set_user({"id": "alice", "email": "a@x.com"})
        errsight.set_tag("region", "us-east")
        errsight.add_breadcrumb(category="ui", message="enqueueing")

        headers: Dict[str, Any] = {}
        _before_task_publish(sender="tests.task", headers=headers, body=None)

        payload = headers.get(HEADER_KEY)
        assert isinstance(payload, dict)
        assert payload["user"]["id"] == "alice"
        assert payload["tags"]["region"] == "us-east"
        assert any(c["message"] == "enqueueing" for c in payload["breadcrumbs"])


def test_before_task_publish_skips_when_scope_is_empty():
    headers: Dict[str, Any] = {}
    _before_task_publish(sender="tests.task", headers=headers, body=None)
    # Empty scope produces an empty dict; don't pollute headers.
    assert HEADER_KEY not in headers


def test_before_task_publish_tolerates_missing_headers():
    _before_task_publish(sender="tests.task", headers=None, body=None)  # must not raise


def test_task_prerun_rehydrates_publisher_scope(fake_task):
    # Simulate the message arriving at the worker with the publisher's scope
    # already in headers.
    publisher_scope = Scope()
    publisher_scope.set_user({"id": "bob"})
    publisher_scope.set_tag("source", "api")
    publisher_scope.add_breadcrumb(category="ui", message="enqueued")
    fake_task.request.headers[HEADER_KEY] = publisher_scope.to_dict()

    _task_prerun(task_id="task-123", task=fake_task)

    current = hub.current_scope()
    assert current.user == {"id": "bob"}
    assert current.tags["source"] == "api"
    assert current.tags["task"] == "tests.fake_task"
    assert current.tags["task_id"] == "task-123"
    assert any(c["message"] == "enqueued" for c in current.user_breadcrumbs)

    _task_postrun(task_id="task-123", task=fake_task)
    # After postrun, scope is popped — no user from publisher leaks.
    assert hub.current_scope().user is None


def test_task_prerun_without_publisher_scope_still_pushes_a_clean_one(fake_task):
    _task_prerun(task_id="task-no-pub", task=fake_task)
    current = hub.current_scope()
    # No publisher scope → root copy + task tags.
    assert current.user is None
    assert current.tags["task"] == "tests.fake_task"
    assert current.tags["task_id"] == "task-no-pub"
    _task_postrun(task_id="task-no-pub", task=fake_task)


def test_task_failure_captures_with_task_metadata(httpserver, fake_task):
    _init(httpserver)
    fake_task.request.args = (1, 2, 3)
    fake_task.request.kwargs = {"order_id": 42}
    fake_task.request.retries = 1

    _task_prerun(task_id="task-fail", task=fake_task)
    try:
        raise ValueError("task boom")
    except ValueError as exc:
        _task_failure(task_id="task-fail", exception=exc, task=fake_task)
    _task_postrun(task_id="task-fail", task=fake_task)
    errsight.close()

    event = _events(httpserver)[0]
    assert event["tags"]["task"] == "tests.fake_task"
    assert event["tags"]["task_id"] == "task-fail"
    md = event["metadata"]
    assert md["task"] == "tests.fake_task"
    assert md["task_id"] == "task-fail"
    assert md["args"] == [1, 2, 3]
    assert md["kwargs"] == {"order_id": 42}
    assert md["retries"] == 1
    assert md["exception_class"] == "ValueError"


def test_task_failure_inherits_publisher_user(httpserver, fake_task):
    """The whole point of the headers round-trip: a task that fails on
    the worker should attribute the error to the user who triggered it
    on the API side.
    """
    _init(httpserver)
    publisher = Scope()
    publisher.set_user({"id": "alice", "email": "a@x.com"})
    fake_task.request.headers[HEADER_KEY] = publisher.to_dict()

    _task_prerun(task_id="t-1", task=fake_task)
    try:
        raise RuntimeError("worker exploded")
    except RuntimeError as exc:
        _task_failure(task_id="t-1", exception=exc, task=fake_task)
    _task_postrun(task_id="t-1", task=fake_task)
    errsight.close()

    event = _events(httpserver)[0]
    assert event["user"] == {"id": "alice", "email": "a@x.com"}


def test_install_is_idempotent():
    install()
    install()
    uninstall()
    # Calling uninstall twice is also safe.
    uninstall()


# ---- End-to-end: Celery eager mode -----------------------------------------


@pytest.fixture
def celery_app():
    """A Celery app in eager mode — tasks execute inline in the caller.

    Note: ``before_task_publish`` does NOT fire in eager mode (no actual
    publish happens). The handler-level test above covers that path; this
    fixture exercises the prerun/postrun/failure path through real Celery
    machinery.
    """
    app = Celery("errsight_test")
    app.conf.update(
        task_always_eager=True,
        task_eager_propagates=False,
        broker_url="memory://",
        result_backend="cache+memory://",
    )
    install()
    yield app
    uninstall()


def test_celery_eager_task_failure_captured_end_to_end(httpserver, celery_app):
    _init(httpserver)

    @celery_app.task(name="tests.boom")
    def boom():
        raise ValueError("real celery failure")

    boom.apply()
    errsight.close()

    events = _events(httpserver)
    assert len(events) == 1
    event = events[0]
    assert event["tags"]["task"] == "tests.boom"
    assert event["metadata"]["task"] == "tests.boom"
    assert event["metadata"]["exception_class"] == "ValueError"
    assert "real celery failure" in event["message"]


def test_celery_eager_successful_task_emits_no_event(httpserver, celery_app):
    _init(httpserver)

    @celery_app.task(name="tests.add")
    def add(x, y):
        return x + y

    result = add.apply(args=(2, 3))
    assert result.result == 5

    errsight.close()
    assert _events(httpserver) == []
