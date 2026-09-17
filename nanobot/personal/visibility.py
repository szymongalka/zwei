"""Deterministic corpus layers for the personal archive.

The archive keeps every raw record, but only part of it is *memory*.  Retrieved
text is split into three layers, derived from the record's source and payload
and never from a model, so the classification is reproducible and free:

``hot``
    Curated material: mail, contacts, calendar, sent mail and the owner's own
    turns.  Indexed locally, eligible for the remote vector projection and for
    injection into the runtime context.
``cold``
    Session transcripts - the agent's answers and tool output.  Still indexed for
    an explicit forensic request, never injected automatically.
``quarantine``
    Workspace-file copies, scheduled/background sessions and the KSeF archive.
    Never indexed for retrieval and never injected; the record stays readable by
    identifier.

The rule matters because the measured archive was 97% documents / 90% characters
of the agent's own work (evidence 2026-09-17), which is what made relevance a
coin flip on that corpus.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, cast

Visibility = Literal["hot", "cold", "quarantine"]

HOT: Visibility = "hot"
COLD: Visibility = "cold"
QUARANTINE: Visibility = "quarantine"

#: Sources that are not part of the memory corpus at all.
QUARANTINE_SOURCE_PREFIXES: tuple[str, ...] = ("native_memory", "ksef")

#: Session keys produced by a schedule, a background worker or a diagnostic run.
BACKGROUND_SESSION_PREFIXES: tuple[str, ...] = (
    "heartbeat",
    "personal-development",
    "personal-evolution",
    "cron",
    "subagent",
    "diagnostic",
)


def message_role(payload: object) -> str:
    """Role of a stored history record, empty when the payload cannot be read."""
    if not isinstance(payload, Mapping):
        return ""
    role = cast(Mapping[str, Any], payload).get("role")
    return role if isinstance(role, str) else ""


def classify(source: str, payload: object = None) -> Visibility:
    """The layer a raw record belongs to, from its source and message role."""
    if any(source.startswith(prefix) for prefix in QUARANTINE_SOURCE_PREFIXES):
        return QUARANTINE
    if source.startswith("session:"):
        key = source[len("session:"):]
        if any(key.startswith(prefix) for prefix in BACKGROUND_SESSION_PREFIXES):
            return QUARANTINE
        return HOT if message_role(payload) == "user" else COLD
    return HOT
