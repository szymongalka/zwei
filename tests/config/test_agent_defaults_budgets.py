"""Tests for the background iteration budget and history window config fields."""

from __future__ import annotations

from nanobot.config.schema import AgentDefaults


def test_defaults_keep_upstream_compatible_values() -> None:
    defaults = AgentDefaults()

    assert defaults.max_tool_iterations == 200
    assert defaults.max_tool_iterations_background == 120
    assert defaults.max_history_messages == 0


def test_camel_case_aliases_parse() -> None:
    defaults = AgentDefaults.model_validate({
        "maxToolIterations": 24,
        "maxToolIterationsBackground": 90,
        "maxHistoryMessages": 60,
    })

    assert defaults.max_tool_iterations == 24
    assert defaults.max_tool_iterations_background == 90
    assert defaults.max_history_messages == 60


def test_serialization_uses_camel_case() -> None:
    dumped = AgentDefaults(
        max_tool_iterations_background=90,
        max_history_messages=60,
    ).model_dump(by_alias=True, exclude_none=True)

    assert dumped["maxToolIterationsBackground"] == 90
    assert dumped["maxHistoryMessages"] == 60
