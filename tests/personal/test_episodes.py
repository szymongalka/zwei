"""Task episodes: the memory unit that replaces transcripts in the curated layer."""

import asyncio
from datetime import datetime, timezone

from nanobot.agent.memory import MemoryArchiver, MemoryStore
from nanobot.personal.config import PersonalConfig
from nanobot.personal.episodes import (
    EPISODE_SOURCE_PREFIX,
    build_episode,
    episode_key,
    episode_projection,
)
from nanobot.personal.service import PersonalService

SUMMARY = """- [durable] Bramę trzeba podnosić po zmianie configu, inaczej crash-loop.
- [correction] Rewizja to 4a32498b, nie 9466ddc1.
- [ephemeral] Sprawdziłem log bramy.
Wdrożenie PR #27 na /root/zwei-git zakończone; testy: 1832 passed; polecenie `ruff check nanobot tests`.
"""

MESSAGES = [
    {"role": "user", "content": "Wdróż P0 na żywej bramie", "timestamp": "2026-09-17T18:00:00Z"},
    {"role": "assistant", "content": "Robię", "tools_used": ["exec", "read_file"],
     "timestamp": "2026-09-17T18:05:00Z"},
    {"role": "tool", "content": "ok", "timestamp": "2026-09-17T18:06:00Z"},
]


def episode(**overrides):
    payload = build_episode("telegram:7365213421", SUMMARY, MESSAGES,
                            now=datetime(2026, 9, 17, 18, 30, tzinfo=timezone.utc))
    assert payload is not None
    payload.update(overrides)
    return payload


class TestBuildEpisode:
    def test_background_sessions_have_no_episode(self):
        for key in ("heartbeat", "personal-development:x", "cron:daily", "subagent:z", "diagnostic"):
            assert build_episode(key, SUMMARY, MESSAGES) is None

    def test_records_what_outcome_decisions_and_evidence(self):
        payload = episode()
        assert payload["what"] == "Wdróż P0 na żywej bramie"
        assert payload["outcome"].startswith("- [durable] Bramę trzeba podnosić")
        assert payload["decisions"] == [
            "Bramę trzeba podnosić po zmianie configu, inaczej crash-loop.",
            "Rewizja to 4a32498b, nie 9466ddc1.",
        ]
        assert "PR #27" in payload["evidence"]
        assert "4a32498b" in payload["evidence"]
        assert "1832 passed" in payload["evidence"]
        assert "ruff check nanobot tests" in payload["evidence"]
        assert payload["turns"] == 3
        assert payload["tools"] == ["exec", "read_file"]
        assert payload["window"] == ["2026-09-17T18:00", "2026-09-17T18:06"]

    def test_hex_words_are_not_revisions(self):
        payload = build_episode("telegram:1", "Tekst o defaced i acceded bez commita", [])
        assert payload is not None and payload["evidence"] == []

    def test_bounds_the_record(self):
        payload = build_episode("telegram:1", "x" * 9000, [{"role": "user", "content": "y" * 900}])
        assert payload is not None
        assert len(payload["outcome"]) == 2000
        assert len(payload["what"]) == 300

    def test_command_like_opening_is_not_the_goal(self):
        payload = build_episode("telegram:1", "wynik", [
            {"role": "user", "content": "/new"},
            {"role": "user", "content": "Prawdziwe zlecenie"},
        ])
        assert payload is not None and payload["what"] == "Prawdziwe zlecenie"


class TestEpisodeIdentityAndProjection:
    def test_key_is_stable_for_the_same_outcome(self):
        assert episode_key(episode()) == episode_key(episode())
        assert episode_key(episode(outcome="inny wynik")) != episode_key(episode())

    def test_projection_carries_the_retrievable_fields(self):
        text = episode_projection(episode())
        assert text.startswith("episode telegram:7365213421")
        assert "what: Wdróż P0 na żywej bramie" in text
        assert "decisions: " in text and "evidence: " in text
        assert len(episode_projection(episode(outcome="x" * 9000))) <= 4000


class TestServiceWiring:
    def test_episode_lands_in_the_curated_layer(self, tmp_path):
        service = PersonalService(PersonalConfig(data_dir=str(tmp_path / "data")), tmp_path)
        identifier = service.record_episode("telegram:7365213421", SUMMARY, MESSAGES)
        assert identifier is not None
        record = service.store.get(identifier)
        assert record["source"] == EPISODE_SOURCE_PREFIX + "telegram:7365213421"
        assert record["visibility"] == "hot"
        hits = service.search("PR #27 wdrożenie bramy", 5)
        assert [item["source"] for item in hits] == [EPISODE_SOURCE_PREFIX + "telegram:7365213421"]

    def test_repeat_compaction_adds_nothing(self, tmp_path):
        service = PersonalService(PersonalConfig(data_dir=str(tmp_path / "data")), tmp_path)
        first = service.record_episode("telegram:1", SUMMARY, MESSAGES)
        assert first is not None
        # Re-compacting the same session content must not add a second copy.
        assert service.record_episode("telegram:1", SUMMARY, MESSAGES) is None
        assert service.store.status()["documents"] == 1

    def test_background_session_is_not_recorded(self, tmp_path):
        service = PersonalService(PersonalConfig(data_dir=str(tmp_path / "data")), tmp_path)
        assert service.record_episode("heartbeat", SUMMARY, MESSAGES) is None
        assert service.store.status()["documents"] == 0


class TestArchiverSink:
    def archiver(self, store):
        return MemoryArchiver(store, lambda **kwargs: [], lambda: [])

    def test_successful_compaction_calls_the_episode_sink(self, tmp_path):
        store = MemoryStore(tmp_path)
        seen: list[tuple[str, str, int]] = []
        store.episode_sink = lambda key, summary, messages: seen.append((key, summary, len(messages)))
        asyncio.run(self.archiver(store)._record_episode("telegram:1", "wynik", MESSAGES))
        assert seen == [("telegram:1", "wynik", 3)]

    def test_a_failing_sink_never_gates_the_archive(self, tmp_path):
        store = MemoryStore(tmp_path)

        def boom(key, summary, messages):
            raise RuntimeError("sink down")

        store.episode_sink = boom
        asyncio.run(self.archiver(store)._record_episode("telegram:1", "wynik", MESSAGES))

    def test_no_sink_is_not_an_error(self, tmp_path):
        store = MemoryStore(tmp_path)
        assert store.episode_sink is None
        asyncio.run(self.archiver(store)._record_episode("telegram:1", "wynik", MESSAGES))
