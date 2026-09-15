"""Authenticated WebUI adapter for the optional personal platform."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from websockets.http11 import Response

from nanobot.webui.http_utils import http_json_response

if TYPE_CHECKING:
    from nanobot.personal.service import PersonalService
    from nanobot.webui.settings_services import WebUISettingsServices


class PersonalRoutes:
    def __init__(self, settings: WebUISettingsServices):
        self.settings = settings
        self._service: PersonalService | None = None
        self._signature: tuple[str, str] | None = None

    def enabled(self) -> bool:
        try:
            return self.settings.config.load().personal.enabled
        except Exception:
            return False

    async def dispatch(self, path: str, payload: dict[str, Any] | None) -> Response:
        config = await asyncio.to_thread(self.settings.config.load)
        if not config.personal.enabled:
            return http_json_response({"enabled": False}, status=200 if path.endswith("/status") else 404)
        try:
            from nanobot.personal.service import PersonalAction, PersonalService

            signature = (config.personal.model_dump_json(), str(config.workspace_path))
            if self._service is None or signature != self._signature:
                self._service = await asyncio.to_thread(PersonalService, config.personal, config.workspace_path)
                self._signature = signature
            if path.endswith("/status"):
                result = await asyncio.to_thread(self._service.status)
            else:
                if payload is None:
                    return http_json_response({"error": "Use authenticated WebSocket mutations"}, status=405)
                request = PersonalAction.model_validate(payload)
                result = await asyncio.to_thread(self._service.action, request)
            return http_json_response({"result": result}, extra_headers=[("Cache-Control", "no-store")])
        except Exception as exc:
            # Validation errors and network exceptions may include credentials or mail contents.
            return http_json_response({"error": "Personal action failed", "kind": type(exc).__name__}, status=400,
                                      extra_headers=[("Cache-Control", "no-store")])
