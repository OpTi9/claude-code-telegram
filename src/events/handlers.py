"""Event handlers that bridge the event bus to Claude and Telegram.

AgentHandler: translates events into ClaudeIntegration.run_command() calls.
NotificationHandler: subscribes to AgentResponseEvent and delivers to Telegram.
"""

from pathlib import Path
from typing import Any, Dict, List

import structlog

from ..claude.facade import ClaudeIntegration
from .bus import Event, EventBus
from .types import AgentResponseEvent, ScheduledEvent, WebhookEvent

logger = structlog.get_logger()
EVEN_G2_PROMPT_PREFIX = (
    "You are responding for Even G2 smart glasses.\n"
    "Output rules (strict):\n"
    "1) Plain text only.\n"
    "2) No markdown, no code fences, no tables, no HTML.\n"
    "3) Keep it concise by default: about 6-10 short lines.\n"
    "4) Start with the direct answer, then short numbered steps if needed.\n"
    "5) Keep lines short and easy to read on a tiny display.\n"
    "6) If the user explicitly asks for detail, provide at most 3 short sections.\n"
    "7) If commands are needed, put one command per line.\n"
    "8) For code changes in /home/aza/Desktop/even-dev-pip-boy/apps/g2claude:\n"
    "   after edits and checks, when user asks to restart, restart the stack before final response by running:\n"
    "   npm run g2:down\n"
    "   npm run g2:up\n"
    "   (run as two separate commands, not chained with &&).\n"
)


class AgentHandler:
    """Translates incoming events into Claude agent executions.

    Webhook and scheduled events are converted into prompts and sent
    to ClaudeIntegration.run_command(). The response is published
    back as an AgentResponseEvent for delivery.
    """

    def __init__(
        self,
        event_bus: EventBus,
        claude_integration: ClaudeIntegration,
        default_working_directory: Path,
        default_user_id: int = 0,
    ) -> None:
        self.event_bus = event_bus
        self.claude = claude_integration
        self.default_working_directory = default_working_directory.resolve()
        self.default_user_id = default_user_id

    def register(self) -> None:
        """Subscribe to events that need agent processing."""
        self.event_bus.subscribe(WebhookEvent, self.handle_webhook)
        self.event_bus.subscribe(ScheduledEvent, self.handle_scheduled)

    async def handle_webhook(self, event: Event) -> None:
        """Process a webhook event through Claude."""
        if not isinstance(event, WebhookEvent):
            return

        logger.info(
            "Processing webhook event through agent",
            provider=event.provider,
            event_type=event.event_type_name,
            delivery_id=event.delivery_id,
        )

        if event.provider == "even-g2":
            raw_prompt = str(event.payload.get("text", "")).strip()
            session_id = str(event.payload.get("session_id", "")).strip() or None
            if not raw_prompt:
                logger.warning(
                    "Skipping even-g2 webhook with empty prompt",
                    delivery_id=event.delivery_id,
                )
                return
            prompt = self._build_even_g2_prompt(raw_prompt)
            working_directory = self._resolve_even_g2_working_directory(
                event.payload.get("working_directory")
            )
        else:
            prompt = self._build_webhook_prompt(event)
            session_id = None
            working_directory = self.default_working_directory

        try:
            response = await self.claude.run_command(
                prompt=prompt,
                working_directory=working_directory,
                user_id=self.default_user_id,
                session_id=session_id,
            )

            if response.content:
                # We don't know which chat to send to from a webhook alone.
                # The notification service needs configured target chats.
                # Publish with chat_id=0 — the NotificationService
                # will broadcast to configured notification_chat_ids.
                await self.event_bus.publish(
                    AgentResponseEvent(
                        chat_id=0,
                        text=response.content,
                        provider=event.provider,
                        session_id=session_id,
                        originating_event_id=event.id,
                    )
                )
        except Exception:
            logger.exception(
                "Agent execution failed for webhook event",
                provider=event.provider,
                event_id=event.id,
            )

    async def handle_scheduled(self, event: Event) -> None:
        """Process a scheduled event through Claude."""
        if not isinstance(event, ScheduledEvent):
            return

        logger.info(
            "Processing scheduled event through agent",
            job_id=event.job_id,
            job_name=event.job_name,
        )

        prompt = event.prompt
        if event.skill_name:
            prompt = (
                f"/{event.skill_name}\n\n{prompt}" if prompt else f"/{event.skill_name}"
            )

        working_dir = event.working_directory or self.default_working_directory

        try:
            response = await self.claude.run_command(
                prompt=prompt,
                working_directory=working_dir,
                user_id=self.default_user_id,
            )

            if response.content:
                for chat_id in event.target_chat_ids:
                    await self.event_bus.publish(
                        AgentResponseEvent(
                            chat_id=chat_id,
                            text=response.content,
                            originating_event_id=event.id,
                        )
                    )

                # Also broadcast to default chats if no targets specified
                if not event.target_chat_ids:
                    await self.event_bus.publish(
                        AgentResponseEvent(
                            chat_id=0,
                            text=response.content,
                            originating_event_id=event.id,
                        )
                    )
        except Exception:
            logger.exception(
                "Agent execution failed for scheduled event",
                job_id=event.job_id,
                event_id=event.id,
            )

    def _build_webhook_prompt(self, event: WebhookEvent) -> str:
        """Build a Claude prompt from a webhook event."""
        payload_summary = self._summarize_payload(event.payload)

        return (
            f"A {event.provider} webhook event occurred.\n"
            f"Event type: {event.event_type_name}\n"
            f"Payload summary:\n{payload_summary}\n\n"
            f"Analyze this event and provide a concise summary. "
            f"Highlight anything that needs my attention."
        )

    def _build_even_g2_prompt(self, user_prompt: str) -> str:
        """Build a strict plain-text prompt for Even G2 rendering constraints."""
        return f"{EVEN_G2_PROMPT_PREFIX}\nUser request:\n{user_prompt}"

    def _resolve_even_g2_working_directory(self, raw_value: Any) -> Path:
        """Validate optional even-g2 working directory against approved root."""
        if not isinstance(raw_value, str):
            return self.default_working_directory

        value = raw_value.strip()
        if not value:
            return self.default_working_directory

        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = self.default_working_directory / candidate

        try:
            resolved = candidate.resolve()
        except (RuntimeError, OSError, ValueError):
            logger.warning(
                "Ignoring invalid even-g2 working directory",
                requested=value,
            )
            return self.default_working_directory

        if not self._is_within_approved_root(resolved):
            logger.warning(
                "Ignoring even-g2 working directory outside approved root",
                requested=value,
                approved_root=str(self.default_working_directory),
            )
            return self.default_working_directory

        if not resolved.exists() or not resolved.is_dir():
            logger.warning(
                "Ignoring even-g2 working directory that is not an existing directory",
                requested=value,
            )
            return self.default_working_directory

        return resolved

    def _is_within_approved_root(self, candidate: Path) -> bool:
        try:
            candidate.relative_to(self.default_working_directory)
            return True
        except ValueError:
            return False

    def _summarize_payload(self, payload: Dict[str, Any], max_depth: int = 2) -> str:
        """Create a readable summary of a webhook payload."""
        lines: List[str] = []
        self._flatten_dict(payload, lines, max_depth=max_depth)
        # Cap at 2000 chars to keep prompt reasonable
        summary = "\n".join(lines)
        if len(summary) > 2000:
            summary = summary[:2000] + "\n... (truncated)"
        return summary

    def _flatten_dict(
        self,
        data: Any,
        lines: list,
        prefix: str = "",
        depth: int = 0,
        max_depth: int = 2,
    ) -> None:
        """Flatten a nested dict into key: value lines."""
        if depth >= max_depth:
            lines.append(f"{prefix}: ...")
            return

        if isinstance(data, dict):
            for key, value in data.items():
                full_key = f"{prefix}.{key}" if prefix else key
                if isinstance(value, (dict, list)):
                    self._flatten_dict(value, lines, full_key, depth + 1, max_depth)
                else:
                    val_str = str(value)
                    if len(val_str) > 200:
                        val_str = val_str[:200] + "..."
                    lines.append(f"{full_key}: {val_str}")
        elif isinstance(data, list):
            lines.append(f"{prefix}: [{len(data)} items]")
            for i, item in enumerate(data[:3]):  # Show first 3 items
                self._flatten_dict(item, lines, f"{prefix}[{i}]", depth + 1, max_depth)
        else:
            lines.append(f"{prefix}: {data}")
