from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.command.builtin import cmd_model_menu
from nanobot.command.router import CommandContext
from nanobot.config.schema import ModelPresetConfig

MENU_DATA = {
    "providers": {
        "TestProvider": {"families": {"TestFamily": ["Preset One", "Preset Two"]}},
    },
    "presets": {
        "Preset One": {
            "model": "test/one",
            "reasoning": "high",
            "weights": "n/d",
            "context": "1M",
            "price": "$1 / $2",
            "time": "~1 s",
            "use_case": "Do testow.",
        },
        "Preset Two": {"model": "test/two"},
    },
}


def _provider(default_model: str, max_tokens: int = 123) -> MagicMock:
    provider = MagicMock()
    provider.get_default_model.return_value = default_model
    provider.generation = SimpleNamespace(
        max_tokens=max_tokens,
        temperature=0.1,
        reasoning_effort=None,
    )
    return provider


def _make_loop(tmp_path: Path, *, model_presets: dict | None = None) -> AgentLoop:
    return AgentLoop(
        bus=MessageBus(),
        provider=_provider("base-model", max_tokens=123),
        workspace=tmp_path,
        model="base-model",
        context_window_tokens=1000,
        model_presets=model_presets
        or {
            "default": ModelPresetConfig(
                model="base-model",
                max_tokens=123,
                context_window_tokens=1000,
            ),
        },
    )


def _ctx(loop: AgentLoop, raw: str, args: str = "") -> CommandContext:
    msg = InboundMessage(channel="telegram", sender_id="user", chat_id="direct", content=raw)
    return CommandContext(msg=msg, session=None, key=msg.session_key, raw=raw, args=args, loop=loop)


@pytest.fixture()
def loop_with_menu(tmp_path: Path) -> AgentLoop:
    (tmp_path / "model-menu.json").write_text(
        str(MENU_DATA).replace("'", '"'), encoding="utf-8"
    )
    return _make_loop(tmp_path)


@pytest.mark.asyncio
async def test_model_menu_no_args_lists_providers(loop_with_menu: AgentLoop) -> None:
    out = await cmd_model_menu(_ctx(loop_with_menu, "/model_menu"))

    assert isinstance(out, OutboundMessage)
    assert "Menu modeli" in out.content
    assert "/model_menu TestProvider" in out.buttons[0]
    assert out.buttons[-1] == ["/model_menu pomiary"]


@pytest.mark.asyncio
async def test_model_menu_provider_lists_families(loop_with_menu: AgentLoop) -> None:
    out = await cmd_model_menu(_ctx(loop_with_menu, "/model_menu TestProvider", args="TestProvider"))

    assert "TestProvider" in out.content
    assert ["/model_menu TestFamily"] in out.buttons
    assert out.buttons[-1] == ["/model_menu"]


@pytest.mark.asyncio
async def test_model_menu_family_lists_presets_with_cards(loop_with_menu: AgentLoop) -> None:
    out = await cmd_model_menu(_ctx(loop_with_menu, "/model_menu TestFamily", args="TestFamily"))

    assert "Preset One — model: test/one" in out.content
    assert "reasoning: high" in out.content
    assert "Do testow." in out.content
    assert ["/model Preset One", "/model Preset Two"] in out.buttons
    assert out.buttons[-1] == ["/model_menu TestProvider"]


@pytest.mark.asyncio
async def test_model_menu_measurements_level(loop_with_menu: AgentLoop) -> None:
    out = await cmd_model_menu(_ctx(loop_with_menu, "/model_menu pomiary", args="pomiary"))

    assert "~1 s" in out.content
    assert "Preset Two" not in out.content.split("Odświeżenie")[0].replace("Ostatnie", "")


@pytest.mark.asyncio
async def test_model_menu_unknown_arg_falls_back_to_providers(loop_with_menu: AgentLoop) -> None:
    out = await cmd_model_menu(_ctx(loop_with_menu, "/model_menu nonsense", args="nonsense"))

    assert "Nieznany poziom menu" in out.content
    assert "/model_menu TestProvider" in out.buttons[0]


@pytest.mark.asyncio
async def test_model_menu_falls_back_to_configured_presets(tmp_path: Path) -> None:
    loop = _make_loop(
        tmp_path,
        model_presets={
            "fast": ModelPresetConfig(
                model="openai/gpt-4.1",
                provider="openai",
                max_tokens=4096,
                context_window_tokens=32768,
            ),
        },
    )
    out = await cmd_model_menu(_ctx(loop, "/model_menu"))

    assert out.buttons
    assert "/model_menu pomiary" in out.buttons[-1]
