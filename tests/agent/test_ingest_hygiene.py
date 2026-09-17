"""Ingest hygiene: provenance, dedup, boilerplate, and the Dream candidate gate."""

import json

import pytest

from nanobot.agent.memory import (
    HISTORY_HYGIENE_ENV,
    MemoryStore,
    _content_hash,
    _is_boilerplate,
)
from nanobot.session_kinds import is_candidate, session_kind


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path)


def entries(store):
    if not store.history_file.exists():
        return []
    return [json.loads(line) for line in store.history_file.read_text().splitlines() if line.strip()]


class TestSessionKinds:
    def test_classifies_by_prefix(self):
        assert session_kind("telegram:7365213421") == "interactive"
        assert session_kind("websocket:abc") == "interactive"
        assert session_kind(None) == "unknown"
        assert session_kind("heartbeat") == "heartbeat"
        assert session_kind("personal-development:710d") == "development"
        assert session_kind("personal-evolution:710d") == "development"
        assert session_kind("dream") == "dream"
        assert session_kind("cron:daily") == "cron"
        assert session_kind("subagent:xyz") == "subagent"
        assert session_kind("diagnostic") == "diagnostic"

    def test_only_interactive_and_unkeyed_sessions_are_candidates(self):
        assert is_candidate("telegram:1")
        assert is_candidate(None)
        for key in ("heartbeat", "personal-development:x", "dream", "cron:y", "subagent:z", "diagnostic"):
            assert not is_candidate(key), key


class TestProvenanceAtIngest:
    def test_every_entry_carries_kind_origin_and_hash(self, store):
        store.append_history("Projekt X jest aktywny", session_key="telegram:1")
        record = entries(store)[0]
        assert record["session_kind"] == "interactive"
        assert record["origin"] == "agent"
        assert record["content_hash"] == _content_hash("Projekt X jest aktywny")

    def test_raw_archives_are_untrusted(self, store):
        store.raw_archive([{"role": "user", "content": "pytanie"},
                           {"role": "assistant", "content": "odpowiedz"}],
                          session_key="telegram:1")
        stored = entries(store)
        assert stored and stored[0]["origin"] == "untrusted"


class TestIngestFilter:
    def test_exact_repeat_is_not_appended(self, store):
        first = store.append_history("Ten sam fakt o repozytorium", session_key="telegram:1")
        second = store.append_history("Ten sam fakt o repozytorium", session_key="telegram:1")
        assert second == first
        assert len(entries(store)) == 1

    def test_repeat_is_detected_across_whitespace_and_case(self, store):
        store.append_history("Fakt: rewizja repozytorium", session_key="telegram:1")
        store.append_history("  fakt:   rewizja\nrepozytorium ", session_key="telegram:1")
        assert len(entries(store)) == 1

    @pytest.mark.parametrize("content", ["All clear", "nothing to report.", "- [ephemeral] Nic nowego"])
    def test_boilerplate_is_not_appended(self, store, content):
        assert _is_boilerplate(content)
        store.append_history(content, session_key="heartbeat")
        assert entries(store) == []

    def test_substantive_entry_is_kept(self, store):
        store.append_history("- [durable] Bramę trzeba podnosić po zmianie configu", session_key="telegram:1")
        assert len(entries(store)) == 1

    def test_switch_restores_the_old_ingest_behaviour(self, tmp_path, monkeypatch):
        monkeypatch.setenv(HISTORY_HYGIENE_ENV, "0")
        store = MemoryStore(tmp_path)
        assert store.history_hygiene is False
        store.append_history("All clear", session_key="heartbeat")
        store.append_history("Ten sam fakt", session_key="telegram:1")
        store.append_history("Ten sam fakt", session_key="telegram:1")
        assert len(entries(store)) == 3

    def test_empty_content_keeps_the_strip_think_contract(self, store):
        # A leaked template is persisted as an empty record, never as the leak.
        store.append_history("<channel|>", session_key="telegram:1")
        stored = entries(store)
        assert len(stored) == 1 and stored[0]["content"] == ""


class TestDreamCandidateGate:
    def test_scheduled_sessions_never_reach_the_model(self, store):
        store.append_history("Heartbeat state: no active checks in HEARTBEAT.md", session_key="heartbeat")
        store.append_history("Cykl rozwoju zakonczony bez zmian w kodzie", session_key="personal-development:x")
        store.append_history("Ustalenie: projekt X wymaga migracji danych", session_key="telegram:1")
        result = store.build_dream_prompt()
        assert result is not None
        prompt, cursor = result
        assert "projekt X wymaga migracji" in prompt
        assert "HEARTBEAT.md" not in prompt
        assert "Cykl rozwoju" not in prompt
        assert "2 entries from scheduled or background sessions" in prompt
        # The cursor covers every entry that was considered, noise included.
        assert cursor == entries(store)[-1]["cursor"]

    def test_only_noise_stays_idle(self, store):
        store.append_history("Heartbeat state: no active checks", session_key="heartbeat")
        store.append_history("Kontrola dryfu: nic nowego", session_key="heartbeat")
        assert store.build_dream_prompt() is None
        assert store.dream_remaining_entries() == 2

    def test_exact_repeat_inside_a_batch_is_collapsed(self, store):
        # Ingest hygiene prevents new exact repeats; the view still has to collapse
        # them for journals written before the filter existed.
        legacy = [{"cursor": c, "timestamp": "2026-09-01 10:0%d" % c,
                   "content": "- [durable] Revizia repo: 4a32498b"} for c in (1, 2)]
        store.history_file.write_text(
            "\n".join(json.dumps(item) for item in legacy) + "\n", encoding="utf-8")
        result = store.build_dream_prompt()
        assert result is not None
        prompt, _ = result
        assert "1 exact repeats were collapsed" in prompt

    def test_legacy_entries_without_provenance_are_still_candidates(self, store):
        legacy = {"cursor": 1, "timestamp": "2026-09-01 10:00", "content": "Stary wpis bez provenance"}
        store.history_file.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
        result = store.build_dream_prompt()
        assert result is not None and "Stary wpis bez provenance" in result[0]

    def test_run_record_reports_the_gate(self, store):
        store.append_history("Heartbeat state: no active checks", session_key="heartbeat")
        store.append_history("Ustalenie: rewizja repozytorium to 4a32498b", session_key="telegram:1")
        assert store.build_dream_prompt() is not None
        record = store.record_dream_run(completed=True, commit="abc123")
        assert record is not None and record["excluded"] == 1 and record["repeats"] == 0
