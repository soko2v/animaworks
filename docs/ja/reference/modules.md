<!-- 自動生成ファイル・編集禁止。再生成: uv run python scripts/gen_reference.py modules -->
<!-- generator: gen_reference/1  kind: modules  source-sha256: 68db5701b33f6e995cdc56b41072c896d63cafc25f9e6ff6c12a7bc781b4cd0f -->

# モジュール一覧

`git ls-files core cli server` で追跡対象の Python ファイルを列挙しています。非公開モジュールには印を付けています。

## `cli`

—

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `cli` | 9 | — |
| `cli.__main__（非公開）` | 9 | — |
| `cli._gateway（非公開）` | 112 | — |
| `cli.demo` | 392 | Native ``animaworks demo`` command. |
| `cli.parser` | 1006 | — |

## `cli.commands`

—

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `cli.commands` | 5 | — |
| `cli.commands.anima` | 214 | — |
| `cli.commands.anima_merge` | 72 | — |
| `cli.commands.anima_mgmt` | 1451 | CLI commands for anima process management. |
| `cli.commands.board` | 191 | — |
| `cli.commands.company_cmd` | 226 | — |
| `cli.commands.cost_cmd` | 232 | — |
| `cli.commands.cron_guard` | 93 | CLI commands for inspecting and re-enabling cron guard tasks. |
| `cli.commands.import_cmd` | 88 | — |
| `cli.commands.index_cmd` | 426 | — |
| `cli.commands.init_cmd` | 150 | — |
| `cli.commands.internal_cmd` | 362 | — |
| `cli.commands.logs` | 206 | CLI commands for viewing anima logs. |
| `cli.commands.mcp_cmd` | 66 | — |
| `cli.commands.messaging` | 167 | — |
| `cli.commands.migrate_cmd` | 114 | — |
| `cli.commands.models_cmd` | 219 | CLI commands for model information and management. |
| `cli.commands.optimize_assets` | 189 | — |
| `cli.commands.profile` | 332 | — |
| `cli.commands.rag_repair_status` | 148 | Status reporting for persistent RAG repair state. |
| `cli.commands.remake_cmd` | 272 | — |
| `cli.commands.repair_rag_cmd` | 135 | — |
| `cli.commands.server` | 975 | — |
| `cli.commands.skills` | 211 | — |
| `cli.commands.supervisor_cmd` | 109 | — |
| `cli.commands.task_cmd` | 568 | — |
| `cli.commands.task_store_cmd` | 118 | Operator-only, cohort-scoped task migration and current-state export. |
| `cli.commands.tmp_cmd` | 173 | — |
| `cli.commands.vault_cmd` | 247 | — |

## `cli.tui`

—

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `cli.tui` | 203 | — |
| `cli.tui.app` | 1817 | — |
| `cli.tui.client` | 394 | — |
| `cli.tui.commands` | 251 | — |
| `cli.tui.keybindings` | 90 | Keybinding configuration for the TUI. |
| `cli.tui.markdown` | 535 | Markdown → Rich renderables, tuned for a terminal transcript. |
| `cli.tui.session` | 213 | Session persistence for the TUI. |
| `cli.tui.sse` | 98 | — |
| `cli.tui.state` | 280 | Pure client-side UI state for the TUI sidebar, activity feed and palette. |
| `cli.tui.widgets.call_human` | 85 | A call_human notification card rendered in the transcript. |
| `cli.tui.widgets.chat_input` | 152 | — |
| `cli.tui.widgets.palette` | 115 | Slash-command completion palette. |
| `cli.tui.widgets.response_status` | 63 | — |
| `cli.tui.widgets.sidebar` | 248 | Sidebar widget: list of animas + activity feed. |
| `cli.tui.widgets.status_bar` | 160 | — |
| `cli.tui.widgets.thinking` | 98 | — |
| `cli.tui.widgets.tool_card` | 140 | — |
| `cli.tui.widgets.transcript` | 291 | — |

## `cli.tui.widgets`

—

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `cli.tui.widgets` | 32 | — |

## `core`

—

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core` | 7 | — |
| `core.chat_attachments` | 1274 | — |
| `core.exceptions` | 145 | — |
| `core.internal_api` | 38 | — |
| `core.paths` | 217 | Centralized path resolution for AnimaWorks. |
| `core.schemas` | 240 | — |
| `core.time_utils` | 103 | — |

## `core.agent`

LLM エージェントの実行、会話制御、エンジン連携。

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.agent` | 23 | — |
| `core.agent.agent_core` | 324 | — |
| `core.agent.cycle` | 1607 | — |
| `core.agent.executor_factory` | 178 | — |
| `core.agent.priming` | 465 | — |
| `core.agent.prompt_log` | 194 | — |
| `core.agent.session_compactor` | 562 | Per-Anima × per-thread_id idle compaction timer management. |

## `core.anima`

Digital Anima のライフサイクルと実行時オブジェクト。

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.anima` | 23 | — |
| `core.anima.asset_reconciler` | 675 | — |
| `core.anima.bootstrap_state` | 575 | — |
| `core.anima.digital_anima` | 674 | — |
| `core.anima.emotion_tag` | 84 | Shared emotion-tag extraction for LLM responses. |
| `core.anima.factory` | 770 | Anima creation factory: create new Digital Animas from templates, blank, or MD files. |
| `core.anima.heartbeat` | 946 | — |
| `core.anima.image_artifacts` | 219 | — |
| `core.anima.inbox` | 1009 | — |
| `core.anima.inbox_overflow` | 130 | — |
| `core.anima.lifecycle` | 1356 | — |
| `core.anima.messaging` | 1772 | — |
| `core.anima.response_normalize` | 141 | — |
| `core.anima.roster` | 83 | — |
| `core.anima.skills_check` | 15 | — |

## `core.auth`

ユーザー認証、セッション、認証情報の管理。

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.auth` | 7 | — |
| `core.auth.manager` | 192 | — |
| `core.auth.models` | 45 | — |

## `core.config`

アプリケーション設定のスキーマ、読み込み、検証、移行。

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.config` | 42 | — |
| `core.config.anima_registry` | 295 | Anima registration in config.json: register, unregister, rename. |
| `core.config.cli` | 348 | CLI handlers for the ``animaworks config`` subcommand. |
| `core.config.env_slots` | 118 | — |
| `core.config.file_access_policy` | 285 | — |
| `core.config.global_permissions` | 251 | — |
| `core.config.io` | 253 | Configuration I/O: singleton cache, load, and save. |
| `core.config.local_llm` | 100 | Helpers for local Ollama-backed model defaults and role presets. |
| `core.config.migrate` | 944 | Migrate legacy config.md files to unified config.json. |
| `core.config.model_catalog` | 171 | Static model catalog and per-request model override validation. |
| `core.config.model_config` | 860 | Model configuration resolution: load_model_config, penalties, max_tokens. |
| `core.config.model_discovery` | 521 | Dynamic discovery of the "mode + model" catalog from the installed CLIs. |
| `core.config.model_mode` | 446 | Model execution mode resolution for canonical S/C/D/G/X/A modes. |
| `core.config.models` | 121 | Central configuration module — facade re-exporting split modules. |
| `core.config.resolver` | 172 | Configuration resolution: status.json merge with anima_defaults. |
| `core.config.schemas` | 1448 | Pydantic configuration schemas for AnimaWorks. |
| `core.config.vault` | 473 | Credential vault with PyNaCl SealedBox encryption. |

## `core.execution`

ツール実行、コマンド実行、安全制御。

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.execution` | 60 | — |
| `core.execution._sanitize（非公開）` | 433 | — |
| `core.execution._session（非公開）` | 112 | — |
| `core.execution._streaming（非公開）` | 303 | — |
| `core.execution._tool_summary（非公開）` | 102 | — |
| `core.execution.backoff` | 42 | Backoff timing helpers for coordinated LLM retry. |
| `core.execution.base` | 888 | — |
| `core.execution.cli_stream` | 253 | — |
| `core.execution.engine_base` | 75 | — |
| `core.execution.engine_session` | 101 | — |
| `core.execution.engines.claude._sdk_hooks（非公開）` | 679 | — |
| `core.execution.engines.claude._sdk_interrupt（非公開）` | 106 | — |
| `core.execution.engines.claude._sdk_options（非公開）` | 555 | — |
| `core.execution.engines.claude._sdk_patch（非公開）` | 261 | — |
| `core.execution.engines.claude._sdk_security（非公開）` | 305 | — |
| `core.execution.engines.claude._sdk_session（非公開）` | 531 | — |
| `core.execution.engines.claude._sdk_stream（非公開）` | 461 | — |
| `core.execution.engines.claude.agent_sdk` | 1013 | — |
| `core.execution.engines.codex.codex_sdk` | 847 | — |
| `core.execution.engines.codex.events` | 667 | — |
| `core.execution.engines.codex.setup` | 952 | — |
| `core.execution.engines.cursor.cursor_agent` | 711 | — |
| `core.execution.engines.gemini.gemini_cli` | 467 | — |
| `core.execution.engines.grok.grok_cli` | 1087 | — |
| `core.execution.engines.litellm._litellm_context（非公開）` | 524 | — |
| `core.execution.engines.litellm._litellm_streaming（非公開）` | 1398 | — |
| `core.execution.engines.litellm._litellm_tools（非公開）` | 399 | — |
| `core.execution.engines.litellm.litellm_loop` | 617 | — |
| `core.execution.error_classifier` | 805 | Centralized LLM API error classification for coordinated recovery. |
| `core.execution.events` | 105 | — |
| `core.execution.fallback_activity` | 290 | Activity-log integration for ephemeral runtime model fallback. |
| `core.execution.github_identity` | 162 | GitHub identity resolution for executor environments. |
| `core.execution.loop_guards` | 411 | In-loop guard mechanisms for the self-hosted execution loops (Mode A/B). |
| `core.execution.process_runner` | 184 | — |
| `core.execution.rate_guard` | 339 | Cross-process LLM rate guard (fleet-wide circuit breaker). |
| `core.execution.reminder` | 128 | — |
| `core.execution.session_context` | 109 | — |
| `core.execution.session_store` | 130 | — |
| `core.execution.session_types` | 73 | — |
| `core.execution.tool_evidence` | 182 | — |
| `core.execution.watchdog` | 139 | — |

## `core.execution.engines`

—

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.execution.engines` | 6 | — |

## `core.execution.engines.claude`

—

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.execution.engines.claude` | 10 | — |

## `core.execution.engines.codex`

—

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.execution.engines.codex` | 6 | — |

## `core.execution.engines.cursor`

—

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.execution.engines.cursor` | 6 | — |

## `core.execution.engines.gemini`

—

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.execution.engines.gemini` | 6 | — |

## `core.execution.engines.grok`

—

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.execution.engines.grok` | 6 | — |

## `core.execution.engines.litellm`

—

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.execution.engines.litellm` | 6 | — |

## `core.i18n`

翻訳カタログと言語選択。

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.i18n` | 135 | Lightweight i18n support for runtime strings. |
| `core.i18n.strings.communication` | 52 | Domain-specific i18n strings. |
| `core.i18n.strings.company` | 14 | Localized strings for company management. |
| `core.i18n.strings.config` | 310 | Domain-specific i18n strings. |
| `core.i18n.strings.discord` | 28 | — |
| `core.i18n.strings.execution` | 205 | Domain-specific i18n strings. |
| `core.i18n.strings.handler` | 384 | Domain-specific i18n strings (handler part 1). |
| `core.i18n.strings.handler_ext` | 368 | Domain-specific i18n strings (handler part 2). |
| `core.i18n.strings.lifecycle` | 104 | Domain-specific i18n strings. |
| `core.i18n.strings.memory` | 409 | Domain-specific i18n strings. |
| `core.i18n.strings.migrate` | 94 | — |
| `core.i18n.strings.misc` | 434 | Domain-specific i18n strings. |
| `core.i18n.strings.misc_routes` | 12 | Domain-specific i18n strings (legacy route modules). |
| `core.i18n.strings.room_manager` | 29 | i18n strings for meeting room manager. |
| `core.i18n.strings.server` | 357 | Domain-specific i18n strings. |
| `core.i18n.strings.supervisor` | 91 | Domain-specific i18n strings. |
| `core.i18n.strings.tmp` | 74 | — |
| `core.i18n.strings.tooling` | 121 | Domain-specific i18n strings (tool prompts and tooling). |
| `core.i18n.strings.tooling_schema` | 476 | Domain-specific i18n strings (schema.*). |
| `core.i18n.strings.tooling_schema_ext` | 132 | Domain-specific i18n strings (schema.* part 2). |
| `core.i18n.strings.zoom` | 26 | — |

## `core.i18n.strings`

—

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.i18n.strings` | 61 | Merge all domain string modules into a single dict. |

## `core.infra`

ログ、データベース、キャッシュなどの基盤機能。

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.infra` | 6 | — |
| `core.infra.auto_updater` | 198 | — |
| `core.infra.event_export` | 379 | — |
| `core.infra.gpu` | 173 | — |
| `core.infra.logging_config` | 530 | Centralized logging configuration for AnimaWorks. |
| `core.infra.runtime_init` | 436 | First-launch initialization: copy templates to runtime data directory. |
| `core.infra.startup_progress` | 191 | — |
| `core.infra.tmp_cleanup` | 254 | — |

## `core.integrations`

外部サービス連携と animaworks-tool の実装。

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.integrations` | 461 | AnimaWorks external tools package. |
| `core.integrations._anima_icon_url（非公開）` | 306 | Anima icon URL resolution — dashboard, outbound, Slack, notifications, tools, etc. |
| `core.integrations._async_compat（非公開）` | 41 | Async compatibility helpers for tools with synchronous HTTP clients. |
| `core.integrations._base（非公開）` | 372 | Base infrastructure for AnimaWorks tools. |
| `core.integrations._cache（非公開）` | 96 | Shared SQLite message cache base class for communication tools. |
| `core.integrations._chatwork_cache（非公開）` | 304 | SQLite message cache for Chatwork offline search and unreplied detection. |
| `core.integrations._chatwork_client（非公開）` | 240 | HTTP client for the Chatwork v2 API. |
| `core.integrations._chatwork_cli（非公開）` | 633 | Standalone CLI entry point for the Chatwork tool. |
| `core.integrations._chatwork_identity（非公開）` | 76 | Chatwork identity and delegation resolution. |
| `core.integrations._chatwork_markdown（非公開）` | 162 | Markdown-to-Chatwork format conversion utilities. |
| `core.integrations._comm_cli（非公開）` | 71 | — |
| `core.integrations._discord_cache（非公開）` | 266 | SQLite message cache for Discord (offline search, sync state). |
| `core.integrations._discord_client（非公開）` | 366 | Discord REST API v10 client with rate-limit retry. |
| `core.integrations._discord_cli（非公開）` | 311 | Standalone CLI entry point for Discord tools. |
| `core.integrations._discord_markdown（非公開）` | 138 | Discord markup helpers: plain-text cleanup and length limits. |
| `core.integrations._image_clients（非公開）` | 93 | API clients and shared constants for image/3D generation. |
| `core.integrations._image_cli（非公開）` | 369 | CLI entry point for ``animaworks-tool image_gen``. |
| `core.integrations._image_glb（非公開）` | 473 | GLB/FBX asset conversion, optimisation, and compression. |
| `core.integrations._image_pipeline（非公開）` | 809 | ImageGenPipeline – orchestrates the full character asset generation. |
| `core.integrations._image_schemas（非公開）` | 42 | Tool schemas and CLI guide for image generation. |
| `core.integrations._retry（非公開）` | 223 | Shared retry/backoff utility for AnimaWorks tools. |
| `core.integrations._slack_cache（非公開）` | 387 | SQLite message cache for Slack (offline search, unreplied detection). |
| `core.integrations._slack_client（非公開）` | 320 | Slack Web API client with rate-limit retry and pagination. |
| `core.integrations._slack_cli（非公開）` | 320 | Standalone CLI entry point for Slack tools. |
| `core.integrations._slack_markdown（非公開）` | 240 | Slack markdown conversion and formatting utilities. |
| `core.integrations.aws_collector` | 393 | AnimaWorks AWS collector tool — ECS status, CloudWatch logs & metrics. |
| `core.integrations.call_human` | 402 | — |
| `core.integrations.chatwork` | 192 | Chatwork integration for AnimaWorks. |
| `core.integrations.discord` | 276 | Discord integration for AnimaWorks. |
| `core.integrations.github` | 400 | AnimaWorks GitHub tool — gh CLI wrapper. |
| `core.integrations.gmail` | 1255 | AnimaWorks Gmail tool -- direct Gmail API access. |
| `core.integrations.google_calendar` | 628 | — |
| `core.integrations.google_sheets` | 470 | — |
| `core.integrations.google_tasks` | 455 | AnimaWorks Google Tasks tool -- Google Tasks API access. |
| `core.integrations.image.atlascloud` | 156 | Optional Atlas Cloud backend for character images and reference edits. |
| `core.integrations.image.codex` | 319 | Codex CLI image generation client (image_gen tool via local codex). |
| `core.integrations.image.constants` | 57 | URL constants, timeouts, and execution profiles for image/3D generation. |
| `core.integrations.image.diffusers_local` | 904 | Local Diffusers-backed image generation helpers. |
| `core.integrations.image.fal` | 243 | Fal.ai Flux Kontext and Flux Pro text-to-image API clients. |
| `core.integrations.image.meshy` | 310 | Meshy Image-to-3D, Rigging, and Animation API client. |
| `core.integrations.image.novelai` | 192 | NovelAI V4.5 API client for anime full-body image generation. |
| `core.integrations.image.prompts` | 197 | Prompt constants for bustup, chibi, and expression variants. |
| `core.integrations.image.utils` | 135 | Shared utilities for image/3D generation clients. |
| `core.integrations.image_gen` | 433 | Character image & 3-D model generation tool for AnimaWorks. |
| `core.integrations.local_llm` | 542 | AnimaWorks local LLM tool -- Ollama API client. |
| `core.integrations.notion` | 825 | Notion integration for AnimaWorks. |
| `core.integrations.slack` | 245 | Slack integration for AnimaWorks. |
| `core.integrations.transcribe` | 420 | AnimaWorks transcribe tool -- Whisper speech-to-text with LLM refinement. |
| `core.integrations.web_search` | 399 | Web Search tool for AnimaWorks. |
| `core.integrations.x_search` | 335 | X (Twitter) Search tool for AnimaWorks. |

## `core.integrations.image`

—

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.integrations.image` | 85 | Image and 3D generation API clients and shared constants. |

## `core.lifecycle`

anima の起動、停止、初期化のライフサイクル。

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.lifecycle` | 18 | — |
| `core.lifecycle.anima_merge.content_refs` | 386 | — |
| `core.lifecycle.anima_merge.credential_refs` | 65 | — |
| `core.lifecycle.anima_merge.external_refs` | 435 | — |
| `core.lifecycle.anima_merge.finalize` | 440 | — |
| `core.lifecycle.anima_merge.journal` | 188 | — |
| `core.lifecycle.anima_merge.service` | 1695 | — |
| `core.lifecycle.anima_merge.task_refs` | 402 | — |
| `core.lifecycle.anima_merge.taskboard_refs` | 162 | — |
| `core.lifecycle.anima_merge.verification` | 268 | — |
| `core.lifecycle.knowledge_correction` | 127 | — |
| `core.lifecycle.system_consolidation` | 279 | — |

## `core.lifecycle.anima_merge`

—

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.lifecycle.anima_merge` | 22 | — |

## `core.mcp`

Model Context Protocol サーバーとクライアント。

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.mcp` | 0 | — |
| `core.mcp.server` | 739 | — |
| `core.mcp.trigger_tools` | 84 | — |

## `core.memory`

会話・エピソード記憶の保存、検索、整理。

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.memory` | 22 | — |
| `core.memory._io（非公開）` | 76 | — |
| `core.memory._llm_parse（非公開）` | 200 | Shared LLM-output parsing helpers for the memory pipeline. |
| `core.memory._llm_utils（非公開）` | 885 | Shared LLM helper utilities for memory-management modules. |
| `core.memory.activity.audit` | 290 | — |
| `core.memory.activity.conversation` | 518 | — |
| `core.memory.activity.format` | 567 | — |
| `core.memory.activity.logger` | 585 | — |
| `core.memory.activity.models` | 202 | — |
| `core.memory.activity.replay` | 537 | — |
| `core.memory.activity.rotation` | 187 | — |
| `core.memory.activity.timeline` | 348 | — |
| `core.memory.config_reader` | 31 | — |
| `core.memory.conversation.compression` | 321 | Compression logic for conversation memory. |
| `core.memory.conversation.finalize` | 477 | Session finalization for conversation memory. |
| `core.memory.conversation.memory` | 327 | Conversation memory (会話記憶 / ワーキングメモリ) management. |
| `core.memory.conversation.models` | 149 | Data classes and constants for conversation memory. |
| `core.memory.conversation.prompt` | 427 | Prompt building functions for conversation memory. |
| `core.memory.conversation.shortterm` | 369 | Short-term memory (短期記憶) management. |
| `core.memory.conversation.state_update` | 49 | State update functions for conversation memory finalization. |
| `core.memory.conversation.streaming_journal` | 473 | — |
| `core.memory.facts.config` | 110 | — |
| `core.memory.facts.entity_index` | 452 | — |
| `core.memory.facts.extraction` | 435 | — |
| `core.memory.facts.extractor` | 328 | LLM-based entity and fact extraction pipeline. |
| `core.memory.facts.invalidation` | 497 | — |
| `core.memory.facts.invalidation_llm` | 109 | — |
| `core.memory.facts.observability` | 41 | — |
| `core.memory.facts.ontology` | 241 | Pydantic models for entity / fact extraction results. |
| `core.memory.facts.prompts.en` | 77 | English prompts for entity / fact extraction. |
| `core.memory.facts.prompts.ja` | 78 | Japanese prompts for entity / fact extraction. |
| `core.memory.facts.store` | 460 | — |
| `core.memory.frontmatter` | 430 | — |
| `core.memory.maintenance.background_review` | 554 | — |
| `core.memory.maintenance.consolidation` | 926 | — |
| `core.memory.maintenance.cron_logger` | 159 | — |
| `core.memory.maintenance.distillation` | 542 | — |
| `core.memory.maintenance.forgetting` | 507 | — |
| `core.memory.maintenance.housekeeping` | 1691 | — |
| `core.memory.maintenance.hygiene` | 75 | — |
| `core.memory.maintenance.reconsolidation` | 842 | — |
| `core.memory.maintenance.resolution_tracker` | 61 | — |
| `core.memory.manager` | 675 | — |
| `core.memory.peer_profiles` | 37 | — |
| `core.memory.priming.channel_a` | 70 | — |
| `core.memory.priming.channel_b` | 534 | — |
| `core.memory.priming.channel_c` | 618 | — |
| `core.memory.priming.channel_e` | 203 | — |
| `core.memory.priming.channel_f` | 215 | — |
| `core.memory.priming.constants` | 92 | — |
| `core.memory.priming.engine` | 666 | — |
| `core.memory.priming.format` | 119 | — |
| `core.memory.priming.items` | 59 | — |
| `core.memory.priming.outbound` | 142 | — |
| `core.memory.priming.policy` | 39 | — |
| `core.memory.priming.result` | 72 | — |
| `core.memory.priming.utils` | 303 | — |
| `core.memory.rag.cli_access` | 231 | CLI access to phase3 vector stores through the active owner or server. |
| `core.memory.rag.contextual_header` | 163 | — |
| `core.memory.rag.direct_access` | 12 | — |
| `core.memory.rag.embedding` | 396 | — |
| `core.memory.rag.endpoints` | 78 | — |
| `core.memory.rag.entity_graph` | 319 | — |
| `core.memory.rag.episode_time` | 46 | — |
| `core.memory.rag.exclusion` | 38 | — |
| `core.memory.rag.facts_chunker` | 100 | — |
| `core.memory.rag.graph` | 813 | — |
| `core.memory.rag.http_store` | 13 | — |
| `core.memory.rag.index_signature` | 27 | Compatibility diagnostics for an existing embedding index signature. |
| `core.memory.rag.indexer` | 1593 | — |
| `core.memory.rag.indexer_delete` | 135 | — |
| `core.memory.rag.owner_lock` | 84 | Exclusive ownership lock for an anima's native vector database. |
| `core.memory.rag.repair` | 33 | — |
| `core.memory.rag.repair_rebuild` | 250 | — |
| `core.memory.rag.repair_service` | 621 | — |
| `core.memory.rag.repair_snapshot` | 152 | Private inputs and metadata publication for the existing RAG rebuild path. |
| `core.memory.rag.repair_state` | 150 | Persistent repair-state helpers for RAG auto-repair. |
| `core.memory.rag.repair_types` | 27 | — |
| `core.memory.rag.repair_utils` | 163 | — |
| `core.memory.rag.retriever` | 1080 | — |
| `core.memory.rag.shared_check_registry` | 140 | — |
| `core.memory.rag.shared_meta` | 162 | — |
| `core.memory.rag.singleton` | 47 | — |
| `core.memory.rag.sqlite_health` | 359 | — |
| `core.memory.rag.store` | 788 | — |
| `core.memory.rag.vector_client` | 491 | — |
| `core.memory.rag.vector_ops` | 82 | Conversion between vector API requests and the MemoryService wire format. |
| `core.memory.rag.vector_registry` | 160 | — |
| `core.memory.retrieval.access_boost` | 131 | — |
| `core.memory.retrieval.bm25` | 1325 | — |
| `core.memory.retrieval.code_index` | 221 | — |
| `core.memory.retrieval.confidence_gate` | 39 | — |
| `core.memory.retrieval.entity` | 632 | — |
| `core.memory.retrieval.pipeline` | 109 | — |
| `core.memory.retrieval.query_expansion` | 496 | — |
| `core.memory.retrieval.rag_search` | 1297 | — |
| `core.memory.retrieval.reranker` | 264 | — |
| `core.memory.retrieval.rrf` | 107 | — |
| `core.memory.retrieval.search_metadata` | 108 | — |
| `core.memory.retrieval.temporal` | 196 | — |
| `core.memory.retrieval.time_expr` | 230 | — |
| `core.memory.retrieval.types` | 38 | — |
| `core.memory.retrieval.unified_search` | 895 | — |
| `core.memory.skill_metadata` | 301 | — |
| `core.memory.state_lock` | 96 | Process-safe locking for ``state/current_state.md`` updates. |

## `core.memory.activity`

—

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.memory.activity` | 24 | — |

## `core.memory.conversation`

—

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.memory.conversation` | 23 | — |

## `core.memory.facts`

—

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.memory.facts` | 37 | — |

## `core.memory.facts.prompts`

—

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.memory.facts.prompts` | 3 | — |

## `core.memory.maintenance`

—

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.memory.maintenance` | 6 | — |

## `core.memory.priming`

—

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.memory.priming` | 50 | Priming layer - automatic memory retrieval (自動想起). |

## `core.memory.rag`

—

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.memory.rag` | 30 | — |

## `core.memory.retrieval`

—

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.memory.retrieval` | 23 | — |

## `core.messaging`

anima 間および外部とのメッセージ配送。

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.messaging` | 6 | — |
| `core.messaging.cascade_limiter` | 224 | — |
| `core.messaging.discord_webhooks` | 296 | — |
| `core.messaging.meeting_room_store` | 130 | — |
| `core.messaging.messenger` | 1094 | — |
| `core.messaging.outbound` | 406 | — |
| `core.messaging.outbound_auto` | 393 | — |

## `core.migrations`

実行時データ形式の段階的な移行。

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.migrations` | 21 | — |
| `core.migrations.legacy_flat_skills` | 241 | — |
| `core.migrations.registry` | 149 | — |
| `core.migrations.steps` | 2276 | Migration step implementations for AnimaWorks runtime data. |
| `core.migrations.template_sync` | 133 | — |
| `core.migrations.tool_prompts` | 194 | — |
| `core.migrations.tracker` | 110 | — |

## `core.notification`

通知の生成と配送。

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.notification` | 50 | — |
| `core.notification.channels.chatwork` | 87 | — |
| `core.notification.channels.discord` | 240 | — |
| `core.notification.channels.line` | 86 | — |
| `core.notification.channels.ntfy` | 87 | — |
| `core.notification.channels.slack` | 259 | — |
| `core.notification.channels.telegram` | 91 | — |
| `core.notification.interactive` | 684 | — |
| `core.notification.notifier` | 219 | — |
| `core.notification.reply_routing` | 476 | — |
| `core.notification.slack_names` | 67 | — |

## `core.notification.channels`

—

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.notification.channels` | 7 | — |

## `core.org`

会社、部署、役割などの組織モデル。

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.org` | 6 | — |
| `core.org.company` | 1213 | Company membership and cross-company boundary helpers. |
| `core.org.company_resources` | 86 | — |
| `core.org.hierarchy` | 39 | — |
| `core.org.org_sync` | 551 | — |
| `core.org.workspace` | 234 | — |

## `core.platform`

実行エンジンや OS ごとの差異を吸収する連携層。

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.platform` | 4 | — |
| `core.platform.atomic_io` | 109 | — |
| `core.platform.claude_code` | 194 | — |
| `core.platform.codex` | 214 | — |
| `core.platform.cursor` | 60 | — |
| `core.platform.fd_limits` | 60 | — |
| `core.platform.gemini` | 42 | — |
| `core.platform.grok` | 40 | — |
| `core.platform.locks` | 125 | — |
| `core.platform.process` | 318 | — |
| `core.platform.processing_lease` | 326 | — |

## `core.prompt`

システムプロンプトとコンテキストの構築。

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.prompt` | 8 | — |
| `core.prompt.assembler` | 307 | — |
| `core.prompt.builder` | 1276 | — |
| `core.prompt.context` | 486 | Context window usage tracker. |
| `core.prompt.messaging` | 146 | — |
| `core.prompt.org_context` | 375 | — |
| `core.prompt.sections` | 52 | — |
| `core.prompt.tokens` | 92 | — |
| `core.prompt.tool_content` | 42 | — |

## `core.skills`

スキルの発見、読み込み、実行支援。

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.skills` | 135 | — |
| `core.skills.activation` | 459 | — |
| `core.skills.activation_render` | 56 | — |
| `core.skills.activation_state` | 89 | — |
| `core.skills.autolearn` | 210 | — |
| `core.skills.autolearn_lifecycle` | 52 | — |
| `core.skills.cron_context` | 251 | — |
| `core.skills.curator` | 606 | — |
| `core.skills.dense` | 226 | — |
| `core.skills.guard` | 430 | — |
| `core.skills.hub` | 551 | — |
| `core.skills.hub_storage` | 63 | — |
| `core.skills.index` | 613 | — |
| `core.skills.ledger` | 404 | — |
| `core.skills.loader` | 176 | — |
| `core.skills.migration._common（非公開）` | 178 | — |
| `core.skills.migration.hermes` | 513 | — |
| `core.skills.migration.hermes_format` | 170 | — |
| `core.skills.migration.openclaw` | 220 | — |
| `core.skills.migration.report` | 125 | — |
| `core.skills.models` | 384 | — |
| `core.skills.pointer_rewriter` | 221 | — |
| `core.skills.policy` | 84 | — |
| `core.skills.probation_promotion` | 163 | — |
| `core.skills.promotion` | 604 | — |
| `core.skills.promotion_approval` | 85 | — |
| `core.skills.promotion_utils` | 148 | — |
| `core.skills.reference_rewriter` | 422 | — |
| `core.skills.router` | 504 | — |
| `core.skills.router_metadata` | 85 | — |
| `core.skills.sources.github` | 152 | — |
| `core.skills.sources.local` | 81 | — |
| `core.skills.sources.url` | 51 | — |
| `core.skills.trust` | 143 | — |
| `core.skills.trust_gate` | 22 | — |
| `core.skills.usage` | 222 | — |

## `core.skills.migration`

—

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.skills.migration` | 14 | — |

## `core.skills.sources`

—

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.skills.sources` | 13 | — |

## `core.supervisor`

anima の監督、委任、実行調整。

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.supervisor` | 34 | Process isolation supervisor package. |
| `core.supervisor._mgr_health（非公開）` | 458 | Health check mixin for ProcessSupervisor. |
| `core.supervisor._mgr_rag_repair（非公開）` | 245 | Supervised RAG repair mixin for ProcessSupervisor. |
| `core.supervisor._mgr_reconcile（非公開）` | 316 | Reconciliation mixin for ProcessSupervisor. |
| `core.supervisor._mgr_scheduler（非公開）` | 1059 | System scheduler mixin for ProcessSupervisor. |
| `core.supervisor.cron_followup` | 45 | Shared command-cron follow-up policy for legacy and isolated runners. |
| `core.supervisor.event_bus` | 88 | In-process event buffer for events emitted by an anima root runner. |
| `core.supervisor.inbox_rate_limiter` | 402 | Inbox rate limiting, cascade detection, and deferred trigger management. |
| `core.supervisor.ipc` | 508 | IPC communication layer using JSON Lines over a platform-specific transport. |
| `core.supervisor.ipc_v2` | 414 | Persistent duplex IPC v2 used between an anima root and task runners. |
| `core.supervisor.manager` | 1093 | Process Supervisor - Manages lifecycle of Anima child processes. |
| `core.supervisor.memory_service` | 765 | Root-owned vector memory service. |
| `core.supervisor.process_handle` | 767 | Process handle for managing child Anima processes. |
| `core.supervisor.restart_state` | 169 | Unified restart state machine for ProcessSupervisor. |
| `core.supervisor.runner` | 1248 | Child process entry point for Anima subprocess. |
| `core.supervisor.schedule_parser` | 484 | — |
| `core.supervisor.scheduler_manager` | 1140 | APScheduler management for heartbeat and cron tasks. |
| `core.supervisor.streaming_handler` | 439 | Streaming IPC message handler. |
| `core.supervisor.task_runner` | 952 | Disposable task runner entry point. |
| `core.supervisor.task_runner_supervisor` | 1104 | Root-side lifecycle manager for disposable task runner processes. |
| `core.supervisor.transport` | 236 | Transport helpers for IPC server/client communication. |

## `core.tasks`

タスクの登録、状態管理、実行制御。

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.tasks` | 1 | Task queue, task board, delegated/background task execution and external task sources. |
| `core.tasks.background` | 604 | — |
| `core.tasks.board.board_actions` | 237 | — |
| `core.tasks.board.housekeeping` | 185 | — |
| `core.tasks.board.models` | 37 | Pydantic models for the single TaskBoard view (read straight from TaskStore). |
| `core.tasks.board.notices` | 119 | — |
| `core.tasks.board.readiness` | 31 | Read-only boundary between legacy task files and canonical execution. |
| `core.tasks.board.tasks` | 1247 | Durable execution records; the single source of truth for the TaskBoard. |
| `core.tasks.board.view` | 118 | Single TaskBoard view built directly from the canonical TaskStore. |
| `core.tasks.dispatch` | 358 | — |
| `core.tasks.external.collector` | 209 | Multi-source external tasks collector with per-source fault isolation. |
| `core.tasks.external.models` | 47 | Data models for the external tasks snapshot store. |
| `core.tasks.external.sources.chatwork` | 228 | Chatwork external tasks collector (open my-tasks + unreplied To). |
| `core.tasks.external.sources.github` | 182 | GitHub external tasks collector via ``gh`` CLI. |
| `core.tasks.external.sources.gmail` | 124 | Gmail external tasks collector (unread inbox, last 7 days). |
| `core.tasks.external.sources.slack` | 184 | Slack external tasks collector (unreplied mentions via message cache). |
| `core.tasks.external.store` | 51 | Atomic JSON snapshot store for external tasks. |
| `core.tasks.pending_executor` | 1850 | Pending task watcher and executor. |
| `core.tasks.pending_housekeeping` | 49 | — |
| `core.tasks.queue` | 495 | — |
| `core.tasks.wake` | 70 | Cross-process wake fan-out for the PendingTaskExecutor. |

## `core.tasks.board`

—

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.tasks.board` | 11 | TaskBoard: a single view read directly from the canonical TaskStore. |

## `core.tasks.external`

—

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.tasks.external` | 21 | External tasks snapshot store and multi-source collector skeleton. |

## `core.tasks.external.sources`

—

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.tasks.external.sources` | 8 | Per-source collectors for external tasks (stubs in this phase). |

## `core.tooling`

ツールのスキーマ、権限、実行基盤。

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.tooling` | 39 | — |
| `core.tooling.action_gate` | 188 | — |
| `core.tooling.codex_command_hook` | 93 | Codex ``PreToolUse`` hook: deny shell commands by the shared command policy. |
| `core.tooling.command_policy` | 455 | — |
| `core.tooling.dispatch` | 255 | — |
| `core.tooling.handler` | 865 | — |
| `core.tooling.handler_base` | 367 | — |
| `core.tooling.handler_comms` | 902 | — |
| `core.tooling.handler_create_anima` | 237 | — |
| `core.tooling.handler_delegation` | 260 | — |
| `core.tooling.handler_files` | 1220 | — |
| `core.tooling.handler_memory` | 1257 | — |
| `core.tooling.handler_org` | 39 | — |
| `core.tooling.handler_org_dashboard` | 199 | — |
| `core.tooling.handler_perms` | 432 | — |
| `core.tooling.handler_skills` | 879 | — |
| `core.tooling.handler_subordinate_control` | 417 | — |
| `core.tooling.handler_workspace` | 254 | — |
| `core.tooling.org_helpers` | 154 | — |
| `core.tooling.permissions` | 331 | — |
| `core.tooling.schemas.admin` | 238 | — |
| `core.tooling.schemas.builder` | 155 | — |
| `core.tooling.schemas.channel` | 118 | — |
| `core.tooling.schemas.converters` | 27 | — |
| `core.tooling.schemas.loader` | 101 | — |
| `core.tooling.schemas.memory` | 228 | — |
| `core.tooling.schemas.notification` | 67 | — |
| `core.tooling.schemas.session_todo` | 62 | — |
| `core.tooling.schemas.skill` | 306 | — |
| `core.tooling.schemas.supervisor` | 331 | — |
| `core.tooling.schemas.task` | 180 | — |
| `core.tooling.schemas.workspace` | 46 | — |
| `core.tooling.skill_creator` | 120 | — |
| `core.tooling.skill_promotion_tool` | 176 | — |
| `core.tooling.standalone` | 187 | — |

## `core.tooling.schemas`

—

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.tooling.schemas` | 79 | Canonical tool schema definitions and format converters. |

## `core.tools`

内部で利用するツール実装。

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.tools` | 58 | — |

## `core.usage`

LLM 利用量とコストの記録・集計。

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.usage` | 1 | Token usage accounting and per-Anima token budgets. |
| `core.usage.token_budget` | 55 | — |
| `core.usage.token_usage` | 498 | — |

## `core.voice`

音声入出力と音声対話。

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `core.voice` | 7 | Voice chat subsystem — STT, TTS, and session orchestration. |
| `core.voice.front` | 365 | Voice front lane — lightweight speech-first chat path via a local LLM. |
| `core.voice.sentence_splitter` | 73 | Japanese-aware sentence splitting for streaming TTS. |
| `core.voice.session` | 1835 | Voice session — STT -> Chat -> TTS orchestration. |
| `core.voice.stt` | 131 | Voice STT — in-memory PCM transcription via faster-whisper. |
| `core.voice.stt_stream` | 300 | Streaming STT — rolling buffer + LocalAgreement-2 prefix commitment. |
| `core.voice.tts_base` | 61 | TTS abstract base — provider interface and config. |
| `core.voice.tts_elevenlabs` | 133 | ElevenLabs TTS provider — REST API streaming. |
| `core.voice.tts_factory` | 47 | TTS provider factory. |
| `core.voice.tts_gemini` | 183 | Gemini TTS provider — Gemini API Interactions endpoint (SSE streaming). |
| `core.voice.tts_irodori` | 79 | Irodori-TTS provider — HTTP API. |
| `core.voice.tts_sbv2` | 112 | Style-BERT-VITS2 / AivisSpeech TTS provider. |
| `core.voice.tts_voicevox` | 110 | VOICEVOX TTS provider — Engine HTTP API. |

## `server`

—

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `server` | 7 | — |
| `server.app` | 1412 | — |
| `server.events` | 56 | — |
| `server.internal_auth` | 232 | — |
| `server.localhost` | 86 | — |
| `server.reload_manager` | 115 | — |
| `server.room_manager` | 540 | Meeting room lifecycle, orchestration, and minutes generation. |
| `server.stream_registry` | 493 | — |
| `server.websocket` | 165 | — |

## `server.gateways`

—

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `server.gateways` | 1 | Inbound chat/meeting gateways (Slack, Discord, Zoom, GitHub) run by the server. |
| `server.gateways.discord_channel_sync` | 271 | — |
| `server.gateways.discord_gateway` | 731 | — |
| `server.gateways.github_gateway` | 534 | — |
| `server.gateways.slack_channel_sync` | 492 | — |
| `server.gateways.slack_interactive` | 239 | — |
| `server.gateways.slack_socket` | 1081 | — |
| `server.gateways.zoom_gateway` | 700 | — |

## `server.routes`

—

| モジュール | 行数 | docstring 1行目 |
|---|---:|---|
| `server.routes` | 61 | — |
| `server.routes.animas` | 900 | — |
| `server.routes.approve` | 89 | — |
| `server.routes.assets` | 1595 | — |
| `server.routes.auth` | 133 | — |
| `server.routes.channels` | 493 | — |
| `server.routes.chat` | 452 | — |
| `server.routes.chat_chunk_handler` | 288 | — |
| `server.routes.chat_emotion` | 8 | — |
| `server.routes.chat_files` | 231 | — |
| `server.routes.chat_images` | 108 | — |
| `server.routes.chat_models` | 59 | — |
| `server.routes.chat_producer` | 376 | — |
| `server.routes.chat_resume` | 114 | — |
| `server.routes.chat_ui_state` | 103 | — |
| `server.routes.chat_ws_effects` | 55 | — |
| `server.routes.config_routes` | 520 | — |
| `server.routes.external_tasks` | 261 | — |
| `server.routes.internal` | 1013 | — |
| `server.routes.logs_routes` | 213 | — |
| `server.routes.media_proxy` | 186 | — |
| `server.routes.memory_routes` | 459 | — |
| `server.routes.room` | 443 | Meeting room API routes with SSE streaming. |
| `server.routes.sessions` | 302 | — |
| `server.routes.setup` | 609 | — |
| `server.routes.skills` | 132 | — |
| `server.routes.system` | 1200 | — |
| `server.routes.taskboard` | 235 | — |
| `server.routes.usage_routes` | 847 | — |
| `server.routes.users` | 279 | — |
| `server.routes.voice` | 257 | Voice chat WebSocket endpoint. |
| `server.routes.webhooks` | 502 | — |
| `server.routes.websocket_route` | 46 | — |
