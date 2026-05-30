"""Starlette / FastAPI integration for ErrSight.

Wire up Starlette::

    from starlette.applications import Starlette
    from errsight.starlette import ErrsightMiddleware
    import errsight

    errsight.init(api_key=...)
    app = Starlette(routes=[...])
    app.add_middleware(ErrsightMiddleware)

FastAPI is built on Starlette, so the same middleware works there too —
or import from :mod:`errsight.fastapi` for discoverability::

    from fastapi import FastAPI
    from errsight.fastapi import ErrsightMiddleware

    errsight.init(api_key=...)
    app = FastAPI()
    app.add_middleware(ErrsightMiddleware)

Add the middleware **first** (calls to ``app.add_middleware`` register
outermost-first), so it wraps every other middleware and the view itself.
Then exceptions from any layer below bubble up to where we can capture
them.

The ContextVar-backed scope means concurrent async requests on the same
event-loop thread don't share state — each request runs in its own
``asyncio.Task`` with a copy-on-task ContextVar context.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Optional

try:
    from starlette.exceptions import HTTPException as StarletteHTTPException
except ImportError as e:  # pragma: no cover - exercised by env-without-starlette
    raise ImportError(
        "errsight.starlette requires starlette to be installed "
        "(pip install starlette, or install via the fastapi/starlette extras)"
    ) from e

import errsight
from errsight import hub

ASGIScope = Dict[str, Any]
ASGIMessage = Dict[str, Any]
ASGIReceive = Callable[[], Awaitable[ASGIMessage]]
ASGISend = Callable[[ASGIMessage], Awaitable[None]]
ASGIApp = Callable[[ASGIScope, ASGIReceive, ASGISend], Awaitable[None]]


class ErrsightMiddleware:
    """Pure ASGI middleware (not :class:`BaseHTTPMiddleware`) that pushes a
    per-request scope, populates tags from the ASGI request, and captures
    unhandled exceptions while the scope is still active.

    Why pure ASGI and not ``BaseHTTPMiddleware``: Starlette's own docs
    flag ``BaseHTTPMiddleware`` for breaking streaming responses and
    introducing an extra context switch. Pure ASGI is also what every
    other production-grade observability middleware uses for the same
    reasons.

    The view's ``await self.app(scope, receive, send)`` runs nested
    middleware, routing, and the view itself; exceptions that aren't
    caught by Starlette's ``ExceptionMiddleware`` (i.e. non-HTTPException
    application errors) propagate back here where we capture them.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self, scope: ASGIScope, receive: ASGIReceive, send: ASGISend
    ) -> None:
        # Websocket and lifespan pass through untouched. Capture only
        # makes sense for HTTP requests.
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async with errsight.with_scope():
            self._populate_scope(scope)
            try:
                await self.app(scope, receive, send)
            except Exception as exc:
                # HTTPException would normally be caught by Starlette's
                # ExceptionMiddleware before reaching us. Filter defensively
                # in case it raises from middleware that sits above
                # ExceptionMiddleware in the stack.
                if not isinstance(exc, StarletteHTTPException):
                    try:
                        self._augment_user_from_auth(scope)
                        self._augment_endpoint_tag(scope)
                        errsight.capture_exception(
                            exc, metadata=self._exception_metadata(scope)
                        )
                    except Exception:
                        # Capture must never break the request — let
                        # Starlette's ServerErrorMiddleware render the
                        # 500 (or debug page).
                        pass
                raise

    def _populate_scope(self, scope: ASGIScope) -> None:
        try:
            tags: Dict[str, str] = {}
            method = scope.get("method")
            if method:
                tags["request_method"] = str(method)
            path = scope.get("path")
            if path:
                tags["path"] = str(path)
            if tags:
                errsight.set_tags(tags)

            ip = self._client_ip(scope)
            if ip:
                errsight.set_user({"ip_address": ip})
        except Exception:
            pass

    def _augment_user_from_auth(self, scope: ASGIScope) -> None:
        """Late-bind user from ``scope['user']`` (set by Starlette's
        ``AuthenticationMiddleware``) at capture time.

        Precedence: a view that called ``errsight.set_user({"id": ...})``
        is preserved as-is; we only augment if the current scope user
        has no ``id`` (i.e. is still the anonymous IP-only baseline).
        """
        current = hub.current_scope().user
        if current and current.get("id"):
            return
        auth_user = self._user_from_auth_scope(scope)
        if not auth_user:
            return
        if current and current.get("ip_address") and "ip_address" not in auth_user:
            auth_user["ip_address"] = current["ip_address"]
        errsight.set_user(auth_user)

    def _augment_endpoint_tag(self, scope: ASGIScope) -> None:
        route_name = self._route_name(scope)
        if route_name:
            errsight.set_tag("endpoint", route_name)

    @staticmethod
    def _user_from_auth_scope(scope: ASGIScope) -> Optional[Dict[str, Any]]:
        user = scope.get("user")
        if user is None:
            return None
        try:
            if not getattr(user, "is_authenticated", False):
                return None
        except Exception:
            return None

        result: Dict[str, Any] = {}
        # Starlette's BaseUser has identity + display_name. Custom user
        # implementations frequently expose id/username/email directly.
        identity = getattr(user, "identity", None)
        if identity:
            result["id"] = str(identity)
        elif getattr(user, "id", None) is not None:
            result["id"] = str(user.id)

        display_name = getattr(user, "display_name", None) or getattr(
            user, "username", None
        )
        if display_name:
            result["username"] = str(display_name)

        email = getattr(user, "email", None)
        if email:
            result["email"] = str(email)

        return result or None

    @staticmethod
    def _route_name(scope: ASGIScope) -> Optional[str]:
        # Starlette doesn't expose the matched Route in the ASGI scope —
        # only the endpoint function. To recover the explicit Route name
        # (Route("/foo", view, name="my-route")), walk app.routes looking
        # for the Route whose endpoint matches ours. The fallback when no
        # explicit name was given is the endpoint function's __name__,
        # which is also Starlette's default for unnamed routes.
        endpoint = scope.get("endpoint")
        app = scope.get("app")
        if endpoint is not None and app is not None:
            named = ErrsightMiddleware._find_route_name(
                getattr(app, "routes", None), endpoint
            )
            if named:
                return named
        if endpoint is not None:
            name = getattr(endpoint, "__name__", None)
            if name:
                return str(name)
        return None

    @staticmethod
    def _find_route_name(routes: Any, endpoint: Any) -> Optional[str]:
        if not routes:
            return None
        try:
            for route in routes:
                if getattr(route, "endpoint", None) is endpoint:
                    name = getattr(route, "name", None)
                    if name:
                        return str(name)
                    return None
                # Recurse into Mount / Router sub-routers.
                sub = getattr(route, "routes", None)
                if sub:
                    found = ErrsightMiddleware._find_route_name(sub, endpoint)
                    if found:
                        return found
        except Exception:
            return None
        return None

    @staticmethod
    def _client_ip(scope: ASGIScope) -> Optional[str]:
        # X-Forwarded-For leftmost wins. ASGI headers are list of
        # (bytes, bytes) tuples; names are guaranteed lowercase.
        headers = scope.get("headers", [])
        if isinstance(headers, (list, tuple)):
            for entry in headers:
                if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                    continue
                name, value = entry[0], entry[1]
                if name != b"x-forwarded-for":
                    continue
                if not isinstance(value, (bytes, bytearray)):
                    continue
                try:
                    decoded = value.decode("latin-1", errors="replace")
                except Exception:
                    continue
                first = decoded.split(",")[0].strip()
                if first:
                    return first
        # Fall back to the client tuple — (host, port) or None.
        client = scope.get("client")
        if isinstance(client, (list, tuple)) and len(client) >= 1:
            host = client[0]
            return str(host) if host else None
        return None

    def _exception_metadata(self, scope: ASGIScope) -> Dict[str, Any]:
        try:
            md: Dict[str, Any] = {}
            method = scope.get("method")
            if method:
                md["request_method"] = str(method)
            path = scope.get("path")
            if path:
                md["path"] = str(path)
            qs = scope.get("query_string")
            if isinstance(qs, (bytes, bytearray)) and qs:
                try:
                    md["query_string"] = qs.decode("latin-1", errors="replace")
                except Exception:
                    pass
            route_name = self._route_name(scope)
            if route_name:
                md["endpoint"] = route_name
            return md
        except Exception:
            return {}
