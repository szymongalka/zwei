"""Topic notes, day notes and the three-level read."""

import pytest

from nanobot.agent.memory import MemoryStore
from nanobot.agent.memory_notes import (
    OVERVIEW_CHARS,
    SUMMARY_CHARS,
    append_day_note,
    append_episode_line,
    day_note_path,
    index_lines,
    list_notes,
    note_path,
    notes_dir,
    read_note,
    topic_slug,
)


@pytest.fixture
def workspace(tmp_path):
    return tmp_path


def write_note(workspace, topic, body):
    path = note_path(workspace, topic)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


class TestTopicNames:
    def test_slug_is_file_safe(self):
        assert topic_slug("Brama Zwei / deploy") == "brama-zwei-deploy"
        assert topic_slug("KSeF") == "ksef"

    def test_empty_topic_is_rejected(self):
        with pytest.raises(ValueError):
            topic_slug("  ---  ")


class TestThreeLevelRead:
    def test_summary_stops_at_the_summary_budget(self, workspace):
        write_note(workspace, "brama", "First line about the gateway.\n" + "x" * SUMMARY_CHARS)
        summary = read_note(workspace, "brama", "summary")
        assert summary.startswith("First line about the gateway.")
        assert len(summary) < SUMMARY_CHARS + 120

    def test_overview_is_larger_than_summary_and_bounded(self, workspace):
        write_note(workspace, "brama", "y" * (OVERVIEW_CHARS + 5_000))
        overview = read_note(workspace, "brama", "overview")
        assert len(overview) > SUMMARY_CHARS
        assert len(overview) < OVERVIEW_CHARS + 120

    def test_full_returns_everything(self, workspace):
        body = "z" * (OVERVIEW_CHARS + 5_000)
        write_note(workspace, "brama", body)
        assert read_note(workspace, "brama", "full") == body

    def test_unknown_level_is_an_error(self, workspace):
        write_note(workspace, "brama", "body")
        with pytest.raises(ValueError):
            read_note(workspace, "brama", "everything")

    def test_missing_note_is_an_error_not_an_empty_string(self, workspace):
        with pytest.raises(ValueError):
            read_note(workspace, "brak", "summary")


class TestIndex:
    def test_index_lines_point_at_notes_without_copying_them(self, workspace):
        write_note(workspace, "ksef", "Wysyłka faktur przez usługę Zwei KSeF.\n" + "x" * 500)
        lines = index_lines(workspace)
        assert len(lines) == 1
        assert lines[0].startswith("- ksef: Wysyłka faktur")
        assert "memory/notes/ksef.md" in lines[0]
        assert "x" * 50 not in lines[0]

    def test_day_notes_are_not_part_of_the_topic_index(self, workspace):
        write_note(workspace, "ksef", "Temat")
        append_day_note(workspace, "coś się stało", day="2026-09-17")
        assert [line for line in index_lines(workspace) if "2026-09-17" in line] == []

    def test_list_notes_marks_day_notes(self, workspace):
        write_note(workspace, "ksef", "Temat")
        append_day_note(workspace, "wpis", day="2026-09-17")
        notes = {note.topic: note for note in list_notes(workspace)}
        assert notes["ksef"].day is None
        assert notes["2026-09-17"].day == "2026-09-17"


class TestDayNotes:
    def test_append_creates_a_header_and_is_idempotent(self, workspace):
        path = append_day_note(workspace, "pierwsza rzecz", day="2026-09-17")
        append_day_note(workspace, "pierwsza rzecz", day="2026-09-17")
        text = path.read_text(encoding="utf-8")
        assert text.startswith("# Notatki dnia 2026-09-17")
        assert text.count("pierwsza rzecz") == 1

    def test_episode_line_keeps_the_headline_short(self, workspace):
        path = append_episode_line(workspace, "telegram:1", "Wynik: " + "x" * 500,
                                   day="2026-09-17")
        line = [line for line in path.read_text(encoding="utf-8").splitlines() if "Wynik" in line][0]
        assert len(line) < 260

    def test_day_note_path_uses_the_given_day(self, workspace):
        assert day_note_path(workspace, "2026-01-02").name == "2026-01-02.md"
        assert notes_dir(workspace).name == "notes"


class TestFlushFromTheStore:
    def test_session_boundary_lands_in_today_note(self, workspace):
        store = MemoryStore(workspace)
        path = store.flush_day_note("telegram:1", first_cursor=5, last_cursor=5,
                                    entries=1, characters=120)
        assert path is not None
        assert "[telegram:1] journal cursors 5-5" in path.read_text(encoding="utf-8")

    def test_raw_fallback_reports_message_count(self, workspace):
        store = MemoryStore(workspace)
        path = store.flush_day_note("telegram:1", first_cursor=0, last_cursor=0,
                                    entries=12, characters=900)
        assert path is not None
        assert "12 messages" in path.read_text(encoding="utf-8")
