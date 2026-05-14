# AGENTS.md

## Purpose

This file describes the recommended way to work in this repository for future agents, maintainers, and contributors. The goal is not to impose an idealized architecture, but to capture how the project is structured today and what the safest way is to evolve it.

When the request is to "update documentation", the expected scope in this project is:

- `README.md`
- `ui/Docs.html`
- `AGENTS.md`

## Project Summary

This branch is a rebuild branch for a LangChain-centered backend.

The active backend in `server.py` has been partially rebuilt. It now includes auth, role enforcement, user management, conversation persistence, file upload/delete/download, RAG chunk ingestion, inconsistency detection, provider/model selection, and chat generation through LangChain chat model integrations.

The pre-rebuild backend was copied to:

- `server_legacy.py`

Use `server_legacy.py` only as a historical reference for previous behavior, data flow, endpoints, permissions, persistence rules, and the old RAG pipeline. It is not active runtime code and does not need to be preserved indefinitely.

The original system implemented:

- FastAPI backend.
- Static frontend in `ui/*.html`.
- Main persistence in SQLite (`emma.db`).
- Source RAG files in `files/`.
- Chunks in `chunks/`.
- Chat audit logs in `logs/chat_audit/`.
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

- `server_legacy.py`
  - Historical reference copy of the pre-rebuild backend.
  - Contains the old auth, conversations, chat, files, indexing, retrieval, inconsistency detection, and auditing logic.
  - Do not treat it as active code. It may be deleted later once the rebuild has enough coverage and confidence.

- `prompts.py`
  - Canonical location for active system prompts.
  - Currently contains only active prompts: `build_inconsistency_prompt(...)` and `build_rag_prompt(...)`.
  - Do not reintroduce legacy routing prompts for "most relevant" files unless the RAG strategy changes again.

- `ui/index.html`
  - Main home screen.
  - Should reflect visible permissions and available entry points by role.

- `ui/chat.html`
  - Main chat client.
  - Uses the backend-provided model catalog and sends the selected model id to `/chat`.
  - Pay close attention to local state, rendering, and DOM cleanup when deleting or recreating conversations.

- `ui/upload.html`
  - RAG management screen.
  - Shows indexed chunks and persisted inconsistency results from `/files`.
  - The frontend may hide options by role, but the backend must remain the source of truth.

- `ui/admin.html`
  - Administrative UI.
  - Several parts were historically mocked; always verify what is connected to real backend behavior and what is not.

- `emma.db`
  - Local SQLite database.
  - Do not casually commit changes that only come from local usage or ephemeral data.

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

- check whether the behavior exists only in `server_legacy.py`;
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

Current RAG strategy deliberately does not route to "probable" files or select top-k chunks. Chat loads all visible chunks and lets the selected model reason over the full visible context.

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
- If a text or rule is hard to locate, move it to a canonical place.
- Keep names consistent with the current domain: `global`, `mine`, `owner_id`, `role`, `is_active`, and so on.
- Do not introduce empty abstractions such as managers or state-less classes if simple functions are enough.
- If adding LangChain or LangGraph, keep framework integration behind a thin internal boundary so endpoint code remains easy to read and test.
- Model generation should go through `generate_ai_reply(...)` and the LangChain model factory. Do not call provider REST APIs directly from endpoint code.
- Keep API keys server-side only. `/health` may report available providers/models, but must never return secret values.
- RAG ingestion writes chunks as JSON only. Embeddings and `.npy` files are not part of the current rebuilt flow.
- Inconsistency detection is asynchronous after upload and persisted in `conflicts_index.json`; UI should poll `/files` instead of relying only on the immediate upload response.

## UX And Frontend

- Preserve the current visual language unless the goal is explicitly to redesign it.
- Solve responsiveness with measured, concrete changes, not complete rewrites.
- When cards or grids are conditionally shown by role, ensure stable centering and layout even when the number of visible items changes.
- If a screen does not apply to a role, hide it and block direct access when appropriate.

## Execution And Verification

Recommended workflow:

- use the local `.venv`;
- run the test sequencer before handing off backend changes:
  - `.\test.bat`
- or run the suite manually:
  - `.\.venv\Scripts\python.exe -m unittest discover tests`
- validate quick syntax manually when needed:
  - `.\.venv\Scripts\python.exe -m py_compile server.py prompts.py server_legacy.py`
- tests must mock external model/server calls. Do not make Gemini/OpenAI/Anthropic calls from automated tests.

Current automated tests:

- `tests/test_permissions.py` covers role restrictions for admin/file-management behavior.
- `tests/test_rag_pipeline.py` covers chunk ingestion, file indexes, mocked inconsistency persistence, and chat prompt construction with all visible chunks.
- `tests/test_core_endpoints.py` covers auth, admin user management, conversation CRUD, file upload/list/download/delete, model catalog behavior, and LangChain missing-dependency errors.

Useful manual smoke tests after changes:

- login with `admin`, `user`, and `read_only`;
- correct card visibility in `index.html`;
- upload and delete of user-owned RAGs;
- upload two contradictory RAGs and confirm `upload.html` shows conflicts after polling;
- `read_only` restrictions;
- user management from admin;
- chat creation, deletion, and recreation;
- ask chat a question that requires multiple RAGs and confirm it answers from all visible chunks;
- index and conflict consistency when a file is deleted.

## Known Technical Debt

These debt items may exist consciously and should not be "fixed" without aligning scope first:

- `first use` flow and forced initial password change;
- `server.py` remains monolithic;
- parts of the admin UI may still need cleanup;
- `emma.db` often reflects local working state, not only schema.
- FastAPI `@app.on_event("startup")` emits a deprecation warning during tests; migrate to lifespan when convenient.
- Streaming currently wraps the full model response into JSON-line chunks after generation rather than streaming provider tokens directly.
- `server_legacy.py` still exists only as reference and can be removed later.

## What To Do When Inheriting This Repo

Recommended order to understand it:

1. Read `server.py` to see the active backend flow.
2. Read `prompts.py` to understand active model behavior.
3. Read `tests/` to understand the intended current contract.
4. Review `ui/index.html`, `ui/chat.html`, `ui/upload.html`, and `ui/admin.html` to understand frontend expectations.
5. Confirm the real schema in `emma.db`.
6. Review `files/`, `chunks/`, and `logs/chat_audit/` to understand auxiliary persistence.
7. Consult `server_legacy.py` only if you need historical context.

Current rebuild status:

1. Auth and `/auth/me`: rebuilt.
2. Admin/user role enforcement: rebuilt.
3. Conversation persistence: rebuilt.
4. Provider/model selection: rebuilt using LangChain integrations.
5. Upload, chunk ingestion, and inconsistency detection: rebuilt.
6. Chat: rebuilt using all visible chunks instead of legacy top-k retrieval.
7. Tests: active and expected to pass.

Likely next work:

- split `server.py` into small modules once behavior stabilizes;
- migrate startup to FastAPI lifespan;
- decide whether to remove `server_legacy.py`;
- improve direct provider-token streaming if needed.

## General Criterion

The best contribution in this project is usually to:

- make important things easier to find;
- harden backend behavior before polishing frontend behavior;
- port legacy behavior deliberately instead of copying the old monolith back into place;
- reduce surprises;
- and leave each change easier to understand than before.
