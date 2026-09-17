"""Memory storage, transcript archiving, and session checkpoint consolidation."""

# Tool schemas are installed by the ``@tool_parameters`` class decorator at
# runtime; static analyzers cannot observe that it clears ``parameters`` from
# ``__abstractmethods__`` before these classes are instantiated.
# pyright: reportAbstractUsage=false, reportPrivateUsage=false

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import weakref
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator, Mapping, cast
from uuid import uuid4

from loguru import logger

from nanobot.agent.context_budget import (
    MEMORY_LIMIT_CHARS,
    USER_LIMIT_CHARS,
    StaticContext,
    budget_enforced,
    measure,
)
from nanobot.agent.memory_gate import (
    TARGET_LIMITS,
    Candidate,
    WriteOutcome,
    apply_decisions,
    collect_candidates,
    fallback_entries,
    parse_decisions,
    render_candidates,
)
from nanobot.agent.memory_notes import append_day_note
from nanobot.events import NO_EVENTS, ContextCompactionEvent, EventSink
from nanobot.llm_usage.context import llm_usage_source
from nanobot.providers.base import LLMResponse, ProviderConversationState
from nanobot.providers.conversation_state import ProviderConversationStateController
from nanobot.runtime_context import public_history_messages
from nanobot.session.manager import Session, SessionManager
from nanobot.session.summary import is_summary_checkpoint, session_summary_from_metadata
from nanobot.session_kinds import is_candidate, session_kind
from nanobot.utils.gitstore import GitStore
from nanobot.utils.helpers import (
    build_assistant_message,
    content_with_media_breadcrumbs,
    ensure_dir,
    estimate_prompt_tokens_chain,
    strip_think,
    truncate_text,
    truncate_text_to_tokens,
)
from nanobot.utils.prompt_templates import render_template
from nanobot.utils.workspace_prompts import (
    WORKSPACE_PROMPT_MAX_CHARS,
    has_workspace_prompt_override,
    load_workspace_prompt_override,
    workspace_prompt_file,
)

if TYPE_CHECKING:
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.utils.llm_runtime import LLMRuntime

# ---------------------------------------------------------------------------
# MemoryStore — pure file I/O layer
# ---------------------------------------------------------------------------


class MemoryStore:
    """Pure file I/O for memory files: MEMORY.md, history.jsonl, SOUL.md, USER.md."""

    _DEFAULT_MAX_HISTORY = 1000
    # Durable files whose real working-tree delta grounds Dream commit messages.
    # Deliberately excludes memory/.dream_cursor so progress bookkeeping never
    # appears as a durable-memory edit in the audit record.
    _DREAM_CONTENT_PATHS = ("SOUL.md", "USER.md", "memory/MEMORY.md")
    _LEGACY_ENTRY_START_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}[^\]]*)\]\s*")
    _LEGACY_TIMESTAMP_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\]\s*")
    _LEGACY_RAW_MESSAGE_RE = re.compile(
        r"^\[\d{4}-\d{2}-\d{2}[^\]]*\]\s+[A-Z][A-Z0-9_]*(?:\s+\[tools:\s*[^\]]+\])?:"
    )

    def __init__(self, workspace: Path, max_history_entries: int = _DEFAULT_MAX_HISTORY):
        self.workspace = workspace
        self.archive_sink: Callable[[str, list[dict[str, Any]], str], str | None] | None = None
        #: Called with (session_key, summary, messages) after a successful compaction, so
        #: the curated layer can store a task episode instead of the whole transcript.
        self.episode_sink: Callable[[str, str, list[dict[str, Any]]], object] | None = None
        self.max_history_entries = max_history_entries
        self.memory_dir = ensure_dir(workspace / "memory")
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "history.jsonl"
        self.legacy_history_file = self.memory_dir / "HISTORY.md"
        self.soul_file = workspace / "SOUL.md"
        self.user_file = workspace / "USER.md"
        self._cursor_file = self.memory_dir / ".cursor"
        self._dream_cursor_file = self.memory_dir / ".dream_cursor"
        self._corruption_logged = False  # rate-limit invalid cursor warning
        self._malformed_entry_logged = False  # rate-limit bad history shape warning
        self._oversize_logged = False  # rate-limit oversized-entry warning
        self._dream_prompt_oversize_logged = False
        self._last_dream_batch: dict[str, Any] | None = None
        self._last_dream_candidates: list[Candidate] = []
        self._last_dream_baseline: dict[str, str] = {}
        self._append_lock = threading.Lock()  # serialize cursor allocation + append
        # Ingest hygiene is on by default; the environment switch is the rollback
        # lever, because the journal has no configuration section of its own.
        self.history_hygiene = _env_flag(HISTORY_HYGIENE_ENV, default=True)
        # Dream mode: "legacy" lets the model rewrite the files, "gated" makes the
        # model return decisions that a deterministic writer applies.
        self.dream_mode = _dream_mode_from_env()
        self._git = GitStore(workspace, tracked_files=[
            "SOUL.md", "USER.md", "memory/MEMORY.md", "memory/.dream_cursor",
        ])
        self._maybe_migrate_legacy_history()

    @property
    def git(self) -> GitStore:
        return self._git

    # -- generic helpers -----------------------------------------------------

    @staticmethod
    def read_file(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def _maybe_migrate_legacy_history(self) -> None:
        """One-time upgrade from legacy HISTORY.md to history.jsonl.

        The migration is best-effort and prioritizes preserving as much content
        as possible over perfect parsing.
        """
        if not self.legacy_history_file.exists():
            return
        if self.history_file.exists() and self.history_file.stat().st_size > 0:
            return

        try:
            legacy_text = self.legacy_history_file.read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            logger.exception("Failed to read legacy HISTORY.md for migration")
            return

        entries = self._parse_legacy_history(legacy_text)
        try:
            if entries:
                self._write_entries(entries)
                last_cursor = entries[-1]["cursor"]
                self._cursor_file.write_text(str(last_cursor), encoding="utf-8")
                # Default to "already processed" so upgrades do not replay the
                # user's entire historical archive into Dream on first start.
                self._dream_cursor_file.write_text(str(last_cursor), encoding="utf-8")

            backup_path = self._next_legacy_backup_path()
            self.legacy_history_file.replace(backup_path)
            logger.info(
                "Migrated legacy HISTORY.md to history.jsonl ({} entries)",
                len(entries),
            )
        except Exception:
            logger.exception("Failed to migrate legacy HISTORY.md")

    def _parse_legacy_history(self, text: str) -> list[dict[str, Any]]:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            return []

        fallback_timestamp = self._legacy_fallback_timestamp()
        entries: list[dict[str, Any]] = []
        chunks = self._split_legacy_history_chunks(normalized)

        for cursor, chunk in enumerate(chunks, start=1):
            timestamp = fallback_timestamp
            content = chunk
            match = self._LEGACY_TIMESTAMP_RE.match(chunk)
            if match:
                timestamp = match.group(1)
                remainder = chunk[match.end():].lstrip()
                if remainder:
                    content = remainder

            entries.append({
                "cursor": cursor,
                "timestamp": timestamp,
                "content": content,
            })
        return entries

    def _split_legacy_history_chunks(self, text: str) -> list[str]:
        lines = text.split("\n")
        chunks: list[str] = []
        current: list[str] = []
        saw_blank_separator = False

        for line in lines:
            if saw_blank_separator and line.strip() and current:
                chunks.append("\n".join(current).strip())
                current = [line]
                saw_blank_separator = False
                continue
            if self._should_start_new_legacy_chunk(line, current):
                chunks.append("\n".join(current).strip())
                current = [line]
                saw_blank_separator = False
                continue
            current.append(line)
            saw_blank_separator = not line.strip()

        if current:
            chunks.append("\n".join(current).strip())
        return [chunk for chunk in chunks if chunk]

    def _should_start_new_legacy_chunk(self, line: str, current: list[str]) -> bool:
        if not current:
            return False
        if not self._LEGACY_ENTRY_START_RE.match(line):
            return False
        if self._is_raw_legacy_chunk(current) and self._LEGACY_RAW_MESSAGE_RE.match(line):
            return False
        return True

    def _is_raw_legacy_chunk(self, lines: list[str]) -> bool:
        first_nonempty = next((line for line in lines if line.strip()), "")
        match = self._LEGACY_TIMESTAMP_RE.match(first_nonempty)
        if not match:
            return False
        return first_nonempty[match.end():].lstrip().startswith("[RAW]")

    def _legacy_fallback_timestamp(self) -> str:
        try:
            return datetime.fromtimestamp(
                self.legacy_history_file.stat().st_mtime,
            ).strftime("%Y-%m-%d %H:%M")
        except OSError:
            return datetime.now().strftime("%Y-%m-%d %H:%M")

    def _next_legacy_backup_path(self) -> Path:
        candidate = self.memory_dir / "HISTORY.md.bak"
        suffix = 2
        while candidate.exists():
            candidate = self.memory_dir / f"HISTORY.md.bak.{suffix}"
            suffix += 1
        return candidate

    # -- MEMORY.md (long-term facts) -----------------------------------------

    def read_memory(self) -> str:
        return self.read_file(self.memory_file)

    def write_memory(self, content: str) -> None:
        self._check_budget(self.memory_file, content, MEMORY_LIMIT_CHARS)
        self.memory_file.write_text(content, encoding="utf-8")

    # -- SOUL.md -------------------------------------------------------------

    def read_soul(self) -> str:
        return self.read_file(self.soul_file)

    def write_soul(self, content: str) -> None:
        self.soul_file.write_text(content, encoding="utf-8")

    # -- USER.md -------------------------------------------------------------

    def read_user(self) -> str:
        return self.read_file(self.user_file)

    def write_user(self, content: str) -> None:
        self._check_budget(self.user_file, content, USER_LIMIT_CHARS)
        self.user_file.write_text(content, encoding="utf-8")

    # -- context injection (used by context.py) ------------------------------

    def get_memory_context(self) -> str:
        long_term = self.read_memory()
        return f"## Long-term Memory\n{long_term}" if long_term else ""

    def static_context(self) -> StaticContext:
        """Measure the files that are injected on every turn."""
        return measure(self.workspace)

    def flush_day_note(self, session_key: str, *, first_cursor: int, last_cursor: int,
                       entries: int, characters: int) -> Path | None:
        """Record a session boundary in today's note; it never gates the archive.

        The note is the readable trail of the day; the content of what happened
        lives in the journal entry and in the episode that the same boundary
        produced, so this line only has to say where to look.
        """
        span = (f"journal cursors {first_cursor}-{last_cursor}"
                if first_cursor and last_cursor else f"{entries} messages")
        try:
            return append_day_note(self.workspace, f"[{session_key}] {span} ({characters} chars)")
        except OSError:
            logger.warning("Day note is not writable under {}", self.memory_dir / "notes")
            return None

    def _check_budget(self, path: Path, content: str, limit: int) -> None:
        """Refuse a write that grows an over-limit file without consolidating it.

        A write that shrinks the file is always allowed, even while the file is
        still over its limit: a consolidation in progress must not be blocked by
        the rule that exists to force it. The refusal message carries what the
        writer needs to fix it in the same turn instead of guessing.
        """
        if not budget_enforced() or len(content) <= limit:
            return
        current = path.read_text(encoding="utf-8") if path.is_file() else ""
        if len(content) < len(current):
            return
        headings = [line.strip() for line in current.splitlines() if line.startswith("#")]
        raise ValueError(
            f"{path.name} would hold {len(content)} characters; the limit is {limit}. "
            "Move the details into memory/notes/<topic>.md and keep one index line per topic "
            "instead of growing this file. Current sections: " + ", ".join(headings[:12]) + "."
        )

    # -- history.jsonl — append-only, JSONL format ---------------------------

    def _normalize_history_entry(
        self,
        entry: str,
        *,
        max_chars: int | None = None,
    ) -> str:
        """Return the exact bounded, model-safe text accepted by the journal."""
        limit = max_chars if max_chars is not None else _HISTORY_ENTRY_HARD_CAP
        raw = entry.rstrip()
        content = strip_think(raw)
        if len(content) > limit:
            if not self._oversize_logged:
                self._oversize_logged = True
                logger.warning(
                    "history entry exceeds {} chars ({}); truncating. "
                    "Usually means a caller forgot its own cap; "
                    "further occurrences suppressed.",
                    limit,
                    len(content),
                )
            content = truncate_text(content, limit)
        return content

    def append_history(
        self,
        entry: str,
        *,
        max_chars: int | None = None,
        session_key: str | None = None,
        origin: str = "agent",
    ) -> int:
        """Append *entry* to history.jsonl and return its auto-incrementing cursor.

        Entries are passed through `strip_think` to drop template-level leaks
        (e.g. unclosed `<think` prefixes, `<channel|>` markers) before being
        persisted. If the cleaned content is empty but the raw entry wasn't,
        the record is persisted with an empty string rather than falling back
        to the raw leak — otherwise `strip_think`'s guarantees would be
        undone when Dream consumes the journal entry.

        A defensive cap (*max_chars*, default ``_HISTORY_ENTRY_HARD_CAP``) is
        applied as a final safety net: individual callers should cap their own
        content more tightly; this default only exists to catch unintentional
        large writes (e.g. an LLM echoing its input back as a "summary").

        Every record carries its provenance: the kind of session that produced it,
        who is accountable for it (*origin*) and a content hash, so the Dream gate
        can drop scheduled-session noise and repeated content without reading the
        prose. With ingest hygiene enabled (the default) an entry that only
        restates a recent one is not written at all.
        """
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        raw = entry.rstrip()
        content = self._normalize_history_entry(entry, max_chars=max_chars)
        if self.history_hygiene:
            rejection = self._ingest_rejection(content)
            if rejection:
                logger.debug(
                    "history entry skipped by ingest hygiene ({}): {} chars from {}",
                    rejection,
                    len(content),
                    session_key or "unknown",
                )
                return self.get_latest_cursor()
        # Cursor allocation and the append must be atomic: concurrent writers
        # could otherwise read the same current cursor and emit duplicates.
        with self._append_lock:
            cursor = self._next_cursor()
            if raw and not content:
                logger.debug(
                    "history entry {} stripped to empty (likely template leak); "
                    "persisting empty content to avoid re-polluting Dream input",
                    cursor,
                )
            record = {"cursor": cursor, "timestamp": ts, "content": content,
                      "session_kind": session_kind(session_key), "origin": origin,
                      "content_hash": _content_hash(content)}
            if session_key:
                record["session_key"] = session_key
            with open(self.history_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._cursor_file.write_text(str(cursor), encoding="utf-8")
        return cursor

    def _ingest_rejection(self, content: str) -> str:
        """Why this content does not deserve a journal entry, or empty when it does.

        Empty content is not rejected: `strip_think` deliberately persists an empty
        record instead of a leaked template, and that contract belongs to the
        journal, not to this filter.
        """
        if not content.strip():
            return ""
        if _is_boilerplate(content):
            return "boilerplate"
        digest = _content_hash(content)
        for entry in reversed(self._read_entries()[-HISTORY_DEDUP_WINDOW:]):
            stored = entry.get("content_hash")
            if not isinstance(stored, str):
                stored = _content_hash(str(entry.get("content", "")))
            if stored == digest:
                return "duplicate"
        return ""

    @staticmethod
    def _valid_cursor(value: Any) -> int | None:
        """Non-negative int cursors only; reject bool (``isinstance(True, int)`` is True)."""
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    def _iter_valid_entries(self) -> Iterator[tuple[dict[str, Any], int]]:
        """Yield ``(entry, cursor)`` for well-formed entries; warn once on corruption."""
        poisoned: Any = None
        malformed_cursor: int | None = None
        for entry in self._read_entries():
            raw = entry.get("cursor")
            if raw is None:
                continue
            cursor = self._valid_cursor(raw)
            if cursor is None:
                poisoned = raw
                continue
            if not self._valid_history_payload(entry):
                malformed_cursor = cursor
                continue
            yield entry, cursor
        if poisoned is not None and not self._corruption_logged:
            self._corruption_logged = True
            logger.warning(
                "history.jsonl contains an invalid cursor ({!r}); dropping it. "
                "Usually caused by an external writer; further occurrences suppressed.",
                poisoned,
            )
        if malformed_cursor is not None and not self._malformed_entry_logged:
            self._malformed_entry_logged = True
            logger.warning(
                "history.jsonl contains a malformed entry at cursor {}; dropping it. "
                "Usually caused by an external writer; further occurrences suppressed.",
                malformed_cursor,
            )

    @staticmethod
    def _valid_history_payload(entry: dict[str, Any]) -> bool:
        if not isinstance(entry.get("timestamp"), str):
            return False
        if not isinstance(entry.get("content"), str):
            return False
        session_key = entry.get("session_key")
        return session_key is None or isinstance(session_key, str)

    def _read_cursor_counter(self) -> int | None:
        """Return the persisted cursor counter when it is usable."""
        if not self._cursor_file.exists():
            return None
        with suppress(ValueError, OSError):
            cursor = int(self._cursor_file.read_text(encoding="utf-8").strip())
            if cursor >= 0:
                return cursor
        return None

    def _next_cursor(self) -> int:
        """Read the current cursor counter and return the next value."""
        cursor_counter = self._read_cursor_counter()
        last = self._read_last_entry() or {}
        last_cursor = self._valid_cursor(last.get("cursor"))
        if cursor_counter is not None:
            if last_cursor is not None:
                return max(cursor_counter, last_cursor) + 1
            max_history_cursor = max((c for _, c in self._iter_valid_entries()), default=0)
            return max(cursor_counter, max_history_cursor) + 1

        # Fast path: trust the tail when intact.  Otherwise scan the whole
        # file and take ``max`` — that stays correct even if the monotonic
        # invariant was broken by external writes.
        if last_cursor is not None:
            return last_cursor + 1
        return max((c for _, c in self._iter_valid_entries()), default=0) + 1

    def read_unprocessed_history(self, since_cursor: int) -> list[dict[str, Any]]:
        """Return history entries with a valid cursor > *since_cursor*."""
        return [e for e, c in self._iter_valid_entries() if c > since_cursor]

    def compact_history(self) -> None:
        """Drop oldest processed entries without discarding pending Dream input."""
        if self.max_history_entries <= 0:
            return
        entries = self._read_entries()
        if len(entries) <= self.max_history_entries:
            return
        last_dream_cursor = self.get_last_dream_cursor()
        first_unprocessed = next(
            (
                index
                for index, entry in enumerate(entries)
                if (
                    (cursor := self._valid_cursor(entry.get("cursor"))) is not None
                    and cursor > last_dream_cursor
                )
            ),
            len(entries),
        )
        keep_from = min(len(entries) - self.max_history_entries, first_unprocessed)
        kept = entries[keep_from:]
        if len(kept) > self.max_history_entries:
            logger.warning(
                "History compaction retained {} unprocessed entries beyond the configured "
                "limit of {}",
                len(kept),
                self.max_history_entries,
            )
        self._write_entries(kept)

    # -- JSONL helpers -------------------------------------------------------

    def _read_entries(self) -> list[dict[str, Any]]:
        """Read all entries from history.jsonl."""
        entries: list[dict[str, Any]] = []
        with suppress(FileNotFoundError):
            with open(self.history_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            parsed: object = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(parsed, dict):
                            entries.append(cast(dict[str, Any], parsed))

        return entries

    def _read_last_entry(self) -> dict[str, Any] | None:
        """Read the last entry from the JSONL file efficiently."""
        try:
            with open(self.history_file, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                if size == 0:
                    return None
                read_size = min(size, 4096)
                f.seek(size - read_size)
                data = f.read().decode("utf-8")
                lines = [line for line in data.split("\n") if line.strip()]
                if not lines:
                    return None
                parsed: object = json.loads(lines[-1])
                return cast(dict[str, Any], parsed) if isinstance(parsed, dict) else None
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _write_entries(self, entries: list[dict[str, Any]]) -> None:
        """Overwrite history.jsonl with the given entries (atomic write)."""
        tmp_path = self.history_file.with_suffix(self.history_file.suffix + ".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                for entry in entries:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.history_file)

            # fsync the directory so the rename is durable.
            # On Windows, opening a directory with O_RDONLY raises
            # PermissionError — skip the dir sync there (NTFS
            # journals metadata synchronously).
            with suppress(PermissionError):
                fd = os.open(str(self.history_file.parent), os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    # -- dream cursor --------------------------------------------------------

    def get_last_dream_cursor(self) -> int:
        if self._dream_cursor_file.exists():
            with suppress(ValueError, OSError):
                return int(self._dream_cursor_file.read_text(encoding="utf-8").strip())
        return 0

    def set_last_dream_cursor(self, cursor: int) -> None:
        self._dream_cursor_file.write_text(str(cursor), encoding="utf-8")

    def get_latest_cursor(self) -> int:
        return max(self._next_cursor() - 1, 0)

    @property
    def dream_prompt_file(self) -> Path:
        name = "dream_gated" if self.dream_mode == "gated" else "dream"
        return workspace_prompt_file(self.workspace, name)

    def has_dream_prompt_override(self) -> bool:
        return has_workspace_prompt_override(self.dream_prompt_file)

    @staticmethod
    def default_dream_prompt() -> str:
        from nanobot.agent.skills import BUILTIN_SKILLS_DIR

        return render_template(
            "agent/dream.md",
            strip=True,
            skill_creator_path=str(BUILTIN_SKILLS_DIR / "skill-creator" / "SKILL.md"),
        )

    @staticmethod
    def default_gated_dream_prompt() -> str:
        """The decision contract used when Dream runs in gated mode."""
        return render_template("agent/dream_gated.md", strip=True)

    def _default_dream_template(self) -> str:
        return (self.default_gated_dream_prompt() if self.dream_mode == "gated"
                else self.default_dream_prompt())

    def _dream_template(self) -> str:
        text, original_chars = load_workspace_prompt_override(self.dream_prompt_file)
        if text is not None:
            if (
                original_chars > WORKSPACE_PROMPT_MAX_CHARS
                and not self._dream_prompt_oversize_logged
            ):
                self._dream_prompt_oversize_logged = True
                logger.warning(
                    "workspace Dream prompt exceeds {} chars ({}); truncating. "
                    "Further occurrences suppressed.",
                    WORKSPACE_PROMPT_MAX_CHARS, original_chars,
                )
            return text
        return self._default_dream_template()

    def build_dream_prompt(self, *, max_entries: int = 20) -> tuple[str, int] | None:
        """Build the Dream prompt with unprocessed history context.

        Returns ``(prompt, last_cursor)`` or ``None`` if nothing to process.

        The current contents of the durable memory files (SOUL.md, USER.md,
        memory/MEMORY.md) reach Dream through the normal agent system context.
        """
        if self.dream_mode == "gated":
            return self._build_gated_dream_prompt(max_entries=max_entries)
        last_cursor = self.get_last_dream_cursor()
        entries = self.read_unprocessed_history(since_cursor=last_cursor)
        if not entries:
            return None

        window = entries[: max(DREAM_SCAN_WINDOW, max_entries)]
        selected: list[dict[str, Any]] = []
        covered = 0
        for index, entry in enumerate(window):
            if self.history_hygiene and not is_candidate(entry.get("session_key")):
                continue
            selected.append(entry)
            covered = index + 1
            if len(selected) >= max_entries:
                break
        if not selected:
            logger.info(
                "Dream: {} unprocessed entries, none is a memory candidate; staying idle",
                len(window),
            )
            return None
        original_batch = selected
        excluded = covered - len(selected)
        remaining = len(entries) - covered
        batch, duplicates = dedupe_history_batch(original_batch)
        batch, repeats = _dedupe_by_content_hash(batch)
        history_text = "\n".join(
            f"[{e['timestamp']}] {truncate_text(e['content'], DREAM_PROMPT_ENTRY_CHARS)}"
            for e in batch
        )
        template = self._dream_template()
        header = self._dream_batch_header(batch, covered=covered, remaining=remaining,
                                          duplicates=duplicates, excluded=excluded,
                                          repeats=repeats)
        prompt = f"{template}\n\n{header}\n\n## Conversation History\n{history_text}"
        # The cursor must move past every entry of the batch, including collapsed
        # duplicates: their information is already covered by the newest copy.
        last_batch_cursor = original_batch[-1]["cursor"]
        self._last_dream_batch = {
            "entries": covered,
            "shown": len(batch),
            "duplicates": duplicates,
            "excluded": excluded,
            "repeats": repeats,
            "first_cursor": original_batch[0]["cursor"],
            "last_cursor": last_batch_cursor,
            "remaining": remaining,
        }
        return (prompt, last_batch_cursor)

    @staticmethod
    def _dream_batch_header(
        batch: list[dict[str, Any]],
        *,
        covered: int,
        remaining: int,
        duplicates: int,
        excluded: int = 0,
        repeats: int = 0,
    ) -> str:
        """Tell Dream exactly which slice of the journal it is reading."""
        first = batch[0]["cursor"] if batch else None
        last = batch[-1]["cursor"] if batch else None
        lines = [
            "## History batch",
            f"- This run covers history cursors {first}-{last}: {covered} entries, "
            f"{len(batch)} shown below.",
            f"- {remaining} further journal entries remain unprocessed; they arrive in a later run.",
        ]
        if excluded:
            lines.append(
                f"- {excluded} entries from scheduled or background sessions were not candidates "
                "for durable memory and are not shown."
            )
        if duplicates:
            lines.append(
                f"- {duplicates} near-duplicate entries were collapsed in this view; the newest copy is shown."
            )
        if repeats:
            lines.append(f"- {repeats} exact repeats were collapsed in this view.")
        lines.append(
            "- Consolidate only what this batch supports and do not claim the whole journal was processed."
        )
        return "\n".join(lines)

    # -- gated mode (P2) ------------------------------------------------------

    def _build_gated_dream_prompt(self, *, max_entries: int) -> tuple[str, int] | None:
        """Offer the gate's candidates and ask for decisions instead of prose."""
        last_cursor = self.get_last_dream_cursor()
        entries = self.read_unprocessed_history(since_cursor=last_cursor)
        if not entries:
            return None
        window = entries[: max(DREAM_SCAN_WINDOW, max_entries)]
        eligible = [entry for entry in window
                    if not self.history_hygiene or is_candidate(entry.get("session_key"))]
        candidates, rejected = collect_candidates(eligible)
        if not candidates:
            logger.info(
                "Dream (gated): {} entries, none passed the promotion gate ({})",
                len(window), rejected,
            )
            return None
        batch = candidates[:max_entries]
        covered = max(candidate.cursor for candidate in batch)
        remaining = sum(1 for entry in entries if int(entry.get("cursor", 0)) > covered)
        reasons = ", ".join(f"{name} {count}" for name, count in sorted(rejected.items())) or "none"
        header = "\n".join([
            "## Promotion gate",
            f"- {len(candidates)} of {len(window)} considered entries passed the gate; rejected: {reasons}.",
            f"- This run covers cursors {min(c.cursor for c in batch)}-{covered} "
            f"({len(batch)} candidates shown below).",
            f"- {remaining} further journal entries remain unprocessed; they arrive in a later run.",
            "- Return decisions for these candidates only.",
        ])
        prompt = "\n\n".join([self._dream_template(), header,
                               "## Candidates\n" + render_candidates(batch)])
        self._last_dream_batch = {
            "entries": len(window),
            "shown": len(batch),
            "duplicates": rejected.get("duplicate", 0),
            "excluded": len(window) - len(eligible),
            "repeats": 0,
            "first_cursor": min(candidate.cursor for candidate in batch),
            "last_cursor": covered,
            "remaining": remaining,
        }
        self._last_dream_candidates = batch
        # Optimistic concurrency: the writer refuses to build on a file that changed
        # while the model was thinking.
        self._last_dream_baseline = {name: _content_digest(self._read_durable(name))
                                     for name in TARGET_LIMITS}
        return (prompt, covered)

    def _durable_paths(self) -> dict[str, Path]:
        return {"MEMORY.md": self.memory_file, "USER.md": self.user_file}

    def _read_durable(self, name: str) -> str:
        path = self._durable_paths()[name]
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def _write_durable(self, name: str, content: str) -> None:
        if name == "MEMORY.md":
            self.write_memory(content)
        else:
            self.write_user(content)

    def _pre_image(self, name: str) -> str | None:
        """Keep the previous content of a durable file before a gated write."""
        path = self._durable_paths()[name]
        if not path.is_file():
            return None
        directory = ensure_dir(self.memory_dir / "pre-image")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = directory / f"{path.name}.{stamp}"
        try:
            target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError as exc:
            logger.warning("Dream pre-image not written ({}): {}", type(exc).__name__, target)
            return None
        for stale in sorted(directory.glob(f"{path.name}.*"))[:-20]:
            with suppress(OSError):
                stale.unlink()
        return str(target)

    def apply_dream_result(self, response: object | None, cursor: int) -> dict[str, Any] | None:
        """Apply a gated Dream answer; a no-op while Dream runs in legacy mode.

        The model's answer is a decision document, not prose: the writer validates it,
        keeps every existing entry, writes the durable files atomically and records the
        run in ``memory/DREAMS.md``.  An unparsable answer falls back to a deterministic
        append-only promotion, so a provider failure cannot freeze memory.
        """
        if self.dream_mode != "gated":
            return None
        text = getattr(response, "content", "") or ""
        decisions, problems = parse_decisions(text)
        contents = {name: self._read_durable(name) for name in TARGET_LIMITS}
        report: dict[str, Any] = {"mode": "gated", "cursors": cursor,
                                  "problems": problems, "fallback": False}
        if decisions is None:
            report["fallback"] = True
            report["reason"] = problems[0] if problems else "unparsable answer"
            outcome = self._fallback_promotion(contents)
        elif self._durable_drifted(contents):
            # A hand edit or another session touched the files mid-run: promote
            # append-only instead of writing on top of unknown content.
            report["fallback"] = True
            report["reason"] = "durable files changed during the run"
            outcome = self._fallback_promotion(contents)
        else:
            outcome = apply_decisions(contents, decisions, today=datetime.now().strftime("%Y-%m-%d"))
        report["counts"] = outcome.counts()
        report["rejections"] = [f"{decision.op}: {reason}" for decision, reason in outcome.rejected]
        written: list[str] = []
        for name, content in outcome.contents.items():
            if content == contents.get(name, ""):
                continue
            self._pre_image(name)
            try:
                self._write_durable(name, content)
            except ValueError as exc:
                report["rejections"].append(f"{name}: {exc}")
                continue
            written.append(name)
        report["written"] = written
        self._record_dreams_entry(report)
        return report

    def _durable_drifted(self, contents: Mapping[str, str]) -> bool:
        """Whether a durable file changed since the prompt was built."""
        baseline = self._last_dream_baseline
        if not baseline:
            return False
        return any(baseline.get(name) != _content_digest(contents.get(name, ""))
                   for name in TARGET_LIMITS)

    def _fallback_promotion(self, contents: Mapping[str, str]) -> WriteOutcome:
        """Promote the gate's best candidates without a model, append-only."""
        entries = fallback_entries(self._last_dream_candidates)
        outcome = WriteOutcome(contents=dict(contents))
        if not entries:
            return outcome
        current = contents.get("MEMORY.md", "").rstrip()
        outcome.contents["MEMORY.md"] = (current + "\n\n" if current else "") + "\n".join(entries) + "\n"
        outcome.applied.extend(f"fallback {name}" for name in ["MEMORY.md"])
        return outcome

    def _record_dreams_entry(self, report: Mapping[str, Any]) -> None:
        """Append the run to the readable Dream journal; counters only, no content."""
        counts = cast(Mapping[str, int], report.get("counts", {}))
        lines = [
            f"\n## {datetime.now().strftime('%Y-%m-%d %H:%M')} ({report.get('mode', 'gated')})",
            f"- decisions: {counts.get('applied', 0)} applied, {counts.get('rejected', 0)} rejected",
        ]
        for key in sorted(counts):
            if key.startswith("rejected_"):
                lines.append(f"- {key.removeprefix('rejected_')}: {counts[key]}")
        lines.append(f"- files written: {', '.join(cast(list[str], report.get('written', []))) or 'none'}")
        if report.get("fallback"):
            lines.append(f"- fallback append-only: {report.get('reason', 'unknown')}")
        for problem in cast(list[str], report.get("problems", []))[:5]:
            lines.append(f"- problem: {problem}")
        for rejection in cast(list[str], report.get("rejections", []))[:10]:
            lines.append(f"- rejected: {rejection}")
        path = self.memory_dir / DREAMS_LOG_NAME
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
        except OSError:
            logger.warning("Dream journal is not writable: {}", path)

    def record_dream_run(
        self,
        *,
        completed: bool,
        reason: str = "",
        commit: str = "",
    ) -> dict[str, Any] | None:
        """Append one Dream run outcome to the audit journal (best effort)."""
        batch = self._last_dream_batch
        if batch is None:
            return None
        record = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "entries": batch["entries"],
            "shown": batch["shown"],
            "duplicates": batch["duplicates"],
            "excluded": batch.get("excluded", 0),
            "repeats": batch.get("repeats", 0),
            "cursors": [batch["first_cursor"], batch["last_cursor"]],
            "remaining": batch["remaining"],
            "completed": bool(completed),
            "reason": reason or None,
            "commit": commit or None,
        }
        path = self.memory_dir / DREAM_LOG_NAME
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            if path.stat().st_size > DREAM_LOG_MAX_BYTES:
                lines = path.read_text(encoding="utf-8").splitlines()
                path.write_text("\n".join(lines[-DREAM_LOG_KEEP_LINES:]) + "\n", encoding="utf-8")
        except OSError:
            logger.warning("Dream journal is not writable: {}", path)
        return record

    def dream_remaining_entries(self) -> int:
        """Unprocessed history entries left after the last batch."""
        return len(self.read_unprocessed_history(since_cursor=self.get_last_dream_cursor()))

    def dream_content_diff(self) -> str:
        """Structured summary of uncommitted changes to the durable memory files.

        Returns "" when git is unavailable or no content file changed. This is
        the ground-truth input for diff-grounded Dream commit messages.
        """
        if not self._git.is_initialized():
            return ""
        return self._git.summarize_working_tree(list(self._DREAM_CONTENT_PATHS))

    def build_dream_tools(self) -> ToolRegistry:
        """Build the restricted tool registry used by Dream runs."""
        from nanobot.agent.skills import BUILTIN_SKILLS_DIR
        from nanobot.agent.tools.apply_patch import ApplyPatchTool
        from nanobot.agent.tools.file_state import FileStates
        from nanobot.agent.tools.filesystem import EditFileTool, ReadFileTool, WriteFileTool
        from nanobot.agent.tools.registry import ToolRegistry

        tools = ToolRegistry()
        file_states = FileStates()
        workspace = self.workspace
        skills_dir = workspace / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)

        extra_read = [BUILTIN_SKILLS_DIR] if BUILTIN_SKILLS_DIR.exists() else None
        editable_files = [self.memory_file, self.soul_file, self.user_file]

        tools.register(ReadFileTool(
            workspace=workspace,
            allowed_dir=workspace,
            extra_read_allowed_dirs=extra_read,
            file_states=file_states,
        ))
        if self.dream_mode == "gated":
            # In gated mode the deterministic writer owns the durable files, so the
            # model reads them but cannot change them.
            return tools
        tools.register(EditFileTool(
            workspace=workspace,
            allowed_dir=skills_dir,
            extra_write_allowed_files=editable_files,
            file_states=file_states,
        ))
        tools.register(ApplyPatchTool(
            workspace=workspace,
            allowed_dir=skills_dir,
            extra_write_allowed_files=editable_files,
            file_states=file_states,
        ))
        tools.register(WriteFileTool(
            workspace=workspace,
            allowed_dir=skills_dir,
            extra_write_allowed_files=editable_files,
            file_states=file_states,
        ))
        return tools

    @staticmethod
    def dream_run_completed(
        resp: object | None,
    ) -> bool:
        """Return True when the Dream agent reached a normal terminal response."""
        metadata = getattr(resp, "metadata", None)
        if not isinstance(metadata, dict):
            return False
        return cast(dict[str, Any], metadata).get("_stop_reason") == "completed"

    @staticmethod
    def dream_incompletion_reason(
        resp: object | None,
    ) -> str:
        """Human-readable explanation of why a Dream run cannot advance."""
        metadata = getattr(resp, "metadata", None)
        if isinstance(metadata, dict):
            stop_reason = cast(dict[str, Any], metadata).get("_stop_reason", "unknown")
        else:
            stop_reason = "missing response metadata"
        return f"stop_reason: {stop_reason}"

    # -- message formatting utility ------------------------------------------

    @staticmethod
    def _format_messages(messages: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for message in messages:
            content = content_with_media_breadcrumbs(
                message.get("role"),
                message.get("content", ""),
                message.get("media"),
            )
            if not content:
                continue
            tools_used = message.get("tools_used")
            tools = (
                f" [tools: {', '.join(cast(list[str], tools_used))}]"
                if tools_used
                else ""
            )
            raw_timestamp = message.get("timestamp")
            timestamp = str(raw_timestamp) if raw_timestamp is not None else "?"
            role = str(message.get("role") or "unknown")
            lines.append(f"[{timestamp[:16]}] {role.upper()}{tools}: {content}")
        return "\n".join(lines)

    def raw_archive(
        self,
        messages: list[dict[str, Any]],
        *,
        max_chars: int | None = None,
        session_key: str | None = None,
    ) -> str:
        """Persist and return a bounded raw checkpoint when summarization degrades."""
        checkpoint = self._build_raw_checkpoint(messages, max_chars=max_chars)
        # A raw dump quotes the transcript verbatim, so it is not the agent's own
        # consolidation: mark it untrusted, as the conservative provenance class.
        self.append_history(checkpoint, session_key=session_key, origin="untrusted")
        logger.warning(
            "Memory consolidation degraded: raw-archived {} messages", len(messages)
        )
        return checkpoint

    def _build_raw_checkpoint(
        self,
        messages: list[dict[str, Any]],
        *,
        max_chars: int | None = None,
    ) -> str:
        """Build the same bounded checkpoint as :meth:`raw_archive` without writing it."""
        limit = max_chars if max_chars is not None else _RAW_ARCHIVE_MAX_CHARS
        checkpoint = (
            f"[RAW] {len(messages)} messages\n"
            f"{self._format_messages(public_history_messages(messages))}"
        )
        return self._normalize_history_entry(checkpoint, max_chars=limit)

    # ------------------------------------------------------------------
    # Dream helpers
    # ------------------------------------------------------------------

    @staticmethod
    def dream_session_key() -> str:
        """Return a unique session key for a Dream run, e.g. ``dream:20260528-100000``."""
        return f"dream:{datetime.now():%Y%m%d-%H%M%S}"

    @staticmethod
    def build_dream_commit_message(prefix: str, diff_body: str) -> str:
        """Build a Dream commit message grounded in the real working-tree diff.

        *diff_body* is a structured, machine-derived summary of the actual file
        changes (see :meth:`dream_content_diff` /
        :meth:`GitStore.summarize_working_tree`). The LLM narrative is
        deliberately excluded so the audit record (``/dream-log``) reflects the
        filesystem's truth, not the model's self-report.

        An empty *diff_body* yields the bare *prefix*, which ``auto_commit``
        turns into a no-op when there is nothing to stage.
        """
        diff_body = (diff_body or "").strip()
        if not diff_body:
            return prefix
        return f"{prefix}\n\n{diff_body}"

    @staticmethod
    def prune_dream_sessions(sessions: SessionManager, *, keep: int = 10) -> None:
        """Remove the oldest Dream session files, keeping only the N most recent.

        Only current base64url-encoded Dream session keys are considered.
        Non-dream session files are never touched.
        """
        with sessions.locked_session_files() as sessions_dir:
            dream_files: list[tuple[Path, str]] = []
            for path in sessions_dir.glob("*.jsonl"):
                decoded_key = SessionManager.decode_storage_key(path.stem)
                if decoded_key is not None and decoded_key.startswith("dream:"):
                    dream_files.append((path, decoded_key))
            dream_files.sort(key=lambda item: item[0].stat().st_mtime)

            for path, key in dream_files[: max(0, len(dream_files) - keep)]:
                if sessions.delete_session(key):
                    logger.debug("Pruned old dream session: {}", path.stem)
                else:
                    logger.warning("Failed to prune dream session {}", path)


# ---------------------------------------------------------------------------
# Memory ingestion and context-pressure coordination
# ---------------------------------------------------------------------------

# Raw fallbacks use a tighter cap. Completed model summaries may scale with the
# configured generation budget, while append_history() still enforces the
# emergency hard cap against pathological provider output.
_RAW_ARCHIVE_MAX_CHARS = 16_000   # fallback dump (LLM failed)
_HISTORY_ENTRY_HARD_CAP = 64_000  # emergency cap in append_history
# One Dream run reads a bounded batch; the batch must say so explicitly, and a
# batch full of restatements of the same event must not waste its budget.
DREAM_PROMPT_ENTRY_CHARS = 1000
# Measured on real journal pairs (2026-09-17): restatements of the same entry land
# at 0.83-1.0 trigram similarity, while a single changed word already drops to 0.70.
# 0.8 therefore collapses repeats without swallowing distinct content.
DREAM_BATCH_DEDUP_THRESHOLD = 0.8
# Short entries are cheap and share trigrams by accident ("entry 1" vs "entry 10"),
# so only entries at least this long are compared for duplication.
DREAM_BATCH_DEDUP_MIN_CHARS = 200
DREAM_LOG_NAME = "dream_log.jsonl"
DREAM_LOG_MAX_BYTES = 1_000_000
DREAM_LOG_KEEP_LINES = 500
# Readable journal of gated Dream runs: what was added, merged, superseded and
# rejected, with counters only - never the rejected content.
DREAMS_LOG_NAME = "DREAMS.md"
# Ingest hygiene (etap P0). Measured 2026-09-17 on the live journal: 38 of 138
# entries came from heartbeat sessions and repeated their own status verbatim,
# and 11 of 67 entries in Dream batches (16%) were near-duplicates.  The gate is
# deterministic and reversible: `NANOBOT_HISTORY_HYGIENE=0` restores the old
# ingest behaviour without touching the journal that is already written.
HISTORY_HYGIENE_ENV = "NANOBOT_HISTORY_HYGIENE"
# Dream mode switch (etap P2). "legacy" keeps the model as the author of the
# durable files; "gated" makes it return decisions that the deterministic writer
# applies. The gateway sets the attribute from `agents.defaults.dream.mode`; the
# environment variable is the rollback lever for every other entry point.
DREAM_MODE_ENV = "NANOBOT_DREAM_MODE"
DREAM_MODES = ("legacy", "gated")
# How far back an identical entry still counts as a repeat of the same content.
HISTORY_DEDUP_WINDOW = 200
# How many unprocessed entries one Dream view scans to find its candidates; a
# window full of scheduled-session noise must not starve a run.
DREAM_SCAN_WINDOW = 200
# A journal entry whose whole content is one of these adds nothing to consolidate.
_BOILERPLATE_PATTERNS = (
    re.compile(r"^(?:all clear|nothing to report|nothing to consolidate|no changes|brak zmian|"
               r"nic nowego|nic do zrobienia)\b", re.IGNORECASE),
)
_ENTRY_TAG = re.compile(r"^(?:-\s*)?\[(?:ephemeral|permanent|durable|correction)\]\s*", re.IGNORECASE)


def _env_flag(name: str, *, default: bool) -> bool:
    """Read a boolean environment switch; anything unexpected keeps the default."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _content_digest(content: str) -> str:
    """Digest of a durable file's content, for optimistic concurrency."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _dream_mode_from_env() -> str:
    """Dream mode from the environment; the config value is applied by the runner."""
    value = os.environ.get(DREAM_MODE_ENV, "").strip().lower()
    return value if value in DREAM_MODES else "legacy"


def _content_hash(content: str) -> str:
    """Hash of the whitespace- and case-insensitive content, for exact dedup."""
    return hashlib.sha256(re.sub(r"\s+", " ", content.strip().lower()).encode()).hexdigest()


def _is_boilerplate(content: str) -> bool:
    """Whether an entry carries only a status marker and no consolidatable fact."""
    stripped = _ENTRY_TAG.sub("", content.strip()).strip()
    if not stripped:
        return True
    return any(pattern.match(stripped) for pattern in _BOILERPLATE_PATTERNS)
_ARCHIVE_TOOL_RESULT = (
    "Session archival does not execute tools. Use only the supplied conversation and "
    "return the requested compact checkpoint now; do not call another tool."
)


def _dedupe_by_content_hash(
    batch: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Drop exact repeats inside one batch, keeping the newest copy.

    The trigram filter in :func:`dedupe_history_batch` catches restatements; this
    one catches identical text the journal may still hold from before ingest
    hygiene existed. The journal itself is never modified.
    """
    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in reversed(batch):
        digest = entry.get("content_hash")
        if not isinstance(digest, str):
            digest = _content_hash(str(entry.get("content", "")))
        if digest in seen:
            continue
        seen.add(digest)
        kept.append(entry)
    kept.reverse()
    return kept, len(batch) - len(kept)


def _trigram_similarity(left: str, right: str) -> float:
    """Trigram Jaccard similarity of two whitespace-normalized strings (1.0 = identical)."""
    first = re.sub(r"\s+", " ", left.lower()).strip()
    second = re.sub(r"\s+", " ", right.lower()).strip()
    if first == second:
        return 1.0
    if len(first) < 3 or len(second) < 3:
        return 0.0
    left_grams = {first[index:index + 3] for index in range(len(first) - 2)}
    right_grams = {second[index:index + 3] for index in range(len(second) - 2)}
    union = left_grams | right_grams
    return len(left_grams & right_grams) / len(union) if union else 0.0


def dedupe_history_batch(
    batch: list[dict[str, Any]],
    threshold: float = DREAM_BATCH_DEDUP_THRESHOLD,
) -> tuple[list[dict[str, Any]], int]:
    """Keep the newest copy of each repeated history entry in one Dream batch.

    Entries are visited newest-first so the most complete wording survives, then
    restored to chronological order for the prompt.  The journal itself is never
    modified; only the view handed to the model is collapsed.  A threshold
    outside ``(0, 1)`` disables the filter.
    """
    if not 0.0 < threshold < 1.0:
        return batch, 0
    kept: list[dict[str, Any]] = []
    seen: list[str] = []
    for entry in reversed(batch):
        text = truncate_text(str(entry.get("content", "")), DREAM_PROMPT_ENTRY_CHARS)
        comparable = len(text) >= DREAM_BATCH_DEDUP_MIN_CHARS
        if comparable and any(
            len(other) >= DREAM_BATCH_DEDUP_MIN_CHARS and _trigram_similarity(text, other) > threshold
            for other in seen
        ):
            continue
        seen.append(text)
        kept.append(entry)
    kept.reverse()
    return kept, len(batch) - len(kept)


class MemoryArchiver:
    """Write durable transcript batches to the Memory ingestion journal.

    The archiver deliberately has no SessionManager dependency: it may read a
    captured transcript batch and append to history.jsonl, but it cannot mutate
    provider continuation state or advance a session watermark.
    """

    def __init__(
        self,
        store: MemoryStore,
        build_messages: Callable[..., list[dict[str, Any]]],
        get_tool_definitions: Callable[[], list[dict[str, Any]]],
        resolve_prompt_context: Callable[[Session], tuple[str | None, Path | None]] | None = None,
    ) -> None:
        self.store = store
        self._build_messages = build_messages
        self._get_tool_definitions = get_tool_definitions
        self._resolve_prompt_context = resolve_prompt_context

    def _raw_checkpoint(
        self,
        messages: list[dict[str, Any]],
        *,
        session_key: str,
        previous_summary: str | None,
        max_tokens: int,
    ) -> str:
        """Persist the failed chunk and return a bounded replacement checkpoint."""
        raw = self.store.raw_archive(messages, session_key=session_key)
        return self._combine_raw_checkpoint(
            raw,
            previous_summary=previous_summary,
            max_tokens=max_tokens,
        )

    @staticmethod
    def _combine_raw_checkpoint(
        raw: str,
        *,
        previous_summary: str | None,
        max_tokens: int,
    ) -> str:
        """Return a bounded checkpoint that preserves prior and newly archived context."""
        token_limit = max(1, max_tokens)
        if not previous_summary:
            return truncate_text_to_tokens(raw, token_limit)

        combined = (
            "[Previous archived context]\n"
            f"{previous_summary}\n\n"
            "[Newly archived raw context]\n"
            f"{raw}"
        )
        bounded = truncate_text_to_tokens(combined, token_limit)
        if bounded == combined:
            return combined

        # Keep evidence from both sides when their full concatenation cannot fit.
        section_limit = max(1, (token_limit - 32) // 2)
        return truncate_text_to_tokens(
            "[Previous archived context]\n"
            f"{truncate_text_to_tokens(previous_summary, section_limit)}\n\n"
            "[Newly archived raw context]\n"
            f"{truncate_text_to_tokens(raw, section_limit)}",
            token_limit,
        )

    async def archive(
        self,
        source_messages: list[dict[str, Any]],
        *,
        runtime: LLMRuntime,
        session_key: str,
        history: list[dict[str, Any]],
        request_tools: list[dict[str, Any]],
        previous_summary: str | None = None,
        input_token_budget: int | None = None,
        fallback_max_tokens: int | None = None,
        provider_state: ProviderConversationState | None = None,
    ) -> str | None:
        """Append the archive prompt to H and persist its summary."""
        if not source_messages:
            return None

        if self.store.archive_sink is not None:
            await asyncio.to_thread(self.store.archive_sink, session_key, source_messages, "pre-compaction")

        def raw_fallback() -> str:
            checkpoint = self._raw_checkpoint(
                source_messages,
                session_key=session_key,
                previous_summary=previous_summary,
                max_tokens=(
                    fallback_max_tokens
                    if fallback_max_tokens is not None
                    else runtime.generation.max_tokens
                ),
            )
            self.store.flush_day_note(session_key, first_cursor=0, last_cursor=0,
                                      entries=len(source_messages),
                                      characters=len(checkpoint))
            return checkpoint

        prompt = render_template(
            "agent/consolidator_archive.md",
            strip=True,
            archive_count=len(source_messages),
        )
        prompt_message = {"role": "user", "content": prompt}
        provider_context = None
        state_controller: ProviderConversationStateController | None = None
        state_messages: list[dict[str, Any]] = []
        call_tools = request_tools
        if provider_state is not None:
            instruction_messages: list[dict[str, Any]] = []
            for message in history:
                if message.get("role") not in {"system", "developer"}:
                    break
                instruction_messages.append(dict(message))
            request_messages = [*instruction_messages, prompt_message]
            state_controller = ProviderConversationStateController(
                provider=runtime.provider,
                model=runtime.model,
                messages=state_messages,
                state=provider_state,
                session_id=session_key,
            )
            state_messages.append(dict(prompt_message))
            provider_context = state_controller.prepare_request(
                state_messages,
                context_window_tokens=runtime.context_window_tokens,
            )
            if provider_context is None or provider_context.conversation_state is None:
                return raw_fallback()
            call_tools = []
        else:
            request_messages = [
                *[dict(message) for message in history],
                prompt_message,
            ]
        if input_token_budget is not None and provider_context is None:
            estimated, source = estimate_prompt_tokens_chain(
                runtime.provider,
                runtime.model,
                request_messages,
                call_tools,
            )
            if input_token_budget <= 0 or estimated > input_token_budget:
                logger.debug(
                    "Memory archive input does not fit for {}: {}/{} via {}; raw-dumping",
                    session_key,
                    estimated,
                    input_token_budget,
                    source,
                )
                return raw_fallback()

        response: LLMResponse | None = None
        for attempt in range(2):
            try:
                with llm_usage_source("dream"):
                    response = await runtime.provider.chat_stream_with_retry(
                        model=runtime.model,
                        messages=request_messages,
                        tools=call_tools,
                        temperature=runtime.generation.temperature,
                        max_tokens=runtime.generation.max_tokens,
                        reasoning_effort=runtime.generation.reasoning_effort,
                        provider_context=provider_context,
                    )
            except Exception:
                phase = "provider call" if attempt == 0 else "tool-call recovery"
                logger.warning(
                    "Memory archive {} failed, raw-dumping to history",
                    phase,
                )
                return raw_fallback()
            if response.should_execute_tools is not True or attempt == 1:
                break

            logger.info(
                "Memory archive provider returned {} tool call(s); requesting checkpoint",
                len(response.tool_calls),
            )
            assistant_message = build_assistant_message(
                response.content,
                tool_calls=[call.to_openai_tool_call() for call in response.tool_calls],
                reasoning_content=response.reasoning_content,
                thinking_blocks=response.thinking_blocks,
            )
            tool_messages = [
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": _ARCHIVE_TOOL_RESULT,
                }
                for call in response.tool_calls
            ]
            request_messages = [
                *request_messages,
                assistant_message,
                *tool_messages,
            ]
            if state_controller is not None:
                state_controller.observe_response(
                    response,
                    state_messages,
                )
                state_messages.extend([
                    state_controller.project_response_message(
                        dict(assistant_message),
                        response,
                    ),
                    *[dict(message) for message in tool_messages],
                ])
                provider_context = state_controller.prepare_request(
                    state_messages,
                    context_window_tokens=runtime.context_window_tokens,
                )
                if provider_context is None or provider_context.conversation_state is None:
                    return raw_fallback()
        assert response is not None
        if response.finish_reason in {"error", "length"}:
            logger.warning(
                "Memory archive provider did not complete ({}), raw-dumping to history",
                response.finish_reason,
            )
            return raw_fallback()
        if response.has_tool_calls is True:
            logger.warning("Memory archive provider returned tool calls, raw-dumping to history")
            return raw_fallback()
        summary = response.content
        if not summary or not summary.strip():
            logger.warning("Memory archive provider returned no summary, raw-dumping to history")
            return raw_fallback()
        summary = self.store._normalize_history_entry(summary)
        if not summary:
            logger.warning("Memory archive provider summary was not safe to replay, raw-dumping")
            return raw_fallback()
        if summary != "(nothing)":
            first_cursor = self.store.append_history(summary, session_key=session_key)
            self.store.flush_day_note(session_key, first_cursor=first_cursor,
                                      last_cursor=first_cursor, entries=1,
                                      characters=len(summary))
            await self._record_episode(session_key, summary, source_messages)
        return summary

    async def _record_episode(
        self,
        session_key: str,
        summary: str,
        messages: list[dict[str, Any]],
    ) -> None:
        """Hand the compaction result to the episode sink; it never gates the archive."""
        sink = self.store.episode_sink
        if sink is None:
            return
        try:
            await asyncio.to_thread(sink, session_key, summary, messages)
        except Exception as exc:  # noqa: BLE001 - an episode is a projection, not the record
            logger.warning("Episode record skipped ({})", type(exc).__name__)

    async def archive_session(
        self,
        session: Session,
        *,
        archive_end: int,
        runtime: LLMRuntime,
        input_token_budget: int,
    ) -> str | None:
        """Archive a captured session prefix without mutating the session."""
        if self.store.archive_sink is not None and session.policy.persist and session.policy.log_content:
            await asyncio.to_thread(self.store.archive_sink, session.key,
                                    session.messages[:archive_end], "pre-session-compaction")
        messages = [
            message for message in session.messages[session.last_archived:archive_end]
            if not message.get("_command") and not is_summary_checkpoint(message)
        ]
        if not messages:
            return None
        session_summary = session_summary_from_metadata(
            session.metadata,
            fallback_last_active=session.updated_at,
        )
        previous_summary = session_summary["text"] if session_summary else None

        if input_token_budget <= 0:
            logger.debug(
                "Memory archive has no safe input budget for {}; raw-dumping",
                session.key,
            )
            return self._raw_checkpoint(
                messages,
                session_key=session.key,
                previous_summary=previous_summary,
                max_tokens=runtime.generation.max_tokens,
            )
        prefix = Session(
            key=session.key,
            messages=list(session.messages[:archive_end]),
            last_consolidated=session.last_archived,
        )
        history = prefix.get_history(max_tokens=input_token_budget)
        archive_history = Session(
            key=session.key,
            messages=messages,
        ).get_history()
        if not archive_history or history[-len(archive_history):] != archive_history:
            logger.debug(
                "Memory archive cannot replay the full chunk for {}; raw-dumping",
                session.key,
            )
            return self._raw_checkpoint(
                messages,
                session_key=session.key,
                previous_summary=previous_summary,
                max_tokens=runtime.generation.max_tokens,
            )
        channel = session.key.split(":", 1)[0] if ":" in session.key else None
        workspace: Path | None = None
        if self._resolve_prompt_context is not None:
            channel, workspace = self._resolve_prompt_context(session)
        history_messages = self._build_messages(
            history=history,
            current_message=None,
            channel=channel,
            session_summary=session_summary,
            workspace=workspace,
        )
        tools = self._get_tool_definitions()
        return await self.archive(
            messages,
            runtime=runtime,
            session_key=session.key,
            history=history_messages,
            request_tools=tools,
            previous_summary=previous_summary,
            input_token_budget=input_token_budget,
        )


class Consolidator:
    """Coordinate session Memory checkpoints through ``MemoryArchiver``."""

    _SAFETY_BUFFER = 1024  # extra headroom for tokenizer estimation drift

    def __init__(
        self,
        store: MemoryStore,
        sessions: SessionManager,
        build_messages: Callable[..., list[dict[str, Any]]],
        get_tool_definitions: Callable[[], list[dict[str, Any]]],
        resolve_prompt_context: Callable[[Session], tuple[str | None, Path | None]] | None = None,
    ):
        self.store = store
        self.sessions = sessions
        self._build_messages = build_messages
        self._get_tool_definitions = get_tool_definitions
        self.archiver = MemoryArchiver(
            store=store,
            build_messages=build_messages,
            get_tool_definitions=get_tool_definitions,
            resolve_prompt_context=resolve_prompt_context,
        )
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    def get_lock(self, session_key: str) -> asyncio.Lock:
        """Return the shared consolidation lock for one session."""
        return self._locks.setdefault(session_key, asyncio.Lock())

    async def summarize_transcript(
        self,
        accepted_messages: list[dict[str, Any]],
        previous_summary: str | None,
        *,
        runtime: LLMRuntime,
        session_key: str,
        tools: list[dict[str, Any]],
        provider_state: ProviderConversationState | None = None,
    ) -> str | None:
        """Summarize the exact transcript prefix already accepted by the model."""
        source_messages = [
            dict(message)
            for message in accepted_messages
            if message.get("role") != "system"
        ]
        if not source_messages:
            return None

        max_output_tokens = max(0, runtime.generation.max_tokens)
        input_token_budget = runtime.context_window_tokens - max_output_tokens
        checkpoint_tokens = min(
            max_output_tokens,
            max(1, (input_token_budget - self._SAFETY_BUFFER) // 2),
        )

        summary = await self.archiver.archive(
            source_messages,
            runtime=runtime,
            session_key=session_key,
            history=accepted_messages,
            request_tools=tools,
            previous_summary=previous_summary,
            input_token_budget=input_token_budget,
            fallback_max_tokens=max(1, checkpoint_tokens),
            provider_state=provider_state,
        )
        if summary is None:
            return None
        return truncate_text_to_tokens(summary, max(1, max_output_tokens))

    async def summarize_provider_compaction(
        self,
        state: ProviderConversationState,
        fallback_messages: list[dict[str, Any]],
        previous_summary: str | None,
        *,
        runtime: LLMRuntime,
        session_key: str,
        tools: list[dict[str, Any]],
    ) -> str | None:
        """Prompt a native compacted state without replaying its raw history."""
        return await self.summarize_transcript(
            fallback_messages,
            previous_summary,
            runtime=runtime,
            session_key=session_key,
            tools=tools,
            provider_state=state,
        )

    @staticmethod
    def _full_replay_history(
        session: Session,
    ) -> list[dict[str, Any]]:
        """Return all messages that can reach the next model prompt."""
        if not session.messages:
            return []
        return session.get_history()

    def estimate_session_prompt_tokens(
        self,
        session: Session,
        *,
        runtime: LLMRuntime,
    ) -> tuple[int, str]:
        """Estimate prompt size from the full replayable session history."""
        history = self._full_replay_history(session)
        channel = session.key.split(":", 1)[0] if ":" in session.key else None
        summary = session_summary_from_metadata(
            session.metadata,
            fallback_last_active=session.updated_at,
        )
        probe_messages = self._build_messages(
            history=history,
            current_message="[token-probe]",
            channel=channel,
            session_summary=summary,
        )
        return estimate_prompt_tokens_chain(
            runtime.provider,
            runtime.model,
            probe_messages,
            self._get_tool_definitions(),
        )

    def _input_token_budget(self, runtime: LLMRuntime) -> int:
        """Available input token budget for consolidation LLM."""
        return (
            runtime.context_window_tokens
            - runtime.generation.max_tokens
            - self._SAFETY_BUFFER
        )

    async def archive_session(
        self,
        session: Session,
        *,
        archive_end: int,
        runtime: LLMRuntime,
    ) -> str | None:
        """Archive one captured session range through the shared Memory path."""
        return await self.archiver.archive_session(
            session,
            archive_end=archive_end,
            runtime=runtime,
            input_token_budget=self._input_token_budget(runtime),
        )

    async def compact_idle_session(
        self,
        session_key: str,
        *,
        runtime: LLMRuntime,
        max_suffix: int = 0,
        events: EventSink = NO_EVENTS,
    ) -> str | None:
        """Replace archived history with a summary checkpoint.

        ``max_suffix`` is accepted for SDK compatibility and no longer retains
        archived messages. All compaction triggers share checkpoint replay.
        """
        lock = self.get_lock(session_key)
        async with lock:
            self.sessions.invalidate(session_key)
            session = self.sessions.get_or_create(session_key)

            archive_start = session.last_archived
            messages_to_archive = list(session.messages[archive_start:])
            has_new_messages = any(
                not message.get("_command") and not is_summary_checkpoint(message)
                for message in messages_to_archive
            )
            if not has_new_messages:
                return ""

            compaction_id = uuid4().hex
            await events.emit(
                ContextCompactionEvent(compaction_id=compaction_id, phase="started"),
            )
            last_active = session.updated_at
            archive_end = archive_start + len(messages_to_archive)
            try:
                summary = await self.archive_session(
                    session, archive_end=archive_end, runtime=runtime,
                )
                if summary:
                    # Concurrent appends remain after the captured boundary.
                    session.commit_summary_checkpoint(
                        summary, insert_at=archive_end, last_active=last_active,
                    )
                    # Resume from the summary and retained transcript, not the old provider history.
                    session.provider_state = None
                    self.sessions.save(session)
            except (Exception, asyncio.CancelledError) as exc:
                await events.emit(
                    ContextCompactionEvent(
                        compaction_id=compaction_id,
                        phase="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                    ),
                )
                raise
            if not summary:
                await events.emit(
                    ContextCompactionEvent(compaction_id=compaction_id, phase="failed"),
                )
                return None

            await events.emit(
                ContextCompactionEvent(
                    compaction_id=compaction_id,
                    phase="succeeded",
                ),
            )

            logger.info(
                "Idle-session compact for {}: archived={}, visible={}, retained={}, summary={}",
                session_key,
                len(messages_to_archive),
                len(session.get_history()),
                len(session.messages),
                bool(summary),
            )

            return summary
