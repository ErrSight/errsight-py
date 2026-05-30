from __future__ import annotations

import asyncio
import json
from typing import Dict, List

import errsight


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


def test_scope_user_appears_in_event(httpserver):
    _init(httpserver)
    with errsight.with_scope():
        errsight.set_user({"id": "alice", "email": "a@b.com"})
        errsight.log(level="error", message="hello")
    errsight.close()

    events = _events(httpserver)
    assert events[0]["user"] == {"id": "alice", "email": "a@b.com"}


def test_per_call_user_overrides_scope_user(httpserver):
    _init(httpserver)
    with errsight.with_scope():
        errsight.set_user({"id": "alice"})
        errsight.log(level="error", message="override", user={"id": "bob"})
    errsight.close()

    assert _events(httpserver)[0]["user"] == {"id": "bob"}


def test_scope_tags_and_per_call_tags_merge(httpserver):
    _init(httpserver)
    with errsight.with_scope():
        errsight.set_tag("region", "us-east")
        errsight.set_tag("service", "api")
        errsight.log(
            level="error",
            message="merged",
            tags={"service": "worker", "release": "v1.2"},  # service collides
        )
    errsight.close()

    event_tags = _events(httpserver)[0]["tags"]
    assert event_tags == {
        "region": "us-east",
        "service": "worker",  # per-call wins on collision
        "release": "v1.2",
    }


def test_breadcrumbs_attached_to_event(httpserver):
    _init(httpserver)
    with errsight.with_scope():
        errsight.add_breadcrumb(category="ui", message="clicked submit")
        errsight.add_breadcrumb(category="http", message="GET /api/foo", level="info")
        errsight.log(level="error", message="boom")
    errsight.close()

    crumbs = _events(httpserver)[0]["breadcrumbs"]
    assert [c["message"] for c in crumbs] == ["clicked submit", "GET /api/foo"]
    assert crumbs[0]["category"] == "ui"


def test_scope_state_does_not_leak_between_with_scope_blocks(httpserver):
    _init(httpserver)
    with errsight.with_scope():
        errsight.set_user({"id": "alice"})
        errsight.log(level="error", message="alice's event")
    with errsight.with_scope():
        # No set_user here — must not see alice.
        errsight.log(level="error", message="anonymous event")
    errsight.close()

    events = {e["message"]: e for e in _events(httpserver)}
    assert events["alice's event"]["user"] == {"id": "alice"}
    assert "user" not in events["anonymous event"]


def test_async_concurrent_requests_each_see_own_user(httpserver):
    """Same isolation test as test_hub but end-to-end through the transport —
    proves user identity isn't lost when many requests run concurrently
    on a single asyncio thread (FastAPI/Starlette pattern).
    """
    _init(httpserver)

    async def handle(user_id: str, sleep: float):
        with errsight.with_scope():
            errsight.set_user({"id": user_id})
            await asyncio.sleep(sleep)
            errsight.log(level="error", message=f"err for {user_id}")

    async def main():
        await asyncio.gather(
            handle("alice", 0.02),
            handle("bob", 0.005),
            handle("carol", 0.015),
        )

    asyncio.run(main())
    errsight.close()

    by_msg = {e["message"]: e for e in _events(httpserver)}
    assert by_msg["err for alice"]["user"] == {"id": "alice"}
    assert by_msg["err for bob"]["user"] == {"id": "bob"}
    assert by_msg["err for carol"]["user"] == {"id": "carol"}
