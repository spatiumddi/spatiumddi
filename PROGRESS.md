# Branch progress: `feat/operator-copilot`

Single-issue branch for #90 — Operator Copilot (MCP server + multi-provider
LLM chat). Big feature; split into four waves that each land as a working
commit you can test and steer from.

## Resume protocol

1. `git checkout feat/operator-copilot`
2. Read this file from top to bottom.
3. The first unchecked box under "Checkpoints" is the next thing to do.
4. After each wave commits, run `make ci`, smoke-test the new surface, and
   confirm with the user before starting the next wave.
5. When all "Done when" criteria check, merge to `main` with `--no-ff`.

## Issue #90 — Operator Copilot

### Wave 1 — Foundation (LLM driver + provider config)

- [x] `feat/operator-copilot` branch + PROGRESS.md
- [x] Migration: `ai_provider` table (`a4b8c2d619e7_ai_provider`) — id, name, kind (`openai_compat`|`anthropic`|`google`|`azure_openai`), base_url, api_key_encrypted (Fernet), default_model, is_enabled, priority, options (JSONB), timestamps. Indexed on `(is_enabled, priority)`. CHECK constraint on kind enum.
- [x] Model: `AIProvider` (`backend/app/models/ai.py`) mirroring `AuthProvider` shape. Registered in `models/__init__.py`.
- [x] Driver ABC: `app/drivers/llm/base.py` — `LLMDriver` with `chat()` (async-iterator streaming) / `list_models()` / `test_connection()`. Neutral dataclasses (`ChatMessage`, `ChatRequest`, `ChatChunk`, `ToolCall`, `ToolDefinition`, `ToolResult`, `ModelInfo`, `TestConnectionResult`) modeled on the OpenAI Chat Completions schema.
- [x] OpenAI-compat driver: `app/drivers/llm/openai_compat.py` — uses `openai` SDK pointed at `base_url`. Streaming with tool-call delta reassembly. test_connection bypasses list_models so connection / auth / status errors surface distinctly.
- [x] Driver registry: `app/drivers/llm/registry.py` — `get_driver(provider)`, `register_driver(name, cls)`, `known_kinds()`.
- [x] New dep: `openai>=1.40.0` (resolved to 2.33.0 at install time).
- [x] Settings router: `/api/v1/ai/providers` GET list / POST create / GET /{id} / PUT /{id} / DELETE /{id} + `/providers/test` (unsaved probe) + `/providers/{id}/test` + `/providers/{id}/models`. Mounted under `/api/v1/ai`. SuperAdmin only.
- [x] Live smoke-test (7/7 assertions): list / create / probe / unsaved-probe / update / kind-enum-validation / delete all return correct codes. Bad URL surfaces "connection failed" not the misleading "no models".
- [x] Frontend: `frontend/src/pages/admin/AIProvidersPage.tsx` — list + edit modal + test-connection + model-picker. Modal shows live test result inline; row actions: test / list models / edit / delete.
- [x] `aiApi` types + methods in `frontend/src/lib/api.ts`: `listProviders` / `getProvider` / `createProvider` / `updateProvider` / `deleteProvider` / `testProvider` / `testUnsaved` / `listModels`. `AI_PROVIDER_KIND_LABELS` + `AI_PROVIDER_KIND_AVAILABLE` (Wave 1 ships only `openai_compat`).
- [x] Sidebar entry under Administration → Platform: "AI Providers" with `Sparkles` icon. Route `/admin/ai/providers` in `App.tsx`.
- [x] CI green: ruff / black / mypy / eslint / prettier / tsc / vite build all pass.
- [x] Smoke (final, post-format): list (200) → create with key → test (graceful failure surfaces "connection failed") → has_api_key=true → clear key → has_api_key=false → delete (204). All assertions pass.

### Wave 2 — MCP tool surface (read-only)

- [ ] Tool registry: `app/services/ai/tools/__init__.py` with decorator + dispatch
- [ ] ~15 read-only tools wrapping existing service calls:
  - `list_spaces`, `list_blocks`, `list_subnets`, `find_ip`, `get_subnet_summary`
  - `query_dns_zones`, `query_dns_records`, `find_dhcp_leases`, `list_alerts`
  - `search_logs`, `get_audit_history`, `count_devices_matching`
  - `find_by_tag`, `find_by_custom_field`, `find_by_owner`
- [ ] MCP server endpoint: `/mcp/v1` over Streamable HTTP transport (cleanest for browser + remote clients)
- [ ] Token-based auth (reuse #74 API tokens) — Claude Desktop / external clients use a scoped token
- [ ] Tool schemas auto-generated from Pydantic models for the tool args
- [ ] Smoke-test: connect Claude Desktop via `mcp-remote` adapter, list tools, call `list_subnets`
- [ ] `make ci` clean
- [ ] Single Wave 2 commit

### Wave 3 — In-app chat

- [ ] Migration: `ai_chat_session` (per-user, name, system_prompt, model, total tokens, total cost)
- [ ] Migration: `ai_chat_message` (role, content, tokens_in, tokens_out, latency_ms, tool_calls JSONB)
- [ ] Chat orchestrator: `app/services/ai/chat.py` — load user context, build system prompt, call driver, route tool calls back through registry, persist messages
- [ ] Streaming endpoint: `POST /api/v1/ai/chat` (SSE response) — one message at a time, streams tokens
- [ ] Session CRUD: `/api/v1/ai/sessions`
- [ ] Frontend: floating chat button bottom-right (hidden when `ai.enabled=false`)
- [ ] Frontend: chat drawer with markdown rendering + tool-call cards
- [ ] Frontend: session history list, switch / rename / delete sessions
- [ ] Smoke-test: ask "list my IP spaces" through Ollama, verify tool call + response
- [ ] `make ci` clean
- [ ] Single Wave 3 commit

### Wave 4 — Observability + cost guardrails

- [ ] PlatformSettings additions: `ai_enabled`, `ai_default_model`, `ai_per_user_daily_token_cap`, `ai_per_user_daily_cost_cap_usd`
- [ ] Per-call rate-sheet (provider+model → input/output $/Mtoken) in `app/services/ai/pricing.py`
- [ ] Cost computed at write time (input_tokens × in_rate + output_tokens × out_rate) and stored on `ai_chat_message`
- [ ] Cap enforcement before each call — 429 with `Retry-After: <seconds-until-midnight-utc>` when over
- [ ] Platform-insights AI usage card: total messages today, tokens today, cost today, top users
- [ ] Frontend: cap progress bar in chat drawer ("$0.04 of $5.00 today")
- [ ] `make ci` clean
- [ ] Single Wave 4 commit

### Done when

All four wave commits land + merged to main.

## Out of scope (explicitly deferred to issue #90 Phase 2-4)

- Anthropic / Gemini / Azure OpenAI drivers
- Failover chain (priority-ordered fallback across providers)
- Custom prompts library
- Write tools with preview/confirm flow
- Cmd-K command palette overlay
- Right-click "Ask AI about this" affordances throughout the UI
- Natural-language search bar
- Diagnostic mode walks
- Bulk-edit-by-prompt
- Daily digest cron
- Vendor-specific MCP transport (stdio for local-process clients)

## Risks / unknowns

- **MCP spec churn.** The spec is evolving fast (Streamable HTTP replaced HTTP+SSE in late 2025). Pin the SDK version we build against; revisit on each minor bump.
- **OpenAI SDK as a hard dep.** ~5 MB but covers most operator use cases. Acceptable for v1.
- **Ollama model-listing on Linux.** The OpenAI-compat driver hits `/v1/models`; Ollama's compat surface lists pulled models there. Test on real Ollama before claiming list works.
- **Tool-call streaming.** OpenAI SDK supports streaming with tool-call deltas; the chat orchestrator needs to buffer tool-call deltas across chunks before dispatching. Document in Wave 3.
- **Token counting for streaming responses.** OpenAI returns usage in the final chunk when `stream_options.include_usage=true`. Set this consistently or fall back to tiktoken estimation.
- **Cost rate sheet drift.** Rates change quarterly. Make the rate sheet operator-overridable in PlatformSettings.
