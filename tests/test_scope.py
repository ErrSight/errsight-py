from __future__ import annotations

from errsight.scope import MAX_DB_BREADCRUMBS, MAX_USER_BREADCRUMBS, Scope


def test_set_user_with_dict_replaces():
    s = Scope()
    s.set_user({"id": "alice", "email": "a@b.com"})
    assert s.user == {"id": "alice", "email": "a@b.com"}


def test_set_user_with_none_clears():
    s = Scope()
    s.set_user({"id": "alice"})
    s.set_user(None)
    assert s.user is None


def test_set_user_with_non_mapping_clears():
    s = Scope()
    s.set_user({"id": "alice"})
    s.set_user("not a dict")  # type: ignore[arg-type]
    assert s.user is None


def test_set_tag_stringifies():
    s = Scope()
    s.set_tag("count", 42)
    s.set_tag(7, True)
    assert s.tags == {"count": "42", "7": "True"}


def test_set_tags_merges():
    s = Scope()
    s.set_tag("a", "1")
    s.set_tags({"b": "2", "a": "overwritten"})
    assert s.tags == {"a": "overwritten", "b": "2"}


def test_add_breadcrumb_appends_with_timestamp_and_caps():
    s = Scope()
    for i in range(MAX_USER_BREADCRUMBS + 5):
        s.add_breadcrumb(category="ui", message=f"click {i}")
    assert len(s.user_breadcrumbs) == MAX_USER_BREADCRUMBS
    # Oldest 5 evicted; "click 5" is now the front.
    assert s.user_breadcrumbs[0]["message"] == "click 5"
    assert s.user_breadcrumbs[-1]["message"] == f"click {MAX_USER_BREADCRUMBS + 4}"
    assert s.user_breadcrumbs[0]["timestamp"].endswith("Z")


def test_db_breadcrumb_has_independent_cap():
    s = Scope()
    for i in range(MAX_DB_BREADCRUMBS + 3):
        s.add_db_breadcrumb(message=f"SELECT {i}")
    assert len(s.db_breadcrumbs) == MAX_DB_BREADCRUMBS

    # Adding lots of DB crumbs must not evict user crumbs.
    s.add_breadcrumb(category="ui", message="manual")
    for i in range(100):
        s.add_db_breadcrumb(message=f"SELECT extra {i}")
    assert any(c["message"] == "manual" for c in s.user_breadcrumbs)


def test_breadcrumbs_property_merges_sorted_by_timestamp():
    s = Scope()
    s.add_breadcrumb(category="ui", message="user-1")
    s.add_db_breadcrumb(message="db-1")
    s.add_breadcrumb(category="ui", message="user-2")
    merged = s.breadcrumbs
    timestamps = [c["timestamp"] for c in merged]
    assert timestamps == sorted(timestamps)
    assert {c["message"] for c in merged} == {"user-1", "db-1", "user-2"}


def test_copy_returns_independent_scope():
    parent = Scope()
    parent.set_user({"id": "alice"})
    parent.set_tag("region", "us-east")
    parent.add_breadcrumb(category="ui", message="seen")

    child = parent.copy()
    child.set_user({"id": "bob"})
    child.set_tag("region", "eu-west")
    child.add_breadcrumb(category="ui", message="child-only")

    # Parent untouched by child mutations.
    assert parent.user == {"id": "alice"}
    assert parent.tags == {"region": "us-east"}
    assert [c["message"] for c in parent.user_breadcrumbs] == ["seen"]


def test_to_dict_from_dict_roundtrip_drops_db_crumbs():
    s = Scope()
    s.set_user({"id": "alice"})
    s.set_tag("region", "us")
    s.add_breadcrumb(category="ui", message="manual")
    s.add_db_breadcrumb(message="SELECT 1")

    serialized = s.to_dict()
    rehydrated = Scope.from_dict(serialized)

    assert rehydrated.user == {"id": "alice"}
    assert rehydrated.tags == {"region": "us"}
    assert [c["message"] for c in rehydrated.user_breadcrumbs] == ["manual"]
    # DB crumbs are deliberately not propagated across process boundaries.
    assert rehydrated.db_breadcrumbs == []


def test_from_dict_handles_garbage():
    assert Scope.from_dict(None).user is None
    assert Scope.from_dict({"user": "not a dict"}).user is None
    assert Scope.from_dict({"tags": ["not", "a", "dict"]}).tags == {}
    assert Scope.from_dict({"breadcrumbs": "not a list"}).user_breadcrumbs == []


def test_merge_overlays_other_scope():
    base = Scope()
    base.set_user({"id": "root"})
    base.set_tag("region", "us")
    base.add_breadcrumb(category="ui", message="base")

    overlay = Scope()
    overlay.set_user({"id": "request"})
    overlay.set_tag("request_id", "abc")
    overlay.add_breadcrumb(category="ui", message="overlay")

    base.merge(overlay)
    assert base.user == {"id": "request"}
    assert base.tags == {"region": "us", "request_id": "abc"}
    assert [c["message"] for c in base.user_breadcrumbs] == ["base", "overlay"]
