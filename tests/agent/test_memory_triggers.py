"""Trigger injection from curated memory entries (P3)."""

from __future__ import annotations

import json

import pytest

from nanobot.agent import memory_triggers
from nanobot.agent.memory_triggers import (
    INJECTION_CHARS,
    TRIGGERS_ENV,
    build_block,
    enabled,
    match_entries,
    parse_entries,
)

MEMORY = """# Pamięć

- Brama: deploy wymaga sekcji `personal` w config.json (memory/notes/gateway.md)
  <!-- observed: 2026-09-15 | source: memory/EVOLUTION.md | importance: 9 | trigger: deploy, brama -->
- KSeF: faktury trzymane wyłącznie w bazie usługi (memory/notes/ksef.md)
  <!-- observed: 2026-09-17 | source: skills/ksef-archive | importance: 6 | trigger: faktura -->
- Wpis bez notatki, więc nie do wstrzyknięcia <!-- trigger: cokolwiek -->
"""


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "memory" / "notes").mkdir(parents=True)
    (tmp_path / "memory" / "MEMORY.md").write_text(MEMORY, encoding="utf-8")
    (tmp_path / "memory" / "notes" / "gateway.md").write_text(
        "# Brama\n\nRestart wymaga XDG_RUNTIME_DIR i SIGKILL, bo SIGTERM nie kończy event loopu.\n",
        encoding="utf-8")
    (tmp_path / "memory" / "notes" / "ksef.md").write_text(
        "# KSeF\n\nFaktury FA(3) mieszkają w PostgreSQL usługi, nie w archiwum pamięci.\n",
        encoding="utf-8")
    return tmp_path


class TestParse:
    def test_reads_annotations_above_and_inline(self):
        entries = parse_entries(MEMORY)
        assert [entry.topic for entry in entries] == ["gateway", "ksef", ""]
        assert entries[0].triggers == ("deploy", "brama")
        assert entries[0].importance == 9
        assert entries[0].source == "memory/EVOLUTION.md"
        assert entries[2].topic == "" and entries[2].triggers == ("cokolwiek",)

    def test_inline_annotation_is_read(self):
        entries = parse_entries("- Temat (memory/notes/t.md) <!-- trigger: alfa | importance: 99 -->")
        assert entries[0].topic == "t" and entries[0].importance == 10

    def test_short_phrases_are_dropped(self):
        entries = parse_entries("- Temat (memory/notes/t.md) <!-- trigger: ok, alfa -->")
        assert entries[0].triggers == ("alfa",)


class TestMatch:
    def test_matches_by_phrase_not_by_similarity(self):
        entries = parse_entries(MEMORY)
        assert [e.topic for e in match_entries(entries, "musimy zrobić deploy bramy dzisiaj")] == ["gateway"]
        assert match_entries(entries, "coś zupełnie innego") == []

    def test_most_important_entry_comes_first_and_limit_holds(self):
        entries = parse_entries(MEMORY)
        matched = match_entries(entries, "deploy i faktura", limit=1)
        assert [e.topic for e in matched] == ["gateway"]

    def test_entries_without_a_note_are_never_matched(self):
        entries = parse_entries(MEMORY)
        assert all(entry.topic for entry in match_entries(entries, "cokolwiek"))


class TestBuildBlock:
    def test_injects_the_note_behind_the_trigger(self, workspace):
        block = build_block(workspace, "jak wygląda deploy bramy po zmianie configu?")
        assert block is not None and block.source == "personal_memory_triggers"
        assert "SIGKILL" in block.content
        # The index line itself is already in the prompt and must not be repeated.
        assert "EVOLUTION.md" not in block.content

    def test_missing_note_is_not_injected(self, workspace):
        assert build_block(workspace, "cokolwiek ciekawego") is None

    def test_short_and_command_messages_are_ignored(self, workspace):
        assert build_block(workspace, "deploy") is None
        assert build_block(workspace, "/deploy bramy") is None

    def test_switch_disables_injection(self, workspace):
        assert build_block(workspace, "jak wygląda deploy bramy?", injection_enabled=False) is None

    def test_budget_is_bounded(self, workspace):
        (workspace / "memory" / "notes" / "gateway.md").write_text(
            "# Brama\n\n" + "x" * 5_000, encoding="utf-8")
        block = build_block(workspace, "jak wygląda deploy bramy?")
        assert block is not None and len(block.content) < INJECTION_CHARS + 300

    def test_run_is_journalled(self, workspace):
        build_block(workspace, "jak wygląda deploy bramy?")
        record = json.loads((workspace / "memory" / "retrieval_stats.jsonl").read_text().splitlines()[-1])
        assert record["kind"] == "trigger"
        assert record["topics"] == ["gateway"] and record["injected_chars"] > 0

    def test_environment_switch(self, monkeypatch):
        monkeypatch.setenv(TRIGGERS_ENV, "0")
        assert enabled() is False
        monkeypatch.setenv(TRIGGERS_ENV, "1")
        assert enabled() is True
        monkeypatch.delenv(TRIGGERS_ENV)
        assert enabled() is True
        assert memory_triggers.TRIGGER_LIMIT == 3
