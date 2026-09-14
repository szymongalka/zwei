"""The personal extension preserves the gateway's authenticated transport boundary."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from websockets.datastructures import Headers
from websockets.http11 import Request

from nanobot.personal.config import PersonalConfig
from nanobot.webui.personal_routes import PersonalRoutes
from nanobot.webui.ws_http import GatewayHTTPHandler


def handler():
    result = object.__new__(GatewayHTTPHandler)
    result.config = SimpleNamespace(token_issue_path="")
    result.tokens = MagicMock()
    result.tokens.check_api_token.return_value = False
    result.personal_routes = MagicMock()
    result.personal_routes.dispatch = AsyncMock()
    return result


async def test_unauthenticated_status_cannot_inspect_accounts():
    gateway = handler()
    path = "/api/personal/status"
    response = await gateway._dispatch_resolved(None, Request(path, Headers()), path)
    assert response.status_code == 401
    gateway.personal_routes.dispatch.assert_not_called()


def test_personal_mutation_path_is_rejected_by_http_dispatch():
    assert handler()._is_webui_mutation_path("/api/personal/action") is True
    assert GatewayHTTPHandler._webui_mutation_path("personal.action", {}) == "/api/personal/action"


async def test_disabled_extension_creates_no_storage(tmp_path):
    settings = MagicMock()
    settings.config.load.return_value = SimpleNamespace(personal=PersonalConfig(data_dir=str(tmp_path / "data")))
    route = PersonalRoutes(settings)
    result = await route.dispatch("/api/personal/status", None)
    assert json.loads(result.body) == {"enabled": False}
    assert not (tmp_path / "data").exists()
    assert (await route.dispatch("/api/personal/action", {"action": "status"})).status_code == 404


async def test_invalid_account_errors_do_not_echo_secrets(tmp_path):
    settings = MagicMock()
    settings.config.load.return_value = SimpleNamespace(
        personal=PersonalConfig(enabled=True, data_dir=str(tmp_path / "data")), workspace_path=tmp_path)
    route = PersonalRoutes(settings)
    result = await route.dispatch("/api/personal/action", {"action": "save_account", "account": {"password": "secret-marker"}})
    assert result.status_code == 400
    assert "secret-marker" not in result.body.decode()
    assert result.headers["Cache-Control"] == "no-store"
