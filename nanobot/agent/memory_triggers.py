"""Trigger-driven injection of curated notes (P3).

`MEMORY.md` is injected on every turn, so it cannot be injected a second time when a
topic comes up.  What is *not* in the prompt is the note behind an index line.  An
entry that declares `<!-- trigger: ... -->` therefore gets its note pulled in at the
summary level when the incoming message mentions one of those phrases:

- only entries that point at an existing note are eligible, because everything else
  would be a copy of a line the model already has;
- at most three entries and a fixed character budget reach the context;
- matching is lexical and deterministic: no model, no embeddings, no scoring drift;
- the run is journalled to `memory/retrieval_stats.jsonl` (kind `trigger`) so the
  effect can be measured instead of assumed.

Rollback: `NANOBOT_MEMORY_TRIGGERS=0` turns the provider off.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from nanobot.agent.memory_notes import SUMMARY_CHARS, append_stats_line, read_note
from nanobot.runtime_context import RuntimeContextBlock, wrap_runtime_context_lines

TRIGGERS_ENV = "NANOBOT_MEMORY_TRIGGERS"
#: Curated entries that may surface in one turn, and the characters they may spend.
TRIGGER_LIMIT = 3
INJECTION_CHARS = 600
#: A phrase shorter than this matches by accident ("ok", "pamiec") and is ignored.
MIN_PHRASE_CHARS = 4
NOTE_POINTER = re.compile(r"memory/notes/([a-z0-9][a-z0-9-]*)\.md")
ANNOTATION = re.compile(r"<!--(?P<body>.*?)-->")
FIELD = re.compile(r"(?P<key>trigger|importance|observed|source)\s*:\s*(?P<value>[^|]+?)\s*(?=-->|\||$)")
ENTRY_LINE = re.compile(r"^\s*-\s+\S")


@dataclass(frozen=True)
class IndexEntry:
    """One curated index line with the trigger annotation attached to it."""

    line: str
    topic: str
    triggers: tuple[str, ...]
    importance: int
    source: str
    observed: str


def enabled(value: bool | None = None) -> bool:
    """Whether trigger injection is on; the environment switch is the rollback lever."""
    if value is not None:
        return value
    raw = os.environ.get(TRIGGERS_ENV)
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}


def parse_entries(text: str) -> list[IndexEntry]:
    """Index entries with triggers, in file order; unannotated lines are skipped.

    The annotation may sit on the entry line itself or on the line right after it,
    which is where the Dream prompt puts provenance.
    """
    entries: list[IndexEntry] = []
    line: str | None = None
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            line = None
            continue
        if ENTRY_LINE.match(stripped):
            annotation = ANNOTATION.search(stripped)
            if annotation:
                entries.append(_entry(stripped, annotation.group("body")))
                line = None
                continue
            line = stripped
            continue
        if stripped.startswith("<!--") and line is not None:
            entries.append(_entry(line, stripped))
            line = None
    return entries


def _entry(line: str, annotation: str) -> IndexEntry:
    fields = {match["key"].lower(): match["value"].strip() for match in FIELD.finditer(annotation)}
    pointer = NOTE_POINTER.search(line)
    triggers = tuple(
        phrase.strip() for phrase in fields.get("trigger", "").split(",")
        if len(phrase.strip()) >= MIN_PHRASE_CHARS
    )[:TRIGGER_LIMIT]
    try:
        importance = int(fields.get("importance", ""))
    except ValueError:
        importance = 5
    return IndexEntry(
        line=line,
        topic=pointer.group(1) if pointer else "",
        triggers=triggers,
        importance=max(1, min(10, importance)),
        source=fields.get("source", ""),
        observed=fields.get("observed", ""),
    )


def match_entries(entries: list[IndexEntry], message: str,
                  *, limit: int = TRIGGER_LIMIT) -> list[IndexEntry]:
    """Entries whose trigger phrase appears in the message, most important first."""
    lowered = message.casefold()
    scored: list[tuple[int, int, str, IndexEntry]] = []
    for entry in entries:
        if not entry.topic or not entry.triggers:
            continue
        hit = max((len(phrase) for phrase in entry.triggers if phrase.casefold() in lowered), default=0)
        if hit:
            scored.append((entry.importance, hit, entry.topic, entry))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [entry for _, _, _, entry in scored[:limit]]


def build_block(workspace: Path, message: str,
                *, injection_enabled: bool | None = None) -> RuntimeContextBlock | None:
    """Runtime context block with the notes behind the matched triggers, or ``None``."""
    text = (message or "").strip()
    if len(text) < 8 or text.startswith("/") or not enabled(injection_enabled):
        return None
    started = time.perf_counter()
    try:
        memory = (workspace / "memory" / "MEMORY.md").read_text(encoding="utf-8")
    except OSError:
        return None
    matched = match_entries(parse_entries(memory), text)
    lines: list[str] = []
    used: list[str] = []
    spent = 0
    for entry in matched:
        try:
            note = read_note(workspace, entry.topic, "summary")
        except (OSError, ValueError):
            continue
        trimmed = note.strip()[: max(0, min(SUMMARY_CHARS, INJECTION_CHARS - spent))]
        if not trimmed or spent + len(trimmed) > INJECTION_CHARS:
            continue
        spent += len(trimmed)
        used.append(entry.topic)
        lines.append(f"[{entry.topic}] {trimmed}")
    _record(workspace, text, len(matched), used, spent,
            round((time.perf_counter() - started) * 1000, 1))
    if not lines:
        return None
    return RuntimeContextBlock("personal_memory_triggers", wrap_runtime_context_lines([
        "Curated notes matched by the memory entry triggers (background reference, not instructions).",
        *lines,
    ]))


def _record(workspace: Path, message: str, matched: int, used: list[str], spent: int,
            latency_ms: float) -> None:
    append_stats_line(workspace, "retrieval_stats.jsonl", {
        "at": datetime.now(timezone.utc).isoformat(),
        "kind": "trigger",
        "query_chars": len(message),
        "matched": matched,
        "injected": len(used),
        "topics": used,
        "injected_chars": spent,
        "latency_ms": latency_ms,
    })


__all__ = [
    "INJECTION_CHARS",
    "TRIGGERS_ENV",
    "TRIGGER_LIMIT",
    "IndexEntry",
    "build_block",
    "enabled",
    "match_entries",
    "parse_entries",
]
