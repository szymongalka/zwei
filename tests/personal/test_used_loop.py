"""The `used` loop: did an injected record show up in the answer?"""

import json

import pytest

from nanobot.agent.tools.context import RequestContext
from nanobot.personal.config import PersonalConfig
from nanobot.personal.service import (
    USED_COVERAGE_THRESHOLD,
    PersonalService,
    answer_coverage,
    injection_signal,
)


@pytest.fixture
def service(tmp_path):
    (tmp_path / "memory").mkdir(exist_ok=True)
    return PersonalService(PersonalConfig(data_dir=str(tmp_path / "data")), tmp_path)


def journal(service):
    path = service.workspace / "memory" / "retrieval_used.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def seed_record(service, body: str):
    return service.store.put("mail:one", "INBOX:1:1", {"headers": {"Subject": "Faktura"}, "body": body})


class TestCoverage:
    def test_full_reuse_scores_one(self):
        text = "faktura za energie 250 zl termin platnosci czternascie dni"
        assert answer_coverage(text, text) == 1.0

    def test_unrelated_answer_scores_zero(self):
        assert answer_coverage("faktura za energie termin platnosci", "pogoda jutro sloneczna") == 0.0

    def test_short_words_are_not_evidence(self):
        assert answer_coverage("ok", "ok") == 0.0

    @pytest.mark.parametrize(("coverage", "expected"), [
        (0.0, "unused-strong"), (0.1, "unused"), (USED_COVERAGE_THRESHOLD, "used"), (0.9, "used"),
    ])
    def test_signal_thresholds(self, coverage, expected):
        assert injection_signal(coverage) == expected


class TestInjectionUse:
    async def test_injected_records_are_scored_against_the_answer(self, service):
        seed_record(service, "faktura za energie 250 zl termin platnosci czternascie dni")
        request = RequestContext(channel="telegram", chat_id="1", session_key="telegram:1",
                                 original_user_text="jaki jest termin platnosci faktury za energie")
        block = await service.runtime_context(request)
        assert block is not None
        assert "telegram:1" in service._pending_injections

        service._record_injection_use("telegram:1", "Termin platnosci faktury za energie to czternascie dni")

        lines = journal(service)
        assert len(lines) == 1
        assert lines[0]["used"] == 1
        assert lines[0]["records"][0]["signal"] == "used"
        assert lines[0]["records"][0]["source"] == "mail:one"
        assert "telegram:1" not in service._pending_injections

    async def test_ignored_injection_is_recorded_as_strong_unused(self, service):
        seed_record(service, "faktura za energie 250 zl termin platnosci czternascie dni")
        request = RequestContext(channel="telegram", chat_id="1", session_key="telegram:1",
                                 original_user_text="jaki jest termin platnosci faktury za energie")
        assert await service.runtime_context(request) is not None

        service._record_injection_use("telegram:1", "Nie mam teraz dostepu do tej informacji.")

        lines = journal(service)
        assert lines[0]["records"][0]["signal"] == "unused-strong"
        assert lines[0]["used"] == 0

    def test_answer_without_injection_writes_nothing(self, service):
        service._record_injection_use("telegram:1", "cokolwiek")
        assert journal(service) == []

    async def test_switch_disables_the_loop(self, tmp_path):
        (tmp_path / "memory").mkdir(exist_ok=True)
        service = PersonalService(
            PersonalConfig(data_dir=str(tmp_path / "data"), retrieval_used_tracking=False), tmp_path)
        seed_record(service, "faktura za energie 250 zl termin platnosci czternascie dni")
        request = RequestContext(channel="telegram", chat_id="1", session_key="telegram:1",
                                 original_user_text="jaki jest termin platnosci faktury za energie")
        assert await service.runtime_context(request) is not None
        assert service._pending_injections == {}
        service._record_injection_use("telegram:1", "cokolwiek")
        assert journal(service) == []

    async def test_pending_injections_are_bounded(self, service):
        seed_record(service, "faktura za energie 250 zl termin platnosci czternascie dni")
        for index in range(40):
            request = RequestContext(channel="telegram", chat_id="1", session_key=f"telegram:{index}",
                                     original_user_text="jaki jest termin platnosci faktury za energie")
            assert await service.runtime_context(request) is not None
        assert len(service._pending_injections) <= 32
