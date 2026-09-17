from nanobot.llm_usage.context import source_from_request, source_from_session_key


def test_automation_metadata_overrides_user_session_source() -> None:
    assert source_from_request(
        "websocket:ordinary-session",
        channel="websocket",
        metadata={"_cron_trigger": {"job_id": "job"}},
    ) == "cron"
    assert source_from_request(
        "websocket:ordinary-session",
        channel="websocket",
        metadata={"_local_trigger": {"trigger_id": "trigger"}},
    ) == "cron"


def test_api_and_system_channels_have_explicit_sources() -> None:
    assert source_from_request("shared-session", channel="api", metadata={}) == "api"
    assert source_from_request("shared-session", channel="system", metadata={}) == "system"


def test_worker_sessions_are_not_user_traffic() -> None:
    """A background worker turn must not get the interactive tool budget.

    The agent loop picks max_tool_iterations for a "user" source and
    max_tool_iterations_background for every other source, so this classification decides
    how many tool rounds an autonomous cycle may spend.
    """
    assert source_from_session_key("personal-development:namespace") == "system"
    assert source_from_session_key("personal-evolution:namespace") == "system"
    assert source_from_session_key("telegram:7365213421") == "user"
