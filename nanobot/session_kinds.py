"""What kind of work produced a session, derived from its key.

Two consumers need the same answer and must not disagree about it:

- the memory journal, which must not turn scheduled or background runs into
  durable memory candidates (``nanobot/agent/memory.py``);
- the personal archive, which keeps those same sessions out of the memory corpus
  (``nanobot/personal/visibility.py``).

The classification is a pure function of the session key: no model, no state, no
I/O. Session keys are produced by the runtime, so the prefix is the reliable
signal; anything unrecognised is treated as an interactive session.
"""

from __future__ import annotations

#: Kinds of work whose output is never a durable memory candidate: no Dream
#: promotion, no archive corpus, no reminder injection.
NON_CANDIDATE_KINDS = frozenset({"heartbeat", "cron", "subagent", "development", "diagnostic", "dream"})

_PREFIX_KINDS: tuple[tuple[str, str], ...] = (
    ("heartbeat", "heartbeat"),
    ("personal-development", "development"),
    ("personal-evolution", "development"),
    ("dream", "dream"),
    ("cron", "cron"),
    ("subagent", "subagent"),
    ("diagnostic", "diagnostic"),
)


def session_kind(session_key: str | None) -> str:
    """Kind of work behind a session key, e.g. ``interactive`` or ``heartbeat``."""
    if not session_key:
        return "unknown"
    for prefix, kind in _PREFIX_KINDS:
        if session_key.startswith(prefix):
            return kind
    return "interactive"


def is_candidate(session_key: str | None) -> bool:
    """Whether a session may produce durable memory candidates."""
    return session_kind(session_key) not in NON_CANDIDATE_KINDS
