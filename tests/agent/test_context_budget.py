"""Static context budget: measurement, refusal to grow, and the bounded prompt copy."""

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.context_budget import (
    BUDGET_ENV,
    MEMORY_LIMIT_CHARS,
    STATIC_CONTEXT_BUDGET_BYTES,
    USER_LIMIT_CHARS,
    StaticContext,
    budget_enforced,
    measure,
    truncate_copy,
)
from nanobot.agent.memory import MemoryStore


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path)


class TestMeasure:
    def test_counts_the_four_injected_files(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("a" * 100, encoding="utf-8")
        (tmp_path / "SOUL.md").write_text("s" * 100, encoding="utf-8")
        (tmp_path / "USER.md").write_text("u" * 100, encoding="utf-8")
        (tmp_path / "memory").mkdir()
        (tmp_path / "memory" / "MEMORY.md").write_text("m" * 100, encoding="utf-8")
        report = measure(tmp_path)
        assert [usage.path for usage in report.files] == [
            "AGENTS.md", "SOUL.md", "USER.md", "memory/MEMORY.md"]
        assert report.total_bytes == 400
        assert not report.over_budget
        assert report.violations() == []

    def test_missing_files_are_not_counted(self, tmp_path):
        assert measure(tmp_path).files == ()
        assert measure(tmp_path).total_bytes == 0

    def test_reports_both_file_and_pool_violations(self, tmp_path):
        (tmp_path / "memory").mkdir()
        (tmp_path / "memory" / "MEMORY.md").write_text("m" * (MEMORY_LIMIT_CHARS + 10),
                                                       encoding="utf-8")
        (tmp_path / "AGENTS.md").write_text("a" * STATIC_CONTEXT_BUDGET_BYTES, encoding="utf-8")
        problems = measure(tmp_path).violations()
        assert any("MEMORY.md" in line and "limit" in line for line in problems)
        assert any("static context" in line and "budget" in line for line in problems)

    def test_report_type_is_stable(self):
        assert isinstance(measure(__import__("pathlib").Path("/nonexistent")), StaticContext)


class TestTruncateCopy:
    def test_short_text_is_untouched(self):
        assert truncate_copy("short", 100, "memory/MEMORY.md") == "short"

    def test_long_text_keeps_the_head_and_says_so(self):
        result = truncate_copy("x" * 500, 100, "memory/MEMORY.md")
        assert result.startswith("x" * 100)
        assert "memory/MEMORY.md is 500 characters" in result
        assert "memory/notes/<topic>.md" in result


class TestWriteBudget:
    def test_write_is_refused_when_it_grows_an_over_limit_file(self, store):
        store.memory_file.write_text("m" * (MEMORY_LIMIT_CHARS + 1), encoding="utf-8")
        with pytest.raises(ValueError) as error:
            store.write_memory("m" * (MEMORY_LIMIT_CHARS + 2))
        assert "memory/notes" in str(error.value)
        assert store.memory_file.read_text() == "m" * (MEMORY_LIMIT_CHARS + 1)

    def test_a_shrinking_write_is_allowed_while_still_over_the_limit(self, store):
        store.memory_file.write_text("m" * (MEMORY_LIMIT_CHARS + 500), encoding="utf-8")
        store.write_memory("m" * (MEMORY_LIMIT_CHARS + 100))
        assert len(store.memory_file.read_text()) == MEMORY_LIMIT_CHARS + 100

    def test_write_under_the_limit_is_untouched(self, store):
        store.write_memory("# Memory\n\n- one fact")
        assert store.read_memory() == "# Memory\n\n- one fact"

    def test_the_limit_applies_to_creating_the_file_too(self, store):
        with pytest.raises(ValueError):
            store.write_memory("x" * (MEMORY_LIMIT_CHARS + 1))
        assert not store.memory_file.exists()
        store.write_memory("x" * MEMORY_LIMIT_CHARS)
        assert len(store.read_memory()) == MEMORY_LIMIT_CHARS

    def test_user_profile_has_its_own_limit(self, store):
        with pytest.raises(ValueError):
            store.write_user("u" * (USER_LIMIT_CHARS + 1))
        store.write_user("u" * USER_LIMIT_CHARS)
        assert len(store.read_user()) == USER_LIMIT_CHARS

    def test_switch_disables_the_refusal(self, tmp_path, monkeypatch):
        monkeypatch.setenv(BUDGET_ENV, "0")
        assert budget_enforced() is False
        store = MemoryStore(tmp_path)
        store.memory_file.write_text("m" * (MEMORY_LIMIT_CHARS + 1), encoding="utf-8")
        store.write_memory("m" * (MEMORY_LIMIT_CHARS + 500))
        assert len(store.read_memory()) == MEMORY_LIMIT_CHARS + 500


class TestPromptCopy:
    def test_memory_and_profile_copies_are_bounded(self, tmp_path):
        (tmp_path / "memory").mkdir()
        (tmp_path / "memory" / "MEMORY.md").write_text(
            "# Memory\n" + "m" * (MEMORY_LIMIT_CHARS + 5_000), encoding="utf-8")
        (tmp_path / "USER.md").write_text("u" * (USER_LIMIT_CHARS + 1_000), encoding="utf-8")
        prompt = ContextBuilder(tmp_path).build_system_prompt()
        assert "memory/MEMORY.md is" in prompt
        assert "USER.md is" in prompt
        # The files on disk keep every character.
        assert len((tmp_path / "memory" / "MEMORY.md").read_text()) == MEMORY_LIMIT_CHARS + 5_009
        assert len((tmp_path / "USER.md").read_text()) == USER_LIMIT_CHARS + 1_000

    def test_a_file_inside_its_limit_is_injected_whole(self, tmp_path):
        (tmp_path / "memory").mkdir()
        (tmp_path / "memory" / "MEMORY.md").write_text("# Memory\n\n- short", encoding="utf-8")
        assert "- short" in ContextBuilder(tmp_path).build_system_prompt()

    def test_switch_disables_the_truncation(self, tmp_path, monkeypatch):
        monkeypatch.setenv(BUDGET_ENV, "0")
        (tmp_path / "memory").mkdir()
        body = "# Memory\n" + "m" * (MEMORY_LIMIT_CHARS + 10)
        (tmp_path / "memory" / "MEMORY.md").write_text(body, encoding="utf-8")
        prompt = ContextBuilder(tmp_path).build_system_prompt()
        assert "is" not in prompt.split("# Memory")[1][:20]
        assert body in prompt
