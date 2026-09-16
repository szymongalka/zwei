"""Calendar records: event-field projection, version supersede and remote cleanup."""

from xml.etree import ElementTree

import pytest

from nanobot.personal import connectors
from nanobot.personal.config import Account, PersonalConfig
from nanobot.personal.ics import event_projection, refresh_calendar_projections
from nanobot.personal.service import PersonalService
from nanobot.personal.vector import VectorMemory

CALENDAR = "https://dav.example.org/cal/"
EVENT = CALENDAR + "event-1.ics"


def account(**kwargs):
    return Account(
        id="one",
        label="one",
        kind="apple",
        username="agent@example.org",
        mail_enabled=False,
        calendar_enabled=True,
        **kwargs,
    )


def ics(
    summary="Montaż TV",
    start="20260918T183000",
    end="20260918T193000",
    location="J Brzechwy 2A/20",
    description="Spotkanie z ekipą\\nWejście od podwórza",
    modified="20260912T101500Z",
):
    return "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Apple Inc.//iOS 17.0//EN",
            "BEGIN:VTIMEZONE",
            "TZID:Europe/Warsaw",
            "BEGIN:STANDARD",
            "DTSTART:19701025T030000",
            "TZOFFSETFROM:+0200",
            "TZOFFSETTO:+0100",
            "END:STANDARD",
            "END:VTIMEZONE",
            "BEGIN:VEVENT",
            "UID:event-1",
            f"DTSTAMP:{modified}",
            f"LAST-MODIFIED:{modified}",
            f"DTSTART;TZID=Europe/Warsaw:{start}",
            f"DTEND;TZID=Europe/Warsaw:{end}",
            f"SUMMARY:{summary}",
            f"LOCATION:{location}",
            f"DESCRIPTION:{description}",
            "ATTENDEE;CN=Szymon:mailto:szymon@example.org",
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        ]
    )


class FakeDAV:
    """Minimal CalDAV surface: one collection of etag-tagged resources."""

    def __init__(self, entries: dict[str, tuple[str, str]]):
        self.entries = entries

    def collections(self, kind: str) -> list[str]:
        return [CALENDAR] if kind == "calendar" else []

    def properties(self, url: str, properties: str, depth: str = "0") -> ElementTree.Element:
        responses = "".join(
            f"<d:response><d:href>{target}</d:href><d:propstat><d:prop>"
            f"<d:getetag>{etag}</d:getetag></d:prop></d:propstat></d:response>"
            for target, (etag, _content) in self.entries.items()
        )
        return ElementTree.fromstring(f'<d:multistatus xmlns:d="DAV:">{responses}</d:multistatus>')

    def target(self, base: str, href: str) -> str:
        return href

    def request(self, method: str, target: str, body: str = "", depth: str = "0") -> bytes:
        return self.entries[target][1].encode()


class Remote:
    """Statement recorder standing in for the pgvector connection."""

    def __init__(self):
        self.statements: list[tuple[str, object]] = []

    def execute(self, sql: str, parameters: object = None) -> None:
        self.statements.append((sql, parameters))

    def __enter__(self) -> "Remote":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


@pytest.fixture
def service(tmp_path):
    service = PersonalService(PersonalConfig(data_dir=str(tmp_path / "data")), tmp_path)
    service.save_account(account().model_dump())
    return service


def install(monkeypatch, entries):
    monkeypatch.setattr(connectors, "DAVClient", lambda _account, _base: FakeDAV(entries))


def vector_for(service, monkeypatch, tmp_path):
    vector = VectorMemory(service.store, tmp_path / "unused", "test-model")
    monkeypatch.setattr(vector, "initialize", lambda: None)
    monkeypatch.setattr(vector, "embed", lambda values: [[0.0] * 384 for _ in values])
    remote = Remote()
    monkeypatch.setattr(vector, "connect", lambda: remote)
    return vector, remote


def test_calendar_projection_keeps_event_fields_without_vcalendar_noise():
    text = event_projection(ics())
    assert "Termin: 2026-09-18 18:30–19:30 (Europe/Warsaw)" in text
    assert "Wydarzenie: Montaż TV" in text
    assert "Miejsce: J Brzechwy 2A/20" in text
    assert "Opis: Spotkanie z ekipą Wejście od podwórza" in text
    assert "VTIMEZONE" not in text and "ATTENDEE" not in text and "19701025T030000" not in text


def test_calendar_projection_covers_all_day_and_utc_values():
    all_day = event_projection(ics(start="20260918", end="20260919"))
    assert "Termin: 2026-09-18 (cały dzień)" in all_day
    utc = event_projection(
        ics()
        .replace("DTSTART;TZID=Europe/Warsaw:20260918T183000", "DTSTART:20260918T163000Z")
        .replace("DTEND;TZID=Europe/Warsaw:20260918T193000", "DTEND:20260918T173000Z")
    )
    assert "Termin: 2026-09-18 16:30–17:30 UTC" in utc
    assert "Opis:" not in event_projection("BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")


def test_calendar_sync_indexes_event_text_and_keeps_the_raw_archive(service, monkeypatch):
    install(monkeypatch, {EVENT: ('"1"', ics())})
    assert service.sync_account("one")["stored"] == 1
    hits = service.store.search("montaż")
    assert [hit["source"] for hit in hits] == ["calendar:one"]
    assert "Brzechwy" in hits[0]["excerpt"] and "VTIMEZONE" not in hits[0]["excerpt"]
    assert service.store.get(hits[0]["id"])["payload"]["content"].startswith("BEGIN:VCALENDAR")


def test_repeated_calendar_sync_is_idempotent_and_supersedes_an_updated_event(service, monkeypatch):
    entries = {EVENT: ('"1"', ics())}
    install(monkeypatch, entries)
    service.sync_account("one")
    service.sync_account("one")  # unchanged etag: nothing new is archived
    assert service.store.status()["documents"] == 1
    entries[EVENT] = (
        '"2"',
        ics(
            summary="Montaż TV (nowa godzina)",
            start="20260918T200000",
            end="20260918T210000",
            modified="20260913T090000Z",
        ),
    )
    service.sync_account("one")
    assert service.store.status()["documents"] == 2  # the archive keeps both versions
    hits = service.store.search("montaż")
    assert len(hits) == 1 and "20:00" in hits[0]["excerpt"]
    pending = {row["id"]: row["superseded"] for row in service.store.pending(10)}
    assert sorted(pending.values()) == [0, 1]  # stale copy is queued for cleanup only


def test_superseded_version_is_removed_from_the_remote_projection(service, monkeypatch, tmp_path):
    entries = {EVENT: ('"1"', ics())}
    install(monkeypatch, entries)
    service.sync_account("one")
    vector, remote = vector_for(service, monkeypatch, tmp_path)
    vector.sync()
    assert service.store.pending(10) == []
    stale = service.store.search("montaż")[0]["id"]
    entries[EVENT] = (
        '"2"',
        ics(summary="Montaż TV", description="Bez zmian opisu", modified="20260913T090000Z"),
    )
    service.sync_account("one")
    vector.sync()
    deletes = [sql for sql, _ in remote.statements if sql.startswith("DELETE")]
    assert any("personal_chunks" in sql for sql in deletes)
    assert (
        "DELETE FROM personal_documents WHERE id=%s AND namespace=%s",
        (stale, service.store.namespace),
    ) in remote.statements
    assert service.store.pending(10) == []


def test_legacy_raw_calendar_records_are_reprojected_for_search(service):
    identifier = service.store.put(
        "calendar:one", EVENT, {"href": EVENT, "etag": '"1"', "content": ics()}, ics()
    )
    assert "BEGIN:VCALENDAR" in service.store.search("montaż")[0]["excerpt"]
    assert refresh_calendar_projections(service.store, "calendar:one") == 1
    assert refresh_calendar_projections(service.store, "calendar:one") == 0
    hits = service.store.search("montaż")
    assert hits[0]["id"] == identifier and hits[0]["excerpt"].startswith("Termin: ")
    assert service.store.pending(5)[0]["text"].startswith("Termin: ")
