"""Topic notes and day notes: `MEMORY.md` stays an index, detail lives beside it.

The curated memory file is injected on every turn, so it cannot also be the store
of record for infrastructure detail.  This module owns the layer beside it:

- ``memory/notes/<topic>.md`` — one topic per file, read on demand in three levels
  (summary ~100 tokens, overview ~2k tokens, full text);
- ``memory/notes/<YYYY-MM-DD>.md`` — a day note: the deterministic trail of what
  happened at each session boundary, with a pointer to the episode that carries
  the content.

Nothing here calls a model and nothing here rewrites a memory file: entries are
appended, and the index lines are returned for whoever owns `MEMORY.md`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

NOTES_DIR = "memory/notes"
NOTE_LEVELS = ("summary", "overview", "full")
#: Roughly 100 and 2000 tokens, the three levels described in the design.
SUMMARY_CHARS = 400
OVERVIEW_CHARS = 8_000
DAY_NOTE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
_SLUG = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class NoteInfo:
    topic: str
    path: str
    chars: int
    day: str | None


def notes_dir(workspace: Path) -> Path:
    return workspace / NOTES_DIR


def topic_slug(topic: str) -> str:
    """File-safe name for a topic; empty input is rejected, not silently renamed."""
    slug = _SLUG.sub("-", topic.strip().lower()).strip("-")
    if not slug:
        raise ValueError("Topic name must contain at least one letter or digit")
    return slug


def note_path(workspace: Path, topic: str) -> Path:
    return notes_dir(workspace) / f"{topic_slug(topic)}.md"


def day_note_path(workspace: Path, day: str | None = None) -> Path:
    return notes_dir(workspace) / f"{day or datetime.now().strftime('%Y-%m-%d')}.md"


def list_notes(workspace: Path) -> list[NoteInfo]:
    """Every note with its size; day notes are marked so they sort apart."""
    directory = notes_dir(workspace)
    if not directory.is_dir():
        return []
    notes: list[NoteInfo] = []
    for path in sorted(directory.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        is_day = bool(DAY_NOTE_PATTERN.match(path.name))
        notes.append(NoteInfo(path.stem, f"{NOTES_DIR}/{path.name}", len(text),
                              path.stem if is_day else None))
    return notes


def read_note(workspace: Path, topic: str, level: str = "summary") -> str:
    """Read a note at one of three levels; an unknown level is an error, not a guess."""
    if level not in NOTE_LEVELS:
        raise ValueError(f"Unknown note level {level!r}; use one of {', '.join(NOTE_LEVELS)}")
    path = note_path(workspace, topic)
    if not path.is_file():
        raise ValueError(f"No note for topic {topic!r} ({NOTES_DIR}/{path.name})")
    text = path.read_text(encoding="utf-8")
    if level == "full":
        return text
    limit = SUMMARY_CHARS if level == "summary" else OVERVIEW_CHARS
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n\n[... {len(text)} characters; showing {limit}. ]"


def index_lines(workspace: Path, *, limit: int = 40) -> list[str]:
    """Index lines for `MEMORY.md`: one topic plus its pointer, never the content."""
    lines: list[str] = []
    for note in list_notes(workspace):
        if note.day is not None:
            continue
        head = _first_meaningful_line(note_path(workspace, note.topic))
        lines.append(f"- {note.topic}: {head} ({note.path}, {note.chars} chars)")
        if len(lines) >= limit:
            break
    return lines


def append_day_note(workspace: Path, line: str, *, day: str | None = None) -> Path:
    """Append one line to today's note; the same line twice is written once."""
    path = day_note_path(workspace, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    if not existing:
        existing = f"# Notatki dnia {path.stem}\n"
    stamped = f"- {datetime.now().strftime('%H:%M')} {line.strip()}"
    if stamped in existing.splitlines():
        return path
    path.write_text(existing.rstrip("\n") + "\n" + stamped + "\n", encoding="utf-8")
    return path


def append_episode_line(workspace: Path, session_key: str, summary: str, *,
                        day: str | None = None) -> Path:
    """Record a session boundary with a pointer to the episode that holds the content."""
    headline = _first_meaningful_line_text(summary)[:200]
    return append_day_note(workspace, f"[{session_key}] {headline}", day=day)


def _first_meaningful_line(path: Path) -> str:
    if not path.is_file():
        return ""
    return _first_meaningful_line_text(path.read_text(encoding="utf-8"))[:120]


def _first_meaningful_line_text(text: str) -> str:
    for raw in text.splitlines():
        line = raw.strip().lstrip("#").strip()
        if line:
            return line
    return ""
