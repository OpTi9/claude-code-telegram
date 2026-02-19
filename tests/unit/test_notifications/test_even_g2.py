"""Tests for the Even G2 notification bridge service."""

from unittest.mock import AsyncMock, MagicMock, patch

from src.events.types import AgentResponseEvent
from src.notifications.even_g2 import EvenG2NotificationService


class TestEvenG2NotificationService:
    """Tests for EvenG2NotificationService."""

    async def test_forwards_even_g2_response(self) -> None:
        """even-g2 responses are posted to the local bridge endpoint."""
        service = EvenG2NotificationService(
            event_bus=AsyncMock(),
            g2_url="http://localhost:5173",
            bridge_secret="bridge-secret",
        )

        mock_client = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_client
        mock_ctx.__aexit__.return_value = False

        with patch(
            "src.notifications.even_g2.httpx.AsyncClient",
            return_value=mock_ctx,
        ):
            await service.handle_response(
                AgentResponseEvent(
                    provider="even-g2",
                    session_id="sess-1",
                    text="<b>Done</b> &amp; ready",
                )
            )

        mock_client.post.assert_called_once()
        call = mock_client.post.call_args
        assert call.args[0] == "http://localhost:5173/__g2_receive"
        assert call.kwargs["headers"]["Authorization"] == "Bearer bridge-secret"
        assert call.kwargs["json"]["sessionId"] == "sess-1"
        assert call.kwargs["json"]["text"] == "Done & ready"

    async def test_ignores_non_even_g2_provider(self) -> None:
        """Events from other providers are ignored."""
        service = EvenG2NotificationService(
            event_bus=AsyncMock(),
            g2_url="http://localhost:5173",
            bridge_secret="bridge-secret",
        )

        with patch("src.notifications.even_g2.httpx.AsyncClient") as mock_client:
            await service.handle_response(
                AgentResponseEvent(provider="github", session_id="sess-1", text="hello")
            )

        mock_client.assert_not_called()

    async def test_ignores_missing_session_id(self) -> None:
        """Events without session_id are ignored."""
        service = EvenG2NotificationService(
            event_bus=AsyncMock(),
            g2_url="http://localhost:5173",
            bridge_secret="bridge-secret",
        )

        with patch("src.notifications.even_g2.httpx.AsyncClient") as mock_client:
            await service.handle_response(
                AgentResponseEvent(provider="even-g2", session_id=None, text="hello")
            )

        mock_client.assert_not_called()

    async def test_post_failures_are_swallowed(self) -> None:
        """Bridge delivery failures should not crash event handling."""
        service = EvenG2NotificationService(
            event_bus=AsyncMock(),
            g2_url="http://localhost:5173",
            bridge_secret="bridge-secret",
        )

        mock_client = AsyncMock()
        mock_client.post.side_effect = RuntimeError("network error")
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_client
        mock_ctx.__aexit__.return_value = False

        with patch(
            "src.notifications.even_g2.httpx.AsyncClient",
            return_value=mock_ctx,
        ):
            await service.handle_response(
                AgentResponseEvent(
                    provider="even-g2",
                    session_id="sess-2",
                    text="hello",
                )
            )

    async def test_non_2xx_callback_status_is_logged(self) -> None:
        """Non-2xx callback responses should be logged for diagnosis."""
        service = EvenG2NotificationService(
            event_bus=AsyncMock(),
            g2_url="http://localhost:5173",
            bridge_secret="bridge-secret",
        )

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "invalid authorization token"

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_client
        mock_ctx.__aexit__.return_value = False

        with patch(
            "src.notifications.even_g2.httpx.AsyncClient",
            return_value=mock_ctx,
        ), patch("src.notifications.even_g2.logger") as mock_logger:
            await service.handle_response(
                AgentResponseEvent(
                    provider="even-g2",
                    session_id="sess-3",
                    text="hello",
                    originating_event_id="evt-1",
                )
            )

        mock_logger.warning.assert_called_with(
            "Even-g2 bridge rejected callback",
            session_id="sess-3",
            source_event="evt-1",
            status_code=401,
            body_snippet="invalid authorization token",
        )

    def test_plain_text_preserves_literal_angle_brackets(self) -> None:
        """Literal bracket content should survive HTML cleanup."""
        text = "Literal: &lt;config&gt; and 2 &lt; 3"
        result = EvenG2NotificationService._to_plain_text(text)
        assert result == "Literal: <config> and 2 < 3"

    def test_plain_text_strips_known_tags_but_keeps_code_symbols(self) -> None:
        """Known Telegram formatting tags are removed while code symbols remain."""
        text = "<p><code>if x &lt; y:</code><br/>pass</p>"
        result = EvenG2NotificationService._to_plain_text(text)
        assert result == "if x < y:\npass"
