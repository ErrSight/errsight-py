from __future__ import annotations

import pytest

from errsight import source_context


@pytest.fixture(autouse=True)
def _clear_linecache():
    source_context.reset_cache()
    yield
    source_context.reset_cache()


def test_fetch_returns_none_for_synthetic_paths():
    assert source_context.fetch("<frozen foo>", 1) is None
    assert source_context.fetch("<string>", 1) is None
    assert source_context.fetch("(eval)", 1) is None


def test_fetch_returns_none_for_garbage_input():
    assert source_context.fetch(None, 1) is None
    assert source_context.fetch("", 1) is None
    assert source_context.fetch("/tmp/foo.py", None) is None
    assert source_context.fetch("/tmp/foo.py", 0) is None
    assert source_context.fetch("/tmp/foo.py", -1) is None


def test_fetch_returns_none_for_missing_file(tmp_path):
    missing = tmp_path / "does_not_exist.py"
    assert source_context.fetch(str(missing), 1) is None


def test_fetch_returns_none_for_out_of_range_lineno(tmp_path):
    f = tmp_path / "small.py"
    f.write_text("only_one_line\n")
    assert source_context.fetch(str(f), 5) is None


def test_fetch_returns_lines_around_target(tmp_path):
    f = tmp_path / "ctx.py"
    f.write_text("\n".join(f"line_{i}" for i in range(1, 21)) + "\n")
    ctx = source_context.fetch(str(f), 10)
    assert ctx is not None
    assert ctx["context_line"] == "line_10"
    assert ctx["pre_context"] == [f"line_{i}" for i in range(5, 10)]
    assert ctx["post_context"] == [f"line_{i}" for i in range(11, 16)]


def test_fetch_handles_top_of_file(tmp_path):
    f = tmp_path / "top.py"
    f.write_text("a\nb\nc\nd\ne\nf\ng\nh\ni\nj\n")
    ctx = source_context.fetch(str(f), 1)
    assert ctx is not None
    assert ctx["context_line"] == "a"
    assert ctx["pre_context"] == []
    assert ctx["post_context"] == ["b", "c", "d", "e", "f"]


def test_fetch_handles_bottom_of_file(tmp_path):
    f = tmp_path / "bot.py"
    f.write_text("a\nb\nc\nd\ne\nf\ng\nh\ni\nj\n")
    ctx = source_context.fetch(str(f), 10)
    assert ctx is not None
    assert ctx["context_line"] == "j"
    assert ctx["pre_context"] == ["e", "f", "g", "h", "i"]
    assert ctx["post_context"] == []


def test_fetch_truncates_long_lines(tmp_path):
    f = tmp_path / "long.py"
    long_line = "x" * 500
    f.write_text(f"{long_line}\n")
    ctx = source_context.fetch(str(f), 1)
    assert ctx is not None
    assert ctx["context_line"].endswith("…[truncated]")
    body = ctx["context_line"].replace("…[truncated]", "")
    assert len(body.encode("utf-8")) <= source_context.MAX_LINE_BYTES


def test_fetch_preserves_short_lines_verbatim(tmp_path):
    f = tmp_path / "exact.py"
    f.write_text("short line\n")
    ctx = source_context.fetch(str(f), 1)
    assert ctx is not None
    assert ctx["context_line"] == "short line"


def test_fetch_handles_files_without_trailing_newline(tmp_path):
    f = tmp_path / "noeol.py"
    f.write_text("a\nb\nc")  # no trailing \n on last line
    ctx = source_context.fetch(str(f), 3)
    assert ctx is not None
    assert ctx["context_line"] == "c"
