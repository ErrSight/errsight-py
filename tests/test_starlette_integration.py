from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

import pytest

starlette = pytest.importorskip("starlette")
httpx = pytest.importorskip("httpx")

from starlette.applications import Starlette  # noqa: E402
from starlette.authentication import (  # noqa: E402
    AuthCredentials,
    AuthenticationBackend,
    BaseUser,
)
from starlette.exceptions import HTTPException  # noqa: E402
from starlette.middleware import Middleware  # noqa: E402
from starlette.middleware.authentication import AuthenticationMiddleware  # noqa: E402
from starlette.responses import PlainTextResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import errsight  # noqa: E402
from errsight import hub  # noqa: E402
from errsight.starlette import ErrsightMiddleware  # noqa: E402


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


class _CapturingUser(BaseUser):
    def __init__(self, identity: str, name: str, email: str) -> None:
        self._id = identity
        self._name = name
        self.email = email

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def display_name(self) -> str:
        return self._name

    @property
    def identity(self) -> str:
        return self._id


class _AuthBackend(AuthenticationBackend):
    """Authenticates anything; returns a fixed test user."""

    async def authenticate(self, conn):
        return AuthCredentials(["authenticated"]), _CapturingUser(
            "user-7", "bob", "b@x.com"
        )


def test_starlette_request_populates_tags_and_ip(httpserver):
    _init(httpserver)
    captured: Dict[str, Any] = {}

    async def view(request):
        scope = hub.current_scope()
        captured["tags"] = dict(scope.tags)
        captured["user"] = dict(scope.user) if scope.user else None
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/hello", view)])
    app.add_middleware(ErrsightMiddleware)

    with TestClient(app) as client:
        resp = client.get("/hello")
        assert resp.status_code == 200

    assert captured["tags"]["request_method"] == "GET"
    assert captured["tags"]["path"] == "/hello"
    assert captured["user"]["ip_address"] == "testclient"


def test_starlette_x_forwarded_for_takes_precedence(httpserver):
    _init(httpserver)
    captured: Dict[str, Any] = {}

    async def view(request):
        captured["user"] = dict(hub.current_scope().user or {})
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", view)])
    app.add_middleware(ErrsightMiddleware)

    with TestClient(app) as client:
        client.get("/", headers={"X-Forwarded-For": "198.51.100.1, 10.0.0.1"})

    assert captured["user"]["ip_address"] == "198.51.100.1"


def test_starlette_view_exception_is_captured(httpserver):
    _init(httpserver)

    async def view(request):
        raise ValueError("kaboom")

    app = Starlette(routes=[Route("/boom", view, name="boom-route")])
    app.add_middleware(ErrsightMiddleware)

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/boom?order_id=42&plan=pro")
        assert resp.status_code == 500

    errsight.close()
    events = _events(httpserver)
    assert len(events) == 1
    event = events[0]
    assert event["tags"]["request_method"] == "GET"
    assert event["tags"]["path"] == "/boom"
    assert event["tags"]["endpoint"] == "boom-route"

    md = event["metadata"]
    assert md["path"] == "/boom"
    assert md["request_method"] == "GET"
    assert md["query_string"] == "order_id=42&plan=pro"
    assert md["endpoint"] == "boom-route"
    assert md["exception_class"] == "ValueError"
    assert "ValueError" in event["message"]
    assert "kaboom" in event["message"]


def test_starlette_http_exception_is_not_captured(httpserver):
    """4xx HTTPExceptions are normal app flow; don't ship them as errors."""
    _init(httpserver)

    async def view(request):
        raise HTTPException(status_code=404, detail="Not Found")

    app = Starlette(routes=[Route("/missing", view)])
    app.add_middleware(ErrsightMiddleware)

    with TestClient(app) as client:
        resp = client.get("/missing")
        assert resp.status_code == 404

    errsight.close()
    assert _events(httpserver) == []


def test_starlette_authenticated_user_attached_on_exception(httpserver):
    """AuthenticationMiddleware runs *after* ErrsightMiddleware enters but
    *before* the view executes. By exception time, scope['user'] is
    populated and we late-bind it onto the event.
    """
    _init(httpserver)

    async def view(request):
        raise ValueError("auth-tagged failure")

    app = Starlette(
        routes=[Route("/boom", view)],
        middleware=[
            Middleware(ErrsightMiddleware),
            Middleware(AuthenticationMiddleware, backend=_AuthBackend()),
        ],
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/boom")
        assert resp.status_code == 500

    errsight.close()
    event = _events(httpserver)[0]
    assert event["user"]["id"] == "user-7"
    assert event["user"]["username"] == "bob"
    assert event["user"]["email"] == "b@x.com"
    # IP was preserved from the early-bound anonymous user.
    assert "ip_address" in event["user"]


def test_starlette_view_set_user_wins_over_auth(httpserver):
    """A view that calls ``errsight.set_user({"id": ...})`` should win
    against the AuthenticationMiddleware-derived user on capture."""
    _init(httpserver)

    async def view(request):
        errsight.set_user({"id": "view-override", "username": "alice"})
        raise ValueError("view set user")

    app = Starlette(
        routes=[Route("/", view)],
        middleware=[
            Middleware(ErrsightMiddleware),
            Middleware(AuthenticationMiddleware, backend=_AuthBackend()),
        ],
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        client.get("/")

    errsight.close()
    event = _events(httpserver)[0]
    assert event["user"]["id"] == "view-override"
    assert event["user"]["username"] == "alice"


def test_starlette_websocket_scope_passes_through(httpserver):
    """Non-HTTP scopes (websocket, lifespan) must pass through unchanged."""
    _init(httpserver)

    received_types: List[str] = []

    async def view(request):  # for routing
        return PlainTextResponse("ok")

    async def inner_app(scope, receive, send):
        received_types.append(scope["type"])
        if scope["type"] == "http":
            await view(None)  # placeholder; we just check the middleware path
            resp = PlainTextResponse("ok")
            await resp(scope, receive, send)

    middleware = ErrsightMiddleware(inner_app)

    async def run():
        ws_scope = {"type": "websocket", "path": "/ws"}
        lifespan_scope = {"type": "lifespan"}

        async def noop_receive():
            return {"type": "lifespan.startup"}

        async def noop_send(_msg):
            return None

        await middleware(ws_scope, noop_receive, noop_send)
        await middleware(lifespan_scope, noop_receive, noop_send)

    asyncio.run(run())
    assert received_types == ["websocket", "lifespan"]


def test_starlette_concurrent_async_requests_have_isolated_scopes(httpserver):
    """The integration test that validates the whole contextvars decision:
    three concurrent requests on a single event-loop thread, each setting
    its own user, each yielding to the others mid-handler. Every event
    must ship with its own user — never another task's.
    """
    _init(httpserver)

    async def view(request):
        name = request.path_params["name"]
        errsight.set_user({"id": name})
        # Yield to other tasks — this is where threading.local would lose.
        await asyncio.sleep(0.02 if name == "alice" else 0.005)
        errsight.log(level="error", message=f"err for {name}")
        return PlainTextResponse(name)

    app = Starlette(routes=[Route("/{name:str}", view)])
    app.add_middleware(ErrsightMiddleware)

    async def main():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            await asyncio.gather(
                client.get("/alice"),
                client.get("/bob"),
                client.get("/carol"),
            )

    asyncio.run(main())
    errsight.close()

    events = _events(httpserver)
    by_msg = {e["message"]: e for e in events}
    assert by_msg["err for alice"]["user"]["id"] == "alice"
    assert by_msg["err for bob"]["user"]["id"] == "bob"
    assert by_msg["err for carol"]["user"]["id"] == "carol"


def test_starlette_no_scope_leakage_between_requests(httpserver):
    """A request that sets user must not leave it visible to subsequent
    requests — verifies the scope is popped on __aexit__.
    """
    _init(httpserver)

    captured: List[Any] = []

    async def view(request):
        scope = hub.current_scope()
        captured.append(dict(scope.user) if scope.user else None)
        name = request.path_params["who"]
        errsight.set_user({"id": name})
        return PlainTextResponse(name)

    app = Starlette(routes=[Route("/{who:str}", view)])
    app.add_middleware(ErrsightMiddleware)

    with TestClient(app) as client:
        client.get("/alice")
        client.get("/bob")

    # Neither observation should have inherited the prior request's user.
    for observation in captured:
        assert observation is None or observation.get("id") is None


def test_fastapi_module_reexports_middleware():
    """The errsight.fastapi shim must expose the same class for
    discoverability."""
    from errsight.fastapi import ErrsightMiddleware as FastAPIM
    from errsight.starlette import ErrsightMiddleware as StarletteM
    assert FastAPIM is StarletteM
