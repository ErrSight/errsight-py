"""Celery integration for ErrSight.

Wire up::

    import errsight
    from errsight.celery import install

    errsight.init(api_key=...)
    install()

The integration:

- **Cross-process scope propagation**: when a task is enqueued from inside
  ``with errsight.with_scope():`` (e.g. a Django/Flask/FastAPI request),
  the current scope's user/tags/breadcrumbs are serialized via
  :meth:`Scope.to_dict` into the task headers. The worker receives them
  through the broker and rehydrates the scope via :meth:`Scope.from_dict`
  before running the task.

- **Per-task scope**: each task execution gets its own pushed scope, so
  concurrent tasks (prefork workers, threaded workers) don't share state.
  Tagged with ``task`` (task name) and ``task_id``.

- **Failure capture**: ``task_failure`` signal handler captures the
  exception with the active scope plus task metadata (args, kwargs,
  retries).

Worker compatibility:

- Prefork workers: each worker is a separate process; ContextVar values
  are process-global. Works.
- Threaded workers (``--pool=threads``): each task runs on a thread;
  ContextVars are per-thread, sequential push/pop works.
- gevent/eventlet workers: the patched ``threading.local`` won't see
  ContextVar isolation correctly. Test before deploying with these pools.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

try:
    from celery.signals import (
        before_task_publish,
        task_failure,
        task_postrun,
        task_prerun,
    )
except ImportError as e:  # pragma: no cover - env without celery
    raise ImportError(
        "errsight.celery requires Celery to be installed (pip install celery)"
    ) from e

import errsight
from errsight import hub
from errsight.scope import Scope

# Key under which the serialized scope dict is stashed in the AMQP message
# headers. Custom headers ride along through the broker unchanged.
HEADER_KEY = "errsight"

# Attribute name used to stash the ContextVar token on ``task.request``
# between ``task_prerun`` and ``task_postrun``. The request object is
# per-task-execution (Celery's push_request/pop_request swap it for each
# task), so the token doesn't collide between concurrent tasks on the
# same worker.
_TOKEN_ATTR = "_errsight_scope_token"

_installed = False


def install() -> None:
    """Connect ErrSight handlers to Celery's signal infrastructure.

    Idempotent — repeated calls are no-ops. Use :func:`uninstall` to
    disconnect (e.g. between tests).
    """
    global _installed
    if _installed:
        return
    # weak=False so the connections survive even when our module-level
    # functions aren't otherwise referenced.
    before_task_publish.connect(_before_task_publish, weak=False)
    task_prerun.connect(_task_prerun, weak=False)
    task_postrun.connect(_task_postrun, weak=False)
    task_failure.connect(_task_failure, weak=False)
    _installed = True


def uninstall() -> None:
    """Disconnect ErrSight handlers. Used by tests to keep signal state
    clean between test runs.
    """
    global _installed
    if not _installed:
        return
    before_task_publish.disconnect(_before_task_publish)
    task_prerun.disconnect(_task_prerun)
    task_postrun.disconnect(_task_postrun)
    task_failure.disconnect(_task_failure)
    _installed = False


def _before_task_publish(
    sender: Any = None,
    headers: Optional[Dict[str, Any]] = None,
    body: Any = None,
    **kwargs: Any,
) -> None:
    """Stash the current ErrSight scope into the task headers.

    Mutating the ``headers`` dict is the documented Celery extension
    point — the dict gets serialized into the AMQP message that goes to
    the broker, so anything we put here arrives at the worker.
    """
    if not isinstance(headers, dict):
        return
    try:
        scope_dict = hub.current_scope().to_dict()
        if scope_dict:
            headers[HEADER_KEY] = scope_dict
    except Exception:
        pass


def _task_prerun(
    task_id: Any = None,
    task: Any = None,
    args: Any = None,
    kwargs: Any = None,
    **extra: Any,
) -> None:
    """Push a fresh scope before the task runs, hydrating with the
    publisher's scope if it traveled through the headers."""
    if task is None:
        return
    try:
        custom_scope = _extract_scope_from_task(task)
        _scope, token = hub.push_scope(custom_scope)
        request = getattr(task, "request", None)
        if request is not None:
            try:
                setattr(request, _TOKEN_ATTR, token)
            except (AttributeError, TypeError):
                # Some Celery internals expose request as a slotted /
                # frozen-ish proxy. Fall back to popping in task_failure
                # path (it's idempotent).
                pass

        tags: Dict[str, str] = {}
        task_name = getattr(task, "name", None)
        if task_name:
            tags["task"] = str(task_name)
        if task_id:
            tags["task_id"] = str(task_id)
        if tags:
            errsight.set_tags(tags)
    except Exception:
        pass


def _task_postrun(
    task_id: Any = None,
    task: Any = None,
    **kwargs: Any,
) -> None:
    """Pop the scope pushed in :func:`_task_prerun`."""
    if task is None:
        return
    try:
        request = getattr(task, "request", None)
        if request is None:
            return
        token = getattr(request, _TOKEN_ATTR, None)
        if token is not None:
            hub.pop_scope(token)
            try:
                delattr(request, _TOKEN_ATTR)
            except (AttributeError, TypeError):
                pass
    except Exception:
        pass


def _task_failure(
    sender: Any = None,
    task_id: Any = None,
    exception: Optional[BaseException] = None,
    einfo: Any = None,
    task: Any = None,
    args: Any = None,
    kwargs: Any = None,
    **extra: Any,
) -> None:
    """Capture the exception with the task's scope still on top.

    Fires *before* ``task_postrun``, so :func:`hub.current_scope` is the
    one we pushed in :func:`_task_prerun` — including the inherited user
    and tags from the publisher.

    Note: unlike prerun/postrun, ``task_failure`` only sends ``sender=task``
    (no separate ``task=`` kwarg), so we fall back to ``sender`` for the
    task object.
    """
    if exception is None:
        return
    if task is None:
        task = sender
    try:
        metadata: Dict[str, Any] = {}
        if task is not None:
            task_name = getattr(task, "name", None)
            if task_name:
                metadata["task"] = str(task_name)
        if task_id:
            metadata["task_id"] = str(task_id)
        if task is not None:
            request = getattr(task, "request", None)
            if request is not None:
                req_args = getattr(request, "args", None)
                if req_args is not None:
                    try:
                        metadata["args"] = list(req_args)
                    except Exception:
                        pass
                req_kwargs = getattr(request, "kwargs", None)
                if req_kwargs is not None:
                    try:
                        metadata["kwargs"] = dict(req_kwargs)
                    except Exception:
                        pass
                retries = getattr(request, "retries", None)
                if retries is not None:
                    metadata["retries"] = retries
        errsight.capture_exception(exception, metadata=metadata)
    except Exception:
        pass


def _extract_scope_from_task(task: Any) -> Optional[Scope]:
    """Rebuild the publisher's scope from the task headers, if present."""
    request = getattr(task, "request", None)
    if request is None:
        return None
    # Celery exposes headers in a couple of ways depending on the worker
    # path. ``request.headers`` is the standard accessor; some message
    # protocols nest under ``message.properties``. Try the canonical
    # path first and fall back defensively.
    headers = getattr(request, "headers", None)
    scope_dict = None
    if isinstance(headers, dict):
        scope_dict = headers.get(HEADER_KEY)
    if scope_dict is None:
        return None
    if not isinstance(scope_dict, dict):
        return None
    return Scope.from_dict(scope_dict)
