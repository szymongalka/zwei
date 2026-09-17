"""A bounded development turn using the existing agent, tools and session journal."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING

from filelock import FileLock, Timeout
from loguru import logger

from nanobot.agent.hook import AgentHook, AgentRunHookContext

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop
    from nanobot.personal.service import PersonalService


class DevelopmentOutcome(AgentHook):
    """Provider failures can return nonempty text; inspect the actual run outcome."""

    def __init__(self) -> None:
        super().__init__()
        self.stop_reason: str | None = None

    async def after_run(self, context: AgentRunHookContext) -> None:
        self.stop_reason = context.stop_reason


async def development_cycle(service: PersonalService, agent: AgentLoop) -> None:
    with FileLock(str(service.store.directory / "development.lock"), timeout=0):
        service.store.set_checkpoint("development_at", str(time.time()))
        service.store.set_checkpoint("development_state", "running")
        base = Path(__file__).with_name("development.md").read_text(encoding="utf-8")
        override = agent.workspace / "prompts" / "development.md"
        if override.is_file():
            base += "\n\nWorkspace-specific scope:\n" + override.read_text(encoding="utf-8")
        try:
            outcome = DevelopmentOutcome()
            # Keep this session key outside the "user" source: the agent loop gives a "user"
            # turn the small interactive tool budget, which cuts a background cycle short.
            response = await asyncio.wait_for(agent.process_direct(
                base, session_key="personal-development:" + service.store.namespace,
                channel="cli", chat_id="personal-development", hooks=[outcome],
            ), timeout=service.config.development_timeout_seconds)
            if outcome.stop_reason != "completed":
                raise RuntimeError("Development turn did not complete successfully")
            if response is None or not response.content:
                raise ValueError("Development turn produced no result")
            service.store.log_evolution("development_completed", {"summary": response.content[:8000]})
            service.store.set_checkpoint("development_state", "completed")
        except asyncio.CancelledError:
            service.store.set_checkpoint("development_state", "interrupted")
            raise
        except Exception as exc:
            service.store.set_checkpoint("development_state", "error:" + type(exc).__name__)
            service.store.log_evolution("development_failed", {"kind": type(exc).__name__})
            logger.warning("Personal development failed ({})", type(exc).__name__)


async def run_development(service: PersonalService, agent: AgentLoop) -> None:
    """Run independently so a development turn cannot block mailbox ingestion."""
    while True:
        last = float(service.store.checkpoint("development_at", "0"))
        if time.time() - last >= service.config.development_interval_seconds:
            try:
                await development_cycle(service, agent)
            except Timeout:
                pass
        await asyncio.sleep(60)
