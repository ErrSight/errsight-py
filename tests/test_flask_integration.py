from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

pytest.importorskip("flask")

from flask import Flask, abort, g  # noqa: E402

import errsight  # noqa: E402
from errsight import hub  # noqa: E402
from errsight.flask import ErrsightFlask  # noqa: E402


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


def _make_app(**view_overrides) -> Flask:
    app = Flask(__name__)
    app.config["TESTING"] = False  # let exceptions flow to error handlers
    return app


def test_flask_request_populates_tags(httpserver):
    _init(httpserver)
    app = _make_app()
    ErrsightFlask(app)

    captured: Dict[str, Any] = {}

    @app.route("/hello")
    def hello():
        scope = hub.current_scope()
        captured["tags"] = dict(scope.tags)
        captured["user"] = dict(scope.user) if scope.user else None
        return "ok"

    resp = app.test_client().get("/hello")
    assert resp.status_code == 200

    assert captured["tags"]["request_method"] == "GET"
    assert captured["tags"]["path"] == "/hello"
    assert captured["tags"]["endpoint"] == "hello"


def test_flask_anonymous_user_from_remote_addr(httpserver):
    _init(httpserver)
    app = _make_app()
    ErrsightFlask(app)

    captured: Dict[str, Any] = {}

    @app.route("/")
    def home():
        captured["user"] = dict(hub.current_scope().user or {})
        return "ok"

    app.test_client().get("/", environ_base={"REMOTE_ADDR": "203.0.113.7"})
    assert captured["user"] == {"ip_address": "203.0.113.7"}


def test_flask_x_forwarded_for_prefers_leftmost(httpserver):
    _init(httpserver)
    app = _make_app()
    ErrsightFlask(app)

    captured: Dict[str, Any] = {}

    @app.route("/")
    def home():
        captured["user"] = dict(hub.current_scope().user or {})
        return "ok"

    app.test_client().get(
        "/",
        headers={"X-Forwarded-For": "198.51.100.1, 10.0.0.1"},
        environ_base={"REMOTE_ADDR": "10.0.0.99"},
    )
    assert captured["user"]["ip_address"] == "198.51.100.1"


def test_flask_g_user_populates_scope(httpserver):
    """User-population middleware must register BEFORE ErrsightFlask so
    its before_request runs first. This is the convention we document.
    """
    _init(httpserver)
    app = _make_app()

    class FakeUser:
        id = 7
        username = "bob"
        email = "b@x.com"

    @app.before_request
    def _set_user():
        g.user = FakeUser()

    ErrsightFlask(app)

    captured: Dict[str, Any] = {}

    @app.route("/")
    def home():
        captured["user"] = dict(hub.current_scope().user or {})
        return "ok"

    app.test_client().get("/")
    assert captured["user"]["id"] == "7"
    assert captured["user"]["username"] == "bob"
    assert captured["user"]["email"] == "b@x.com"


def test_flask_blueprint_tag(httpserver):
    _init(httpserver)
    app = _make_app()

    from flask import Blueprint

    bp = Blueprint("billing", __name__)

    captured: Dict[str, Any] = {}

    @bp.route("/invoice")
    def invoice():
        captured["tags"] = dict(hub.current_scope().tags)
        return "ok"

    app.register_blueprint(bp, url_prefix="/billing")
    ErrsightFlask(app)

    app.test_client().get("/billing/invoice")
    assert captured["tags"]["blueprint"] == "billing"
    assert captured["tags"]["endpoint"] == "billing.invoice"


def test_flask_view_exception_is_captured(httpserver):
    _init(httpserver)
    app = _make_app()
    ErrsightFlask(app)

    @app.route("/boom")
    def boom():
        raise ValueError("kaboom")

    resp = app.test_client().get("/boom?order_id=42&plan=pro")
    assert resp.status_code == 500

    errsight.close()
    events = _events(httpserver)
    assert len(events) == 1
    event = events[0]
    assert event["tags"]["request_method"] == "GET"
    assert event["tags"]["path"] == "/boom"
    assert event["tags"]["endpoint"] == "boom"

    md = event["metadata"]
    assert md["path"] == "/boom"
    assert md["request_method"] == "GET"
    assert md["query_params"] == {"order_id": "42", "plan": "pro"}
    assert md["endpoint"] == "boom"
    assert md["exception_class"] == "ValueError"
    assert "ValueError" in event["message"]
    assert "kaboom" in event["message"]


def test_flask_http_exception_is_not_captured(httpserver):
    """abort(404) and friends are routine application flow, not errors."""
    _init(httpserver)
    app = _make_app()
    ErrsightFlask(app)

    @app.route("/missing")
    def missing():
        abort(404)

    resp = app.test_client().get("/missing")
    assert resp.status_code == 404

    errsight.close()
    assert _events(httpserver) == []


def test_flask_scope_popped_after_each_request(httpserver):
    _init(httpserver)
    app = _make_app()
    ErrsightFlask(app)

    @app.route("/<who>")
    def page(who):
        errsight.set_user({"id": who})
        return who

    client = app.test_client()
    client.get("/alice")
    client.get("/bob")

    # Outside any request, the root scope should be untouched.
    assert hub.current_scope().user is None
    assert hub.current_scope().tags == {}


def test_flask_init_app_factory_pattern(httpserver):
    _init(httpserver)
    ext = ErrsightFlask()  # not bound yet
    app = _make_app()
    ext.init_app(app)

    @app.route("/")
    def home():
        return "ok"

    assert app.extensions["errsight"] is ext
    resp = app.test_client().get("/")
    assert resp.status_code == 200


def test_flask_concurrent_requests_have_isolated_scopes(httpserver):
    """Each request thread/task sees its own scope. Verify via two
    sequential requests where each leaves a marker; the next request
    must not inherit the marker.
    """
    _init(httpserver)
    app = _make_app()
    ErrsightFlask(app)

    captured: List[Any] = []

    @app.route("/<who>")
    def page(who):
        scope = hub.current_scope()
        # Before mutation: no user from prior request should leak in.
        captured.append(scope.user)
        errsight.set_user({"id": who})
        return who

    client = app.test_client()
    client.get("/alice")
    client.get("/bob")

    # Both observations should be the anonymous IP-only baseline — never
    # the previous request's user.
    assert all(u is None or u.get("id") not in {"alice", "bob"} for u in captured)
