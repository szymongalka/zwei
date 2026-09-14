import os
import sys
from pathlib import Path

import pytest

from nanobot.cli import zwei
from nanobot.config.schema import Config


def test_bare_zwei_uses_configured_workspace_and_restores_launch_state(monkeypatch, tmp_path):
    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "personal")
    monkeypatch.setattr(zwei, "load_config", lambda: config)
    monkeypatch.setattr(sys, "argv", ["zwei"])
    monkeypatch.delenv("ZWEI_TUI_SESSION_PICKER", raising=False)
    monkeypatch.chdir(tmp_path)

    def launch():
        assert sys.argv == ["zwei", "--workspace", str(tmp_path / "personal")]
        assert os.environ["ZWEI_TUI_SESSION_PICKER"] == "1"
        raise SystemExit(0)

    monkeypatch.setattr(zwei.entry, "main", launch)
    with pytest.raises(SystemExit) as result:
        zwei.main()
    assert result.value.code == 0
    assert sys.argv == ["zwei"]
    assert "ZWEI_TUI_SESSION_PICKER" not in os.environ
    assert not (tmp_path / "personal").exists()
    assert Path.cwd() == tmp_path


@pytest.mark.parametrize("args", [
    ["status"], ["--help"], ["--session", "websocket:existing"],
    ["-m", "hello"], ["agent", "--classic"], ["--workspace", "/project"],
])
def test_explicit_commands_bypass_picker(monkeypatch, args):
    argv = ["zwei", *args]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setenv("ZWEI_TUI_SESSION_PICKER", "inherited")

    def launch():
        assert sys.argv == argv
        assert "ZWEI_TUI_SESSION_PICKER" not in os.environ

    monkeypatch.setattr(zwei.entry, "main", launch)
    zwei.main()
    assert os.environ["ZWEI_TUI_SESSION_PICKER"] == "inherited"
