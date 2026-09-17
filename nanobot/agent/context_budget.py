"""Budget for the static part of the prompt — the files injected on every turn.

Measured 2026-09-17: `AGENTS.md` 9.6 KB + `SOUL.md` 5.3 KB + `USER.md` 4.9 KB +
`memory/MEMORY.md` 24.5 KB ≈ 44 KB (~11k tokens) before the skills summary and the
tool schemas.  A memory file that grows raises the cost of every single turn, so
the four files are treated as one pool with a ceiling, plus a per-file limit where
growth is expected (`MEMORY.md`, `USER.md`).

Two rules keep the measurement honest:

- the file on disk is never truncated by this module — only the *copy* that goes
  into the prompt is cut, with a visible marker naming the file and its real size;
- a write that would grow an over-limit file is refused by the writer (see
  ``MemoryStore.write_memory``), while a write that shrinks it is allowed, so a
  consolidation in progress is never blocked.

``NANOBOT_MEMORY_BUDGET=0`` disables both the refusal and the truncation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: Ceiling for the four files injected on every turn.
STATIC_CONTEXT_BUDGET_BYTES = 24_000
#: Per-file limits; a file without a limit here is only counted into the pool.
MEMORY_LIMIT_CHARS = 12_000
USER_LIMIT_CHARS = 4_000
STATIC_FILES: tuple[str, ...] = ("AGENTS.md", "SOUL.md", "USER.md", "memory/MEMORY.md")
FILE_LIMITS: dict[str, int] = {
    "memory/MEMORY.md": MEMORY_LIMIT_CHARS,
    "USER.md": USER_LIMIT_CHARS,
}
BUDGET_ENV = "NANOBOT_MEMORY_BUDGET"


@dataclass(frozen=True)
class FileUsage:
    path: str
    chars: int
    size_bytes: int
    limit: int | None

    @property
    def over_limit(self) -> bool:
        return self.limit is not None and self.chars > self.limit


@dataclass(frozen=True)
class StaticContext:
    files: tuple[FileUsage, ...]
    total_bytes: int
    budget_bytes: int = STATIC_CONTEXT_BUDGET_BYTES

    @property
    def over_budget(self) -> bool:
        return self.total_bytes > self.budget_bytes

    def violations(self) -> list[str]:
        """One line per broken rule, empty when the pool and every file fit."""
        problems = [
            f"{usage.path}: {usage.chars} characters over the limit of {usage.limit}"
            for usage in self.files
            if usage.over_limit
        ]
        if self.over_budget:
            problems.append(
                f"static context: {self.total_bytes} bytes over the budget of {self.budget_bytes}"
            )
        return problems


def budget_enforced() -> bool:
    """Whether the budget is enforced; the environment switch is the rollback lever."""
    raw = os.environ.get(BUDGET_ENV)
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}


def measure(workspace: Path) -> StaticContext:
    """Measure the static files that reach every turn; missing files count as zero."""
    files: list[FileUsage] = []
    total = 0
    for name in STATIC_FILES:
        path = workspace / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        size = len(text.encode("utf-8"))
        total += size
        files.append(FileUsage(name, len(text), size, FILE_LIMITS.get(name)))
    return StaticContext(tuple(files), total)


def truncate_copy(text: str, limit: int, path: str) -> str:
    """Cut the prompt copy of an over-limit file and say so in place.

    The marker is deliberately longer than the freed characters: a silently
    shortened memory file is worse than a visible one, because the agent has to
    know that the rest exists on disk.
    """
    if limit <= 0 or len(text) <= limit:
        return text
    marker = (
        f"\n\n[... {path} is {len(text)} characters and only the first {limit} are shown. "
        f"Move the details into memory/notes/<topic>.md and keep one index line here; "
        f"read the full file with read_file if you need it. ...]"
    )
    return text[:limit] + marker
