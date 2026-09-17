"""Gated promotion (P2): the deterministic gate, the decision writer, the journal."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from nanobot.agent.memory import MemoryStore
from nanobot.agent.memory_gate import (
    MAX_ENTRY_CHARS,
    Candidate,
    Decision,
    apply_decisions,
    collect_candidates,
    fallback_entries,
    parse_decisions,
    render_entry,
    trigram_similarity,
)

FACT = ("- [durable] Brama wymaga sekcji personal w config.json; czysty main bez niej wpada "
        "w crash-loop przy starcie. Dowod: PR #26, rewizja 4a32498b, docs/fork-maintenance.md, "
        "sekcja 'Deploy a checked change'. Procedura: zatrzymaj brame, podmien plik, podnies "
        "usluge i sprawdz health endpointu.")


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path)
    s.write_memory("# Memory\n\n## Infrastruktura\n- stara rewizja 89e10525 byla nieaktualna\n")
    s.write_user("# User\n\n- Always: krotkie odpowiedzi\n")
    return s


def journal_entry(cursor, content, timestamp="2026-09-17 10:00", origin="agent", digest=None):
    return {"cursor": cursor, "content": content, "timestamp": timestamp, "origin": origin,
            "content_hash": digest or f"h{cursor}"}


class TestGate:
    def test_untrusted_and_short_entries_are_rejected(self):
        entries = [journal_entry(1, "[RAW] 12 messages\n" + "x" * 400, origin="untrusted"),
                   journal_entry(2, "krotki wpis")]
        candidates, rejected = collect_candidates(entries)
        assert candidates == []
        assert rejected == {"untrusted": 1, "too_short": 1}

    def test_repeated_topic_on_two_days_scores_highest(self):
        entries = [journal_entry(1, FACT, "2026-09-17 10:00", digest="a"),
                   journal_entry(2, FACT + " (korekta daty)", "2026-09-16 10:00", digest="b"),
                   journal_entry(3, "Zupelnie inny, jednorazowy fakt bez powtorki " + "y" * 200,
                                 "2026-09-17 11:00", digest="c")]
        candidates, _ = collect_candidates(entries, now=datetime(2026, 9, 17, 12, tzinfo=timezone.utc))
        assert candidates[0].occurrences >= 2 and candidates[0].days == 2
        assert candidates[0].score > candidates[-1].score
        assert candidates[0].source == "cursor:1"

    def test_duplicate_hash_keeps_the_newest_copy(self):
        entries = [journal_entry(1, FACT, digest="same"), journal_entry(2, FACT, digest="same")]
        candidates, rejected = collect_candidates(entries)
        assert [c.cursor for c in candidates] == [2]
        assert rejected.get("duplicate") == 1

    def test_old_low_score_entry_is_not_offered(self):
        entries = [journal_entry(1, "Jednorazowa notatka o niczym istotnym " + "z" * 200,
                                 "2026-01-01 10:00")]
        candidates, rejected = collect_candidates(entries,
                                                 now=datetime(2026, 9, 17, tzinfo=timezone.utc))
        assert candidates == [] and rejected.get("low_score") == 1

    def test_trigram_similarity_is_symmetric_and_normalized(self):
        assert trigram_similarity("Ten sam fakt", "ten   sam fakt") == 1.0
        assert trigram_similarity("abc", "xyz") == 0.0


class TestDecisionParsing:
    def test_parses_a_fenced_decision_document(self):
        text = ('```json\n{"decisions": [{"op": "add", "target": "MEMORY.md", '
                '"entry": "- [durable] Fakt", "source": "cursor:1", "importance": 9}]}\n```')
        decisions, problems = parse_decisions(text)
        assert problems == []
        assert decisions is not None and len(decisions) == 1
        assert decisions[0].entry == "Fakt"  # the [durable] tag is stripped
        assert decisions[0].importance == 9

    @pytest.mark.parametrize("text", ["", "no json here", '{"decisions": "nope"}'])
    def test_non_decision_answers_are_reported(self, text):
        decisions, problems = parse_decisions(text)
        assert decisions is None and problems

    def test_invalid_decisions_are_dropped_with_a_reason(self):
        text = json.dumps({"decisions": [
            {"op": "explode", "target": "MEMORY.md", "entry": "x", "source": "cursor:1"},
            {"op": "add", "target": "SOUL.md", "entry": "x", "source": "cursor:1"},
            {"op": "add", "target": "MEMORY.md", "entry": "x"},
            {"op": "add", "target": "MEMORY.md", "entry": "y" * (MAX_ENTRY_CHARS + 1),
             "source": "cursor:2"},
            {"op": "reject", "target": "USER.md", "reason": "jednorazowe"},
        ]})
        decisions, problems = parse_decisions(text)
        assert decisions is not None and [d.op for d in decisions] == ["reject"]
        assert len(problems) == 4


class TestWriter:
    def test_add_lands_in_the_named_section_with_provenance(self):
        outcome = apply_decisions({"MEMORY.md": "# Memory\n\n## Infrastruktura\n- stary fakt\n\n## Inne\n- x\n"},
                                  [Decision(op="add", target="MEMORY.md", entry="nowy fakt",
                                            source="cursor:7", observed="2026-09-15", importance=9,
                                            section="Infrastruktura")],
                                  today="2026-09-17")
        text = outcome.contents["MEMORY.md"]
        assert "- nowy fakt\n  <!-- observed: 2026-09-15 | source: cursor:7 | importance: 9 -->" in text
        assert text.index("nowy fakt") < text.index("## Inne")
        assert outcome.applied == ["add MEMORY.md <- cursor:7"]

    def test_supersede_marks_the_old_line_and_inserts_the_new_one(self):
        outcome = apply_decisions({"MEMORY.md": "# Memory\n- stara rewizja 89e10525\n- inny fakt\n"},
                                  [Decision(op="supersede", target="MEMORY.md",
                                            entry="rewizja to 5fe2b07e", source="cursor:8",
                                            match="stara rewizja")],
                                  today="2026-09-17")
        text = outcome.contents["MEMORY.md"]
        assert "<!-- superseded: 2026-09-17 -->" in text
        assert "- stara rewizja 89e10525\n" in text  # the previous entry is kept
        assert "rewizja to 5fe2b07e" in text

    def test_merge_requires_a_matching_line(self):
        base = {"MEMORY.md": "# Memory\n- fakt o bramie i deployu\n"}
        outcome = apply_decisions(base,
                                  [Decision(op="merge", target="MEMORY.md", entry="plus szczegol",
                                            source="cursor:9", match="fakt o bramie"),
                                   Decision(op="merge", target="MEMORY.md", entry="plus szczegol",
                                            source="cursor:10", match="nie ma takiej linii")],
                                  today="2026-09-17")
        assert "- fakt o bramie i deployu plus szczegol" in outcome.contents["MEMORY.md"]
        assert [reason for _, reason in outcome.rejected] == ["no entry matching 'nie ma takiej linii'"]

    def test_batch_over_the_limit_is_rejected_whole(self):
        filler = "- dyrektywa\n"
        base_text = "# User\n" + filler * ((4000 - 20) // len(filler))
        base = {"USER.md": base_text}
        outcome = apply_decisions(base,
                                  [Decision(op="add", target="USER.md", entry="nowa dyrektywa",
                                            source="cursor:11")],
                                  today="2026-09-17")
        assert outcome.contents["USER.md"] == base["USER.md"]
        assert outcome.applied == []
        assert "over limit" in outcome.rejected[0][1]

    def test_no_prior_entry_is_lost(self):
        base = {"MEMORY.md": "# Memory\n- jeden\n- dwa\n- trzy\n"}
        outcome = apply_decisions(base,
                                  [Decision(op="add", target="MEMORY.md", entry="cztery",
                                            source="cursor:12")],
                                  today="2026-09-17")
        for line in ("- jeden", "- dwa", "- trzy"):
            assert line in outcome.contents["MEMORY.md"]

    def test_fallback_entries_are_marked_unconsolidated(self):
        entries = fallback_entries([Candidate(cursor=3, content="fakt", timestamp="2026-09-17 10:00",
                                              score=0.8, signals={})])
        assert entries and "unconsolidated" in entries[0] and "source: cursor:3" in entries[0]

    def test_drop_removes_one_entry_with_its_provenance(self):
        base = {"MEMORY.md": ("# Memory\n"
                              "- wpis do usuniecia\n"
                              "  <!-- observed: 2026-01-01 | source: x | importance: 2 -->\n"
                              + "".join(f"- wpis {n}\n" for n in range(5)))}
        outcome = apply_decisions(base,
                                  [Decision(op="drop", target="MEMORY.md", match="do usuniecia",
                                            reason="nieaktualne")],
                                  today="2026-09-17")
        assert "do usuniecia" not in outcome.contents["MEMORY.md"]
        assert "- wpis 4" in outcome.contents["MEMORY.md"]
        assert outcome.applied == ["drop MEMORY.md (nieaktualne)"]

    def test_dropping_more_than_a_quarter_is_rejected(self):
        base = {"MEMORY.md": "# Memory\n" + "".join(f"- wpis {n}\n" for n in range(8))}
        decisions = [Decision(op="drop", target="MEMORY.md", match=f"wpis {n}") for n in range(4)]
        outcome = apply_decisions(base, decisions, today="2026-09-17")
        assert outcome.contents["MEMORY.md"] == base["MEMORY.md"]
        assert "prior entry loss over limit" in outcome.rejected[-1][1]

    def test_render_entry_carries_trigger_and_suffix(self):
        rendered = render_entry("fakt", source="cursor:1", observed="2026-09-17", importance=5,
                                trigger="deploy, gateway", suffix="unconsolidated")
        assert "trigger: deploy, gateway" in rendered and "unconsolidated" in rendered


class TestGatedDreamRun:
    def test_legacy_mode_is_untouched(self, store):
        assert store.dream_mode == "legacy"
        assert store.apply_dream_result(SimpleNamespace(content="{}"), 1) is None

    def test_gated_prompt_offers_candidates_and_the_contract(self, store):
        store.dream_mode = "gated"
        store.append_history(FACT, session_key="telegram:1")
        store.append_history(FACT + " korekta", session_key="telegram:1")
        result = store.build_dream_prompt()
        assert result is not None
        prompt, cursor = result
        assert "You are running Dream in gated mode" in prompt
        assert "## Promotion gate" in prompt and "[cursor " in prompt
        assert cursor > 0

    def test_gated_prompt_is_none_when_nothing_passes_the_gate(self, store):
        store.dream_mode = "gated"
        store.append_history("krotki wpis", session_key="telegram:1")
        assert store.build_dream_prompt() is None

    def test_decisions_are_written_with_preimage_and_journal(self, store):
        store.dream_mode = "gated"
        store.append_history(FACT, session_key="telegram:1")
        assert store.build_dream_prompt() is not None
        before = store.read_memory()
        answer = json.dumps({"decisions": [
            {"op": "add", "target": "MEMORY.md", "section": "Infrastruktura",
             "entry": "Brama wymaga sekcji personal w config.json.", "source": "cursor:1",
             "observed": "2026-09-15", "importance": 9},
            {"op": "supersede", "target": "MEMORY.md", "match": "stara rewizja",
             "entry": "Aktualna rewizja to 5fe2b07e.", "source": "cursor:1", "importance": 6},
        ]})
        report = store.apply_dream_result(SimpleNamespace(content=answer), 1)
        assert report is not None and report["fallback"] is False
        assert sorted(report["written"]) == ["MEMORY.md"]
        memory = store.read_memory()
        assert "Brama wymaga sekcji personal" in memory and "Aktualna rewizja to 5fe2b07e." in memory
        assert "source: cursor:1" in memory and "<!-- superseded:" in memory
        pre_images = list((store.memory_dir / "pre-image").glob("MEMORY.md.*"))
        assert len(pre_images) == 1 and pre_images[0].read_text(encoding="utf-8") == before
        dreams = (store.memory_dir / "DREAMS.md").read_text(encoding="utf-8")
        assert "decisions: 2 applied" in dreams and "files written: MEMORY.md" in dreams

    def test_unparsable_answer_falls_back_to_append_only(self, store):
        store.dream_mode = "gated"
        store.append_history(FACT, session_key="telegram:1")
        assert store.build_dream_prompt() is not None
        before = store.read_memory()
        report = store.apply_dream_result(SimpleNamespace(content="nie umiem, sorry"), 1)
        assert report is not None and report["fallback"] is True
        memory = store.read_memory()
        assert memory.startswith(before.rstrip())
        assert "unconsolidated" in memory and "source: cursor:1" in memory
        assert "fallback append-only" in (store.memory_dir / "DREAMS.md").read_text(encoding="utf-8")

    def test_concurrent_edit_falls_back_instead_of_overwriting(self, store):
        store.dream_mode = "gated"
        store.append_history(FACT, session_key="telegram:1")
        assert store.build_dream_prompt() is not None
        store.write_memory(store.read_memory() + "\n- recznie dopisany fakt\n")
        answer = json.dumps({"decisions": [
            {"op": "add", "target": "MEMORY.md", "entry": "nowy fakt", "source": "cursor:1"}]})
        report = store.apply_dream_result(SimpleNamespace(content=answer), 1)
        assert report is not None and report["fallback"] is True
        assert report["reason"] == "durable files changed during the run"
        memory = store.read_memory()
        assert "recznie dopisany fakt" in memory
        assert "unconsolidated" in memory

    def test_gated_dream_tools_are_read_only(self, store):
        store.dream_mode = "gated"
        tools = store.build_dream_tools()
        assert tools.get("read_file") is not None
        assert tools.get("write_file") is None and tools.get("edit_file") is None
