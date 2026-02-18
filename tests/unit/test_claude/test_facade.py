"""Tests for ClaudeIntegration fallback behavior."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.claude.exceptions import ClaudeParsingError, ClaudeProcessError
from src.claude.facade import ClaudeIntegration
from src.claude.integration import ClaudeResponse
from src.config.settings import Settings


@pytest.fixture
def config(tmp_path):
    """Create test settings for facade tests."""
    return Settings(
        telegram_bot_token="test:token",
        telegram_bot_username="testbot",
        approved_directory=tmp_path,
        use_sdk=True,
    )


@pytest.fixture
def integration(config):
    """Create ClaudeIntegration with mocked managers."""
    sdk_manager = MagicMock()
    sdk_manager.execute_command = AsyncMock()

    process_manager = MagicMock()
    process_manager.execute_command = AsyncMock()

    return ClaudeIntegration(
        config=config,
        sdk_manager=sdk_manager,
        process_manager=process_manager,
        session_manager=MagicMock(),
        tool_monitor=MagicMock(),
    )


def _make_response(session_id: str = "proc-session") -> ClaudeResponse:
    """Create a subprocess response fixture value."""
    return ClaudeResponse(
        content="ok",
        session_id=session_id,
        cost=0.0,
        duration_ms=1,
        num_turns=1,
    )


async def test_fallback_on_unknown_message_type(integration, tmp_path):
    """Unknown SDK stream message types should fallback to subprocess."""
    integration.sdk_manager.execute_command.side_effect = ClaudeProcessError(
        "Claude SDK error: Unknown message type: rate_limit_event"
    )
    integration.process_manager.execute_command.return_value = _make_response()

    response = await integration._execute_with_fallback(
        prompt="hi",
        working_directory=Path(tmp_path),
        session_id="sdk-session",
        continue_session=True,
    )

    assert response.session_id == "proc-session"
    assert integration._sdk_failed_count == 1
    integration.process_manager.execute_command.assert_awaited_once()


async def test_fallback_on_parsing_error_type(integration, tmp_path):
    """ClaudeParsingError should always trigger subprocess fallback."""
    integration.sdk_manager.execute_command.side_effect = ClaudeParsingError(
        "Failed to parse Claude SDK stream: Unknown message type: rate_limit_event"
    )
    integration.process_manager.execute_command.return_value = _make_response(
        session_id="fallback-session"
    )

    response = await integration._execute_with_fallback(
        prompt="hi",
        working_directory=Path(tmp_path),
    )

    assert response.session_id == "fallback-session"
    integration.process_manager.execute_command.assert_awaited_once()


async def test_non_fallback_error_is_raised(integration, tmp_path):
    """Non-parsing SDK errors should be raised without fallback."""
    integration.sdk_manager.execute_command.side_effect = ClaudeProcessError(
        "Claude SDK error: backend unavailable"
    )

    with pytest.raises(ClaudeProcessError):
        await integration._execute_with_fallback(
            prompt="hi",
            working_directory=Path(tmp_path),
        )

    integration.process_manager.execute_command.assert_not_awaited()
