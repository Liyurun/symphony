"""Regression tests for native TUI stream rendering helpers."""

from symphony.tui.native_tui import _stream_update


def test_stream_update_snapshot_returns_only_suffix():
    full, delta, rewrite = _stream_update("hello", "hello world", replace=True)

    assert full == "hello world"
    assert delta == " world"
    assert rewrite is False


def test_stream_update_skips_repeated_snapshot():
    full, delta, rewrite = _stream_update("hello", "hello", replace=True)

    assert full == "hello"
    assert delta == ""
    assert rewrite is False


def test_stream_update_after_tool_does_not_replay_old_text():
    # Tool events visually split the assistant output, but pi's next event can
    # still be a full cumulative snapshot. The TUI keeps a turn-global full text
    # so only the new suffix after the tool is rendered.
    full = "I will inspect files."

    full, delta, rewrite = _stream_update(
        full,
        "I will inspect files. The issue is fixed.",
        replace=True,
    )

    assert full == "I will inspect files. The issue is fixed."
    assert delta == " The issue is fixed."
    assert rewrite is False


def test_stream_update_true_delta_appends():
    full, delta, rewrite = _stream_update("hello", " world", replace=False)

    assert full == "hello world"
    assert delta == " world"
    assert rewrite is False

