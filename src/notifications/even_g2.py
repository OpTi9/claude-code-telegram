"""Notification bridge for delivering even-g2 responses to the Vite app."""

import html
import re

import httpx
import structlog

from ..events.bus import Event, EventBus
from ..events.types import AgentResponseEvent

logger = structlog.get_logger()
TELEGRAM_FORMAT_TAG_RE = re.compile(
    r"(?is)</?(?:b|strong|i|em|u|s|del|code|pre|blockquote|a)(?:\s+[^>]*)?>"
)


class EvenG2NotificationService:
    """Sends agent responses to the local even-dev Vite middleware."""

    def __init__(
        self,
        event_bus: EventBus,
        g2_url: str,
        bridge_secret: str,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.event_bus = event_bus
        self.g2_url = g2_url.rstrip("/")
        self.bridge_secret = bridge_secret
        self.timeout_seconds = timeout_seconds

    def register(self) -> None:
        """Subscribe to agent response events."""
        self.event_bus.subscribe(AgentResponseEvent, self.handle_response)

    async def handle_response(self, event: Event) -> None:
        """Forward even-g2 responses to the browser bridge endpoint."""
        if not isinstance(event, AgentResponseEvent):
            return
        if event.provider != "even-g2":
            return
        if not event.session_id:
            return

        plain_text = self._to_plain_text(event.text)
        if not plain_text:
            return

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    f"{self.g2_url}/__g2_receive",
                    headers={"Authorization": f"Bearer {self.bridge_secret}"},
                    json={
                        "sessionId": event.session_id,
                        "text": plain_text,
                    },
                )
                if response.status_code >= 400:
                    body_snippet = response.text.replace("\n", " ").strip()[:300]
                    logger.warning(
                        "Even-g2 bridge rejected callback",
                        session_id=event.session_id,
                        source_event=event.originating_event_id,
                        status_code=response.status_code,
                        body_snippet=body_snippet,
                    )
        except Exception:
            logger.warning(
                "Failed to deliver even-g2 notification",
                session_id=event.session_id,
                source_event=event.originating_event_id,
            )

    @staticmethod
    def _to_plain_text(text: str) -> str:
        """Convert Telegram/HTML-formatted output to plain text for glasses."""
        normalized = re.sub(r"(?i)<br\s*/?>", "\n", text)
        normalized = re.sub(r"(?i)<p(?:\s+[^>]*)?>", "\n", normalized)
        normalized = re.sub(r"(?i)</p>", "\n", normalized)
        stripped = TELEGRAM_FORMAT_TAG_RE.sub("", normalized)
        unescaped = html.unescape(stripped)
        compact = re.sub(r"\n{3,}", "\n\n", unescaped)
        return compact.strip()
