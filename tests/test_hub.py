from __future__ import annotations

import asyncio

import errsight
from errsight import hub
from errsight.scope import Scope


def test_current_scope_returns_a_root():
    s = hub.current_scope()
    assert isinstance(s, Scope)
    assert s.user is None
    assert s.tags == {}


def test_with_scope_pushes_and_pops_sync():
    root = hub.current_scope()
    root.set_tag("global", "yes")
    with errsight.with_scope() as scoped:
        assert scoped is not root
        assert scoped.tags == {"global": "yes"}
        scoped.set_tag("request_id", "abc")
        scoped.set_user({"id": "alice"})
        assert hub.current_scope() is scoped
    # After exit, root is the current scope again — and untouched by the push.
    assert hub.current_scope() is root
    assert root.user is None
    assert "request_id" not in root.tags


def test_mutations_via_top_level_helpers_target_current_scope():
    with errsight.with_scope():
        errsight.set_user({"id": "alice"})
        errsight.set_tag("region", "us")
        errsight.add_breadcrumb(category="ui", message="seen")
        current = hub.current_scope()
        assert current.user == {"id": "alice"}
        assert current.tags == {"region": "us"}
        assert current.user_breadcrumbs[-1]["message"] == "seen"
    # Outside the block, mutations were discarded.
    root = hub.current_scope()
    assert root.user is None
    assert root.tags == {}
    assert root.user_breadcrumbs == []


def test_nested_with_scope_is_a_stack():
    with errsight.with_scope() as outer:
        outer.set_tag("layer", "outer")
        with errsight.with_scope() as inner:
            inner.set_tag("layer", "inner")
            assert hub.current_scope() is inner
            assert inner.tags["layer"] == "inner"
        assert hub.current_scope() is outer
        assert outer.tags["layer"] == "outer"


def test_with_scope_accepts_a_prebuilt_scope():
    custom = Scope()
    custom.set_user({"id": "bob"})
    custom.set_tag("rehydrated", "true")
    with errsight.with_scope(custom) as scoped:
        assert scoped is custom
        assert hub.current_scope() is custom
        assert hub.current_scope().user == {"id": "bob"}


def test_async_tasks_have_isolated_scopes():
    """The single most important test: under asyncio.gather, two coroutines
    on the same thread must see their own scope state, not each other's.

    With threading.local this fails — the later set_user() wins on both
    tasks. With ContextVar (what we use) each task gets its own snapshot.
    """
    results: dict[str, dict[str, object]] = {}

    async def task(name: str, delay: float):
        with errsight.with_scope():
            errsight.set_user({"id": name})
            errsight.set_tag("task", name)
            # Yield to the other task — this is where threading.local would lose.
            await asyncio.sleep(delay)
            s = hub.current_scope()
            results[name] = {"user": s.user, "tags": dict(s.tags)}

    async def main():
        await asyncio.gather(task("alice", 0.02), task("bob", 0.01))

    asyncio.run(main())

    assert results["alice"]["user"] == {"id": "alice"}
    assert results["alice"]["tags"] == {"task": "alice"}
    assert results["bob"]["user"] == {"id": "bob"}
    assert results["bob"]["tags"] == {"task": "bob"}


def test_async_with_scope_also_works():
    async def coro():
        async with errsight.with_scope() as scoped:
            scoped.set_user({"id": "alice"})
            assert hub.current_scope() is scoped
        # After the async with block, scope is popped.
        return hub.current_scope().user

    final_user = asyncio.run(coro())
    assert final_user is None
