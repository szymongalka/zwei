"""Tests for apply_history_window boundary safety."""

from __future__ import annotations

from nanobot.agent.context import apply_history_window


def _user(text: str = "u") -> dict[str, str]:
    return {"role": "user", "content": text}


def _assistant_tool_calls(call_id: str = "c1") -> dict[str, object]:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": "exec", "arguments": "{}"}}
        ],
    }


def _tool(call_id: str = "c1") -> dict[str, str]:
    return {"role": "tool", "tool_call_id": call_id, "content": "result"}


def test_disabled_or_small_history_returned_unchanged() -> None:
    history = [_user("a"), _assistant_tool_calls(), _tool()]
    assert apply_history_window(history, 0) is history
    assert apply_history_window(history, 10) is history


def test_window_starts_at_user_boundary() -> None:
    history = [
        _user("old"),
        _assistant_tool_calls("old-call"),
        _tool("old-call"),
        _user("new"),
        _assistant_tool_calls("new-call"),
        _tool("new-call"),
    ]
    window = apply_history_window(history, 3)
    # A naive cut would start at the assistant tool-call message and orphan
    # its tool result; the window must move forward to the user message.
    assert window[0]["role"] == "user"
    assert window == history[3:]


def test_window_without_user_boundary_returns_full_history() -> None:
    history = [_assistant_tool_calls(), _tool(), _assistant_tool_calls("c2"), _tool("c2")]
    assert apply_history_window(history, 2) is history


def test_window_keeps_tail_when_limit_matches() -> None:
    history = [_user("a"), _assistant_tool_calls("a1"), _tool("a1"), _user("b")]
    assert apply_history_window(history, 4) is history
