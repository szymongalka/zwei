"""Deterministic promotion gate and decision writer for Dream (etap P2).

The legacy Dream run hands the model the journal and lets it rewrite the durable
files.  The gated run splits that in two: a deterministic gate decides *which*
journal entries may become memory, the model only returns **decisions** (add,
merge, supersede, reject), and a writer composes the file from validated,
sourced fragments.  The model stops being the author of the file.

Nothing here calls a model, and nothing here writes to disk: the gate ranks
candidates, the parser validates the model's answer, and the writer returns new
file contents plus a report.  :class:`~nanobot.agent.memory.MemoryStore` performs
the atomic write with a pre-image and the ``memory/DREAMS.md`` audit entry.

Rollback: ``NANOBOT_DREAM_MODE=legacy`` restores the previous flow.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, cast

#: An entry that reaches durable memory is short: the plan caps a promoted snippet
#: at 160 tokens, which is roughly this many characters.
MAX_ENTRY_CHARS = 600
#: A candidate shorter than this is a status line, not a fact worth promoting.
MIN_CANDIDATE_CHARS = 200
#: Score a candidate must reach to be offered to the model at all.
MIN_CANDIDATE_SCORE = 0.45
#: Share of previously present entries a single write may drop (plan: 0.25).
MAX_PRIOR_ENTRY_LOSS = 0.25
#: Trigram similarity above which two journal entries describe the same topic.
TOPIC_SIMILARITY = 0.7
#: Recency half-life for the gate, in days.
RECENCY_HALF_LIFE_DAYS = 14.0
#: Weights of the gate signals; they sum to 1.0.
SIGNAL_WEIGHTS: Mapping[str, float] = {
    "frequency": 0.30,
    "multi_day": 0.25,
    "richness": 0.25,
    "recency": 0.20,
}
#: Durable files the writer is allowed to touch, with their character limits.
TARGET_LIMITS: Mapping[str, int] = {"MEMORY.md": 12_000, "USER.md": 4_000}
OPS = ("add", "merge", "supersede", "drop", "reject")

_TAGS = re.compile(r"^\s*(?:-\s*)?\[(?:ephemeral|permanent|durable|correction|skip)\]\s*", re.I)
_ENTRY_LINE = re.compile(r"^-\s+\S")
_PROVENANCE = re.compile(r"^\s*<!--.*-->\s*$")
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
_WHITESPACE = re.compile(r"\s+")


def trigram_similarity(left: str, right: str) -> float:
    """Trigram Jaccard similarity of two whitespace-normalized strings (1.0 = same text)."""
    first = _WHITESPACE.sub(" ", left.lower()).strip()
    second = _WHITESPACE.sub(" ", right.lower()).strip()
    if first == second:
        return 1.0
    if len(first) < 3 or len(second) < 3:
        return 0.0
    left_grams = {first[index:index + 3] for index in range(len(first) - 2)}
    right_grams = {second[index:index + 3] for index in range(len(second) - 2)}
    union = left_grams | right_grams
    return len(left_grams & right_grams) / len(union) if union else 0.0


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    for pattern in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _strip_tags(content: str) -> str:
    return _TAGS.sub("", content.strip()).strip()


@dataclass(frozen=True)
class Candidate:
    """One journal entry the gate offers for promotion."""

    cursor: int
    content: str
    timestamp: str
    score: float
    signals: Mapping[str, float]
    occurrences: int = 1
    days: int = 1

    @property
    def source(self) -> str:
        return f"cursor:{self.cursor}"

    def prompt_line(self) -> str:
        signals = ", ".join(f"{name}={value:.2f}" for name, value in self.signals.items())
        return (f"[cursor {self.cursor}] (score {self.score:.2f}; {signals}; "
                f"seen {self.occurrences}x on {self.days} day(s))\n{self.content}")


def _cluster(entry: Mapping[str, Any], accepted: Sequence[Mapping[str, Any]]) -> int:
    """How many accepted entries describe the same topic as this one."""
    text = _strip_tags(str(entry.get("content", "")))
    if len(text) < MIN_CANDIDATE_CHARS // 2:
        return 1
    return 1 + sum(
        1 for other in accepted
        if trigram_similarity(text, _strip_tags(str(other.get("content", "")))) >= TOPIC_SIMILARITY
    )


def collect_candidates(
    entries: Iterable[Mapping[str, Any]],
    *,
    now: datetime | None = None,
    min_score: float = MIN_CANDIDATE_SCORE,
    min_chars: int = MIN_CANDIDATE_CHARS,
) -> tuple[list[Candidate], dict[str, int]]:
    """Rank the entries that may become durable memory, newest copy per topic.

    ``entries`` are journal records (``cursor``, ``content``, ``timestamp``, plus
    the provenance written at ingest).  Rejections are counted by reason so the
    audit entry can say *why* something was not offered, without quoting it.
    """
    moment = now or datetime.now(timezone.utc)
    rejected: dict[str, int] = {}
    accepted: list[Mapping[str, Any]] = []
    hashes: set[str] = set()

    for entry in reversed(list(entries)):
        content = str(entry.get("content", ""))
        stripped = _strip_tags(content)
        origin = entry.get("origin")
        if origin == "untrusted":
            rejected["untrusted"] = rejected.get("untrusted", 0) + 1
            continue
        if len(stripped) < min_chars:
            rejected["too_short"] = rejected.get("too_short", 0) + 1
            continue
        digest = entry.get("content_hash")
        if isinstance(digest, str) and digest in hashes:
            rejected["duplicate"] = rejected.get("duplicate", 0) + 1
            continue
        if isinstance(digest, str):
            hashes.add(digest)
        accepted.append(entry)

    candidates: list[Candidate] = []
    for entry in accepted:
        content = _strip_tags(str(entry.get("content", "")))
        occurrences = _cluster(entry, accepted)
        day_keys = {
            str(other.get("timestamp", ""))[:10] for other in accepted
            if trigram_similarity(content, _strip_tags(str(other.get("content", "")))) >= TOPIC_SIMILARITY
        }
        days = max(1, len({key for key in day_keys if key}))
        stamp = _parse_timestamp(entry.get("timestamp"))
        age_days = max(0.0, (moment - stamp).total_seconds() / 86_400) if stamp else RECENCY_HALF_LIFE_DAYS
        signals = {
            "frequency": min(1.0, (occurrences - 1) / 2.0),
            "multi_day": 1.0 if days >= 2 else 0.0,
            "richness": 1.0 if (len(content) >= 400 or content.count("\n") >= 2) else 0.5,
            "recency": 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS),
        }
        score = sum(SIGNAL_WEIGHTS[name] * value for name, value in signals.items())
        if score < min_score:
            rejected["low_score"] = rejected.get("low_score", 0) + 1
            continue
        candidates.append(Candidate(cursor=int(entry.get("cursor", 0)), content=content,
                                    timestamp=str(entry.get("timestamp", "")),
                                    score=round(score, 4), signals=signals,
                                    occurrences=occurrences, days=days))
    candidates.sort(key=lambda item: item.score, reverse=True)
    return candidates, rejected


def render_candidates(candidates: Sequence[Candidate]) -> str:
    if not candidates:
        return "(no candidate passed the gate)"
    return "\n\n".join(candidate.prompt_line() for candidate in candidates)


@dataclass(frozen=True)
class Decision:
    """One validated instruction from the consolidation model."""

    op: str
    target: str
    entry: str = ""
    source: str = ""
    observed: str = ""
    importance: int = 5
    trigger: str = ""
    match: str = ""
    section: str = ""
    reason: str = ""


@dataclass
class WriteOutcome:
    """Result of applying decisions to the durable files, without touching disk."""

    contents: dict[str, str] = field(default_factory=dict)
    applied: list[str] = field(default_factory=list)
    rejected: list[tuple[Decision, str]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.applied)

    def counts(self) -> dict[str, int]:
        summary = {"applied": len(self.applied), "rejected": len(self.rejected)}
        for decision, _ in self.rejected:
            key = f"rejected_{decision.op}"
            summary[key] = summary.get(key, 0) + 1
        return summary


def _extract_json(text: str) -> str | None:
    fenced = _FENCE.search(text)
    candidate = fenced.group(1) if fenced else text
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start < 0 or end <= start:
        return None
    return candidate[start:end + 1]


def parse_decisions(text: str) -> tuple[list[Decision] | None, list[str]]:
    """Parse the consolidation answer; ``None`` means it was not a decision document."""
    payload = _extract_json(text or "")
    if payload is None:
        return None, ["no JSON object in the answer"]
    try:
        parsed: Any = json.loads(payload)
    except ValueError as exc:
        return None, [f"invalid JSON ({type(exc).__name__})"]
    if not isinstance(parsed, dict):
        return None, ["missing 'decisions' list"]
    document = cast(dict[str, Any], parsed)
    if not isinstance(document.get("decisions"), list):
        return None, ["missing 'decisions' list"]

    decisions: list[Decision] = []
    problems: list[str] = []
    for index, raw in enumerate(cast(list[Any], document["decisions"])):
        if not isinstance(raw, dict):
            problems.append(f"decision {index}: not an object")
            continue
        item = cast(dict[str, Any], raw)
        op = str(item.get("op", "")).strip().lower()
        target = str(item.get("target", "MEMORY.md")).strip() or "MEMORY.md"
        if op not in OPS:
            problems.append(f"decision {index}: unknown op {op!r}")
            continue
        if target not in TARGET_LIMITS:
            problems.append(f"decision {index}: unknown target {target!r}")
            continue
        entry = _strip_tags(str(item.get("entry", "")))
        if op == "drop":
            if not str(item.get("match", "")).strip():
                problems.append(f"decision {index}: drop without 'match'")
                continue
        elif op != "reject":
            if not entry:
                problems.append(f"decision {index}: empty entry")
                continue
            if len(entry) > MAX_ENTRY_CHARS:
                problems.append(f"decision {index}: entry longer than {MAX_ENTRY_CHARS} chars")
                continue
            if op in {"merge", "supersede"} and not str(item.get("match", "")).strip():
                problems.append(f"decision {index}: {op} without 'match'")
                continue
            if not str(item.get("source", "")).strip():
                problems.append(f"decision {index}: entry without source")
                continue
        try:
            importance = int(item.get("importance", 5))
        except (TypeError, ValueError):
            importance = 5
        decisions.append(Decision(
            op=op, target=target, entry=entry,
            source=str(item.get("source", "")).strip(),
            observed=str(item.get("observed", "")).strip(),
            importance=max(1, min(10, importance)),
            trigger=str(item.get("trigger", "")).strip(),
            match=str(item.get("match", "")).strip(),
            section=str(item.get("section", "")).strip(),
            reason=str(item.get("reason", "")).strip(),
        ))
    return decisions, problems


def render_entry(entry: str, *, source: str, observed: str, importance: int,
                 trigger: str = "", suffix: str = "") -> str:
    """One durable entry with its provenance comment (the P1 entry contract).

    An entry is one line: a journal candidate can span several bullets, and a
    multi-line entry would smuggle extra bullets into the index.  Whitespace is
    collapsed here, in the single place every writer path goes through.
    """
    flattened = _WHITESPACE.sub(" ", entry).strip()
    parts = [f"observed: {observed or 'unknown'}", f"source: {source}",
             f"importance: {importance}"]
    if trigger:
        parts.append(f"trigger: {trigger}")
    if suffix:
        parts.append(suffix)
    return f"- {flattened}\n  <!-- {' | '.join(parts)} -->"


def _entry_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if _ENTRY_LINE.match(line)]


def _insert_after_line(lines: list[str], index: int, block: list[str]) -> None:
    lines[index + 1:index + 1] = block


def _append_to_section(lines: list[str], section: str, block: list[str]) -> bool:
    """Insert *block* at the end of the section whose heading contains *section*."""
    if not section:
        return False
    start = None
    for index, line in enumerate(lines):
        if line.startswith("#") and section.lower() in line.lower():
            start = index
            break
    if start is None:
        return False
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("#"):
            end = index
            break
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    lines[end:end] = block
    return True


def _find_entry(lines: list[str], match: str) -> int | None:
    needle = match.lower()
    for index, line in enumerate(lines):
        if _ENTRY_LINE.match(line) and needle in line.lower():
            return index
    return None


def apply_decisions(
    contents: Mapping[str, str],
    decisions: Sequence[Decision],
    *,
    today: str,
) -> WriteOutcome:
    """Apply validated decisions to the durable files and report what happened.

    The writer never removes a line: it adds entries, appends to an existing entry
    (merge) or marks one superseded in place and inserts the replacement below it.
    A batch that would push a file over its character limit is rejected whole, so
    the caller can fall back to an append-only promotion instead of a partial write.
    """
    outcome = WriteOutcome(contents={name: contents.get(name, "") for name in TARGET_LIMITS})
    before = {name: len(_entry_lines(outcome.contents[name])) for name in TARGET_LIMITS}
    pending: dict[str, list[Decision]] = {name: [] for name in TARGET_LIMITS}

    for decision in decisions:
        if decision.op == "reject":
            outcome.rejected.append((decision, decision.reason or "model rejected"))
            continue
        pending[decision.target].append(decision)

    for name, items in pending.items():
        if not items:
            continue
        lines = outcome.contents[name].splitlines()
        for decision in items:
            block = render_entry(decision.entry, source=decision.source,
                                 observed=decision.observed or today,
                                 importance=decision.importance,
                                 trigger=decision.trigger).splitlines()
            if decision.op == "add":
                if not _append_to_section(lines, decision.section, block):
                    lines.extend(block)
                outcome.applied.append(f"add {name} <- {decision.source}")
            elif decision.op == "drop":
                index = _find_entry(lines, decision.match)
                if index is None:
                    outcome.rejected.append((decision, f"no entry matching {decision.match!r}"))
                    continue
                end = index + 1
                while end < len(lines) and _PROVENANCE.match(lines[end]):
                    end += 1
                del lines[index:end]
                outcome.applied.append(f"drop {name} ({decision.reason or 'stale'})")
            else:
                index = _find_entry(lines, decision.match)
                if index is None:
                    outcome.rejected.append((decision, f"no entry matching {decision.match!r}"))
                    continue
                if decision.op == "merge":
                    if decision.entry.lower() in lines[index].lower():
                        outcome.rejected.append((decision, "already present"))
                        continue
                    lines[index] = f"{lines[index].rstrip()} {decision.entry}"
                    outcome.applied.append(f"merge {name} <- {decision.source}")
                else:
                    _insert_after_line(lines, index, [f"  <!-- superseded: {today} -->", *block])
                    outcome.applied.append(f"supersede {name} <- {decision.source}")
        outcome.contents[name] = "\n".join(lines).rstrip() + "\n"

    for name, limit in TARGET_LIMITS.items():
        text = outcome.contents[name]
        if len(text) > limit:
            outcome.contents[name] = contents.get(name, "")
            outcome.rejected.append((Decision(op="batch", target=name), f"over limit {limit}"))
            outcome.applied = [item for item in outcome.applied if name not in item]
            continue
        kept = len(_entry_lines(text))
        allowed = before[name] * (1 - MAX_PRIOR_ENTRY_LOSS)
        if kept < allowed:
            outcome.contents[name] = contents.get(name, "")
            outcome.rejected.append((Decision(op="batch", target=name), "prior entry loss over limit"))
            outcome.applied = [item for item in outcome.applied if name not in item]
    return outcome


def fallback_entries(candidates: Sequence[Candidate], *, limit: int = 5) -> list[str]:
    """Deterministic promotion used when the model cannot return decisions.

    Memory must not freeze because a provider returned 429: the best candidates are
    promoted by the gate alone, marked ``unconsolidated`` so a later run can merge
    them properly.
    """
    entries: list[str] = []
    for candidate in candidates[:limit]:
        importance = max(1, min(10, round(candidate.score * 10)))
        entries.append(render_entry(candidate.content, source=candidate.source,
                                    observed=candidate.timestamp[:10],
                                    importance=importance, suffix="unconsolidated"))
    return entries
