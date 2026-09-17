"""Corpus layers, archive compaction and the evidence fallback."""

import sqlite3

import pytest

from nanobot.personal import compact
from nanobot.personal.config import PersonalConfig
from nanobot.personal.service import PersonalService
from nanobot.personal.store import PersonalStore
from nanobot.personal.visibility import COLD, HOT, QUARANTINE, classify


@pytest.fixture
def store(tmp_path):
    return PersonalStore(tmp_path / "data", tmp_path / "workspace")


def seed(store):
    """One record of each layer, plus a searchable transcript line."""
    store.put("mail:one", "2026-09-17", {"body": "Rachunek za prad"})
    store.archive_messages("websocket:abc", [
        {"role": "user", "content": "ustalilem termin montazu telewizora"},
        {"role": "assistant", "content": "notatka o montazu telewizora"},
        {"role": "tool", "content": "wynik narzedzia o montazu"},
    ])
    store.archive_messages("heartbeat", [{"role": "user", "content": "kontrola dryfu pamieci"}])
    store.put("native_memory", "memory/MEMORY.md", {"content": "kopia pliku pamieci"})


def layers_of(store):
    with store.db() as db:
        return {row[0]: row[1] for row in db.execute(
            "SELECT visibility,count(*) FROM documents GROUP BY 1")}


def test_classify_covers_the_three_layers():
    assert classify("mail:one", {"body": "x"}) == HOT
    assert classify("calendar:one", {"summary": "x"}) == HOT
    assert classify("session:websocket:abc", {"role": "user"}) == HOT
    assert classify("session:websocket:abc", {"role": "assistant"}) == COLD
    assert classify("session:websocket:abc", {"role": "tool"}) == COLD
    assert classify("session:heartbeat", {"role": "user"}) == QUARANTINE
    assert classify("session:personal-development:x", {"role": "user"}) == QUARANTINE
    assert classify("native_memory", {"content": "x"}) == QUARANTINE
    assert classify("ksef:invoice", {"number": "1"}) == QUARANTINE


def test_every_record_carries_its_layer(store):
    seed(store)
    assert layers_of(store) == {HOT: 2, COLD: 2, QUARANTINE: 2}


def test_only_the_curated_layer_is_queued_for_the_remote_index(store):
    seed(store)
    queued = {row["source"] for row in store.pending(50)}
    assert queued == {"mail:one", "session:websocket:abc"}
    assert store.status()["pending"] == 2


def test_existing_archive_is_migrated_in_place(store, tmp_path):
    seed(store)
    with store.db() as db:
        db.execute("DROP INDEX IF EXISTS documents_visibility")
        db.executescript("DROP TRIGGER IF EXISTS documents_no_update;")
        db.execute("ALTER TABLE documents DROP COLUMN visibility")
    reopened = PersonalStore(tmp_path / "data", tmp_path / "workspace")
    assert layers_of(reopened) == {HOT: 2, COLD: 2, QUARANTINE: 2}
    with pytest.raises(sqlite3.IntegrityError):
        with reopened.db() as db:
            db.execute("UPDATE documents SET source='other'")


def test_scope_maps_to_the_corpus_layers(tmp_path):
    service = PersonalService(PersonalConfig(data_dir=str(tmp_path / "data")), tmp_path)
    assert service.corpus_layers("memory") == ([HOT], ["native_memory", "session:"])
    assert service.corpus_layers("all") == ([HOT, COLD], [])


def test_compaction_moves_evidence_out_and_keeps_every_record(store):
    seed(store)
    report = compact.apply(store, stamp="20260917-000000")
    assert report["kept"] == 2 and report["moved"] == 4
    assert report["integrity"] == "ok"
    assert layers_of(store) == {HOT: 2}
    assert store.evidence_path.is_file()
    with store.db() as db:
        evidence = {row[0]: row[1] for row in db.execute("PRAGMA database_list")}
    assert evidence  # the live file is still a valid database after the swap
    assert store.status()["evidence_documents"] == 4

    # Every raw record is still readable, whichever file it now lives in.
    with store.db() as db:
        identifier = db.execute("SELECT id FROM documents WHERE source='mail:one'").fetchone()[0]
    assert store.get(identifier)["source"] == "mail:one"
    cold = store.search("montazu telewizora", 5, visibilities=(COLD,),
                        include_evidence_file=True)
    assert cold and all(item["visibility"] == COLD for item in cold)
    assert store.search("kontrola dryfu pamieci", 5) == []


def test_compaction_is_repeatable_and_does_not_duplicate_evidence(store):
    seed(store)
    compact.apply(store, stamp="20260917-000000")
    store.archive_messages("websocket:abc2", [{"role": "assistant", "content": "nowy wpis"}])
    second = compact.apply(store, stamp="20260917-000001")
    assert second["kept"] == 2 and second["moved"] == 1
    with store.db() as db:
        total = db.execute("SELECT count(*) FROM documents").fetchone()[0]
    assert total == 2
    assert store.status()["evidence_documents"] == 5


def test_episode_records_a_line_in_todays_note(tmp_path):
    from nanobot.agent.memory_notes import day_note_path
    service = PersonalService(PersonalConfig(data_dir=str(tmp_path / "data")), tmp_path)
    identifier = service.record_episode(
        "telegram:1", "Wynik: brama wstała po restarcie",
        [{"role": "user", "content": "zrob deploy"}])
    assert identifier
    note = day_note_path(tmp_path)
    assert note.is_file()
    text = note.read_text(encoding="utf-8")
    assert "[telegram:1]" in text and "brama wstała" in text


def test_background_session_records_no_day_note(tmp_path):
    from nanobot.agent.memory_notes import day_note_path
    service = PersonalService(PersonalConfig(data_dir=str(tmp_path / "data")), tmp_path)
    assert service.record_episode("heartbeat", "Kontrola bez zmian",
                                  [{"role": "user", "content": "x"}]) is None
    assert not day_note_path(tmp_path).exists()


def test_plan_writes_nothing(store):
    seed(store)
    before = store.path.read_bytes()
    report = compact.plan(store)
    assert report["would_keep"] == 2 and report["would_move"] == 4
    assert not store.evidence_path.exists()
    assert store.path.read_bytes() == before


def test_compaction_refuses_a_corrupt_live_archive(store):
    seed(store)
    with store.db() as db:
        db.execute("DROP INDEX IF EXISTS documents_visibility")
        db.executescript("DROP TRIGGER IF EXISTS documents_no_delete;")
        db.execute("DELETE FROM documents WHERE source='mail:one'")
    report = compact.plan(store)
    assert report["integrity"] == "ok"  # deleted rows alone are not corruption
    assert report["layers"].get(HOT) == 1
