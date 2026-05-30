"""Django integration for ErrSight.

Install at or near the top of ``MIDDLEWARE`` — at minimum after
``AuthenticationMiddleware`` so ``request.user`` is populated by the time
this middleware reads it::

    MIDDLEWARE = [
        "django.contrib.sessions.middleware.SessionMiddleware",
        "django.contrib.auth.middleware.AuthenticationMiddleware",
        "errsight.django.ErrsightMiddleware",
        ...
    ]

Call ``errsight.init(api_key=..., environment=...)`` once at process boot
(typically in your project's ``wsgi.py``, ``asgi.py``, or an
``AppConfig.ready()`` method).

The middleware:

- Pushes a fresh ErrSight scope per request (so concurrent requests on
  ``contextvars``-aware servers stay isolated).
- Populates the scope's user from ``request.user`` (Django auth).
- Adds tags: ``request_method``, ``path``, ``view`` (when URL routing
  resolves the path).
- Captures uncaught view exceptions via ``process_exception`` with full
  request metadata (path, method, view, query params).
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Optional, Union

try:
    from asgiref.sync import iscoroutinefunction, markcoroutinefunction
    from django.http import HttpRequest, HttpResponse
    from django.urls import Resolver404, resolve
except ImportError as e:  # pragma: no cover - exercised by env-without-django
    raise ImportError(
        "errsight.django requires Django to be installed (pip install django)"
    ) from e

import errsight

SyncGetResponse = Callable[[HttpRequest], HttpResponse]
AsyncGetResponse = Callable[[HttpRequest], Awaitable[HttpResponse]]
GetResponse = Union[SyncGetResponse, AsyncGetResponse]


class ErrsightMiddleware:
    """Django middleware that pushes a per-request scope and captures
    uncaught view exceptions. Supports both sync and async views.

    Why ``with errsight.with_scope()`` works for capture: Django's
    ``convert_exception_to_response`` catches view exceptions at the
    innermost wrapper and invokes each middleware's ``process_exception``
    while our ``__call__`` is still paused inside ``self.get_response()``.
    The ContextVar-backed scope stack is still active during that window,
    so ``capture_exception`` sees the populated scope. The same holds in
    async mode — Django runs ``process_exception`` via
    ``sync_to_async(thread_sensitive=False)`` which propagates the
    coroutine's ContextVar context to the worker thread.
    """

    # Class attributes Django reads to decide whether to wrap with
    # sync_to_async / async_to_sync adapters. Setting both to True lets
    # Django use us natively in either mode without an adapter layer.
    sync_capable = True
    async_capable = True

    def __init__(self, get_response: GetResponse) -> None:
        self.get_response = get_response
        self._async_mode = iscoroutinefunction(get_response)
        if self._async_mode:
            # Marks this instance's __call__ as a coroutine function so
            # Django's middleware chain awaits it instead of running it
            # through sync_to_async.
            markcoroutinefunction(self)

    def __call__(
        self, request: HttpRequest
    ) -> Union[HttpResponse, Awaitable[HttpResponse]]:
        if self._async_mode:
            return self._async_call(request)
        return self._sync_call(request)

    def _sync_call(self, request: HttpRequest) -> HttpResponse:
        with errsight.with_scope():
            self._populate_scope(request)
            return self.get_response(request)

    async def _async_call(self, request: HttpRequest) -> HttpResponse:
        async with errsight.with_scope():
            self._populate_scope(request)
            return await self.get_response(request)

    def process_exception(
        self, request: HttpRequest, exception: BaseException
    ) -> None:
        try:
            errsight.capture_exception(
                exception, metadata=self._exception_metadata(request)
            )
        except Exception:
            # Capture must never break Django's error handling. Swallowing
            # here means a buggy SDK release can't turn a 500 into a 500
            # plus a broken exception page.
            pass
        return None

    def _populate_scope(self, request: HttpRequest) -> None:
        try:
            tags: Dict[str, str] = {
                "request_method": str(getattr(request, "method", "") or ""),
                "path": str(getattr(request, "path", "") or ""),
            }
            view_name = self._view_name(request)
            if view_name:
                tags["view"] = view_name
            errsight.set_tags(tags)

            user = self._user_from_request(request)
            if user:
                errsight.set_user(user)
        except Exception:
            # Don't let middleware bugs poison the request.
            pass

    @staticmethod
    def _view_name(request: HttpRequest) -> Optional[str]:
        path = getattr(request, "path", None)
        if not path:
            return None
        try:
            match = resolve(path)
        except Resolver404:
            return None
        except Exception:
            return None
        name = getattr(match, "view_name", None)
        return str(name) if name else None

    def _user_from_request(
        self, request: HttpRequest
    ) -> Optional[Dict[str, Any]]:
        user = getattr(request, "user", None)
        if user is None:
            return self._anon_user(request)

        try:
            authenticated = bool(getattr(user, "is_authenticated", False))
        except Exception:
            authenticated = False
        if not authenticated:
            return self._anon_user(request)

        result: Dict[str, Any] = {}
        for attr in ("pk", "id"):
            val = getattr(user, attr, None)
            if val is not None:
                result["id"] = str(val)
                break

        username = getattr(user, "username", None)
        if not username:
            getter = getattr(user, "get_username", None)
            if callable(getter):
                try:
                    username = getter()
                except Exception:
                    username = None
        if username:
            result["username"] = str(username)

        email = getattr(user, "email", None)
        if email:
            result["email"] = str(email)

        ip = self._client_ip(request)
        if ip:
            result["ip_address"] = ip
        return result or self._anon_user(request)

    @staticmethod
    def _anon_user(request: HttpRequest) -> Optional[Dict[str, Any]]:
        ip = ErrsightMiddleware._client_ip(request)
        return {"ip_address": ip} if ip else None

    @staticmethod
    def _client_ip(request: HttpRequest) -> Optional[str]:
        meta = getattr(request, "META", None)
        if not isinstance(meta, dict):
            return None
        # Leftmost in X-Forwarded-For is the original client. Production
        # setups behind a proxy should validate trusted-proxy depth — that
        # tuning lives downstream of this SDK.
        xff = meta.get("HTTP_X_FORWARDED_FOR")
        if isinstance(xff, str) and xff:
            first = xff.split(",")[0].strip()
            if first:
                return first
        addr = meta.get("REMOTE_ADDR")
        return str(addr) if addr else None

    def _exception_metadata(
        self, request: HttpRequest
    ) -> Dict[str, Any]:
        try:
            md: Dict[str, Any] = {}
            path = getattr(request, "path", None)
            if path:
                md["path"] = str(path)
            method = getattr(request, "method", None)
            if method:
                md["request_method"] = str(method)
            try:
                md["full_path"] = request.get_full_path()
            except Exception:
                pass
            try:
                # Query params are already exposed in the URL itself, so
                # shipping them isn't a new leak. Body params are not
                # captured — they're more likely to carry credentials.
                md["query_params"] = dict(request.GET.items())
            except Exception:
                pass
            view_name = self._view_name(request)
            if view_name:
                md["view"] = view_name
            return md
        except Exception:
            return {}
