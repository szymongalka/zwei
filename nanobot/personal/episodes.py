"""Task episodes: what a session was for, what came out of it, and on what evidence.

The archive keeps the full transcript, but a transcript is evidence, not memory.
The unit that belongs in the curated layer is the episode: the goal, the outcome,
the decisions and the concrete evidence (commits, paths, counts). This module
builds that record **deterministically** from the session's own consolidation
summary — no extra model call, no new service.

Why it matters: measured 2026-09-17, transcripts were 90% of the corpus characters
and almost all of the noise, while the useful part of a finished session is a few
lines. Episodes replace transcripts in the hot layer, so retrieval answers from
them instead of re-reading what the turn already printed.

Only sessions that may produce durable memory get an episode (see
``nanobot.session_kinds``): a heartbeat run has nothing to remember.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, cast

from nanobot.session_kinds import is_candidate

#: Source prefix of an episode record in the archive; episodes are the hot layer.
EPISODE_SOURCE_PREFIX = "episode:"

WHAT_LIMIT = 300
OUTCOME_LIMIT = 2000
DECISION_LIMIT = 240
PROJECTION_LIMIT = 4000
MAX_EVIDENCE = 12
MAX_DECISIONS = 6
MAX_TOOLS = 12

#: Consolidation summaries tag their own lines; these three carry durable content.
_TAG = re.compile(r"^\s*[-*]\s*\[(durable|correction|permanent)\]\s*", re.IGNORECASE)
_PR = re.compile(r"\bPR\s*#(\d+)\b")
_TESTS = re.compile(r"\b\d+\s+(?:passed|failed)\b")
#: A revision is a hex token with at least one digit — "defaced" is not a commit.
_REVISION = re.compile(r"(?<![\w])(?=[0-9a-f]{7,40}(?![\w]))(?=[0-9a-f]*\d)[0-9a-f]+")
_PATH = re.compile(
    r"(?:/root|/var|/etc|/opt|/srv)[\w./-]{2,}"
    r"|(?<![\w/])(?:work|skills|docs|memory|artifacts|projects|nanobot|tests)/[\w./-]{2,}"
)
_COMMAND = re.compile(r"`([^`\n]{3,80})`")
_TRAILING = ".,;:)\u201d\"'"


def message_text(message: Mapping[str, Any]) -> str:
    """Flatten one stored message into text, ignoring media payloads."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in cast(list[object], content):
            if isinstance(item, Mapping):
                text = cast(Mapping[str, Any], item).get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return ""


def _clip(value: str, limit: int) -> str:
    condensed = re.sub(r"\s+", " ", value).strip()
    return condensed[:limit]


def _decisions(summary: str) -> list[str]:
    decisions: list[str] = []
    for line in summary.splitlines():
        match = _TAG.match(line)
        if not match:
            continue
        decision = _clip(line[match.end():], DECISION_LIMIT)
        if decision and decision not in decisions:
            decisions.append(decision)
        if len(decisions) >= MAX_DECISIONS:
            break
    return decisions


def _evidence(summary: str) -> list[str]:
    """Concrete, checkable references the summary mentions, in order of appearance."""
    found: list[str] = []
    for pattern in (_PR, _REVISION, _TESTS, _PATH, _COMMAND):
        for match in pattern.finditer(summary):
            token = match.group(1) if pattern is _COMMAND else match.group(0)
            token = token.rstrip(_TRAILING)
            if pattern is _PR:
                token = f"PR #{match.group(1)}"
            if token and token not in found:
                found.append(token)
            if len(found) >= MAX_EVIDENCE:
                return found
    return found


def _what(messages: Sequence[Mapping[str, Any]]) -> str:
    """The session's opening request, which is what the episode is about."""
    for message in messages:
        if str(message.get("role")) != "user":
            continue
        text = _clip(message_text(message), WHAT_LIMIT)
        if text and not text.startswith("/"):
            return text
    return ""


def _tools(messages: Sequence[Mapping[str, Any]]) -> list[str]:
    names: list[str] = []
    for message in messages:
        for name in cast(list[object], message.get("tools_used") or []):
            if isinstance(name, str) and name not in names:
                names.append(name)
            if len(names) >= MAX_TOOLS:
                return names
    return names


def _window(messages: Sequence[Mapping[str, Any]]) -> list[str]:
    stamps = [str(message["timestamp"])[:16] for message in messages
              if isinstance(message.get("timestamp"), str)]
    return [stamps[0], stamps[-1]] if stamps else []


def build_episode(
    session_key: str,
    summary: str,
    messages: Sequence[Mapping[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """The episode of one finished session, or ``None`` when it is not memory.

    Background work (heartbeat, cron, development, diagnostics) has no episode:
    it may write task artefacts, but nothing it emits is durable memory.
    """
    if not is_candidate(session_key):
        return None
    outcome = (summary or "").strip()
    return {
        "session_key": session_key,
        "at": (now or datetime.now(timezone.utc)).isoformat(),
        "what": _what(messages),
        "outcome": outcome[:OUTCOME_LIMIT],
        "decisions": _decisions(outcome),
        "evidence": _evidence(outcome),
        "turns": len(messages),
        "tools": _tools(messages),
        "window": _window(messages),
    }


def episode_key(episode: Mapping[str, Any]) -> str:
    """Stable identity of an episode, so re-compacting a session adds nothing."""
    identity = f"{episode.get('session_key', '')}\n{episode.get('outcome', '')}"
    return hashlib.sha256(identity.encode()).hexdigest()[:16]


def episode_projection(episode: Mapping[str, Any]) -> str:
    """Searchable text of an episode: the record the curated layer retrieves."""
    lines = [f"episode {episode.get('session_key', '')}"]
    what = str(episode.get("what") or "")
    outcome = str(episode.get("outcome") or "")
    decisions = [str(item) for item in cast(Sequence[object], episode.get("decisions") or [])]
    evidence = [str(item) for item in cast(Sequence[object], episode.get("evidence") or [])]
    if what:
        lines.append("what: " + what)
    if outcome:
        lines.append("outcome: " + outcome)
    if decisions:
        lines.append("decisions: " + " | ".join(decisions))
    if evidence:
        lines.append("evidence: " + ", ".join(evidence))
    return "\n".join(lines)[:PROJECTION_LIMIT]
