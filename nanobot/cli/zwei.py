"""Fork entrypoint: choose a gateway session before opening the native terminal."""

from __future__ import annotations

import os
import sys

from nanobot.cli import entry

_PICKER_ENV = "ZWEI_TUI_SESSION_PICKER"


def main() -> None:
    """Keep explicit nanobot commands intact; bare ``zwei`` opens the chooser."""
    original_argv = sys.argv
    previous_picker = os.environ.pop(_PICKER_ENV, None)
    try:
        if len(original_argv) == 1:
            # Select the shared local gateway without a workspace override:
            # that override would address a separate gateway instance.
            sys.argv = [original_argv[0], "agent"]
            os.environ[_PICKER_ENV] = "1"
        entry.main()
    finally:
        sys.argv = original_argv
        os.environ.pop(_PICKER_ENV, None)
        if previous_picker is not None:
            os.environ[_PICKER_ENV] = previous_picker
