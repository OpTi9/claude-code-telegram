# agents.md — Project Intelligence for AI Agents

Quick-start reference for Claude Code and other AI agents working in this repo.
Written after full codebase analysis. Do not delete — update as the project evolves.

---

## What This Project Is

A Telegram bot that provides remote access to Claude Code (Anthropic's AI coding assistant).
Users send messages via Telegram; the bot forwards them to Claude and returns responses.
Built with Python 3.10+, Poetry, `python-telegram-bot`, and `claude-agent-sdk`.

---

## Two Operating Modes

Controlled by `AGENTIC_MODE` env var (default: `true`).

### Agentic Mode (default, recommended)
- Minimal UI: only `/start`, `/new`, `/status` commands
- Natural language: user just chats, Claude handles everything
- Session auto-resumes per project (user + directory)
- No inline keyboards shown during conversation

### Classic Mode (`AGENTIC_MODE=false`)
- 13 explicit commands: `/cd`, `/ls`, `/pwd`, `/git`, `/export`, etc.
- Rich inline keyboards with action buttons
- `ConversationEnhancer` and Quick Actions are present in code but currently have
  wiring/signature mismatches (see "Known Broken Wiring")

---

## Key Source Files & What They Do

```
src/
├── main.py                        Entry point, wires all components together, ordered shutdown
├── bot/
│   ├── core.py                    Main Bot class, handler registration
│   ├── orchestrator.py            Routes messages; registers agentic OR classic handlers
│   ├── handlers/
│   │   ├── command.py             Classic mode: all 13 commands
│   │   ├── callback.py            Inline keyboard callbacks (cd, action, git, export, followup)
│   │   └── message.py             Classic mode message handler
│   ├── middleware/
│   │   ├── auth.py                Auth middleware (group -2)
│   │   ├── rate_limit.py          Rate limit middleware (group -1)
│   │   └── security.py            Input validation middleware (group -3)
│   ├── features/
│   │   ├── conversation_mode.py   ConversationEnhancer (currently mismatched with handler calls)
│   │   ├── quick_actions.py       QuickActionManager (currently mismatched with handler calls)
│   │   ├── git_integration.py     Git status/diff/log
│   │   ├── file_handler.py        File upload handling
│   │   ├── image_handler.py       Image prompt builder (no OCR/vision payload passed to Claude yet)
│   │   └── session_export.py      Session export formatter (currently not wired end-to-end)
│   └── utils/
│       ├── html_format.py         escape_html() and HTML formatting helpers
│       └── formatting.py          Text/markdown utilities
├── claude/
│   ├── facade.py                  ClaudeIntegration: public interface, chooses SDK vs CLI
│   ├── sdk_integration.py         PRIMARY: claude-agent-sdk async query() with streaming
│   ├── integration.py             FALLBACK: CLI subprocess manager; also defines ClaudeResponse
│   ├── session.py                 Session persistence, auto-resume logic
│   ├── monitor.py                 ToolMonitor: validates tool calls during streaming
│   └── exceptions.py              Claude-specific exceptions
├── config/
│   ├── settings.py                Pydantic Settings v2; all env vars defined here
│   ├── features.py                Feature flags (MCP, git, uploads, quick actions, etc.)
│   ├── loader.py                  Config file loader
│   └── environments.py            Dev/prod environment detection
├── security/
│   ├── validators.py              SecurityValidator: path traversal, dangerous patterns, forbidden files
│   ├── auth.py                    WhitelistAuthProvider + TokenAuthProvider
│   ├── rate_limiter.py            Token bucket per user; cost-aware
│   └── audit.py                   AuditLogger abstractions (main currently uses in-memory audit storage)
├── storage/
│   ├── facade.py                  Storage: high-level interface used by handlers
│   ├── database.py                SQLite async wrapper (aiosqlite)
│   ├── models.py                  Data models: User, Session, Message, ToolUsage, etc.
│   ├── repositories.py            Repository pattern: User, Session, Message, ToolUsage, Audit, Cost, Analytics
│   └── session_storage.py         Session-specific persistence
├── events/
│   ├── bus.py                     EventBus: async pub/sub, type-safe subscriptions
│   ├── types.py                   Event types: WebhookEvent, ScheduledEvent, AgentResponseEvent
│   ├── handlers.py                AgentHandler: converts events → Claude prompts
│   └── middleware.py              EventSecurityMiddleware
├── api/
│   ├── server.py                  FastAPI server: receives GitHub + generic webhooks
│   └── auth.py                    HMAC-SHA256 (GitHub) + Bearer token auth
├── notifications/
│   └── service.py                 NotificationService: rate-limited Telegram delivery
└── scheduler/
    └── scheduler.py               APScheduler cron jobs → EventBus
```

---

## Request Flow (Agentic Mode)

```
Telegram message
  → Security middleware (group -3): blocks dangerous patterns
  → Auth middleware (group -2): whitelist or token check
  → Rate limit middleware (group -1): token bucket
  → MessageOrchestrator.agentic_text() (group 10)
  → ClaudeIntegration.run_command()
      → ClaudeSDKManager (primary, streaming)
          → ToolMonitor validates each tool call in real-time
      → ClaudeProcessManager (fallback on SDK JSON/TaskGroup errors)
  → Session updated in SQLite
  → Response formatted as HTML
  → Sent back to Telegram
  → AuditLogger records interaction
```

## Request Flow (External Triggers)

```
Webhook POST /webhooks/{provider}
  → HMAC-SHA256 or Bearer token verification
  → Atomic deduplication (webhook_events table)
  → EventBus.publish(WebhookEvent)
  → AgentHandler.handle_webhook()
  → ClaudeIntegration.run_command()
  → EventBus.publish(AgentResponseEvent)
  → NotificationService → Telegram
```

---

## Middleware Groups (order matters)

| Group | Middleware        |
|-------|------------------|
| -3    | Security validation |
| -2    | Authentication   |
| -1    | Rate limiting    |
| 10    | Message routing  |

---

## Dependency Injection Pattern

All components injected via `context.bot_data` in handlers:

```python
auth_manager       = context.bot_data["auth_manager"]
claude_integration = context.bot_data["claude_integration"]
storage            = context.bot_data["storage"]
security_validator = context.bot_data["security_validator"]
rate_limiter       = context.bot_data.get("rate_limiter")
audit_logger       = context.bot_data.get("audit_logger")
features           = context.bot_data.get("features")
# Optional feature services are normally accessed through the feature registry:
# conversation_enhancer = features.get_conversation_enhancer()
# quick_actions         = features.get_quick_actions()
```

Wired together in `main.py`. Add new components there.

---

## Session Management

- Sessions keyed by `(user_id, working_directory)`
- Stored in SQLite; loaded on each message
- Temporary IDs (`temp_*`) are **never** sent to Claude for resume
- Expired sessions (default: 24 hours via `SESSION_TIMEOUT_HOURS`) are cleaned up automatically
- SDK sessions and CLI fallback sessions are **separate session spaces** — they don't share IDs

---

## Claude Dual-Backend

`ClaudeIntegration` (facade) wraps:

1. **`ClaudeSDKManager`** — primary, uses `claude-agent-sdk` async `query()`, streaming, session IDs from Claude's `ResultMessage`
2. **`ClaudeProcessManager`** — fallback, CLI subprocess, triggered automatically on JSON decode or TaskGroup errors

`ClaudeResponse` dataclass is defined in `integration.py` (the CLI module) but used by both backends.

---

## Security Model (5 layers)

1. **Input validation** — blocks `..`, `~`, `${}`, `$()`, backticks, `;`, `&&`, `||`, `>`, `<`, `|`, null bytes
2. **Authentication** — whitelist of Telegram user IDs (`ALLOWED_USERS`) or token-based auth
3. **Directory sandbox** — all paths must be within `APPROVED_DIRECTORY`; path traversal via `Path.resolve()` check
4. **Rate limiting** — token bucket per user, configurable burst, cost-aware
5. **Audit logging** — command/security audit logger currently uses in-memory storage in `main.py`; interaction/audit tables also exist in SQLite via `storage/`

**Forbidden files** (blocked by SecurityValidator): `.env*`, `.ssh`, `.aws`, `.docker`, `id_rsa`, `id_dsa`, `id_ecdsa`, `passwd`, `shadow`, `sudoers`, `.bash_history`, `.zsh_history`

---

## Adding a New Command

### Agentic mode
1. Add handler in `src/bot/orchestrator.py`
2. Register in `MessageOrchestrator._register_agentic_handlers()`
3. Add to `MessageOrchestrator.get_bot_commands()`
4. Add audit logging

### Classic mode
1. Add handler in `src/bot/handlers/command.py`
2. Register in `MessageOrchestrator._register_classic_handlers()`
3. Add to `MessageOrchestrator.get_bot_commands()`
4. Add audit logging

---

## Configuration (Key Env Vars)

| Variable | Default | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | required | |
| `TELEGRAM_BOT_USERNAME` | required | |
| `APPROVED_DIRECTORY` | required | Sandbox root |
| `ALLOWED_USERS` | — | Comma-separated Telegram IDs |
| `AGENTIC_MODE` | `true` | `false` for classic mode |
| `USE_SDK` | `true` | `false` forces CLI fallback |
| `ANTHROPIC_API_KEY` | — | Optional if CLI already logged in |
| `CLAUDE_MODEL` | `claude-3-5-sonnet-20241022` | |
| `CLAUDE_MAX_TURNS` | `10` | |
| `CLAUDE_TIMEOUT_SECONDS` | `300` | |
| `CLAUDE_MAX_COST_PER_USER` | `10.0` | Daily USD limit |
| `CLAUDE_ALLOWED_TOOLS` | `Read,Write,Edit,Bash,Glob,Grep,LS,Task,MultiEdit,NotebookRead,NotebookEdit,WebFetch,TodoRead,TodoWrite,WebSearch` | Comma-separated whitelist |
| `CLAUDE_DISALLOWED_TOOLS` | `git commit,git push` | Explicit command/tool blacklist |
| `ENABLE_MCP` | `false` | Requires `MCP_CONFIG_PATH` |
| `SESSION_TIMEOUT_HOURS` | `24` | Used by `SessionManager` expiration checks |
| `SESSION_TIMEOUT_MINUTES` | `120` | |
| `MAX_SESSIONS_PER_USER` | `5` | |
| `DATABASE_URL` | `sqlite:///data/bot.db` | |
| `RATE_LIMIT_REQUESTS` | `10` | |
| `RATE_LIMIT_WINDOW` | `60` | Seconds |
| `RATE_LIMIT_BURST` | `20` | |
| `ENABLE_API_SERVER` | `false` | FastAPI webhook server |
| `API_SERVER_PORT` | `8080` | |
| `ENABLE_SCHEDULER` | `false` | APScheduler cron jobs |
| `GITHUB_WEBHOOK_SECRET` | — | HMAC-SHA256 |
| `WEBHOOK_API_SECRET` | — | Bearer token for generic providers |
| `NOTIFICATION_CHAT_IDS` | — | Comma-separated chat IDs |
| `DEVELOPMENT_MODE` | `false` | Allows auth fallback only when no providers configured |
| `DEBUG` | `false` | |

---

## What Is NOT Implemented (Stubs / TODOs)

- **Plan mode**: No support for Claude's plan-mode output (where Claude proposes a plan and asks clarifying questions via inline keyboard). Claude's text will appear but there's no structured parsing or button rendering.
- **Follow-up callback** (`callback.py:863`): `handle_followup_callback` is a stub — clicking a follow-up button shows "integration pending" and does nothing.
- **Token auth storage**: `TokenAuthProvider` stores tokens in memory only (no database persistence).
- **Session export path**: UI exists, but callback/export/storage signatures are inconsistent, so export is not reliable end-to-end.
- **Quick Actions path**: manager API, callback data names, and handler expectations are inconsistent; execution path is not reliable.
- **ConversationEnhancer path**: handler calls don't match enhancer method signatures; suggestion flow is effectively disabled.
- **Image understanding**: image bytes are converted to base64 in `ImageHandler` but only text prompt is sent to Claude integration.
- **File context analysis**: `_analyze_context()` in `quick_actions.py` uses keyword matching on chat history instead of actual filesystem inspection.

---

## Known Broken Wiring (High Priority)

- **FeatureRegistry dependency key mismatch**: `FeatureRegistry` is initialized with `security=self.deps.get("security")` in `bot/core.py`, but `main.py` injects `security_validator` (not `security`). Result: features expecting a validator may receive `None`.
- **Quick actions interface mismatch**:
  - `command.py` calls `quick_action_manager.get_suggestions(session_data=...)`, but `QuickActionManager.get_suggestions()` expects a `SessionModel`.
  - `command.py` calls `create_inline_keyboard(..., max_columns=2)`, but method param is `columns`.
  - `quick_actions.py` emits `callback_data="quick_action:..."`, while callback router handles `quick:...`.
  - `callback.py` reads `context.bot_data["quick_actions"]`, but registry is usually under `context.bot_data["features"]`.
- **Conversation enhancer API mismatch**: `message.py` calls `update_context(...)`, `should_show_suggestions(...)`, and `generate_follow_up_suggestions(...)` with incompatible argument signatures.
- **Session export API mismatch**:
  - `callback.py` calls `session_exporter.export_session(claude_session_id, export_format)`.
  - `SessionExporter.export_session()` expects `(user_id, session_id, format)` and uses `Storage` methods that are not exposed with matching names in `storage/facade.py`.
- **Rate-limit double charging risk**: rate limit is checked in middleware and again inside message/agentic handlers, so cost accounting can be stricter than intended.

---

## Storage Schema (SQLite)

Tables managed by repository pattern in `storage/repositories.py`:

| Table | Purpose |
|---|---|
| `users` | User records |
| `sessions` | Session tracking per user+directory |
| `messages` | Full interaction history |
| `tool_usage` | Every Claude tool call (audit) |
| `cost_tracking` | Per-user daily cost |
| `audit_log` | Security events |
| `webhook_events` | Deduplication + audit for webhooks |
| `scheduled_jobs` | APScheduler persistence |

---

## Code Style Rules

- **Black** 88-char line length
- **isort** with black profile
- **flake8** + **mypy strict** (`disallow_untyped_defs = true`)
- **structlog** for all logging (JSON in prod, console in dev)
- **pytest-asyncio** with `asyncio_mode = "auto"`
- Type hints required on ALL functions

Run `make lint` before committing. Run `make format` to auto-fix style.

---

## Testing

```bash
make test                                          # Full suite with coverage
poetry run pytest tests/unit/test_config.py -v    # Single file
poetry run pytest -k test_name -v                  # Single test
poetry run mypy src                                # Type check only
```

Tests currently live in `tests/unit/`.

---

## Graceful Shutdown Order

```
Scheduler → API Server → NotificationService → EventBus
  → Bot → ClaudeIntegration → Storage
```

Implemented in `main.py`. Respect this order when adding new components.

---

## Common Pitfalls

- `ClaudeResponse` is defined in `integration.py` (the CLI module) — import it from there even when using SDK backend
- Session IDs starting with `temp_` must never be passed to Claude for resume
- SDK and CLI sessions are separate — switching backends loses session continuity
- `handle_followup_callback` is a stub; don't assume follow-up buttons work end-to-end
- `FeatureRegistry` is currently initialized with the wrong security dependency key (`security` vs `security_validator`)
- `AuditLogger` in `main.py` uses in-memory storage right now; restart clears those audit events
- `DEVELOPMENT_MODE=true` can enable allow-all auth fallback when no auth providers are configured — never use that setup in production
- Webhook deduplication uses a unique constraint on `delivery_id`; duplicate POSTs are silently dropped
- Classic mode and agentic mode register completely different handler sets; mode cannot be switched at runtime (requires bot restart)
