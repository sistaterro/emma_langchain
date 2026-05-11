# AGENTS.md

## Purpose

This file describes the recommended way to work in this repository for future agents, maintainers, and contributors. The goal is not to impose an idealized architecture, but to capture how the project is structured today and what the safest way is to evolve it.

When the request is to "update documentation", the expected scope in this project is:

- `README.md`
- `ui/Docs.html`
- `AGENTS.md`

## Project Summary

This branch is a rebuild branch for experimenting with a LangChain/LangGraph-style backend.

The original local chat application with RAG has been intentionally hollowed out in `server.py`. The current active backend is only a FastAPI endpoint shell with request models and `pass` bodies. It is expected to fail functionally until the new implementation is rebuilt.

The pre-rebuild backend was copied to:

- `server_legacy.py`

Use `server_legacy.py` as the reference for the previous behavior, data flow, endpoints, permissions, persistence rules, and RAG pipeline. Do not treat it as active runtime code unless the user explicitly asks to restore or port from it.

The original system implemented:

- FastAPI backend.
- Static frontend in `ui/*.html`.
- Main persistence in SQLite (`emma.db`).
- Source RAG files in `files/`.
- Chunks and embeddings in `chunks/`.
- Chat audit logs in `logs/chat_audit/`.
- Ollama integration for generation and auxiliary tasks.

The rebuild goal is to replace the monolithic backend with a cleaner architecture, likely centered around LangChain/LangGraph or a similarly explicit pipeline, while preserving the local/offline-first nature of the app.

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
  - In this branch, it is intentionally reduced to endpoint stubs.
  - Keep it as the HTTP boundary and avoid rebuilding the entire monolith inside it.

- `server_legacy.py`
  - Frozen reference copy of the pre-rebuild backend.
  - Contains the old auth, conversations, chat, files, indexing, retrieval, inconsistency detection, and auditing logic.
  - Port behavior from here intentionally and incrementally; do not casually edit it.

- `prompts.py`
  - Canonical location for active system prompts.
  - Use pure functions such as `build_*_prompt(...)`.
  - Avoid embedding long prompts back into `server.py`.

- `ui/index.html`
  - Main home screen.
  - Should reflect visible permissions and available entry points by role.

- `ui/chat.html`
  - Main chat client.
  - Pay close attention to local state, rendering, and DOM cleanup when deleting or recreating conversations.

- `ui/upload.html`
  - RAG management screen.
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
- clear names such as `build_rag_prompt`, `build_route_prompt`, `build_safety_prompt`.

Avoid prompt classes without real state.

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
- Build the new backend in small modules instead of recreating a large `server.py`.
- If a change can be isolated in a helper function or module, do it.
- If a text or rule is hard to locate, move it to a canonical place.
- Keep names consistent with the current domain: `global`, `mine`, `owner_id`, `role`, `is_active`, and so on.
- Do not introduce empty abstractions such as managers or state-less classes if simple functions are enough.
- If adding LangChain or LangGraph, keep framework integration behind a thin internal boundary so endpoint code remains easy to read and test.

## UX And Frontend

- Preserve the current visual language unless the goal is explicitly to redesign it.
- Solve responsiveness with measured, concrete changes, not complete rewrites.
- When cards or grids are conditionally shown by role, ensure stable centering and layout even when the number of visible items changes.
- If a screen does not apply to a role, hide it and block direct access when appropriate.

## Execution And Verification

Recommended workflow:

- use the local `.venv`;
- expect functional endpoint tests from the legacy app to fail until endpoints are rebuilt;
- validate quick syntax with:
  - `.\.venv\Scripts\python.exe -m py_compile server.py prompts.py server_legacy.py`
- when a rebuilt endpoint becomes functional, add or update focused tests for that endpoint before relying on manual UI checks.

Current automated tests:

- `tests/test_permissions.py` was written for the pre-rebuild backend contract. In this branch it documents intended permission behavior, but it is not expected to pass while `server.py` endpoints are still stubs.

Useful manual smoke tests after changes:

- login with `admin`, `user`, and `read_only`;
- correct card visibility in `index.html`;
- upload and delete of user-owned RAGs;
- `read_only` restrictions;
- user management from admin;
- chat creation, deletion, and recreation;
- index consistency when a file is deleted.

## Known Technical Debt

These debt items may exist consciously and should not be "fixed" without aligning scope first:

- `first use` flow and forced initial password change;
- `server.py` remains monolithic;
- parts of the admin UI may still need cleanup;
- `emma.db` often reflects local working state, not only schema.

## What To Do When Inheriting This Repo

Recommended order to understand it:

1. Read `server.py` to see the active endpoint shell.
2. Read `server_legacy.py` to understand the previous behavior and data flow.
3. Read `prompts.py` to understand model behavior that may still be worth preserving.
4. Review `ui/index.html`, `ui/chat.html`, `ui/upload.html`, and `ui/admin.html` to understand frontend expectations.
5. Confirm the real schema in `emma.db`.
6. Review `files/`, `chunks/`, and `logs/chat_audit/` to understand auxiliary persistence.

Suggested rebuild order:

1. Restore auth and `/auth/me`.
2. Restore admin/user role enforcement.
3. Restore conversation persistence.
4. Add the new model/RAG pipeline boundary.
5. Rebuild upload, indexing, retrieval, and chat in small slices.
6. Re-enable or rewrite tests as each slice returns.

## General Criterion

The best contribution in this project is usually to:

- make important things easier to find;
- harden backend behavior before polishing frontend behavior;
- port legacy behavior deliberately instead of copying the old monolith back into place;
- reduce surprises;
- and leave each change easier to understand than before.
