"""Flask integration for ErrSight.

Wire up::

    from flask import Flask
    import errsight
    from errsight.flask import ErrsightFlask

    errsight.init(api_key=...)
    app = Flask(__name__)
    ErrsightFlask(app)

App-factory pattern::

    errsight_ext = ErrsightFlask()

    def create_app():
        app = Flask(__name__)
        errsight_ext.init_app(app)
        return app

User identity is read from (in order):
    1. ``flask_login.current_user`` if Flask-Login is installed.
    2. ``flask.g.user`` (a manual convention used by many apps).
    3. Falls back to anonymous + IP.

Register ErrsightFlask *after* any ``before_request`` hooks that populate
``g.user`` — Flask runs ``before_request`` handlers in registration order.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

try:
    from flask import Flask, g, got_request_exception, request
    from werkzeug.exceptions import HTTPException
except ImportError as e:  # pragma: no cover - exercised by env-without-flask
    raise ImportError(
        "errsight.flask requires Flask to be installed (pip install flask)"
    ) from e

import errsight
from errsight import hub

_FLASK_SCOPE_TOKEN_KEY = "_errsight_scope_token"


class ErrsightFlask:
    """Flask extension that pushes an ErrSight scope per request, populates
    it from the request, and captures unhandled exceptions via the
    ``got_request_exception`` signal.

    The signal fires while the request context is still active, so
    ``capture_exception`` sees the populated scope. ``HTTPException``
    subclasses (404, 403, …) are skipped — those are routine application
    flow, not errors worth tracking.
    """

    def __init__(self, app: Optional[Flask] = None) -> None:
        self.app = app
        if app is not None:
            self.init_app(app)

    def init_app(self, app: Flask) -> None:
        # Per Flask convention, attach to app.extensions so the app holds
        # a strong reference to this extension. Signal connections are
        # weak by default — without a strong ref, garbage collection
        # would silently disconnect our handler.
        extensions = getattr(app, "extensions", None)
        if extensions is None:
            app.extensions = {}
            extensions = app.extensions
        extensions["errsight"] = self

        app.before_request(self._before_request)
        app.teardown_request(self._teardown_request)
        got_request_exception.connect(self._on_exception, app)

    def _before_request(self) -> None:
        _scope, token = hub.push_scope()
        # Token stashed on flask.g, which lives in the request context —
        # concurrent threaded/async requests each have their own g and
        # therefore their own token, no cross-request leakage.
        setattr(g, _FLASK_SCOPE_TOKEN_KEY, token)
        self._populate_scope()

    def _teardown_request(
        self, exception: Optional[BaseException] = None
    ) -> None:
        token = getattr(g, _FLASK_SCOPE_TOKEN_KEY, None)
        if token is not None:
            hub.pop_scope(token)
            try:
                delattr(g, _FLASK_SCOPE_TOKEN_KEY)
            except AttributeError:
                pass

    def _on_exception(
        self, sender: Flask, exception: BaseException, **kwargs: Any
    ) -> None:
        # HTTPException is Flask's way of representing routed application
        # responses (abort(404), abort(403), redirects). Capturing them
        # would flood the issue list with not-found "errors."
        if isinstance(exception, HTTPException):
            return
        try:
            errsight.capture_exception(
                exception, metadata=self._exception_metadata()
            )
        except Exception:
            pass

    def _populate_scope(self) -> None:
        try:
            tags: Dict[str, str] = {
                "request_method": str(getattr(request, "method", "") or ""),
                "path": str(getattr(request, "path", "") or ""),
            }
            endpoint = getattr(request, "endpoint", None)
            if endpoint:
                tags["endpoint"] = str(endpoint)
            blueprint = getattr(request, "blueprint", None)
            if blueprint:
                tags["blueprint"] = str(blueprint)
            errsight.set_tags(tags)

            user = self._user_from_request()
            if user:
                errsight.set_user(user)
        except Exception:
            pass

    def _user_from_request(self) -> Optional[Dict[str, Any]]:
        flask_login_user = self._flask_login_user()
        if flask_login_user is not None:
            return flask_login_user

        g_user = getattr(g, "user", None)
        if g_user is not None:
            extracted = self._extract_user_attrs(g_user)
            if extracted:
                return extracted

        return self._anon_user()

    def _flask_login_user(self) -> Optional[Dict[str, Any]]:
        try:
            from flask_login import current_user
        except ImportError:
            return None
        try:
            if not getattr(current_user, "is_authenticated", False):
                return None
        except Exception:
            return None
        return self._extract_user_attrs(current_user)

    def _extract_user_attrs(self, user: Any) -> Optional[Dict[str, Any]]:
        result: Dict[str, Any] = {}

        # Flask-Login exposes get_id(); other patterns use .id / .pk.
        getter = getattr(user, "get_id", None)
        if callable(getter):
            try:
                user_id = getter()
            except Exception:
                user_id = None
            if user_id is not None:
                result["id"] = str(user_id)
        if "id" not in result:
            for attr in ("id", "pk"):
                val = getattr(user, attr, None)
                if val is not None:
                    result["id"] = str(val)
                    break

        username = getattr(user, "username", None)
        if username:
            result["username"] = str(username)
        email = getattr(user, "email", None)
        if email:
            result["email"] = str(email)
        ip = self._client_ip()
        if ip:
            result["ip_address"] = ip
        return result or None

    def _anon_user(self) -> Optional[Dict[str, Any]]:
        ip = self._client_ip()
        return {"ip_address": ip} if ip else None

    @staticmethod
    def _client_ip() -> Optional[str]:
        try:
            xff = request.headers.get("X-Forwarded-For")
            if xff:
                first = xff.split(",")[0].strip()
                if first:
                    return first
            addr = request.remote_addr
            return str(addr) if addr else None
        except Exception:
            return None

    def _exception_metadata(self) -> Dict[str, Any]:
        try:
            md: Dict[str, Any] = {}
            path = getattr(request, "path", None)
            if path:
                md["path"] = str(path)
            method = getattr(request, "method", None)
            if method:
                md["request_method"] = str(method)
            try:
                md["full_path"] = request.full_path
            except Exception:
                pass
            try:
                md["query_params"] = dict(request.args.items())
            except Exception:
                pass
            endpoint = getattr(request, "endpoint", None)
            if endpoint:
                md["endpoint"] = str(endpoint)
            blueprint = getattr(request, "blueprint", None)
            if blueprint:
                md["blueprint"] = str(blueprint)
            return md
        except Exception:
            return {}
