from unittest.mock import MagicMock

import pytest

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.command.builtin import (
    build_help_text,
    cmd_model_menu,
    register_builtin_commands,
)
from nanobot.command.router import CommandContext, CommandRouter


def _ctx() -> CommandContext:
    msg = InboundMessage(
        channel="telegram", sender_id="user", chat_id="direct", content="/model_menu"
    )
    return CommandContext(
        msg=msg, session=None, key=msg.session_key, raw="/model_menu", args="", loop=MagicMock()
    )


@pytest.mark.asyncio
async def test_model_menu_returns_provider_buttons() -> None:
    out = await cmd_model_menu(_ctx())

    assert isinstance(out, OutboundMessage)
    assert "Menu modeli" in out.content
    assert out.buttons[0] == ["Menu modeli: ChatGPT", "Menu modeli: OpenRouter"]
    assert out.buttons[1] == ["Menu modeli: Gemini", "Menu modeli: pomiary"]


def test_model_menu_is_dispatchable() -> None:
    router = CommandRouter()
    register_builtin_commands(router)

    assert router.is_dispatchable_command("/model_menu")


def test_model_menu_listed_in_help() -> None:
    assert "/model_menu" in build_help_text()
