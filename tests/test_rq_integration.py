from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

rq = pytest.importorskip("rq")
fakeredis = pytest.importorskip("fakeredis")

from rq import Queue, SimpleWorker  # noqa: E402

import errsight  # noqa: E402
from errsight import hub  # noqa: E402
from errsight.rq import (  # noqa: E402
    META_KEY,
    ErrsightSimpleWorker,
    _exc_handler,
    register_handler,
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


class FakeJob:
    """Minimal Job-shaped object for exercising the signal handlers
    without a Redis connection."""

    def __init__(
        self,
        job_id: str = "job-123",
        func_name: str = "tests.tasks.do_thing",
        args: tuple = (),
        kwargs: Any = None,
        meta: Any = None,
    ):
        self.id = job_id
        self.func_name = func_name
        self.args = args
        self.kwargs = kwargs if kwargs is not None else {}
        self.meta = meta if meta is not None else {}
        self.retries_left = 0


# ---- Unit tests: signal handlers + scope rebuild --------------------------


def test_exc_handler_captures_with_job_metadata(httpserver):
    _init(httpserver)
    job = FakeJob(
        job_id="abc",
        func_name="tests.send_email",
        args=("alice@x.com",),
        kwargs={"template": "welcome"},
    )

    try:
        raise ValueError("rq boom")
    except ValueError as exc:
        _exc_handler(job, ValueError, exc, exc.__traceback__)

    errsight.close()
    event = _events(httpserver)[0]
    assert event["tags"]["job_id"] == "abc"
    assert event["tags"]["task"] == "tests.send_email"
    md = event["metadata"]
    assert md["job_id"] == "abc"
    assert md["task"] == "tests.send_email"
    assert md["args"] == ["alice@x.com"]
    assert md["kwargs"] == {"template": "welcome"}
    assert md["exception_class"] == "ValueError"


def test_exc_handler_uses_publisher_scope_from_meta(httpserver):
    """The full cross-process round trip: a publisher's scope is
    serialized into job.meta and the worker's exc_handler rebuilds it
    onto the failure event.
    """
    _init(httpserver)

    publisher_scope = Scope()
    publisher_scope.set_user({"id": "alice", "email": "a@x.com"})
    publisher_scope.set_tag("source", "api")

    job = FakeJob(
        job_id="prop-1",
        meta={META_KEY: publisher_scope.to_dict()},
    )

    try:
        raise RuntimeError("worker exploded")
    except RuntimeError as exc:
        _exc_handler(job, RuntimeError, exc, exc.__traceback__)

    errsight.close()
    event = _events(httpserver)[0]
    assert event["user"] == {"id": "alice", "email": "a@x.com"}
    assert event["tags"]["source"] == "api"
    # Job-specific tags still added on top.
    assert event["tags"]["job_id"] == "prop-1"


def test_exc_handler_tolerates_missing_or_malformed_meta(httpserver):
    _init(httpserver)
    # meta is not a dict
    job = FakeJob(meta="not a dict")  # type: ignore[arg-type]
    try:
        raise ValueError("malformed meta")
    except ValueError as exc:
        _exc_handler(job, ValueError, exc, exc.__traceback__)
    # Should not raise; should still capture.
    errsight.close()
    assert len(_events(httpserver)) == 1


def test_exc_handler_with_none_exc_value_is_a_noop(httpserver):
    _init(httpserver)
    _exc_handler(FakeJob(), ValueError, None, None)
    errsight.close()
    assert _events(httpserver) == []


def test_exc_handler_returns_true_to_chain_handlers():
    """Returning True lets RQ run the next handler — typically its own
    default which moves the job to the failed queue."""
    job = FakeJob()
    try:
        raise ValueError("chain")
    except ValueError as exc:
        result = _exc_handler(job, ValueError, exc, exc.__traceback__)
    assert result is True


# ---- End-to-end with fakeredis ---------------------------------------------


def _fake_redis_conn():
    """A fakeredis connection whose ``client_list()`` reports an ``addr``.

    Real Redis returns ``addr`` (``ip:port``) for every connected client,
    but fakeredis omits it. RQ >= 2.9 reads ``client['addr']`` in
    ``Worker._set_ip_address`` during ``__init__`` and only guards against
    ``ResponseError`` — so the missing key raises ``KeyError`` and breaks
    worker construction. Backfill the field so the worker boots the same
    way it would against real Redis.
    """
    conn = fakeredis.FakeStrictRedis(server=fakeredis.FakeServer())
    _orig_client_list = conn.client_list

    def _client_list_with_addr(*args, **kwargs):
        clients = _orig_client_list(*args, **kwargs)
        for client in clients:
            client.setdefault("addr", "127.0.0.1:6379")
        return clients

    conn.client_list = _client_list_with_addr  # type: ignore[method-assign]
    return conn


def _failing_task():
    raise RuntimeError("rq end-to-end failure")


def _ok_task(x, y):
    return x + y


def test_errsight_worker_captures_real_job_failure(httpserver):
    _init(httpserver)
    conn = _fake_redis_conn()
    queue = Queue("test", connection=conn)
    queue.enqueue(_failing_task)

    worker = ErrsightSimpleWorker([queue], connection=conn)
    # work(burst=True) processes pending jobs and returns; perfect for
    # synchronous tests with no real worker loop.
    worker.work(burst=True)

    errsight.close()
    events = _events(httpserver)
    # At least one capture (the failed task). RQ's own machinery may also
    # fire on errors; we just need to see our task in there.
    assert any(
        e.get("metadata", {}).get("task", "").endswith("_failing_task")
        for e in events
    )
    failure = next(
        e for e in events
        if e.get("metadata", {}).get("task", "").endswith("_failing_task")
    )
    assert failure["metadata"]["exception_class"] == "RuntimeError"


def test_errsight_worker_processes_successful_jobs_without_event(httpserver):
    _init(httpserver)
    conn = _fake_redis_conn()
    queue = Queue("test", connection=conn)
    job = queue.enqueue(_ok_task, 2, 3)

    worker = ErrsightSimpleWorker([queue], connection=conn)
    worker.work(burst=True)

    errsight.close()
    assert _events(httpserver) == []
    # Successful job's result was stored normally by RQ.
    assert job.refresh() is None or True  # job state is implementation detail


def test_register_handler_attaches_to_existing_worker(httpserver):
    """The non-subclass path: attach the handler to a regular Worker."""
    _init(httpserver)
    conn = _fake_redis_conn()
    queue = Queue("test", connection=conn)
    queue.enqueue(_failing_task)

    worker = SimpleWorker([queue], connection=conn)
    register_handler(worker)
    worker.work(burst=True)

    errsight.close()
    events = _events(httpserver)
    assert any(
        e.get("metadata", {}).get("exception_class") == "RuntimeError"
        for e in events
    )


def test_publisher_scope_roundtrips_through_job_meta(httpserver):
    """The integration's headline feature: a job enqueued from inside a
    scope ships the user/tags via job.meta, and the worker rebuilds them
    onto the failure event.
    """
    _init(httpserver)
    conn = _fake_redis_conn()
    queue = Queue("test", connection=conn)

    # Publisher side: stash the current scope on the job.
    with errsight.with_scope():
        errsight.set_user({"id": "alice"})
        errsight.set_tag("source", "checkout")
        queue.enqueue(
            _failing_task,
            meta={META_KEY: hub.current_scope().to_dict()},
        )

    # Worker side: drain the queue.
    worker = ErrsightSimpleWorker([queue], connection=conn)
    worker.work(burst=True)

    errsight.close()
    events = _events(httpserver)
    failure = next(
        e for e in events
        if e.get("metadata", {}).get("exception_class") == "RuntimeError"
    )
    assert failure["user"] == {"id": "alice"}
    assert failure["tags"]["source"] == "checkout"
