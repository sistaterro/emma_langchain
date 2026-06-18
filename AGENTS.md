# AGENTS.md

## Purpose

This file describes the recommended way to work in this repository for future agents, maintainers, and contributors. The goal is not to impose an idealized architecture, but to capture how the project is structured today and what the safest way is to evolve it.

When the request is to "update documentation", the expected scope in this project is:

- `README.md`
- `ui/Docs.html`
- `AGENTS.md`

Keep these three documents aligned with the active Emma 2.0 behavior: LangChain-backed providers, JSON-only RAG chunks, streaming chat, RAG prompt-injection screening, audit logs, exception logs, and exclusion of high-risk RAGs from chat context.

## Project Summary

This branch is a rebuild branch for a LangChain-centered backend.

The active backend in `server.py` has been partially rebuilt. It now includes auth, role enforcement, user management, conversation persistence, file upload/delete/download, RAG chunk ingestion, inconsistency detection, provider/model selection, and chat generation through LangChain chat model integrations.

The original system implemented:

- FastAPI backend.
- Static frontend in `ui/*.html`.
- Main persistence in SQLite (`emma.db`).
- Source RAG files in `files/`.
- Chunks in `chunks/`.
- Chat audit logs in `logs/chat_audit/`.
- RAG security audit logs in `logs/rag_audit/`.
- Unhandled exception logs in `logs/exception_log/`.
- LangChain chat model integrations for Gemini, OpenAI/GPT, and Anthropic/Sonnet generation.

The rebuild goal is to keep endpoint behavior explicit while moving model calls behind a thin LangChain boundary. The app remains local-first for persistence and RAG storage, but generation can use configured external model APIs.

## Working Principles

- Understand the current flow before refactoring. This repo contains several pragmatic decisions and known technical debt; do not assume something is "wrong" just because it is not heavily modularized.
- Prefer small, safe, reversible changes. Avoid large refactors if the problem can be solved with a localized improvement.
- Keep sensitive logic in the backend. Permissions, validations, and access rules should not rely only on the frontend.
- The frontend should remain thin. The pages in `ui/` call the API with `fetch` and should not absorb complex business rules.
- Preserve the local/offline-first nature of the project. Do not introduce external infrastructure dependencies unless explicitly necessary.
- Prioritize real maintainability. If a rule, prompt, or flow is hard to find, centralize it.

## Structure And Responsibilities

- `server.py`
  - Main application entry point.
  - Active FastAPI backend.
  - Contains the current rebuilt auth, permissions, conversations, file management, RAG chunking, inconsistency detection, model catalog, LangChain provider factory, and chat endpoint.
  - Keep it as the HTTP boundary. If future changes grow large, move cohesive pieces into small modules instead of expanding it indefinitely.

- `prompts.py`
  - Canonical location for active system prompts.
  - Currently contains only active prompts: `build_inconsistency_prompt(...)` and `build_rag_prompt(...)`.
  - Do not reintroduce routing prompts for "most relevant" files unless the RAG strategy changes again.

- `rag_security.py`
  - Canonical location for RAG prompt-injection security analysis, security index persistence, high-risk exclusion decisions, and suspicious RAG audit log creation/rotation.
  - Keep multilingual model-based RAG security review here instead of growing `server.py`.

- `ui/index.html`
  - Main home screen.
  - Should reflect visible permissions and available entry points by role.
  - Card order should stay: Chat Emma, Upload Files, Admin Panel, Documentation. The admin card is role-gated, but when visible the desktop/tablet-wide layout should be a stable 2x2 grid.

- `ui/chat.html`
  - Main chat client.
  - Uses the backend-provided model catalog and sends the selected model id to `/chat`.
  - Supports real incremental streaming from `/chat` when `stream: true`; the UI creates the assistant message as the stream starts and updates it chunk by chunk.
  - Pay close attention to local state, rendering, and DOM cleanup when deleting or recreating conversations.

- `ui/chat_evil_emma.html`
  - Alternate Evil Emma chat client.
  - Should keep its red/black visual language, but stay functionally aligned with `ui/chat.html`: auth, backend conversations, model catalog, `/chat` requests with `conversation_id`, and incremental streaming.
  - Uses `ui/assets/evil-emma-favicon.svg` instead of the default blue Emma favicon.

- `ui/upload.html`
  - RAG management screen.
  - Shows indexed chunks, persisted inconsistency results, and prompt-injection security status from `/files`.
  - The frontend may hide options by role, but the backend must remain the source of truth.

- `ui/admin.html`
  - Administrative UI.
  - Several parts were historically mocked; always verify what is connected to real backend behavior and what is not.

- `emma.db`
  - Local SQLite runtime database generated by `init_db()` on first run.
  - Ignored by Git and should not be versioned.

- `run.bat`
  - Windows startup script.
  - Should separate environment validation from execution as much as possible.

- `test.bat`
  - Windows test sequencer.
  - Runs syntax checks and the full unittest suite.

- `api_keys.json`
  - Local secret file for model provider API keys.
  - Ignored by Git. Do not print, commit, or expose its contents to the frontend.
  - Expected shape:
    ```json
    {
      "gemini": { "api_key": "..." },
      "openai": { "api_key": "..." },
      "anthropic": { "api_key": "..." }
    }
    ```

## Programming Methodology

### 1. Read First, Then Move Things

Before touching a feature:

- locate the backend endpoint involved;
- locate the HTML screen that consumes it;
- review whether there is persisted state in SQLite, JSON indexes, or files on disk;
- confirm whether any async processing is involved.

In this repo, many bugs come from interaction between frontend state, files, and asynchronous indexing, not just from one isolated function.

### 2. Backend First For Permissions

If user or role behavior changes:

- implement the restriction in the backend first;
- then hide or adapt the UI;
- never rely only on visual controls.

Current project roles:

- `admin`: can manage users and all RAGs.
- `user`: can use chat and manage their own `mine` RAGs.
- `read_only`: can only use chat and must not see or use upload.

### 3. Centralized Prompts

Prompts should live in `prompts.py`, not be distributed across multiple files.

Recommended convention:

- constants for shared rules;
- builder functions for dynamic prompts;
- clear names such as `build_rag_prompt` and `build_inconsistency_prompt`.

Avoid prompt classes without real state.

Current RAG strategy deliberately does not route to "probable" files or select top-k chunks. Chat loads all visible safe chunks and lets the selected model reason over that full visible context. RAGs marked with `security.risk == "high"` are excluded from chat context. If no visible safe chunks are available, chat must not let the provider answer from general knowledge; the backend returns a deterministic `[NO INFO]` response instead.

### 4. Protect Visual State

The frontend is simple, so it needs extra care:

- if a view is hidden, clear the DOM if it may reappear with stale state;
- if a conversation or selection is deleted, reset local state explicitly;
- test scenarios with "only one item", because visual leftovers often appear there.

### 5. Defend Against Async Races

File indexing and other background tasks must assume that users can delete or modify resources while processing is still running.

Practical rule:

- before persisting derived results, verify that the original resource still exists;
- when maintaining auxiliary JSON indexes, prune orphaned entries when appropriate.

## Implementation Conventions

- Prefer pragmatic solutions over overengineering.
- Prefer small cohesive modules for future expansion. `server.py` is currently functional but large.
- If a change can be isolated in a helper function or module, do it.
- All Python classes, functions, and async functions should include concise docstrings. New implementations must add or update docstrings as part of the same change so the code remains easy to scan and onboard.
- If a text or rule is hard to locate, move it to a canonical place.
- Keep names consistent with the current domain: `global`, `mine`, `owner_id`, `role`, `is_active`, and so on.
- Do not introduce empty abstractions such as managers or state-less classes if simple functions are enough.
- If adding LangChain or LangGraph, keep framework integration behind a thin internal boundary so endpoint code remains easy to read and test.
- Model generation should go through `generate_ai_reply(...)` and the LangChain model factory. Do not call provider REST APIs directly from endpoint code.
- Keep API keys server-side only. `/health` may report available providers/models, but must never return secret values.
- RAG ingestion writes chunks as JSON only. Embeddings and `.npy` files are not part of the current rebuilt flow.
- Inconsistency detection is asynchronous and persisted in `conflicts_index.json`.
- RAG prompt-injection detection is model-based, multilingual, lives in `rag_security.py`, runs during ingestion, and persists results in `security_index.json` next to the RAG files.
- Missing RAG security records may be created lazily by chat using the currently selected chat model before chunks are allowed into context.
- RAG security levels are `none`, `medium`, and `high`; treat `high` as dangerous for the system and `medium` as requiring review.
- Chat must not use RAG chunks whose prompt-injection security result is `high`. `visible_chat_chunk_sources(...)` is responsible for filtering them out, and it creates a missing security assessment lazily before a RAG can be used.
- Chat must return a backend-generated `[NO INFO]` response when no visible safe chunks are available. Do not call the provider for final answer content in that case; keep safety analysis and audit behavior intact.
- If a provider omits the required leading response tag, the backend must add a conservative tag before returning or persisting the reply: `[NO INFO]` with no context, `[DRIFT]` with context.
- `/files` is responsible for surfacing persisted conflict state and scheduling missing checks for indexed RAGs that have no conflict record yet.
- `/files` also surfaces persisted `security` state for prompt-injection findings.
- When deleting RAGs, prune both direct `conflicts_index.json` entries and orphaned `matches` that reference deleted files.
- When deleting RAGs, also prune `security_index.json`.
- The upload UI should poll `/files` while files are indexing or conflict checks are still marked as `checking`.
- RAG context inserted into chat prompts must be wrapped as untrusted context. Do not remove `BEGIN_UNTRUSTED_CONTEXT` / `END_UNTRUSTED_CONTEXT` delimiters without replacing them with an equivalent defense.
- Chat safety analysis uses `build_safety_prompt(...)` before generation and writes JSON audit files in `logs/chat_audit/` only for `REVIEW` or `SUSPICIOUS` messages.
- Suspicious RAG ingestion writes JSON audit files in `logs/rag_audit/`.
- Unhandled HTTP exceptions and selected background task exceptions write JSON records in `logs/exception_log/`.
- `logs/chat_audit/`, `logs/rag_audit/`, and `logs/exception_log/` rotate at 500 files, deleting the oldest batch when the limit is reached.
- Audit logs should never include API keys; keep chat records focused on user/message metadata, safety assessment, RAG context summary, and response tag/length. RAG and exception logs may include file paths, excerpts, stack traces, and context needed for debugging, but must still avoid secrets where possible.

## UX And Frontend

- Preserve the current visual language unless the goal is explicitly to redesign it.
- Solve responsiveness with measured, concrete changes, not complete rewrites.
- When cards or grids are conditionally shown by role, ensure stable centering and layout even when the number of visible items changes.
- On the home screen, preserve the 2-column card grid for wide responsive layouts so admin users see two rows with two entries.
- Home-screen entry cards should open their destination in a new browser tab/window. On secondary screens, the existing logo/status surface in the upper sidebar should be clickable and return to `ui/index.html`; do not add a separate floating home button.
- If a screen does not apply to a role, hide it and block direct access when appropriate.
- Visible UI version references currently use Emma 2.0.

## Execution And Verification

Recommended workflow:

- use the local `.venv`;
- run the test sequencer before handing off backend changes:
  - `.\test.bat`
- or run the suite manually:
  - `.\.venv\Scripts\python.exe -m unittest discover tests`
- validate quick syntax manually when needed:
  - `.\.venv\Scripts\python.exe -m py_compile server.py prompts.py`
- tests must mock external model/server calls. Do not make Gemini/OpenAI/Anthropic calls from automated tests.

Current automated tests:

- `tests/test_permissions.py` covers role restrictions for admin/file-management behavior.
- `tests/test_rag_pipeline.py` covers chunk ingestion, file indexes, mocked inconsistency persistence, clean conflict checks, orphaned conflict pruning, chat prompt construction with visible safe chunks, and exclusion of high-risk RAGs from chat context.
- `tests/test_core_endpoints.py` covers auth, admin user management, conversation CRUD, file upload/list/download/delete, model catalog behavior, LangChain missing-dependency errors, and `/chat` streaming persistence.

Useful manual smoke tests after changes:

- login with `admin`, `user`, and `read_only`;
- correct card visibility in `index.html`;
- upload and delete of user-owned RAGs;
- upload two contradictory RAGs and confirm `upload.html` shows conflicts after polling;
- upload a RAG containing prompt-injection text and confirm `upload.html` shows `PROMPT INJECTION HIGH`, `files/<user_id>/security_index.json` is updated, and `logs/rag_audit/` receives a JSON record;
- confirm high-risk RAG cards warn that they will not be used by the system, and verify chat answers do not include those RAG chunks in the prompt context;
- delete one side of a conflict and confirm the remaining file no longer shows stale conflict details;
- trigger or simulate an unhandled backend exception and confirm `logs/exception_log/` receives a detailed JSON record;
- `read_only` restrictions;
- user management from admin;
- chat creation, deletion, and recreation;
- streaming chat responses appearing incrementally in `chat.html` and `chat_evil_emma.html`;
- ask chat a question that requires multiple safe RAGs and confirm it answers from all visible safe chunks;
- index and conflict consistency when a file is deleted.

## Known Technical Debt

These debt items may exist consciously and should not be "fixed" without aligning scope first:

- `first use` flow and forced initial password change;
- `server.py` remains monolithic;
- parts of the admin UI may still need cleanup;
- Startup initialization uses FastAPI lifespan handlers.

## What To Do When Inheriting This Repo

Recommended order to understand it:

1. Read `server.py` to see the active backend flow.
2. Read `prompts.py` to understand active model behavior.
3. Read `tests/` to understand the intended current contract.
4. Review `ui/index.html`, `ui/chat.html`, `ui/chat_evil_emma.html`, `ui/upload.html`, and `ui/admin.html` to understand frontend expectations.
5. Confirm the real runtime schema through `init_db()` or a locally generated `emma.db`.
6. Review `files/`, `chunks/`, `logs/chat_audit/`, `logs/rag_audit/`, and `logs/exception_log/` to understand auxiliary persistence.

Current rebuild status:

1. Auth and `/auth/me`: rebuilt.
2. Admin/user role enforcement: rebuilt.
3. Conversation persistence: rebuilt.
4. Provider/model selection: rebuilt using LangChain integrations.
5. Upload, chunk ingestion, inconsistency detection, and RAG prompt-injection detection/auditing: rebuilt.
6. Chat: rebuilt using all visible safe chunks instead of top-k retrieval, excludes high-risk RAGs from context, returns backend-generated `[NO INFO]` when no safe context exists, enforces missing response tags conservatively, and supports real LangChain streaming for streamed requests.
7. Tests: active and expected to pass.

Likely next work:

- split `server.py` into small modules once behavior stabilizes;
- improve provider-token streaming behavior further only if a specific provider integration needs tuning.

## General Criterion

The best contribution in this project is usually to:

- make important things easier to find;
- harden backend behavior before polishing frontend behavior;
- preserve current tested behavior deliberately instead of rebuilding large monoliths;
- reduce surprises;
- and leave each change easier to understand than before.


