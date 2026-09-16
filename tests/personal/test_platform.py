"""Durability and side-effect boundaries for the optional personal platform."""

import asyncio
import base64
import gzip
import json
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from nanobot.agent.hook import AgentRunHookContext, AgentTurnHookContext
from nanobot.agent.memory import MemoryArchiver, MemoryStore
from nanobot.agent.tools.context import RequestContext
from nanobot.personal import connectors, evolution
from nanobot.personal.config import Account, FolderRule, PersonalConfig
from nanobot.personal.inbox import list_inbox, project_mail
from nanobot.personal.mail_sort import move_archived_mail, quote_mailbox
from nanobot.personal.maintenance import development_cycle
from nanobot.personal.service import PersonalService
from nanobot.personal.store import PersonalStore
from nanobot.personal.vector import VectorMemory


@pytest.fixture
def store(tmp_path):
    return PersonalStore(tmp_path / "data", tmp_path / "workspace")


def account(identifier="one", kind="mailbox", **kwargs):
    return Account(id=identifier, label=identifier, kind=kind, username="agent@example.org",
                   imap_host="imap.example.org", smtp_host="smtp.example.org", **kwargs)


def mail(store, identifier="one", date="Mon, 14 Sep 2026 08:00:00 +0000", body="A message"):
    payload = {"account_id": identifier, "folder": "INBOX", "uidvalidity": "42", "uid": 1,
               "headers": {"Subject": "Meeting", "From": "sender@example.org", "Date": date}, "body": body}
    key = store.put("mail:" + identifier, date, payload)
    project_mail(store, key, payload)
    return key


def test_accounts_are_encrypted_and_secret_edits_preserve_password(tmp_path):
    service = PersonalService(PersonalConfig(data_dir=str(tmp_path / "data")), tmp_path)
    result = service.save_account(account(password="private-test-password").model_dump())
    assert result["has_password"] and "password" not in result
    assert b"private-test-password" not in service.store.path.read_bytes()
    service.save_account({**account().model_dump(), "password": ""})
    assert service.store.account("one").password.get_secret_value() == "private-test-password"
    assert service.store.path.stat().st_mode & 0o777 == 0o600
    assert service.store.key_path.stat().st_mode & 0o777 == 0o600


def test_defaults_require_explicit_organization():
    assert account(kind="agent").send_enabled is True
    assert account().send_enabled is False
    assert not account().organize_folders and not account().folder_rules
    with pytest.raises(ValidationError):
        account(organize_folders=True)
    with pytest.raises(ValidationError):
        account(kind="agent", from_address="malformed")


def test_snapshots_preserve_all_original_fields_and_cannot_be_deleted(store):
    messages = [{"role": "user", "content": [{"type": "image", "data": "original-bytes"}]},
                {"role": "assistant", "tool_calls": [{"id": "call-1", "arguments": "original"}]},
                {"role": "tool", "tool_call_id": "call-1", "content": "x" * 100_000}]
    receipt = store.archive_messages("chat", messages)
    assert receipt == store.archive_messages("chat", messages)
    with store.db() as db:
        row = db.execute("SELECT document_ids FROM snapshots WHERE id=?", (receipt,)).fetchone()
        ids = json.loads(gzip.decompress(row[0]))
        assert [store.get(i)["payload"] for i in ids] == messages
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("DELETE FROM documents")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("DELETE FROM snapshots")
    assert store.status()["documents"] == 3


def test_snapshot_failure_rolls_back_all_partial_records(store):
    with pytest.raises(TypeError):
        store.archive_messages("chat", [{"content": "good"}, {"content": object()}])
    assert store.status()["documents"] == store.status()["snapshots"] == 0


def test_namespace_isolation(store, tmp_path):
    identifier = store.put("session", "key", {"text": "needle"})
    other = PersonalStore(store.directory, tmp_path / "different-workspace")
    assert other.search("needle") == []
    with pytest.raises(ValueError):
        other.get(identifier)
    store.save_account(account())
    with pytest.raises(ValueError):
        other.save_account(account())


async def test_compaction_stops_before_summary_if_raw_archive_fails(tmp_path):
    legacy = MemoryStore(tmp_path)
    legacy.archive_sink = MagicMock(side_effect=OSError("disk full"))
    archiver = MemoryArchiver(legacy, lambda **kwargs: [], lambda: [])
    runtime = MagicMock()
    with pytest.raises(OSError, match="disk full"):
        await archiver.archive([{"role": "user", "content": "keep this"}], runtime=runtime,
                               session_key="chat", history=[], request_tools=[], input_token_budget=0)
    assert legacy.read_unprocessed_history(0) == []
    assert not runtime.mock_calls


async def test_private_and_ephemeral_sessions_do_not_enter_archive(tmp_path):
    sessions = MagicMock()
    sessions.get_cached.return_value = SimpleNamespace(policy=SimpleNamespace(persist=False, log_content=False))
    service = PersonalService(PersonalConfig(data_dir=str(tmp_path / "data")), tmp_path, sessions)
    messages = [{"role": "user", "content": "private"}]
    assert service.archive("private", messages, "compact") is None
    await service.hook(AgentTurnHookContext(session_key="private")).after_run(AgentRunHookContext(messages))
    await service.hook(AgentTurnHookContext(session_key="temp", ephemeral=True)).after_run(AgentRunHookContext(messages))
    assert service.store.status()["documents"] == 0


def test_unified_inbox_merges_selected_accounts_but_not_distinct_dates(store):
    for identifier in ("one", "two", "three"):
        store.save_account(account(identifier, include_inbox=identifier != "three"))
        mail(store, identifier)
    mail(store, "one", "Tue, 15 Sep 2026 08:00:00 +0000")
    page = list_inbox(store, [], "", 0, 20)
    assert page["total"] == 2
    assert len(page["messages"][1]["copies"]) == 2
    assert list_inbox(store, ["three"], "", 0, 20)["total"] == 0
    assert list_inbox(store, ["two"], "", 0, 20)["total"] == 1


def fake_imap(raw):
    client = MagicMock()
    client.__enter__.return_value = client
    client.select.return_value = ("OK", [b"1"])
    client.response.return_value = ("UIDVALIDITY", [b"42"])
    def uid(command, *args):
        if command == "SEARCH":
            return "OK", [b"1"]
        if args[-1] == "(RFC822.SIZE)":
            return "OK", [b"1 (RFC822.SIZE " + str(len(raw)).encode() + b")"]
        return "OK", [(b"1 (BODY[])", raw)]
    client.uid.side_effect = uid
    return client


def test_mail_ingestion_archives_raw_before_advancing_and_does_not_modify_mailbox(store, monkeypatch):
    a = account()
    store.save_account(a)
    raw = b"From: sender@example.org\r\nSubject: invoice\r\n\r\nbody\r\n"
    client = fake_imap(raw)
    monkeypatch.setattr(connectors, "_imap", lambda _: client)
    assert connectors.mailbox_sync(store, a, 50) == 1
    client.select.assert_called_once_with('"INBOX"', readonly=True)
    assert {c.args[0] for c in client.uid.call_args_list} == {"FETCH", "SEARCH"}
    assert store.checkpoint("imap:one:INBOX:42") == "1"
    document = store.get(store.pending()[0]["id"])
    assert base64.b64decode(document["payload"]["raw_rfc822_b64"]) == raw
    assert connectors.mailbox_sync(store, a, 50) == 0


def test_mail_failure_retains_uid_cursor(store, monkeypatch):
    a = account(max_message_bytes=1024)
    store.save_account(a)
    client = fake_imap(b"x" * 2048)
    monkeypatch.setattr(connectors, "_imap", lambda _: client)
    with pytest.raises(ValueError, match="size limit"):
        connectors.mailbox_sync(store, a, 10)
    assert store.checkpoint("imap:one:INBOX:42", "0") == "0"
    assert store.status()["documents"] == 0


def test_missing_search_response_means_no_new_mail(store, monkeypatch):
    # iCloud omits the untagged SEARCH line when the range past the cursor
    # matches nothing; imaplib reports [None] and the sync must stay idle.
    a = account()
    store.save_account(a)
    client = fake_imap(b"From: sender@example.org\r\nSubject: x\r\n\r\nbody\r\n")
    def uid(command, *args):
        if command == "SEARCH":
            return "OK", [None]
        raise AssertionError("FETCH must not run without search results")
    client.uid.side_effect = uid
    monkeypatch.setattr(connectors, "_imap", lambda _: client)
    assert connectors.mailbox_sync(store, a, 50) == 0
    assert store.checkpoint("imap:one:INBOX:42", "0") == "0"
    assert store.status()["documents"] == 0


def test_folder_move_requires_archive_and_explicit_rule_without_parent_group(store):
    rule = FolderRule(id="meeting", label="Meeting", folder="Spotkania", contains=["meeting"])
    a = account(organize_folders=True, folder_rules=[rule])
    store.save_account(a)
    key = mail(store)
    client = MagicMock(capabilities=(b"MOVE",))
    client.list.return_value = ("OK", [b'() "/" "Spotkania"'])
    client.uid.return_value = ("OK", [])
    move_archived_mail(store, a, client, key, "INBOX", "42", 1)
    client.uid.assert_called_once_with("MOVE", "1", '"Spotkania"')
    client.create.assert_not_called()
    move_archived_mail(store, a, client, key, "INBOX", "42", 1)
    assert client.uid.call_count == 1
    with pytest.raises(ValueError):
        move_archived_mail(store, a, client, "missing", "INBOX", "42", 1)


def test_no_organization_rule_means_no_remote_mutation(store):
    a = account()
    store.save_account(a)
    key = mail(store)
    client = MagicMock()
    move_archived_mail(store, a, client, key, "INBOX", "42", 1)
    assert not client.mock_calls


def test_polish_imap_folder_names_use_modified_utf7():
    assert quote_mailbox("Zażółć & test").isascii()
    assert "&-" in quote_mailbox("Zażółć & test")


def test_send_records_before_smtp_and_deduplicates_retries(store, monkeypatch):
    a = account(kind="agent")
    smtp = MagicMock()
    smtp.__enter__.return_value = smtp
    def deliver(_):
        assert store.search("outgoing body")
        return {}
    smtp.send_message.side_effect = deliver
    monkeypatch.setattr(connectors, "_smtp", lambda _: smtp)
    args = (store, a, "operation-1", ["target@example.org"], "subject", "outgoing body")
    assert not connectors.send_mail(*args)["replayed"]
    assert connectors.send_mail(*args)["replayed"]
    assert smtp.send_message.call_count == 1
    with pytest.raises(ValueError, match="another message"):
        connectors.send_mail(store, a, "operation-1", ["target@example.org"], "changed", "body")


def test_uncertain_delivery_is_not_retried(store, monkeypatch):
    smtp = MagicMock()
    smtp.__enter__.return_value = smtp
    smtp.send_message.side_effect = OSError("lost acknowledgement")
    monkeypatch.setattr(connectors, "_smtp", lambda _: smtp)
    args = (store, account(kind="agent"), "operation-2", ["target@example.org"], "subject", "body")
    with pytest.raises(OSError):
        connectors.send_mail(*args)
    with pytest.raises(ValueError, match="uncertain"):
        connectors.send_mail(*args)
    assert smtp.send_message.call_count == 1


def test_failed_preparation_never_reserves_or_sends(store, monkeypatch):
    monkeypatch.setattr(store, "_put", MagicMock(side_effect=OSError("disk full")))
    smtp = MagicMock()
    monkeypatch.setattr(connectors, "_smtp", smtp)
    with pytest.raises(OSError):
        connectors.send_mail(store, account(kind="agent"), "operation-3", ["target@example.org"], "subject", "body")
    assert not smtp.mock_calls
    with store.db() as db:
        assert db.execute("SELECT count(*) FROM operations").fetchone()[0] == 0


def test_dav_does_not_forward_credentials_to_another_origin(monkeypatch):
    monkeypatch.setattr(connectors, "checked_target", lambda _: ("203.0.113.1",))
    client = connectors.DAVClient(account(kind="apple"), "https://caldav.icloud.com/")
    assert client.target(client.base_url, "https://p01-caldav.icloud.com/home").endswith("/home")
    with pytest.raises(ValueError, match="origin boundary"):
        client.target(client.base_url, "https://icloud.com.attacker.example/home")
    with pytest.raises(ValueError, match="origin boundary"):
        client.target(client.base_url, "https://caldav.icloud.com:8443/home")


def test_network_policy_blocks_private_mail_endpoint():
    with pytest.raises(ValueError, match="blocked"):
        connectors.checked_target("https://127.0.0.1:993")


def test_failed_remote_commit_retains_outbox(store, monkeypatch, tmp_path):
    store.put("session", "one", {"text": "keep queued"})
    vector = VectorMemory(store, tmp_path / "unused", "test-model")
    monkeypatch.setattr(vector, "initialize", lambda: None)
    monkeypatch.setattr(vector, "embed", lambda values: [[0.0] * 384 for _ in values])
    remote = MagicMock()
    remote.__exit__.side_effect = OSError("remote commit failed")
    monkeypatch.setattr(vector, "connect", lambda: remote)
    with pytest.raises(OSError):
        vector.sync()
    assert len(store.pending()) == 1


def test_evolution_rejects_holdout_regression_then_promotes_and_rolls_back(tmp_path, monkeypatch):
    service = PersonalService(PersonalConfig(data_dir=str(tmp_path / "data"), evolution_min_samples=6), tmp_path)
    service.vector = MagicMock()
    service.store.set_checkpoint("remote_state", "ready")
    monkeypatch.setattr(evolution, "examples", lambda _: [(str(i), str(i)) for i in range(6)])
    def metric(_, samples, policy):
        if policy == "hybrid":
            return 0.5
        return 0.9 if samples[0][0] == "0" else 0.2
    monkeypatch.setattr(evolution, "evaluate", metric)
    assert evolution.evolve(service)["promoted"] is False
    service.store.set_checkpoint("evolution_dataset", "")
    monkeypatch.setattr(evolution, "evaluate", lambda _, samples, policy: 0.5 if policy == "hybrid" else 0.9)
    assert evolution.evolve(service)["promoted"] is True
    assert service.store.checkpoint("retrieval_policy") == "lexical"
    assert evolution.rollback(service.store)["policy"] == "hybrid"


def test_native_memory_is_augmented_and_keeps_earlier_versions(tmp_path):
    service = PersonalService(PersonalConfig(data_dir=str(tmp_path / "data")), tmp_path)
    original = tmp_path / "SOUL.md"
    original.write_text("Earlier identity")
    service.sync_workspace_memory()
    original.write_text("Current identity")
    service.sync_workspace_memory()
    assert service.store.search("Earlier identity")
    assert original.read_text() == "Current identity"
    assert service.store.status()["documents"] == 2


async def test_autonomous_development_uses_existing_agent_and_records_result(tmp_path):
    service = PersonalService(PersonalConfig(data_dir=str(tmp_path / "data")), tmp_path)
    async def completed(*args, **kwargs):
        await kwargs["hooks"][0].after_run(AgentRunHookContext([], stop_reason="completed"))
        return SimpleNamespace(content="Verified one improvement")
    agent = SimpleNamespace(workspace=tmp_path, process_direct=AsyncMock(side_effect=completed))
    await development_cycle(service, agent)
    assert agent.process_direct.call_args.kwargs["channel"] == "cli"
    assert "do not manufacture tasks" in agent.process_direct.call_args.args[0]
    assert service.store.checkpoint("development_state") == "completed"
    assert service.store.status()["evolution"][0]["detail"]["summary"] == "Verified one improvement"


async def test_development_failure_is_recorded_without_secret_exception_text(tmp_path):
    service = PersonalService(PersonalConfig(data_dir=str(tmp_path / "data")), tmp_path)
    agent = SimpleNamespace(workspace=tmp_path, process_direct=AsyncMock(side_effect=ValueError("secret-provider-detail")))
    await development_cycle(service, agent)
    assert service.store.checkpoint("development_state") == "error:ValueError"
    assert "secret-provider-detail" not in json.dumps(service.store.status())


async def test_nonempty_provider_error_is_not_a_successful_development_cycle(tmp_path):
    service = PersonalService(PersonalConfig(data_dir=str(tmp_path / "data")), tmp_path)
    async def failed(*args, **kwargs):
        await kwargs["hooks"][0].after_run(AgentRunHookContext([], stop_reason="error"))
        return SimpleNamespace(content="Provider credentials require renewal")
    agent = SimpleNamespace(workspace=tmp_path, process_direct=AsyncMock(side_effect=failed))
    await development_cycle(service, agent)
    assert service.store.checkpoint("development_state") == "error:RuntimeError"
    assert service.store.status()["evolution"][0]["status"] == "development_failed"


def test_readable_excerpt_condenses_markup_noise_without_touching_the_archive(store):
    from nanobot.personal.store import readable_excerpt
    junk = "<p>\r\n\t  &nbsp;   &amp; </p>\r\n  <b>witaj</b>  \u00a0  ponownie \r\n"
    assert readable_excerpt(junk, 600) == "<p> & </p> <b>witaj</b> ponownie"
    assert readable_excerpt("x" * 2000, 10) == "x" * 10
    assert readable_excerpt("\r\n\t &nbsp; \r\n", 600) == ""
    stored = store.put("mail:one", "INBOX:1:9", {"body": junk})
    assert store.get(stored)["payload"]["body"] == junk


def test_search_excerpts_are_condensed_projections(store, tmp_path):
    from nanobot.personal.service import EXCERPT_LIMIT
    service = PersonalService(PersonalConfig(data_dir=str(tmp_path / "data")), tmp_path)
    body = "raport " * 400
    service.store.put("mail:one", "INBOX:1:1", {"headers": {"Subject": "Raport"}, "body": body})
    results = service.search("raport", 4)
    assert results and len(results[0]["excerpt"]) <= EXCERPT_LIMIT
    assert "  " not in results[0]["excerpt"]


async def test_runtime_context_drops_markup_noise_and_bounds_output(tmp_path):
    service = PersonalService(PersonalConfig(data_dir=str(tmp_path / "data")), tmp_path)
    service.store.put("mail:one", "INBOX:1:2", {
        "headers": {"Subject": "Newsletter"}, "body": "\r\n\t &nbsp;  &nbsp; \r\n   \t"})
    service.store.put("mail:one", "INBOX:1:3", {
        "headers": {"Subject": "Raport"}, "body": "Realna treść raportu " + "słowo " * 200})
    request = RequestContext(channel="telegram", chat_id="1",
                             original_user_text="newsletter raport podsumowanie")
    block = await service.runtime_context(request)
    assert block is not None
    assert "Realna treść" in block.content
    assert "&nbsp;" not in block.content
    # A record whose whole projection condenses below the usable minimum is dropped.
    service.store.put("mail:one", "INBOX:1:4", {
        "headers": {"Subject": "x"}, "body": "\r\n\t &nbsp; \r\n"})
    empty_match = RequestContext(channel="telegram", chat_id="1",
                                 original_user_text="x podsumowanie tygodnia")
    assert await service.runtime_context(empty_match) is None


async def test_runtime_context_never_gates_a_turn_on_slow_or_broken_retrieval(tmp_path, monkeypatch):
    service = PersonalService(
        PersonalConfig(data_dir=str(tmp_path / "data"), retrieval_timeout_seconds=0.5), tmp_path)
    request = RequestContext(channel="telegram", chat_id="1",
                             original_user_text="jakoś to będzie dłuższe osiem znaków")
    def slow(*args, **kwargs):
        time.sleep(2)
        return []
    monkeypatch.setattr(service, "search", slow)
    started = time.perf_counter()
    assert await service.runtime_context(request) is None
    assert time.perf_counter() - started < 1.5
    def broken(*args, **kwargs):
        raise RuntimeError("database unavailable")
    monkeypatch.setattr(service, "search", broken)
    assert await service.runtime_context(request) is None


def test_retrieval_timeout_is_configurable_and_bounded():
    assert PersonalConfig(data_dir="x").retrieval_timeout_seconds == 2.0
    with pytest.raises(ValidationError):
        PersonalConfig(data_dir="x", retrieval_timeout_seconds=0.1)


def test_excerpt_similarity_flags_near_duplicates_only():
    from nanobot.personal.service import excerpt_similarity
    same = "Regulamin świadczenia usług " + "postanowienie " * 40
    assert excerpt_similarity(same, same.upper()) == 1.0
    assert excerpt_similarity(same, same.replace("postanowienie", "warunek", 1)) > 0.7
    assert excerpt_similarity(same, "Faktura za prąd numer 123/2026") < 0.2
    assert excerpt_similarity("", "") == 1.0
    assert excerpt_similarity("ab", same) == 0.0


def test_duplicate_collapsing_is_configurable_and_disabled_outside_its_range():
    from nanobot.personal.service import collapse_duplicate_excerpts
    same = "Regulamin świadczenia usług " + "postanowienie " * 40
    distinct = "Faktura za prąd numer 123/2026, kwota 250 zł"
    items = [{"id": "a", "excerpt": same}, {"id": "b", "excerpt": same},
             {"id": "c", "excerpt": same}, {"id": "d", "excerpt": distinct}]
    assert [item["id"] for item in collapse_duplicate_excerpts(items, 0.7)] == ["a", "d"]
    # 1.0 keeps exact duplicates (filter disabled); 0.0 is the degenerate case and also disabled.
    assert len(collapse_duplicate_excerpts(items, 1.0)) == 4
    assert len(collapse_duplicate_excerpts(items, 0.0)) == 4
    assert PersonalConfig(data_dir="x").retrieval_dedup_threshold == 0.7
    with pytest.raises(ValidationError):
        PersonalConfig(data_dir="x", retrieval_dedup_threshold=2.0)


async def test_runtime_context_keeps_one_copy_of_repeated_excerpts(tmp_path):
    service = PersonalService(PersonalConfig(data_dir=str(tmp_path / "data")), tmp_path)
    repeated = "Regulamin świadczenia usług " + "postanowienie " * 40
    for index in range(4):
        service.store.put("mail:one", f"INBOX:1:{index}", {
            "headers": {"Subject": "Regulamin"}, "body": repeated})
    service.store.put("mail:one", "INBOX:1:9", {
        "headers": {"Subject": "Faktura"},
        "body": "Faktura za prąd numer 123/2026, kwota 250 zł, termin płatności 14 dni"})
    request = RequestContext(channel="telegram", chat_id="1",
                             original_user_text="regulamin faktura postanowienie")
    block = await service.runtime_context(request)
    assert block is not None
    # Four near-identical records reach the prompt as one, and the distinct record survives.
    assert block.content.count("Regulamin świadczenia usług") == 1
    assert block.content.count("Faktura za prąd numer 123/2026") == 1


async def test_runtime_context_dedup_can_be_disabled_by_config(tmp_path):
    service = PersonalService(
        PersonalConfig(data_dir=str(tmp_path / "data"), retrieval_dedup_threshold=1.0), tmp_path)
    repeated = "Regulamin świadczenia usług " + "postanowienie " * 40
    for index in range(3):
        service.store.put("mail:one", f"INBOX:1:{index}", {
            "headers": {"Subject": "Regulamin"}, "body": repeated})
    request = RequestContext(channel="telegram", chat_id="1",
                             original_user_text="regulamin postanowienie")
    block = await service.runtime_context(request)
    assert block is not None
    assert block.content.count("Regulamin świadczenia usług") == 3


def test_memory_warmup_loads_the_model_and_remote_schema_before_the_first_turn(tmp_path):
    service = PersonalService(PersonalConfig(data_dir=str(tmp_path / "data")), tmp_path)
    calls: list[object] = []
    vector = MagicMock()
    vector.initialize = lambda: calls.append("initialize")
    vector.embed = lambda values: calls.append(("embed", values)) or [[0.0] * 384]
    service.vector = vector
    asyncio.run(service._warm_memory_once())
    assert calls == ["initialize", ("embed", ["warmup"])]
    assert service.warm_memory() is True
    assert service.store.checkpoint("remote_state", "pending") == "pending"  # warmup is not a sync


def test_memory_warmup_failure_never_gates_startup(tmp_path):
    service = PersonalService(PersonalConfig(data_dir=str(tmp_path / "data")), tmp_path)
    vector = MagicMock()
    vector.initialize = MagicMock(side_effect=OSError("remote unavailable"))
    service.vector = vector
    asyncio.run(service._warm_memory_once())
    assert vector.initialize.called

    without_remote = PersonalService(PersonalConfig(data_dir=str(tmp_path / "other")), tmp_path)
    asyncio.run(without_remote._warm_memory_once())
    assert without_remote.warm_memory() is False
