from __future__ import annotations

from errsight import backtrace


def test_parse_traceback_returns_empty_for_none():
    assert backtrace.parse_traceback(None) == []


def test_parse_traceback_captures_frame_fields():
    try:
        raise ValueError("boom")
    except ValueError as exc:
        frames = backtrace.parse_traceback(exc.__traceback__)
    assert len(frames) == 1
    frame = frames[0]
    assert frame["function"] == "test_parse_traceback_captures_frame_fields"
    assert frame["filename"].endswith("test_backtrace.py")
    assert frame["abs_path"] == frame["filename"]
    assert isinstance(frame["lineno"], int) and frame["lineno"] > 0
    assert frame["in_app"] is True


def test_parse_traceback_caps_at_max_frames():
    def recurse(n: int) -> None:
        if n == 0:
            raise ValueError("bottom")
        recurse(n - 1)

    try:
        recurse(backtrace.MAX_FRAMES + 20)
    except ValueError as exc:
        frames = backtrace.parse_traceback(exc.__traceback__)
    assert len(frames) == backtrace.MAX_FRAMES


def test_in_app_excludes_site_packages():
    assert backtrace.is_in_app("/usr/lib/python3.11/site-packages/foo/bar.py") is False
    assert backtrace.is_in_app("/some/path/dist-packages/foo/bar.py") is False


def test_in_app_excludes_synthetic():
    assert backtrace.is_in_app("<frozen importlib._bootstrap>") is False
    assert backtrace.is_in_app("<string>") is False
    assert backtrace.is_in_app("(eval)") is False
    assert backtrace.is_in_app("") is False
    assert backtrace.is_in_app(None) is False


def test_in_app_treats_user_code_as_in_app(tmp_path):
    backtrace.reset_defaults()
    user_file = tmp_path / "user_code.py"
    user_file.write_text("x = 1\n")
    assert backtrace.is_in_app(str(user_file)) is True


def test_walk_causes_with_explicit_cause():
    try:
        try:
            raise KeyError("inner")
        except KeyError as inner:
            raise ValueError("outer") from inner
    except ValueError as outer:
        causes = backtrace.walk_causes(outer)
    assert len(causes) == 1
    assert causes[0]["class"] == "KeyError"
    assert "inner" in causes[0]["message"]
    assert isinstance(causes[0]["backtrace"], list)


def test_walk_causes_follows_implicit_context():
    try:
        try:
            raise KeyError("inner")
        except KeyError:
            raise ValueError("outer")  # implicit __context__
    except ValueError as outer:
        causes = backtrace.walk_causes(outer)
    assert len(causes) == 1
    assert causes[0]["class"] == "KeyError"


def test_walk_causes_respects_suppress_context():
    try:
        try:
            raise KeyError("inner")
        except KeyError:
            raise ValueError("outer") from None
    except ValueError as outer:
        causes = backtrace.walk_causes(outer)
    assert causes == []


def test_walk_causes_caps_at_max_depth():
    exc: BaseException = ValueError("level 0")
    for i in range(1, backtrace.MAX_CAUSE_DEPTH + 5):
        new_exc = ValueError(f"level {i}")
        new_exc.__cause__ = exc
        exc = new_exc
    causes = backtrace.walk_causes(exc)
    assert len(causes) == backtrace.MAX_CAUSE_DEPTH


def test_walk_causes_breaks_on_cycle():
    a = ValueError("a")
    b = ValueError("b")
    a.__cause__ = b
    b.__cause__ = a  # pathological loop
    causes = backtrace.walk_causes(a)
    # Should not infinite-loop; cap applied via the seen-set.
    assert len(causes) <= 2


def test_walk_causes_empty_for_no_chain():
    try:
        raise ValueError("standalone")
    except ValueError as exc:
        causes = backtrace.walk_causes(exc)
    assert causes == []
