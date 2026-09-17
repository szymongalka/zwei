"""iCalendar projections: index the event fields a person searches for.

Calendar resources are archived exactly as the DAV server returned them. The text
built here is a *search projection*: it keeps the event summary, its local time
window, the location and the description, and drops VCALENDAR/VTIMEZONE/ATTENDEE
noise. Raw records stay untouched, and ``PersonalStore.set_projection`` can
recompute the view for records ingested before this projection existed.
"""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Any, cast

from nanobot.personal.store import readable_excerpt

if TYPE_CHECKING:
    from nanobot.personal.store import PersonalStore

# Records whose indexed text still starts with this marker were archived raw.
RAW_ICS_MARKER = "BEGIN:VCALENDAR"
_SKIPPED = {"BEGIN", "END", "ATTENDEE", "ORGANIZER"}
_TIMESTAMP = re.compile(r"^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})(Z)?)?$")

Property = tuple[dict[str, str], str]


def _unfold(ics: str) -> list[str]:
    """Split an iCalendar stream into logical lines, joining folded continuations."""
    lines: list[str] = []
    for raw in ics.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in {" ", "\t"} and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _split(line: str) -> tuple[str, dict[str, str], str]:
    """Return the content name, its parameters and the value of one content line."""
    quoted = False
    for index, character in enumerate(line):
        if character == '"':
            quoted = not quoted
        elif character == ":" and not quoted:
            head, value = line[:index], line[index + 1 :]
            break
    else:
        return line.strip().upper(), {}, ""
    parts = head.split(";")
    parameters: dict[str, str] = {}
    for parameter in parts[1:]:
        key, _, parameter_value = parameter.partition("=")
        parameters[key.strip().upper()] = parameter_value.strip().strip('"')
    return parts[0].strip().upper(), parameters, value


def _unescape(value: str) -> str:
    return (
        value.replace("\\N", "\n")
        .replace("\\n", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def _parse(value: str) -> tuple[str, str, bool] | None:
    """Return (date, time, is_utc); ``time`` is empty for an all-day value."""
    match = _TIMESTAMP.match(value.strip())
    if match is None:
        return None
    year, month, day, hour, minute, _second, zulu = match.groups()
    date = f"{year}-{month}-{day}"
    if hour is None:
        return date, "", False
    return date, f"{hour}:{minute}", bool(zulu)


def _zone_suffix(parameters: dict[str, str], zulu: bool) -> str:
    if zulu:
        return " UTC"
    zone = parameters.get("TZID", "")
    return f" ({zone})" if zone else ""


def _window(start: Property, end: Property | None) -> str:
    """Render the event window in the zone the resource itself declares."""
    start_parsed = _parse(start[1])
    if start_parsed is None:
        return start[1].strip()
    date, time, zulu = start_parsed
    if not time:
        return f"{date} (cały dzień)"
    if end is not None:
        end_parsed = _parse(end[1])
        if (
            end_parsed is not None
            and end_parsed[1]
            and end_parsed[0] == date
            and end_parsed[2] == zulu
        ):
            return f"{date} {time}–{end_parsed[1]}{_zone_suffix(start[0], zulu)}"
    return f"{date} {time}{_zone_suffix(start[0], zulu)}"


def _events(ics: str) -> list[dict[str, Property]]:
    """Return the VEVENT blocks of one resource; properties outside them are noise."""
    events: list[dict[str, Property]] = []
    current: dict[str, Property] | None = None
    for line in _unfold(ics):
        name, parameters, value = _split(line)
        if name == "BEGIN":
            if value.strip().upper() == "VEVENT":
                current = {}
            continue
        if name == "END":
            if value.strip().upper() == "VEVENT" and current is not None:
                events.append(current)
                current = None
            continue
        if current is not None and name and name not in _SKIPPED:
            current.setdefault(name, (parameters, value))
    return events


def event_projection(ics: str, *, description_limit: int = 600) -> str:
    """Build the searchable text of a calendar resource (empty when it has no event)."""
    blocks: list[str] = []
    for event in _events(ics):
        lines: list[str] = []
        start = event.get("DTSTART")
        if start is not None:
            lines.append("Termin: " + _window(start, event.get("DTEND")))
        for label, field in (("Wydarzenie", "SUMMARY"), ("Miejsce", "LOCATION")):
            entry = event.get(field)
            if entry is not None and _unescape(entry[1]).strip():
                lines.append(f"{label}: " + readable_excerpt(_unescape(entry[1]), 300))
        description = event.get("DESCRIPTION")
        if description is not None:
            text = readable_excerpt(_unescape(description[1]), description_limit)
            if text:
                lines.append("Opis: " + text)
        if lines:
            blocks.append("\n".join(lines))
    return "\n".join(blocks)


def event_version(ics: str) -> str:
    """Stable change marker for one resource, taken from the event's own timestamps."""
    parts: list[str] = []
    for line in _unfold(ics):
        name, _parameters, value = _split(line)
        if name in {"LAST-MODIFIED", "DTSTAMP", "SEQUENCE"} and value.strip():
            parts.append(f"{name}={value.strip()}")
    if parts:
        return "|".join(parts)
    return "hash:" + hashlib.sha256(event_projection(ics).encode("utf-8")).hexdigest()[:16]


def refresh_calendar_projections(store: PersonalStore, source: str, limit: int = 200) -> int:
    """Re-index calendar records archived before event projections existed."""
    refreshed = 0
    for identifier in store.projection_candidates(source, RAW_ICS_MARKER, limit):
        payload = store.get(identifier)["payload"]
        if not isinstance(payload, dict):
            continue
        # The archived payload is JSON-shaped; cast it so the strict type check
        # can follow the key read below.
        fields = cast("dict[str, Any]", payload)
        content = fields.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        store.set_projection(identifier, event_projection(content))
        refreshed += 1
    return refreshed
