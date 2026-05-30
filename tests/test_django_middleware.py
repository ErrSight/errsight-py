from __future__ import annotations

import json
from typing import Dict, List

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

from django.http import HttpResponse  # noqa: E402
from django.test import RequestFactory  # noqa: E402

import errsight  # noqa: E402
from errsight import hub  # noqa: E402
from errsight.django import ErrsightMiddleware  # noqa: E402


class FakeUser:
    """Quacks like a Django ``AbstractBaseUser`` without requiring the
    auth app + DB setup that a real ``User`` instance would.
    """

    def __init__(self, pk, username=None, email=None):
        self.pk = pk
        self.id = pk
        self.username = username
        self.email = email
        self.is_authenticated = True


class FakeAnonymous:
    is_authenticated = False


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
def rf():
    return RequestFactory()


def test_middleware_populates_scope_with_user_and_tags(rf):
    request = rf.get("/hello/")
    request.user = FakeUser(pk=42, username="alice", email="a@b.com")

    captured: Dict[str, object] = {}

    def view(req):
        scope = hub.current_scope()
        captured["user"] = dict(scope.user) if scope.user else None
        captured["tags"] = dict(scope.tags)
        return HttpResponse("ok")

    ErrsightMiddleware(get_response=view)(request)

    user = captured["user"]
    assert user["id"] == "42"
    assert user["username"] == "alice"
    assert user["email"] == "a@b.com"
    # RequestFactory defaults REMOTE_ADDR to 127.0.0.1 — the middleware
    # picks it up as the client IP. That's the documented behavior.
    assert user["ip_address"] == "127.0.0.1"
    assert captured["tags"]["request_method"] == "GET"
    assert captured["tags"]["path"] == "/hello/"
    assert captured["tags"]["view"] == "hello-view"


def test_middleware_pops_scope_after_request(rf):
    request = rf.get("/hello/")
    request.user = FakeAnonymous()

    ErrsightMiddleware(get_response=lambda r: HttpResponse("ok"))(request)

    # After __call__, the with-scope block has exited and the root scope
    # is empty again — proves mutations were scoped, not leaking to root.
    assert hub.current_scope().user is None
    assert hub.current_scope().tags == {}


def test_anonymous_user_records_only_ip(rf):
    request = rf.get("/hello/", REMOTE_ADDR="203.0.113.7")
    request.user = FakeAnonymous()

    captured: Dict[str, object] = {}

    def view(req):
        scope = hub.current_scope()
        captured["user"] = dict(scope.user) if scope.user else None
        return HttpResponse("ok")

    ErrsightMiddleware(get_response=view)(request)
    assert captured["user"] == {"ip_address": "203.0.113.7"}


def test_anonymous_user_without_ip_yields_no_user(rf):
    request = rf.get("/hello/")
    # RequestFactory defaults REMOTE_ADDR to "127.0.0.1"; strip it.
    request.META.pop("REMOTE_ADDR", None)
    request.user = FakeAnonymous()

    captured: Dict[str, object] = {}

    def view(req):
        captured["user"] = hub.current_scope().user
        return HttpResponse("ok")

    ErrsightMiddleware(get_response=view)(request)
    assert captured["user"] is None


def test_x_forwarded_for_prefers_leftmost(rf):
    request = rf.get(
        "/hello/",
        HTTP_X_FORWARDED_FOR="198.51.100.1, 10.0.0.1, 10.0.0.2",
        REMOTE_ADDR="10.0.0.99",
    )
    request.user = FakeAnonymous()

    captured: Dict[str, object] = {}

    def view(req):
        captured["user"] = dict(hub.current_scope().user or {})
        return HttpResponse("ok")

    ErrsightMiddleware(get_response=view)(request)
    assert captured["user"]["ip_address"] == "198.51.100.1"


def test_view_name_missing_when_url_does_not_resolve(rf):
    request = rf.get("/no-such-route/")
    request.user = FakeAnonymous()

    captured: Dict[str, object] = {}

    def view(req):
        captured["tags"] = dict(hub.current_scope().tags)
        return HttpResponse("ok")

    ErrsightMiddleware(get_response=view)(request)
    assert "view" not in captured["tags"]
    assert captured["tags"]["path"] == "/no-such-route/"


def test_middleware_survives_request_without_user_attribute(rf):
    request = rf.get("/hello/")
    # No request.user assignment.

    captured: Dict[str, object] = {}

    def view(req):
        captured["user"] = hub.current_scope().user
        captured["tags"] = dict(hub.current_scope().tags)
        return HttpResponse("ok")

    ErrsightMiddleware(get_response=view)(request)

    # The IP-only fallback path still works.
    user = captured["user"]
    assert user is None or "id" not in user
    assert captured["tags"]["request_method"] == "GET"


def test_process_exception_captures_with_active_scope(httpserver, rf):
    """End-to-end: simulate Django's wrapping behavior so process_exception
    runs while the middleware's with-scope block is still active.
    """
    _init(httpserver)
    request = rf.get("/hello/?order_id=42&plan=pro")
    request.user = FakeUser(pk=7, username="bob")

    def raising_view(req):
        raise ValueError("boom")

    # convert_exception_to_response analog: catch the view's exception
    # and call process_exception while __call__ is still paused on
    # `self.get_response(request)` — i.e. scope is still active.
    def wrapped_inner(req):
        try:
            return raising_view(req)
        except Exception as exc:
            middleware.process_exception(req, exc)
            return HttpResponse("error", status=500)

    middleware = ErrsightMiddleware(get_response=wrapped_inner)
    response = middleware(request)
    assert response.status_code == 500

    errsight.close()

    events = _events(httpserver)
    assert len(events) == 1
    event = events[0]
    assert event["user"]["id"] == "7"
    assert event["user"]["username"] == "bob"
    assert event["tags"]["request_method"] == "GET"
    assert event["tags"]["view"] == "hello-view"

    md = event["metadata"]
    assert md["path"] == "/hello/"
    assert md["request_method"] == "GET"
    assert md["full_path"] == "/hello/?order_id=42&plan=pro"
    assert md["query_params"] == {"order_id": "42", "plan": "pro"}
    assert md["view"] == "hello-view"
    assert md["exception_class"] == "ValueError"
    assert "ValueError" in event["message"]
    assert "boom" in event["message"]


def test_process_exception_outside_request_falls_back_to_root_scope(httpserver, rf):
    """If process_exception is called without an active scope (defensive
    edge case), capture still happens — just without request context.
    """
    _init(httpserver)
    request = rf.get("/hello/")
    middleware = ErrsightMiddleware(get_response=lambda r: HttpResponse("ok"))

    try:
        raise ValueError("orphan")
    except ValueError as exc:
        middleware.process_exception(request, exc)

    errsight.close()

    events = _events(httpserver)
    assert len(events) == 1
    md = events[0]["metadata"]
    assert md["path"] == "/hello/"
    assert md["exception_class"] == "ValueError"
