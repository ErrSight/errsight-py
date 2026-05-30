"""Async Django middleware tests.

Covers the ``__acall__`` path: ContextVar isolation under
``asyncio.gather``, async view exception capture (simulating Django's
exception-conversion wrapper), and the ``sync_capable``/``async_capable``
markers Django reads to decide whether to wrap us with adapters.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

import pytest

django = pytest.importorskip("django")

from django.conf import settings  # noqa: E402

if not settings.configured:
    settings.configure(
        DEBUG=False,
        SECRET_KEY="errsight-test-secret",
        USE_TZ=True,
        DATABASES={},
        INSTALLED_APPS=[],
        ROOT_URLCONF="tests._django_urls",
    )
    django.setup()

from asgiref.sync import iscoroutinefunction  # noqa: E402
from django.http import HttpResponse  # noqa: E402
from django.test import RequestFactory  # noqa: E402

import errsight  # noqa: E402
from errsight import hub  # noqa: E402
from errsight.django import ErrsightMiddleware  # noqa: E402


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


class FakeUser:
    def __init__(self, pk, username=None):
        self.pk = pk
        self.id = pk
        self.username = username
        self.is_authenticated = True


@pytest.fixture
def rf():
    return RequestFactory()


def test_middleware_detects_async_get_response_and_marks_itself(rf):
    async def view(request):
        return HttpResponse("ok")

    mw = ErrsightMiddleware(get_response=view)
    assert mw._async_mode is True
    # Django uses iscoroutinefunction(mw) to decide whether to await.
    assert iscoroutinefunction(mw)
    assert mw.sync_capable is True
    assert mw.async_capable is True


def test_middleware_with_sync_get_response_is_not_marked_async(rf):
    def view(request):
        return HttpResponse("ok")

    mw = ErrsightMiddleware(get_response=view)
    assert mw._async_mode is False
    assert not iscoroutinefunction(mw)


def test_async_middleware_populates_scope_during_view(rf):
    captured: Dict[str, Any] = {}

    async def view(request):
        scope = hub.current_scope()
        captured["tags"] = dict(scope.tags)
        captured["user"] = dict(scope.user) if scope.user else None
        return HttpResponse("ok")

    request = rf.get("/hello/")
    request.user = FakeUser(pk=42, username="alice")

    mw = ErrsightMiddleware(get_response=view)
    response = asyncio.run(mw(request))

    assert response.status_code == 200
    assert captured["tags"]["request_method"] == "GET"
    assert captured["tags"]["path"] == "/hello/"
    assert captured["user"]["id"] == "42"
    assert captured["user"]["username"] == "alice"


def test_async_middleware_pops_scope_after_call(rf):
    async def view(request):
        return HttpResponse("ok")

    mw = ErrsightMiddleware(get_response=view)
    asyncio.run(mw(rf.get("/")))
    # Scope was pushed inside `async with` and popped on exit.
    assert hub.current_scope().user is None
    assert hub.current_scope().tags == {}


def test_async_process_exception_captures_with_active_scope(httpserver, rf):
    """Simulate Django's exception-conversion wrapper: the view raises,
    the wrapper catches and calls process_exception while our async with
    block is still active (paused inside ``await self.get_response``).
    """
    _init(httpserver)

    async def raising_view(request):
        raise ValueError("async boom")

    async def wrapped_inner(request):
        try:
            return await raising_view(request)
        except Exception as exc:
            mw.process_exception(request, exc)
            return HttpResponse("err", status=500)

    request = rf.get("/boom/?order_id=42")
    request.user = FakeUser(pk=7, username="bob")

    mw = ErrsightMiddleware(get_response=wrapped_inner)
    response = asyncio.run(mw(request))
    assert response.status_code == 500

    errsight.close()
    event = _events(httpserver)[0]
    assert event["user"]["username"] == "bob"
    assert event["tags"]["request_method"] == "GET"
    md = event["metadata"]
    assert md["path"] == "/boom/"
    assert md["query_params"] == {"order_id": "42"}
    assert md["exception_class"] == "ValueError"


def test_async_concurrent_requests_have_isolated_scopes(rf):
    """The async-Django version of the contextvars isolation test.
    Three concurrent ``asyncio.Task`` instances, each setting a different
    user, each yielding mid-handler. All three must see their own user.
    """
    captured: Dict[str, Dict[str, Any]] = {}

    def make_view(name, delay):
        async def view(request):
            errsight.set_user({"id": name})
            await asyncio.sleep(delay)  # yield to other tasks
            scope = hub.current_scope()
            captured[name] = {
                "user": dict(scope.user or {}),
                "tags": dict(scope.tags),
            }
            return HttpResponse(name)
        return view

    async def main():
        mws = [
            (ErrsightMiddleware(get_response=make_view(name, delay)), rf.get(f"/{name}/"))
            for name, delay in [("alice", 0.02), ("bob", 0.005), ("carol", 0.015)]
        ]
        await asyncio.gather(*(mw(req) for mw, req in mws))

    asyncio.run(main())

    assert captured["alice"]["user"]["id"] == "alice"
    assert captured["bob"]["user"]["id"] == "bob"
    assert captured["carol"]["user"]["id"] == "carol"
