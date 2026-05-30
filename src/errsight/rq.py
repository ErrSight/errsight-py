"""RQ (Redis Queue) integration for ErrSight.

Two install paths:

**1. Drop-in Worker subclass (recommended)** — pushes a per-job scope
and captures failures automatically::

    from rq import Queue
    from errsight.rq import ErrsightWorker
    import errsight

    errsight.init(api_key=...)
    worker = ErrsightWorker([Queue()])
    worker.work()

**2. Attach an exception handler to an existing Worker** — captures
failures without replacing your Worker class::

    from rq import Worker
    from errsight.rq import register_handler

    worker = Worker([Queue()])
    register_handler(worker)
    worker.work()

**Cross-process scope propagation**: stash the current ErrSight scope
in ``job.meta["errsight"]`` when enqueueing, and the worker rebuilds it
on the other side. Same pattern as the Celery integration's task
headers — RQ doesn't have a publisher-side signal, so the caller adds
meta explicitly::

    job = queue.enqueue(
        my_task, arg1, arg2,
        meta={"errsight": errsight.hub.current_scope().to_dict()},
    )
"""
from __future__ import annotations

from typing import Any, Dict, Optional

try:
    from rq import SimpleWorker, Worker
except ImportError as e:  # pragma: no cover - env without rq
    raise ImportError(
        "errsight.rq requires rq to be installed (pip install rq)"
    ) from e

import errsight
from errsight import hub
from errsight.scope import Scope

META_KEY = "errsight"

# Sync-flush budget inside perform_job. Forking workers ``os._exit`` their
# children after perform_job returns, skipping ``atexit`` and the
# background flush thread's drain window — events captured in the child
# would be lost without this explicit drain.
_PERFORM_JOB_FLUSH_TIMEOUT = 2.0


class _ErrsightWorkerMixin:
    """Shared lifecycle wrapper for :class:`ErrsightWorker` and
    :class:`ErrsightSimpleWorker`. Auto-registers the exception handler
    in ``__init__`` and synchronously flushes the event queue at the end
    of every ``perform_job`` so forking workers don't lose captured
    events to ``os._exit``.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # push_exc_handler is provided by the concrete Worker base class
        # we're mixed into; mypy can't see it on the bare mixin.
        self.push_exc_handler(_exc_handler)  # type: ignore[attr-defined]

    def perform_job(self, job: Any, queue: Any) -> Any:
        with errsight.with_scope():
            _populate_scope_from_job(job)
            try:
                # super().perform_job resolves to the Worker base via MRO
                # at runtime; mypy doesn't follow the cooperative-inheritance
                # chain for a base-less mixin.
                return super().perform_job(job, queue)  # type: ignore[misc]
            finally:
                try:
                    errsight.flush(timeout=_PERFORM_JOB_FLUSH_TIMEOUT)
                except Exception:
                    pass


class ErrsightWorker(_ErrsightWorkerMixin, Worker):
    """RQ ``Worker`` subclass that pushes a per-job scope and captures
    failures. Forks per job (the production default).

    Drop-in replacement for ``rq.Worker`` — same constructor, same
    ``.work()`` loop, plus per-job scope push + auto-registered
    exception handler + sync flush before each child exits.
    """

    pass


class ErrsightSimpleWorker(_ErrsightWorkerMixin, SimpleWorker):
    """Non-forking variant of :class:`ErrsightWorker`. Useful for tests
    (no fork → no fakeredis-across-processes weirdness) and for
    dev/local-only setups where each job runs inline in the worker
    process.
    """

    pass


def register_handler(worker: Any) -> None:
    """Attach the ErrSight exception handler to an existing RQ Worker.

    RQ's exception handlers are called when a job raises. We push a
    scope (with publisher context from ``job.meta``), capture the
    exception, then pop the scope. Returns ``True`` so subsequent
    handlers in the chain (RQ's default, which moves the job to the
    failed queue) still run.
    """
    worker.push_exc_handler(_exc_handler)


def _exc_handler(
    job: Any, exc_type: Any, exc_value: Any, traceback: Any
) -> bool:
    if exc_value is None:
        return True
    custom = _scope_from_job(job)
    token = None
    try:
        if custom is not None:
            _scope, token = hub.push_scope(custom)
        # Always set task/job tags so the failure is filterable in the UI
        # even when no publisher scope was attached.
        try:
            tags: Dict[str, str] = {}
            job_id = getattr(job, "id", None)
            if job_id:
                tags["job_id"] = str(job_id)
            func_name = getattr(job, "func_name", None)
            if func_name:
                tags["task"] = str(func_name)
            if tags:
                errsight.set_tags(tags)
        except Exception:
            pass
        try:
            errsight.capture_exception(exc_value, metadata=_job_metadata(job))
        except Exception:
            pass
    finally:
        if token is not None:
            hub.pop_scope(token)
    return True  # propagate to the next handler (RQ's default moves to failed)


def _populate_scope_from_job(job: Any) -> None:
    if job is None:
        return
    try:
        # Apply publisher scope on top of the per-job scope (we're already
        # inside with_scope) via Scope.merge — preserves any process-wide
        # tags set at worker boot.
        custom = _scope_from_job(job)
        if custom is not None:
            hub.current_scope().merge(custom)

        tags: Dict[str, str] = {}
        job_id = getattr(job, "id", None)
        if job_id:
            tags["job_id"] = str(job_id)
        func_name = getattr(job, "func_name", None)
        if func_name:
            tags["task"] = str(func_name)
        if tags:
            errsight.set_tags(tags)
    except Exception:
        pass


def _scope_from_job(job: Any) -> Optional[Scope]:
    if job is None:
        return None
    meta = getattr(job, "meta", None)
    if not isinstance(meta, dict):
        return None
    payload = meta.get(META_KEY)
    if not isinstance(payload, dict):
        return None
    return Scope.from_dict(payload)


def _job_metadata(job: Any) -> Dict[str, Any]:
    md: Dict[str, Any] = {}
    if job is None:
        return md
    try:
        job_id = getattr(job, "id", None)
        if job_id:
            md["job_id"] = str(job_id)
        func_name = getattr(job, "func_name", None)
        if func_name:
            md["task"] = str(func_name)
        args = getattr(job, "args", None)
        if args is not None:
            try:
                md["args"] = list(args)
            except Exception:
                pass
        kwargs = getattr(job, "kwargs", None)
        if isinstance(kwargs, dict):
            md["kwargs"] = dict(kwargs)
        # Retry counter exposed differently across RQ versions.
        retries_left = getattr(job, "retries_left", None)
        if retries_left is not None:
            md["retries_left"] = retries_left
    except Exception:
        pass
    return md
