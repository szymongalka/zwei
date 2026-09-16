"""The interactive watchdog: long turns keep pinging, automation turns stay silent."""

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop, TurnKind
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import GenerationSettings, LLMResponse
from nanobot.utils.progress_events import output_events


def _loop(tmp_path, **kwargs) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings()
    provider.chat_stream_with_retry = AsyncMock()
    return AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        cron_service=MagicMock(),
        **kwargs,
    )


def _message(key: str, content: str) -> InboundMessage:
    return InboundMessage(
        channel="websocket",
        sender_id="user",
        chat_id=key.removeprefix("websocket:"),
        content=content,
        session_key_override=key,
        require_existing_session=True,
    )


@pytest.mark.asyncio
async def test_long_interactive_turn_publishes_progress_ping(tmp_path) -> None:
    loop = _loop(tmp_path, progress_ping_seconds=0.05)
    seen: list[str] = []

    async def slow_turn(**_kwargs) -> LLMResponse:
        await asyncio.sleep(0.2)
        return LLMResponse(content="done", usage=None)

    loop.provider.chat_stream_with_retry.side_effect = slow_turn
    key = "websocket:ping-test"
    loop.sessions.get_or_create(key)

    async def on_progress(content: str, **_kwargs) -> None:
        seen.append(content)

    outbound = await loop._process_message(_message(key, "long task"), on_progress=on_progress)

    assert outbound is not None and outbound.content == "done"
    pings = [text for text in seen if "Pracuję dalej" in text]
    assert pings, "a slow interactive turn must announce that it is still working"

    # The monitor dies with the turn: no ping leaks into the idle period.
    await asyncio.sleep(0.12)
    assert [text for text in seen if "Pracuję dalej" in text] == pings


@pytest.mark.asyncio
async def test_progress_ping_disabled_by_config(tmp_path) -> None:
    loop = _loop(tmp_path, progress_ping_seconds=0)
    seen: list[str] = []

    async def slow_turn(**_kwargs) -> LLMResponse:
        await asyncio.sleep(0.2)
        return LLMResponse(content="done", usage=None)

    loop.provider.chat_stream_with_retry.side_effect = slow_turn
    key = "websocket:ping-off"
    loop.sessions.get_or_create(key)

    async def on_progress(content: str, **_kwargs) -> None:
        seen.append(content)

    await loop._process_message(_message(key, "long task"), on_progress=on_progress)

    assert not [text for text in seen if "Pracuję dalej" in text]


@pytest.mark.asyncio
async def test_automation_turns_are_not_monitored(tmp_path) -> None:
    loop = _loop(tmp_path, progress_ping_seconds=30)
    ctx = SimpleNamespace(
        kind=TurnKind.SYSTEM,
        turn_id="cron:1",
        turn_wall_started_at=0.0,
        events=output_events(),
    )
    assert loop._start_progress_monitor(ctx) is None

    interactive = SimpleNamespace(
        kind=TurnKind.USER,
        turn_id="websocket:1",
        turn_wall_started_at=0.0,
        events=output_events(),
    )
    # No publish sink (headless turns) means nothing to ping.
    assert loop._start_progress_monitor(interactive) is None

    interactive.events = output_events(on_progress=AsyncMock())
    monitor = loop._start_progress_monitor(interactive)
    assert monitor is not None
    monitor.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await monitor
