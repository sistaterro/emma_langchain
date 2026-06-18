import asyncio
import json
import os
import re
import secrets
import sqlite3
import traceback
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import bcrypt
import httpx
import rag_security
from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from prompts import build_inconsistency_prompt, build_rag_prompt, build_safety_prompt


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Initialize local runtime state when the FastAPI app starts."""
    initialize_runtime()
    yield


app = FastAPI(title="Emma Server", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_unhandled_http_exceptions(request, call_next):
    """Persist unexpected HTTP exceptions and disable HTML caching."""
    try:
        response = await call_next(request)
        if request.url.path.startswith("/ui/") and request.url.path.endswith(".html"):
            response.headers["Cache-Control"] = "no-store"
        return response
    except Exception as exc:
        persist_exception_log(
            exc,
            {
                "source": "http",
                "method": request.method,
                "url": str(request.url),
                "path": request.url.path,
                "query": str(request.url.query),
                "client": request.client.host if request.client else None,
            },
        )
        raise


app.mount("/ui", StaticFiles(directory="ui"), name="ui")

security = HTTPBearer(auto_error=False)

FAVICON_PATH = Path("assets/emma-favicon.svg")
DB_PATH = Path("emma.db")
LOGS_DIR = Path("logs/chat_audit")
RAG_AUDIT_DIR = Path("logs/rag_audit")
EXCEPTION_LOG_DIR = Path("logs/exception_log")
VALID_ROLES = {"admin", "user", "read_only"}

FILES_ROOT = Path("files")
CHUNKS_ROOT = Path("chunks")
GLOBAL_FILES_DIR = FILES_ROOT / "global"
GLOBAL_CHUNKS_DIR = CHUNKS_ROOT / "global"
CHUNK_MIN_WORDS = 40
CHUNK_MAX_CHARS = 4000
API_KEYS_PATH = Path("api_keys.json")
LEGACY_API_KEY_FILES = {
    "gemini": Path("api.txt"),
    "openai": Path("openai_api.txt"),
    "anthropic": Path("anthropic_api.txt"),
}
CONFLICT_CHECK_TASKS: set[str] = set()

MODEL_CATALOG = [
    {
        "id": "gemini:gemini-2.5-flash",
        "label": "Gemini 2.5 Flash",
        "provider": "gemini",
        "model": "gemini-2.5-flash",
    },
    {
        "id": "gemini:gemini-2.5-pro",
        "label": "Gemini 2.5 Pro",
        "provider": "gemini",
        "model": "gemini-2.5-pro",
    },
    {
        "id": "openai:gpt-4.1",
        "label": "GPT-4.1",
        "provider": "openai",
        "model": "gpt-4.1",
    },
    {
        "id": "openai:gpt-4.1-mini",
        "label": "GPT-4.1 Mini",
        "provider": "openai",
        "model": "gpt-4.1-mini",
    },
    {
        "id": "anthropic:claude-sonnet-4-5",
        "label": "Claude Sonnet 4.5",
        "provider": "anthropic",
        "model": "claude-sonnet-4-5",
    },
    {
        "id": "anthropic:claude-sonnet-4-0",
        "label": "Claude Sonnet 4",
        "provider": "anthropic",
        "model": "claude-sonnet-4-0",
    },
]


class LoginRequest(BaseModel):
    """Request body for username/password login."""
    username: str
    password: str


class AdminUserCreate(BaseModel):
    """Request body for creating an admin-managed user."""
    username: str
    password: str
    full_name: Optional[str] = None
    role: str = "user"


class AdminUserUpdate(BaseModel):
    """Request body for updating admin-managed user fields."""
    full_name: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None


class AdminPasswordReset(BaseModel):
    """Request body for an admin password reset."""
    password: str


class ConversationCreate(BaseModel):
    """Request body for creating a conversation."""
    title: str = "New chat"
    model: str


class ConversationTitleUpdate(BaseModel):
    """Request body for renaming a conversation."""
    title: str


class Message(BaseModel):
    """Chat message exchanged between the UI and backend."""
    role: str
    content: str


class ChatRequest(BaseModel):
    """Request body for chat generation."""
    model: str
    messages: List[Message]
    stream: bool = True
    keep_alive: Optional[str] = None
    conversation_id: Optional[str] = None


def hash_password(password: str) -> str:
    """Hash a plaintext password for SQLite storage."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    """Check a plaintext password against a stored bcrypt hash."""
    return bcrypt.checkpw(password.encode(), hashed.encode())


def get_db():
    """Open a SQLite connection configured for Emma's schema."""
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def normalize_role(role: str) -> str:
    """Normalize and validate a user role string."""
    normalized = str(role or "").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized == "readonly":
        normalized = "read_only"
    if normalized not in VALID_ROLES:
        raise HTTPException(status_code=400, detail="Invalid role")
    return normalized


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    """Add a SQLite table column if it is missing."""
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db() -> None:
    """Create or migrate the local SQLite schema and bootstrap the admin user."""
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'user',
            full_name     TEXT,
            is_active     INTEGER NOT NULL DEFAULT 1,
            last_login_at TEXT,
            created_at    TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            token      TEXT PRIMARY KEY,
            user_id    INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS conversations (
            id         TEXT PRIMARY KEY,
            user_id    INTEGER NOT NULL,
            title      TEXT NOT NULL,
            model      TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id              TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            role            TEXT NOT NULL,
            content         TEXT NOT NULL,
            created_at      TEXT NOT NULL,
            FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
        )
        """
    )
    ensure_column(conn, "users", "full_name", "TEXT")
    ensure_column(conn, "users", "is_active", "INTEGER NOT NULL DEFAULT 1")
    ensure_column(conn, "users", "last_login_at", "TEXT")
    conn.commit()

    count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if count == 0:
        conn.execute(
            "INSERT INTO users (username, password_hash, role, full_name, is_active, created_at) "
            "VALUES (?, ?, 'admin', ?, 1, ?)",
            ("admin", hash_password("admin1234"), "Administrator", datetime.utcnow().isoformat()),
        )
        conn.commit()
    conn.close()


def require_admin(user: dict) -> None:
    """Reject the request unless the current user is an admin."""
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Only admins can perform this action")


def require_upload_access(user: dict) -> None:
    """Reject users who are not allowed to manage RAG files."""
    if user["role"] == "read_only":
        raise HTTPException(status_code=403, detail="Your user cannot manage files")


def get_user_row(user_id: int) -> sqlite3.Row | None:
    """Fetch a user row by id."""
    conn = get_db()
    row = conn.execute(
        "SELECT id, username, full_name, role, is_active, created_at, last_login_at "
        "FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    conn.close()
    return row


def serialize_user_row(row: sqlite3.Row) -> dict:
    """Convert a SQLite user row into an API response dictionary."""
    return {
        "id": row["id"],
        "username": row["username"],
        "full_name": row["full_name"] or row["username"],
        "role": normalize_role(row["role"]),
        "is_active": bool(row["is_active"]),
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
    }


def ensure_admin_survives(
    target_user_id: int,
    new_role: str | None = None,
    new_is_active: bool | None = None,
    deleting: bool = False,
) -> None:
    """Prevent operations that would remove the last active admin."""
    row = get_user_row(target_user_id)
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    current_role = normalize_role(row["role"])
    current_active = bool(row["is_active"])
    resulting_role = normalize_role(new_role) if new_role is not None else current_role
    resulting_active = current_active if new_is_active is None else new_is_active
    if deleting:
        resulting_active = False
    if current_role != "admin" or (resulting_role == "admin" and resulting_active):
        return

    conn = get_db()
    active_admins = conn.execute(
        "SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = 1 AND id != ?",
        (target_user_id,),
    ).fetchone()[0]
    conn.close()
    if active_admins == 0:
        raise HTTPException(status_code=400, detail="At least one active admin must exist")


def user_files_dir(user_id: int) -> Path:
    """Return and create the per-user source-file directory."""
    path = FILES_ROOT / str(user_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def user_chunks_dir(user_id: int) -> Path:
    """Return and create the per-user chunk directory."""
    path = CHUNKS_ROOT / str(user_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_target_user_id(user: dict, owner_id: Optional[int]) -> int:
    """Resolve which user's files an upload or admin action targets."""
    if user["role"] == "admin" and owner_id is not None:
        if not get_user_row(owner_id):
            raise HTTPException(status_code=404, detail="User not found")
        return owner_id
    if owner_id is not None and owner_id != user["id"]:
        raise HTTPException(status_code=403, detail="You cannot manage another user's files")
    return user["id"]


def load_files_index(base_dir: Path) -> dict:
    """Load the human-readable file description index for a directory."""
    idx_path = base_dir / "files_index.json"
    if not idx_path.exists():
        return {}
    try:
        data = json.loads(idx_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def prune_index_entries(base_dir: Path, filename: str) -> dict:
    """Remove file-index and conflict entries for a deleted RAG stem."""
    idx_path = base_dir / filename
    if not idx_path.exists():
        return {}
    try:
        data = json.loads(idx_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    valid_stems = {path.stem for path in base_dir.glob("*.txt")}
    cleaned = {stem: value for stem, value in data.items() if stem in valid_stems}
    if cleaned != data:
        idx_path.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")
    return cleaned


def save_description_to_index(base_dir: Path, stem: str, description: str) -> None:
    """Persist a short description for an indexed RAG file."""
    if not (base_dir / f"{stem}.txt").exists():
        return
    idx_path = base_dir / "files_index.json"
    index = prune_index_entries(base_dir, "files_index.json")
    index[stem] = description
    idx_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


async def assess_rag_prompt_injection(text: str, file_name: str = "rag.txt", model: dict | None = None) -> dict:
    """Run model-based prompt-injection screening for a RAG document."""
    return await rag_security.assess_rag_prompt_injection(
        text,
        file_name,
        model,
        available_models,
        resolve_model,
        generate_ai_reply,
    )


def save_security_to_index(base_dir: Path, stem: str, assessment: dict) -> None:
    """Persist a RAG security assessment next to the source files."""
    rag_security.save_security_to_index(base_dir, stem, assessment)


def security_response(record: dict | None, status: str = "unchecked") -> dict:
    """Return a frontend-safe representation of a security record."""
    return rag_security.security_response(record, status)


async def get_or_create_rag_security_record(
    txt_path: Path,
    scope: str,
    owner_id: int | None,
    model: dict | None = None,
) -> dict:
    """Load or lazily create the security record for a RAG file."""
    return await rag_security.get_or_create_rag_security_record(
        txt_path,
        scope,
        owner_id,
        model,
        RAG_AUDIT_DIR,
        available_models,
        resolve_model,
        generate_ai_reply,
        persist_exception_log,
    )


def is_high_risk_rag_security(record: dict | None) -> bool:
    """Return whether a RAG security record must be excluded from chat."""
    return rag_security.is_high_risk_rag_security(record)


async def should_exclude_rag_from_chat(txt_path: Path, scope: str, owner_id: int | None, model: dict | None) -> bool:
    """Return whether a RAG file should be withheld from chat context."""
    return await rag_security.should_exclude_rag_from_chat(
        txt_path,
        scope,
        owner_id,
        model,
        RAG_AUDIT_DIR,
        available_models,
        resolve_model,
        generate_ai_reply,
        persist_exception_log,
    )


def rotate_rag_audit_logs(max_files: int = 500, delete_count: int = 50) -> None:
    """Keep the suspicious RAG audit directory within its retention limit."""
    rag_security.rotate_rag_audit_logs(RAG_AUDIT_DIR, max_files, delete_count)


def build_rag_audit_record(
    txt_path: Path,
    scope: str,
    owner_id: int | None,
    assessment: dict,
) -> dict:
    """Build a JSON-serializable suspicious RAG audit record."""
    return rag_security.build_rag_audit_record(txt_path, scope, owner_id, assessment)


def persist_suspicious_rag_audit_log(
    txt_path: Path,
    scope: str,
    owner_id: int | None,
    assessment: dict,
) -> None:
    """Write an audit log for a suspicious RAG assessment."""
    rag_security.persist_suspicious_rag_audit_log(RAG_AUDIT_DIR, txt_path, scope, owner_id, assessment)


def rotate_exception_logs(max_files: int = 500, delete_count: int = 50) -> None:
    """Keep exception logs within the retention limit."""
    try:
        files = sorted(EXCEPTION_LOG_DIR.glob("*.json"), key=lambda path: path.stat().st_mtime)
        if len(files) < max_files:
            return
        for path in files[:delete_count]:
            path.unlink(missing_ok=True)
    except Exception as exc:
        print(f"[exception-log] failed to rotate logs: {exc}")


def build_exception_log_record(exc: BaseException, context: dict | None = None) -> dict:
    """Build a JSON-serializable exception log record."""
    return {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "audit_type": "exception",
        "exception": {
            "type": type(exc).__name__,
            "module": type(exc).__module__,
            "message": str(exc),
            "repr": repr(exc),
            "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        },
        "context": context or {},
        "runtime": {
            "cwd": str(Path.cwd()),
            "pid": os.getpid(),
        },
    }


def persist_exception_log(exc: BaseException, context: dict | None = None) -> None:
    """Persist details for an unexpected backend exception."""
    try:
        EXCEPTION_LOG_DIR.mkdir(parents=True, exist_ok=True)
        rotate_exception_logs()
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S%fZ")
        safe_type = re.sub(r"[^\w.-]+", "_", type(exc).__name__).strip("_") or "exception"
        path = EXCEPTION_LOG_DIR / f"exception_{ts}_{safe_type}_{secrets.token_hex(4)}.json"
        record = build_exception_log_record(exc, context)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as log_exc:
        print(f"[exception-log] failed to persist exception log: {log_exc}")


def load_conflicts_index(base_dir: Path) -> dict:
    """Load persisted RAG conflict results for a directory."""
    return prune_index_entries(base_dir, "conflicts_index.json")


def conflicts_response(record: dict | None, status: str = "unchecked") -> dict:
    """Return a frontend-safe representation of a conflict record."""
    if not isinstance(record, dict):
        return {"has_any": False, "matches": [], "status": status}
    matches = record.get("matches") if isinstance(record.get("matches"), list) else []
    response = {
        "has_any": bool(record.get("has_any")) and bool(matches),
        "matches": matches,
        "status": record.get("status") or status,
    }
    if record.get("checked_at"):
        response["checked_at"] = record["checked_at"]
    return response


def conflict_match_still_exists(base_dir: Path, match: dict) -> bool:
    """Check whether a persisted conflict match still points to an existing RAG."""
    if not isinstance(match, dict):
        return False
    name = str(match.get("name") or "").strip()
    if not name:
        return False
    scope = str(match.get("scope") or "user")
    if scope == "global":
        return (GLOBAL_FILES_DIR / name).exists()
    return (base_dir / name).exists()


def prune_orphaned_conflict_matches(base_dir: Path) -> dict:
    """Remove conflict records that reference deleted RAG files."""
    idx_path = base_dir / "conflicts_index.json"
    index = load_conflicts_index(base_dir)
    changed = False
    cleaned_index = {}
    for stem, record in index.items():
        if not isinstance(record, dict):
            changed = True
            continue
        matches = record.get("matches") if isinstance(record.get("matches"), list) else []
        cleaned_matches = [
            match for match in matches if conflict_match_still_exists(base_dir, match)
        ]
        if len(cleaned_matches) != len(matches):
            changed = True
        cleaned_record = dict(record)
        cleaned_record["matches"] = cleaned_matches
        cleaned_record["has_any"] = bool(cleaned_matches)
        cleaned_index[stem] = cleaned_record
    if changed:
        idx_path.write_text(json.dumps(cleaned_index, ensure_ascii=False, indent=2), encoding="utf-8")
    return cleaned_index


def save_conflicts_to_index(base_dir: Path, stem: str, conflicts: dict) -> None:
    """Persist detected conflict results for a RAG file."""
    idx_path = base_dir / "conflicts_index.json"
    index = load_conflicts_index(base_dir)
    index[stem] = {
        "has_any": bool(conflicts.get("has_any")),
        "matches": conflicts.get("matches") if isinstance(conflicts.get("matches"), list) else [],
        "status": "checked",
        "checked_at": datetime.utcnow().isoformat(),
    }
    idx_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_document_text(text: str) -> str:
    """Normalize uploaded document text before chunking."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def is_quality_chunk(text: str) -> bool:
    """Return whether text is useful enough to keep as a chunk."""
    stripped = text.strip()
    if len(stripped) < 80:
        return False
    words = re.findall(r"\w+", stripped)
    if len(words) < 12:
        return False
    alpha_chars = sum(1 for char in stripped if char.isalpha())
    return alpha_chars / max(len(stripped), 1) >= 0.35


def split_long_chunk(text: str, max_chars: int = CHUNK_MAX_CHARS) -> list[str]:
    """Split an oversized text chunk into bounded pieces."""
    if len(text) <= max_chars:
        return [text]
    parts = []
    remaining = text.strip()
    while len(remaining) > max_chars:
        split_at = remaining.rfind("\n", 0, max_chars)
        if split_at < max_chars * 0.5:
            split_at = remaining.rfind(". ", 0, max_chars)
        if split_at < max_chars * 0.5:
            split_at = max_chars
        part = remaining[:split_at].strip()
        if part:
            parts.append(part)
        remaining = remaining[split_at:].strip()
    if remaining:
        parts.append(remaining)
    return parts


def chunk_text(text: str, min_words: int = CHUNK_MIN_WORDS) -> list[str]:
    """Split normalized document text into quality RAG chunks."""
    paragraphs = re.split(r"\n\s*\n", normalize_document_text(text))
    chunks = []
    buffer = ""
    for paragraph in paragraphs:
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        buffer = f"{buffer}\n\n{paragraph}".strip() if buffer else paragraph
        if len(buffer.split()) >= min_words:
            for part in split_long_chunk(buffer):
                if is_quality_chunk(part):
                    chunks.append(part)
            buffer = ""
    if buffer:
        for part in split_long_chunk(buffer):
            if is_quality_chunk(part):
                chunks.append(part)
    return chunks


def load_chunk_file(chunks_dir: Path, stem: str) -> list[dict]:
    """Load stored chunks for a RAG stem."""
    json_path = chunks_dir / f"{stem}.json"
    if not json_path.exists():
        return []
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    chunks = data.get("chunks", [])
    return chunks if isinstance(chunks, list) else []


def build_excerpt_from_chunk_dicts(chunks: list[dict], max_chars: int = 5000) -> str:
    """Build a bounded excerpt from stored chunk dictionaries."""
    parts = []
    total = 0
    for chunk in chunks:
        text = str(chunk.get("text", "")).strip()
        if not text:
            continue
        if total + len(text) > max_chars and parts:
            break
        parts.append(text)
        total += len(text)
        if total >= max_chars:
            break
    return "\n\n---\n\n".join(parts)


def tokenize_for_overlap(text: str) -> set[str]:
    """Tokenize text for lightweight lexical-overlap scoring."""
    return {token for token in re.findall(r"\b\w+\b", text.lower()) if len(token) >= 3}


def lexical_overlap_score(a: str, b: str) -> float:
    """Calculate a simple overlap score between two texts."""
    a_tokens = tokenize_for_overlap(a)
    b_tokens = tokenize_for_overlap(b)
    if not a_tokens or not b_tokens:
        return 0.0
    return len(a_tokens & b_tokens) / max(min(len(a_tokens), len(b_tokens)), 1)


def extract_json_object(text: str) -> dict | None:
    """Extract a JSON object from a model response when possible."""
    if not text:
        return None
    text = text.strip()
    candidates = [text]
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            continue
    return None


def keep_inconsistency_item(item: dict) -> bool:
    """Return whether a model-reported inconsistency is strong enough to keep."""
    combined = " ".join(
        str(item.get(key, ""))
        for key in ("topic", "new_claim", "existing_claim")
    ).lower()
    weak_markers = [
        "more detailed",
        "less detailed",
        "different scope",
        "missing",
        "does not mention",
        "not specified",
        "unclear",
    ]
    return not any(marker in combined for marker in weak_markers)


async def compare_documents_for_inconsistencies(
    new_name: str,
    new_excerpt: str,
    candidate_name: str,
    candidate_scope: str,
    candidate_excerpt: str,
) -> dict | None:
    """Compare a new RAG against one visible candidate document."""
    models = available_models()
    if not models:
        return None
    prompt = build_inconsistency_prompt(
        new_name=new_name,
        new_excerpt=new_excerpt,
        candidate_name=candidate_name,
        candidate_scope=candidate_scope,
        candidate_excerpt=candidate_excerpt,
    )
    try:
        reply = await generate_ai_reply(
            resolve_model(models[0]["id"]),
            [Message(role="user", content=prompt)],
        )
    except Exception:
        return None
    parsed = extract_json_object(reply)
    if not parsed:
        return None

    cleaned_items = []
    for item in (parsed.get("items") or [])[:5]:
        if not isinstance(item, dict):
            continue
        new_claim = str(item.get("new_claim", "")).strip()
        existing_claim = str(item.get("existing_claim", "")).strip()
        if not new_claim or not existing_claim:
            continue
        severity = str(item.get("severity", "medium")).strip().lower()
        if severity not in {"high", "medium", "low"}:
            severity = "medium"
        candidate_item = {
            "topic": str(item.get("topic", "")).strip() or "Possible conflict",
            "new_claim": new_claim,
            "existing_claim": existing_claim,
            "severity": severity,
        }
        if keep_inconsistency_item(candidate_item):
            cleaned_items.append(candidate_item)
    return {
        "has_inconsistencies": bool(parsed.get("has_inconsistencies")) and bool(cleaned_items),
        "summary": str(parsed.get("summary", "")).strip(),
        "items": cleaned_items,
    }


def visible_rag_candidates(user: dict, current_scope: str, current_stem: str) -> list[dict]:
    """List existing RAGs visible during conflict detection."""
    candidates = []
    global_index = load_files_index(GLOBAL_FILES_DIR)
    for txt_path in sorted(GLOBAL_FILES_DIR.glob("*.txt")):
        if current_scope == "global" and txt_path.stem == current_stem:
            continue
        candidates.append(
            {
                "name": txt_path.name,
                "stem": txt_path.stem,
                "scope": "global",
                "files_dir": GLOBAL_FILES_DIR,
                "chunks_dir": GLOBAL_CHUNKS_DIR,
                "description": global_index.get(txt_path.stem, ""),
            }
        )

    own_files_dir = user_files_dir(user["id"])
    own_chunks_dir = user_chunks_dir(user["id"])
    own_index = load_files_index(own_files_dir)
    for txt_path in sorted(own_files_dir.glob("*.txt")):
        if current_scope == "user" and txt_path.stem == current_stem:
            continue
        candidates.append(
            {
                "name": txt_path.name,
                "stem": txt_path.stem,
                "scope": "user",
                "files_dir": own_files_dir,
                "chunks_dir": own_chunks_dir,
                "description": own_index.get(txt_path.stem, ""),
            }
        )
    return candidates


async def visible_chat_chunk_sources(user: dict, model: dict | None = None) -> list[dict]:
    """List visible safe RAG chunk sources for a chat user."""
    sources = []
    for txt_path in sorted(GLOBAL_FILES_DIR.glob("*.txt")):
        if await should_exclude_rag_from_chat(txt_path, "global", None, model):
            continue
        sources.append(
            {
                "key": f"global/{txt_path.stem}",
                "stem": txt_path.stem,
                "scope": "global",
                "chunks_dir": GLOBAL_CHUNKS_DIR,
            }
        )

    own_chunks_dir = user_chunks_dir(user["id"])
    for txt_path in sorted(user_files_dir(user["id"]).glob("*.txt")):
        if await should_exclude_rag_from_chat(txt_path, "user", user["id"], model):
            continue
        sources.append(
            {
                "key": f"mine/{txt_path.stem}",
                "stem": txt_path.stem,
                "scope": "user",
                "chunks_dir": own_chunks_dir,
            }
        )
    return sources


async def load_visible_context_chunks(user: dict, model: dict | None = None) -> list[dict]:
    """Load all visible safe chunks for chat prompt construction."""
    context_chunks = []
    for source in await visible_chat_chunk_sources(user, model):
        for chunk in load_chunk_file(source["chunks_dir"], source["stem"]):
            text = str(chunk.get("text", "")).strip()
            if not text:
                continue
            index = chunk.get("index", len(context_chunks))
            context_chunks.append(
                {
                    "source": f"{source['key']}#{int(index):04d}",
                    "scope": source["scope"],
                    "text": text,
                }
            )
    return context_chunks


def build_chat_messages_with_all_visible_chunks(req: ChatRequest, user: dict, context_chunks: list[dict] | None = None) -> list[Message]:
    """Build chat messages with the full visible safe RAG context."""
    if not req.messages:
        raise HTTPException(status_code=400, detail="No messages to answer")
    question = req.messages[-1].content.strip()
    if not question:
        raise HTTPException(status_code=400, detail="The last message is empty")

    if context_chunks is None:
        context_chunks = []
    if not context_chunks:
        return req.messages

    rag_prompt = build_rag_prompt(question, context_chunks)
    history = req.messages[:-1]
    return [*history, Message(role="user", content=rag_prompt)]


async def detect_rag_inconsistencies(
    new_name: str,
    new_stem: str,
    new_chunks: list[dict],
    scope: str,
    user: dict,
    max_candidates: int = 3,
) -> dict:
    """Detect and persist contradictions for a newly indexed RAG."""
    new_excerpt = build_excerpt_from_chunk_dicts(new_chunks)
    if not new_excerpt:
        return {"has_any": False, "matches": []}

    scored = []
    for candidate in visible_rag_candidates(user, scope, new_stem):
        candidate_chunks = load_chunk_file(candidate["chunks_dir"], candidate["stem"])
        candidate_excerpt = build_excerpt_from_chunk_dicts(candidate_chunks)
        if not candidate_excerpt:
            continue
        score = lexical_overlap_score(new_excerpt[:5000], candidate_excerpt[:5000])
        if score < 0.08:
            continue
        scored.append((score, candidate, candidate_excerpt))

    scored.sort(key=lambda item: item[0], reverse=True)
    findings = []
    for score, candidate, candidate_excerpt in scored[:max_candidates]:
        comparison = await compare_documents_for_inconsistencies(
            new_name=new_name,
            new_excerpt=new_excerpt,
            candidate_name=candidate["name"],
            candidate_scope=candidate["scope"],
            candidate_excerpt=candidate_excerpt,
        )
        if not comparison or not comparison.get("has_inconsistencies"):
            continue
        findings.append(
            {
                "name": candidate["name"],
                "scope": candidate["scope"],
                "description": candidate["description"],
                "similarity": round(score, 3),
                "summary": comparison.get("summary") or "Possible factual conflicts detected",
                "items": comparison["items"],
            }
        )
    return {"has_any": bool(findings), "matches": findings}


async def check_existing_rag_conflicts(
    txt_path: Path,
    chunks_dir: Path,
    scope: str,
    owner_id: int | None,
    user: dict,
) -> None:
    """Run a deferred conflict check for an existing RAG."""
    if not txt_path.exists():
        return
    chunks = load_chunk_file(chunks_dir, txt_path.stem)
    conflicts = await detect_rag_inconsistencies(
        new_name=txt_path.name,
        new_stem=txt_path.stem,
        new_chunks=chunks,
        scope=scope,
        user=user,
    )
    if txt_path.exists():
        save_conflicts_to_index(txt_path.parent, txt_path.stem, conflicts)


def schedule_existing_conflict_check(
    txt_path: Path,
    chunks_dir: Path,
    scope: str,
    owner_id: int | None,
    user: dict,
) -> None:
    """Schedule a background conflict check if one is not already running."""
    key = f"{scope}:{owner_id or 'global'}:{txt_path.stem}"
    if key in CONFLICT_CHECK_TASKS:
        return
    CONFLICT_CHECK_TASKS.add(key)

    async def runner():
        """Function for runner."""
        try:
            await check_existing_rag_conflicts(txt_path, chunks_dir, scope, owner_id, user)
        except Exception as exc:
            persist_exception_log(
                exc,
                {
                    "source": "background_conflict_check",
                    "file": str(txt_path),
                    "scope": scope,
                    "owner_id": owner_id,
                    "user_id": user.get("id") if isinstance(user, dict) else None,
                },
            )
        finally:
            CONFLICT_CHECK_TASKS.discard(key)

    asyncio.create_task(runner())


def build_document_description(text: str, max_chars: int = 260) -> str:
    """Create a compact display description for a document."""
    cleaned = re.sub(r"\s+", " ", normalize_document_text(text))
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 1].rstrip() + "..."


async def process_rag_file(
    txt_path: Path,
    chunks_dir: Path,
    scope: str,
    owner_id: int | None = None,
    user: dict | None = None,
) -> None:
    """Chunk, index, screen, audit, and compare an uploaded RAG file."""
    chunks_dir.mkdir(parents=True, exist_ok=True)
    if not txt_path.exists():
        return

    text = txt_path.read_text(encoding="utf-8", errors="ignore")
    chunks = chunk_text(text)
    if not txt_path.exists():
        return

    output = {
        "schema_version": 1,
        "source": txt_path.name,
        "stem": txt_path.stem,
        "scope": scope,
        "owner_id": owner_id,
        "processed_at": datetime.utcnow().isoformat(),
        "chunking": {
            "strategy": "paragraph_buffer",
            "min_words": CHUNK_MIN_WORDS,
            "max_chars": CHUNK_MAX_CHARS,
        },
        "total": len(chunks),
        "chunks": [
            {
                "id": f"{txt_path.stem}:{index:04d}",
                "index": index,
                "source": txt_path.name,
                "scope": scope,
                "owner_id": owner_id,
                "text": chunk,
            }
            for index, chunk in enumerate(chunks)
        ],
    }
    json_path = chunks_dir / f"{txt_path.stem}.json"
    json_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    save_description_to_index(txt_path.parent, txt_path.stem, build_document_description(text))
    security_index = prune_index_entries(txt_path.parent, "security_index.json")
    if txt_path.stem not in security_index:
        security_assessment = await assess_rag_prompt_injection(text, txt_path.name)
        save_security_to_index(txt_path.parent, txt_path.stem, security_assessment)
        persist_suspicious_rag_audit_log(txt_path, scope, owner_id, security_assessment)
    if user is not None:
        conflicts = await detect_rag_inconsistencies(
            new_name=txt_path.name,
            new_stem=txt_path.stem,
            new_chunks=output["chunks"],
            scope=scope,
            user=user,
        )
        if txt_path.exists():
            save_conflicts_to_index(txt_path.parent, txt_path.stem, conflicts)


def schedule_rag_processing(
    txt_path: Path,
    chunks_dir: Path,
    scope: str,
    owner_id: int | None,
    user: dict,
) -> None:
    """Schedule background processing for a RAG file."""
    async def runner():
        """Function for runner."""
        try:
            await process_rag_file(txt_path, chunks_dir, scope, owner_id, user)
        except Exception as exc:
            persist_exception_log(
                exc,
                {
                    "source": "background_rag_processing",
                    "file": str(txt_path),
                    "scope": scope,
                    "owner_id": owner_id,
                    "user_id": user.get("id") if isinstance(user, dict) else None,
                },
            )

    asyncio.create_task(runner())


def read_key_file(path: Path, *names: str) -> str | None:
    """Read the first available provider key from legacy key files."""
    if not path.exists():
        return None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key_name, value = line.split("=", 1)
            if key_name.strip().upper() in {name.upper() for name in names}:
                return value.strip().strip('"').strip("'")
            continue
        return line.strip('"').strip("'")
    return None


def read_api_keys_json(provider: str, *names: str) -> str | None:
    """Read a provider API key from api_keys.json."""
    if not API_KEYS_PATH.exists():
        return None
    try:
        data = json.loads(API_KEYS_PATH.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Invalid api_keys.json")
    provider_data = data.get(provider)
    if isinstance(provider_data, str):
        return provider_data.strip() or None
    if isinstance(provider_data, dict):
        for name in ("api_key", "key", *names):
            value = provider_data.get(name) or provider_data.get(name.upper())
            if value:
                return str(value).strip()
    return None


def get_provider_key(provider: str) -> str | None:
    """Resolve a provider API key from JSON, environment, or legacy files."""
    provider = provider.lower()
    env_names = {
        "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "openai": ("OPENAI_API_KEY",),
        "anthropic": ("ANTHROPIC_API_KEY",),
    }.get(provider, ())
    for name in env_names:
        value = os.getenv(name)
        if value:
            return value
    value = read_api_keys_json(provider, *env_names)
    if value:
        return value
    key_file = LEGACY_API_KEY_FILES.get(provider)
    if key_file:
        return read_key_file(key_file, *env_names)
    return None


def available_models() -> list[dict]:
    """Return model catalog entries whose providers have configured keys."""
    return [
        model
        for model in MODEL_CATALOG
        if get_provider_key(model["provider"])
    ]


def resolve_model(selection: str) -> dict:
    """Resolve a requested model id into a catalog entry."""
    for model in MODEL_CATALOG:
        if selection in {model["id"], model["model"], model["label"]}:
            if not get_provider_key(model["provider"]):
                raise HTTPException(
                    status_code=400,
                    detail=f"API key is not configured for {model['provider']}",
                )
            return model
    raise HTTPException(status_code=400, detail="Unsupported model")


def to_langchain_messages(messages: list[Message]) -> list:
    """Convert Emma messages into LangChain message objects."""
    try:
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="LangChain is not installed. Run pip install -r requirements.txt",
        )

    converted = []
    for message in messages:
        content = message.content
        if message.role == "system":
            converted.append(SystemMessage(content=content))
        elif message.role == "assistant":
            converted.append(AIMessage(content=content))
        else:
            converted.append(HumanMessage(content=content))
    return converted


def make_langchain_chat_model(model: dict):
    """Instantiate the configured LangChain chat model."""
    provider = model["provider"]
    key = get_provider_key(provider)
    if not key:
        raise HTTPException(status_code=400, detail=f"API key is not configured for {provider}")

    try:
        if provider == "gemini":
            from langchain_google_genai import ChatGoogleGenerativeAI

            return ChatGoogleGenerativeAI(
                model=model["model"],
                google_api_key=key,
                temperature=0.3,
            )
        if provider == "openai":
            from langchain_openai import ChatOpenAI

            return ChatOpenAI(
                model=model["model"],
                api_key=key,
                temperature=0.3,
            )
        if provider == "anthropic":
            from langchain_anthropic import ChatAnthropic

            return ChatAnthropic(
                model=model["model"],
                api_key=key,
                temperature=0.3,
                max_tokens=4096,
            )
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail=f"LangChain dependency is not installed for {provider}. Run pip install -r requirements.txt",
        )
    raise HTTPException(status_code=400, detail="Unsupported provider")


def langchain_response_text(response, *, strip: bool = True) -> str:
    """Extract text content from a LangChain response object."""
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content.strip() if strip else content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
        text = "".join(parts)
        return text.strip() if strip else text
    text = str(content)
    return text.strip() if strip else text


async def generate_ai_reply(model: dict, messages: list[Message]) -> str:
    """Generate a non-streaming reply through the selected LangChain model."""
    chat_model = make_langchain_chat_model(model)
    response = await chat_model.ainvoke(to_langchain_messages(messages))
    return langchain_response_text(response)


async def generate_ai_reply_stream(model: dict, messages: list[Message]):
    """Stream reply text from the selected LangChain model."""
    chat_model = make_langchain_chat_model(model)
    async for chunk in chat_model.astream(to_langchain_messages(messages)):
        piece = langchain_response_text(chunk, strip=False)
        if piece:
            yield piece



def default_safety_assessment() -> dict:
    """Return the default safe chat-manipulation assessment."""
    return {
        "label": "SAFE",
        "confidence": 0.0,
        "summary": "No clear manipulation patterns detected",
        "signals": [],
        "evidence": [],
    }


def normalize_safety_assessment(data: dict | None) -> dict:
    """Normalize model output into the chat safety schema."""
    default = default_safety_assessment()
    if not isinstance(data, dict):
        return default

    label = str(data.get("label", default["label"])).strip().upper()
    if label not in {"SAFE", "REVIEW", "SUSPICIOUS"}:
        label = default["label"]

    try:
        confidence = float(data.get("confidence", default["confidence"]))
    except (TypeError, ValueError):
        confidence = default["confidence"]
    confidence = max(0.0, min(confidence, 1.0))

    summary = str(data.get("summary", default["summary"])).strip() or default["summary"]
    signals = [str(item).strip() for item in (data.get("signals") or []) if str(item).strip()][:6]
    evidence = [str(item).strip() for item in (data.get("evidence") or []) if str(item).strip()][:4]

    return {
        "label": label,
        "confidence": round(confidence, 3),
        "summary": summary,
        "signals": signals,
        "evidence": evidence,
    }


async def analyze_user_message_safety(message: str, model: dict) -> dict:
    """Analyze a user message for manipulation or policy-bypass risk."""
    if not message.strip():
        return default_safety_assessment()
    prompt = build_safety_prompt(message)
    try:
        reply = await generate_ai_reply(model, [Message(role="user", content=prompt)])
        return normalize_safety_assessment(extract_json_object(reply))
    except Exception:
        return {
            **default_safety_assessment(),
            "summary": "Safety analysis unavailable",
        }


def should_persist_chat_audit(safety: dict) -> bool:
    """Return whether a safety assessment should be audit-logged."""
    return safety.get("label") in {"REVIEW", "SUSPICIOUS"}


def rotate_chat_audit_logs(max_files: int = 500, delete_count: int = 50) -> None:
    """Keep chat audit logs within the retention limit."""
    try:
        files = sorted(LOGS_DIR.glob("*.json"), key=lambda path: path.stat().st_mtime)
        if len(files) < max_files:
            return
        for path in files[:delete_count]:
            path.unlink(missing_ok=True)
    except Exception as exc:
        print(f"[audit] failed to rotate chat audit logs: {exc}")


def persist_suspicious_chat_audit_log(record: dict) -> None:
    """Persist a suspicious chat audit record."""
    if not should_persist_chat_audit(record.get("safety", {})):
        return
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        rotate_chat_audit_logs()
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S%fZ")
        path = LOGS_DIR / f"suspicious_{ts}_{secrets.token_hex(4)}.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"[audit] failed to persist chat audit log: {exc}")


def build_chat_audit_record(
    req: ChatRequest,
    user: dict,
    model: dict,
    question: str,
    safety: dict,
    context_chunks: list[dict],
) -> dict:
    """Build a JSON-serializable suspicious chat audit record."""
    sources = sorted({str(chunk.get("source", "unknown")).split("#", 1)[0] for chunk in context_chunks})
    return {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "audit_type": "suspicious_chat",
        "user_id": user["id"],
        "username": user["username"],
        "conversation_id": req.conversation_id,
        "model": model["id"],
        "question": question,
        "question_length": len(question),
        "safety": safety,
        "rag": {
            "active": bool(context_chunks),
            "visible_chunk_count": len(context_chunks),
            "visible_source_count": len(sources),
            "visible_sources": sources,
        },
    }

def store_chat_messages(conversation_id: str | None, user_id: int, messages: list[Message], reply: str) -> None:
    """Persist a user turn and assistant reply in a conversation."""
    if not conversation_id:
        return
    conn = get_db()
    conv = conn.execute(
        "SELECT id FROM conversations WHERE id = ? AND user_id = ?",
        (conversation_id, user_id),
    ).fetchone()
    if not conv:
        conn.close()
        return
    now = datetime.utcnow().isoformat()
    last_user = next((message for message in reversed(messages) if message.role == "user"), None)
    if last_user:
        conn.execute(
            "INSERT INTO messages (id, conversation_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (secrets.token_urlsafe(12), conversation_id, "user", last_user.content, now),
        )
    conn.execute(
        "INSERT INTO messages (id, conversation_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
        (secrets.token_urlsafe(12), conversation_id, "assistant", reply, now),
    )
    conn.execute(
        "UPDATE conversations SET updated_at = ? WHERE id = ?",
        (now, conversation_id),
    )
    conn.commit()
    conn.close()


def response_tag(text: str) -> str | None:
    """Extract Emma's leading grounding tag from a response."""
    stripped = text.strip()
    for tag in ("[RAG]", "[DRIFT]", "[NO INFO]"):
        if stripped.startswith(tag):
            return tag
    return None


def fallback_response_tag(context_chunks: list[dict]) -> str:
    """Return the conservative grounding tag when a model omits one."""
    return "[DRIFT]" if context_chunks else "[NO INFO]"


def ensure_response_tag(text: str, context_chunks: list[dict]) -> str:
    """Prefix a response with a conservative grounding tag if the model omitted it."""
    if response_tag(text):
        return text
    prefix = fallback_response_tag(context_chunks)
    return f"{prefix}\n{text.lstrip()}" if text.strip() else f"{prefix}\n"


def build_no_info_reply(question: str) -> str:
    """Build a deterministic no-context reply in the user's likely language."""
    spanish_markers = {
        "como",
        "cual",
        "cuál",
        "cuando",
        "cuándo",
        "donde",
        "dónde",
        "hola",
        "nació",
        "porque",
        "por qué",
        "qué",
        "quien",
        "quién",
    }
    normalized = question.lower()
    if "¿" in question or any(marker in normalized for marker in spanish_markers):
        return "[NO INFO]\nNo tengo información en los documentos disponibles para responder eso."
    return "[NO INFO]\nI do not have information in the available documents to answer that."


async def stream_static_chat_reply_as_json_lines(
    reply: str,
    req: ChatRequest,
    user: dict,
    audit_record: dict,
):
    """Stream a backend-generated reply and persist it like a model response."""
    store_chat_messages(req.conversation_id, user["id"], req.messages, reply)
    audit_record["response"] = {
        "tag": response_tag(reply),
        "length": len(reply),
    }
    persist_suspicious_chat_audit_log(audit_record)
    yield json.dumps({"text": reply, "done": False}) + "\n"
    yield json.dumps({"text": "", "done": True}) + "\n"


async def stream_chat_as_json_lines(
    model: dict,
    messages: list[Message],
    req: ChatRequest,
    user: dict,
    audit_record: dict,
    context_chunks: list[dict],
):
    """Stream chat chunks as newline-delimited JSON and persist the final reply."""
    reply_parts = []
    prefix_checked = False
    buffered_start = ""
    possible_tags = ("[RAG]", "[DRIFT]", "[NO INFO]")

    async for piece in generate_ai_reply_stream(model, messages):
        if not prefix_checked:
            buffered_start += piece
            stripped_start = buffered_start.lstrip()
            if response_tag(buffered_start):
                prefix_checked = True
                reply_parts.append(buffered_start)
                yield json.dumps({"text": buffered_start, "done": False}) + "\n"
                buffered_start = ""
            elif any(tag.startswith(stripped_start) for tag in possible_tags):
                continue
            else:
                prefix_checked = True
                tagged_start = ensure_response_tag(buffered_start, context_chunks)
                reply_parts.append(tagged_start)
                yield json.dumps({"text": tagged_start, "done": False}) + "\n"
                buffered_start = ""
            continue

        reply_parts.append(piece)
        yield json.dumps({"text": piece, "done": False}) + "\n"

    if not prefix_checked and buffered_start:
        tagged_start = ensure_response_tag(buffered_start, context_chunks)
        reply_parts.append(tagged_start)
        yield json.dumps({"text": tagged_start, "done": False}) + "\n"

    reply = "".join(reply_parts)
    store_chat_messages(req.conversation_id, user["id"], req.messages, reply)
    audit_record["response"] = {
        "tag": response_tag(reply),
        "length": len(reply),
    }
    persist_suspicious_chat_audit_log(audit_record)
    yield json.dumps({"text": "", "done": True}) + "\n"


def append_file_entries(
    result: list[dict],
    files_dir: Path,
    chunks_dir: Path,
    scope: str,
    owner_id: int | None = None,
    owner_username: str | None = None,
    conflict_user: dict | None = None,
    schedule_missing_conflicts: bool = False,
) -> None:
    """Append file metadata entries for the files endpoint response."""
    index = prune_index_entries(files_dir, "files_index.json")
    conflicts = prune_index_entries(files_dir, "conflicts_index.json")
    security = prune_index_entries(files_dir, "security_index.json")
    for txt_path in sorted(files_dir.glob("*.txt")):
        stem = txt_path.stem
        json_path = chunks_dir / f"{stem}.json"
        indexed = json_path.exists()
        chunks = 0
        if indexed:
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
                chunks = int(data.get("total") or 0)
            except Exception:
                pass
        has_conflict_record = stem in conflicts
        if indexed and schedule_missing_conflicts and conflict_user is not None and not has_conflict_record:
            schedule_existing_conflict_check(
                txt_path=txt_path,
                chunks_dir=chunks_dir,
                scope=scope,
                owner_id=owner_id,
                user=conflict_user,
            )
            inconsistencies = conflicts_response(None, "checking")
        elif has_conflict_record:
            inconsistencies = conflicts_response(conflicts.get(stem), "checked")
        else:
            inconsistencies = conflicts_response(None, "unindexed" if not indexed else "unchecked")
        result.append(
            {
                "name": txt_path.name,
                "stem": stem,
                "scope": scope,
                "owner_id": owner_id,
                "owner_username": owner_username,
                "indexed": indexed,
                "chunks": chunks,
                "description": index.get(stem, ""),
                "inconsistencies": inconsistencies,
                "security": security_response(security.get(stem), "unindexed" if not indexed else "unchecked"),
            }
        )


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Resolve the bearer token into an active current user."""
    if not credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")
    conn = get_db()
    row = conn.execute(
        "SELECT u.id, u.username, u.role, u.full_name, u.is_active "
        "FROM sessions s JOIN users u ON s.user_id = u.id WHERE s.token = ?",
        (credentials.credentials,),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=401, detail="Invalid token")
    if not row["is_active"]:
        raise HTTPException(status_code=403, detail="User is disabled")
    return {
        "id": row["id"],
        "username": row["username"],
        "full_name": row["full_name"] or row["username"],
        "role": normalize_role(row["role"]),
    }


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Serve Emma's favicon."""
    if not FAVICON_PATH.exists():
        raise HTTPException(status_code=404, detail="Favicon not found")
    return FileResponse(path=str(FAVICON_PATH), media_type="image/svg+xml")


def initialize_runtime():
    """Create runtime directories and initialize the database."""
    init_db()
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    RAG_AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    EXCEPTION_LOG_DIR.mkdir(parents=True, exist_ok=True)
    GLOBAL_FILES_DIR.mkdir(parents=True, exist_ok=True)
    GLOBAL_CHUNKS_DIR.mkdir(parents=True, exist_ok=True)


@app.post("/auth/login")
async def login(body: LoginRequest):
    """Authenticate a test user and return its token."""
    conn = get_db()
    row = conn.execute(
        "SELECT id, username, password_hash, role, full_name, is_active FROM users WHERE username = ?",
        (body.username,),
    ).fetchone()
    conn.close()
    if not row or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    if not row["is_active"]:
        raise HTTPException(status_code=403, detail="User is disabled")

    token = secrets.token_urlsafe(32)
    now = datetime.utcnow().isoformat()
    conn = get_db()
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)",
        (token, row["id"], now),
    )
    conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now, row["id"]))
    conn.commit()
    conn.close()
    return {
        "token": token,
        "user": {
            "id": row["id"],
            "username": row["username"],
            "full_name": row["full_name"] or row["username"],
            "role": normalize_role(row["role"]),
        },
    }


@app.post("/auth/logout")
async def logout(
    user: dict = Depends(get_current_user),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Delete the current bearer session token."""
    if credentials:
        conn = get_db()
        conn.execute("DELETE FROM sessions WHERE token = ?", (credentials.credentials,))
        conn.commit()
        conn.close()
    return {"status": "ok"}


@app.get("/auth/me")
async def me(user: dict = Depends(get_current_user)):
    """Return the current authenticated user's profile."""
    return {
        "id": user["id"],
        "username": user["username"],
        "full_name": user["full_name"],
        "role": user["role"],
    }


@app.get("/admin/users")
async def admin_list_users(user: dict = Depends(get_current_user)):
    """Return all users for the admin panel."""
    require_admin(user)
    conn = get_db()
    rows = conn.execute(
        "SELECT id, username, full_name, role, is_active, created_at, last_login_at "
        "FROM users ORDER BY created_at ASC"
    ).fetchall()
    conn.close()
    return {"users": [serialize_user_row(row) for row in rows]}


@app.post("/admin/users")
async def admin_create_user(
    body: AdminUserCreate,
    user: dict = Depends(get_current_user),
):
    """Create a user from the admin panel."""
    require_admin(user)
    username = body.username.strip()
    password = body.password.strip()
    if len(username) < 3:
        raise HTTPException(status_code=400, detail="Username must be at least 3 characters")
    if len(password) < 4:
        raise HTTPException(status_code=400, detail="Password must be at least 4 characters")
    role = normalize_role(body.role)
    full_name = (body.full_name or "").strip() or username

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, role, full_name, is_active, created_at) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (username, hash_password(password), role, full_name, datetime.utcnow().isoformat()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=409, detail="That username already exists")
    new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    row = conn.execute(
        "SELECT id, username, full_name, role, is_active, created_at, last_login_at FROM users WHERE id = ?",
        (new_id,),
    ).fetchone()
    conn.close()
    return {"user": serialize_user_row(row)}


@app.patch("/admin/users/{target_user_id}")
async def admin_update_user(
    target_user_id: int,
    body: AdminUserUpdate,
    user: dict = Depends(get_current_user),
):
    """Update an existing user from the admin panel."""
    require_admin(user)
    row = get_user_row(target_user_id)
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    updates = []
    values = []
    if body.role is not None:
        role = normalize_role(body.role)
        ensure_admin_survives(target_user_id, new_role=role)
        updates.append("role = ?")
        values.append(role)
    if body.is_active is not None:
        ensure_admin_survives(target_user_id, new_is_active=body.is_active)
        updates.append("is_active = ?")
        values.append(1 if body.is_active else 0)
    if body.full_name is not None:
        updates.append("full_name = ?")
        values.append(body.full_name.strip() or row["username"])
    if not updates:
        raise HTTPException(status_code=400, detail="No changes to apply")

    conn = get_db()
    conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", (*values, target_user_id))
    if body.is_active is False:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (target_user_id,))
    conn.commit()
    updated = conn.execute(
        "SELECT id, username, full_name, role, is_active, created_at, last_login_at FROM users WHERE id = ?",
        (target_user_id,),
    ).fetchone()
    conn.close()
    return {"user": serialize_user_row(updated)}


@app.post("/admin/users/{target_user_id}/reset-password")
async def admin_reset_password(
    target_user_id: int,
    body: AdminPasswordReset,
    user: dict = Depends(get_current_user),
):
    """Reset a user's password from the admin panel."""
    require_admin(user)
    if len(body.password.strip()) < 4:
        raise HTTPException(status_code=400, detail="Password must be at least 4 characters")
    if not get_user_row(target_user_id):
        raise HTTPException(status_code=404, detail="User not found")
    conn = get_db()
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (hash_password(body.password.strip()), target_user_id),
    )
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (target_user_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.delete("/admin/users/{target_user_id}")
async def admin_delete_user(
    target_user_id: int,
    user: dict = Depends(get_current_user),
):
    """Delete a user and their owned runtime files."""
    require_admin(user)
    if target_user_id == user["id"]:
        raise HTTPException(status_code=400, detail="You cannot delete your own user")
    if not get_user_row(target_user_id):
        raise HTTPException(status_code=404, detail="User not found")
    ensure_admin_survives(target_user_id, deleting=True)

    conn = get_db()
    conv_rows = conn.execute("SELECT id FROM conversations WHERE user_id = ?", (target_user_id,)).fetchall()
    conv_ids = [row["id"] for row in conv_rows]
    if conv_ids:
        placeholders = ",".join(["?"] * len(conv_ids))
        conn.execute(f"DELETE FROM messages WHERE conversation_id IN ({placeholders})", conv_ids)
    conn.execute("DELETE FROM conversations WHERE user_id = ?", (target_user_id,))
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (target_user_id,))
    conn.execute("DELETE FROM users WHERE id = ?", (target_user_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.get("/health")
async def health(user: dict = Depends(get_current_user)):
    """Return provider/model availability without exposing secrets."""
    models = available_models()
    return {
        "status": "ok",
        "models": models,
        "providers": sorted({model["provider"] for model in models}),
    }


@app.get("/files")
async def list_files(user: dict = Depends(get_current_user)):
    """Return visible RAG files with indexing, conflict, and security state."""
    result = []
    append_file_entries(
        result,
        GLOBAL_FILES_DIR,
        GLOBAL_CHUNKS_DIR,
        "global",
        conflict_user=user,
        schedule_missing_conflicts=True,
    )
    if user["role"] == "admin":
        conn = get_db()
        rows = conn.execute(
            "SELECT id, username, role, full_name FROM users ORDER BY id ASC"
        ).fetchall()
        conn.close()
        for row in rows:
            conflict_user = {
                "id": row["id"],
                "username": row["username"],
                "full_name": row["full_name"] or row["username"],
                "role": normalize_role(row["role"]),
            }
            append_file_entries(
                result,
                user_files_dir(row["id"]),
                user_chunks_dir(row["id"]),
                "user",
                owner_id=row["id"],
                owner_username=row["username"],
                conflict_user=conflict_user,
                schedule_missing_conflicts=True,
            )
    else:
        append_file_entries(
            result,
            user_files_dir(user["id"]),
            user_chunks_dir(user["id"]),
            "user",
            owner_id=user["id"],
            owner_username=user["username"],
            conflict_user=user,
            schedule_missing_conflicts=True,
        )
    return {"files": result}


@app.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    scope: str = "user",
    owner_id: Optional[int] = Query(default=None),
    user: dict = Depends(get_current_user),
):
    """Store an uploaded text RAG and schedule processing."""
    require_upload_access(user)
    if not file.filename or not file.filename.endswith(".txt"):
        raise HTTPException(status_code=400, detail="Only .txt files are accepted")
    if scope == "global":
        if user["role"] != "admin":
            raise HTTPException(status_code=403, detail="Only admins can upload global files")
        dest_dir = GLOBAL_FILES_DIR
        chunks_dir = GLOBAL_CHUNKS_DIR
        target_user_id = None
    elif scope == "user":
        target_user_id = resolve_target_user_id(user, owner_id)
        dest_dir = user_files_dir(target_user_id)
        chunks_dir = user_chunks_dir(target_user_id)
    else:
        raise HTTPException(status_code=400, detail="Invalid scope")

    dest_dir.mkdir(parents=True, exist_ok=True)
    chunks_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^\w.]", "_", file.filename.strip()).lower()
    safe_name = re.sub(r"_+", "_", safe_name)
    dest = dest_dir / safe_name
    duplicate_name = dest.exists()
    file_bytes = await file.read()
    dest.write_bytes(file_bytes)
    uploaded_text = file_bytes.decode("utf-8", errors="ignore")
    security = await assess_rag_prompt_injection(uploaded_text, safe_name)
    save_security_to_index(dest_dir, dest.stem, security)
    persist_suspicious_rag_audit_log(dest, scope, target_user_id, security)
    indexing_user = dict(user)
    schedule_rag_processing(dest, chunks_dir, scope, target_user_id, indexing_user)
    return {
        "status": "ok",
        "file": file.filename,
        "stored_as": safe_name,
        "scope": scope,
        "message": "File received, splitting into chunks and checking inconsistencies...",
        "duplicate_name": duplicate_name,
        "inconsistencies": [],
        "security": security,
    }


@app.delete("/files/{scope}/{stem}")
async def delete_file(
    scope: str,
    stem: str,
    owner_id: Optional[int] = Query(default=None),
    user: dict = Depends(get_current_user),
):
    """Delete one visible RAG and prune its derived state."""
    require_upload_access(user)
    if scope == "global":
        if user["role"] != "admin":
            raise HTTPException(status_code=403, detail="Only admins can delete global files")
        files_dir = GLOBAL_FILES_DIR
        chunks_dir = GLOBAL_CHUNKS_DIR
    elif scope == "user":
        target_user_id = resolve_target_user_id(user, owner_id)
        files_dir = user_files_dir(target_user_id)
        chunks_dir = user_chunks_dir(target_user_id)
    else:
        raise HTTPException(status_code=400, detail="Invalid scope")

    deleted = []
    for path in [files_dir / f"{stem}.txt", chunks_dir / f"{stem}.json", chunks_dir / f"{stem}.npy"]:
        if path.exists():
            path.unlink()
            deleted.append(path.name)
    for idx_name in ["files_index.json", "conflicts_index.json", "security_index.json"]:
        idx_path = files_dir / idx_name
        if idx_path.exists():
            try:
                index = json.loads(idx_path.read_text(encoding="utf-8"))
                if stem in index:
                    del index[stem]
                    idx_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
    prune_orphaned_conflict_matches(files_dir)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"File '{stem}' not found")
    return {"status": "ok", "deleted": deleted}


@app.delete("/files/{scope}")
async def delete_all_files(
    scope: str,
    owner_id: Optional[int] = Query(default=None),
    user: dict = Depends(get_current_user),
):
    """Delete all RAGs in an allowed scope."""
    require_upload_access(user)
    if scope == "global":
        if user["role"] != "admin":
            raise HTTPException(status_code=403, detail="Only admins can delete global files")
        files_dir = GLOBAL_FILES_DIR
        chunks_dir = GLOBAL_CHUNKS_DIR
    elif scope == "user":
        target_user_id = resolve_target_user_id(user, owner_id)
        files_dir = user_files_dir(target_user_id)
        chunks_dir = user_chunks_dir(target_user_id)
    else:
        raise HTTPException(status_code=400, detail="Invalid scope")

    deleted_count = 0
    for txt_path in list(files_dir.glob("*.txt")):
        stem = txt_path.stem
        for path in [txt_path, chunks_dir / f"{stem}.json", chunks_dir / f"{stem}.npy"]:
            if path.exists():
                path.unlink()
                deleted_count += 1
    for idx_path in [files_dir / "files_index.json", files_dir / "conflicts_index.json", files_dir / "security_index.json"]:
        if idx_path.exists():
            idx_path.write_text("{}", encoding="utf-8")
    return {"status": "ok", "scope": scope, "deleted_count": deleted_count}


@app.get("/files/{scope}/{stem}/download")
async def download_file(
    scope: str,
    stem: str,
    owner_id: Optional[int] = Query(default=None),
    user: dict = Depends(get_current_user),
):
    """Download a visible source RAG text file."""
    require_upload_access(user)
    if scope == "global":
        if user["role"] != "admin":
            raise HTTPException(status_code=403, detail="Only admins can download global files")
        files_dir = GLOBAL_FILES_DIR
    elif scope == "user":
        files_dir = user_files_dir(resolve_target_user_id(user, owner_id))
    else:
        raise HTTPException(status_code=400, detail="Invalid scope")
    txt_path = files_dir / f"{stem}.txt"
    if not txt_path.exists():
        raise HTTPException(status_code=404, detail=f"File '{stem}' not found")
    return FileResponse(path=str(txt_path), filename=f"{stem}.txt", media_type="text/plain")


@app.get("/conversations")
async def list_conversations(user: dict = Depends(get_current_user)):
    """List conversations owned by the current user."""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, title, model, created_at, updated_at FROM conversations "
        "WHERE user_id = ? ORDER BY updated_at DESC",
        (user["id"],),
    ).fetchall()
    conn.close()
    return {"conversations": [dict(row) for row in rows]}


@app.post("/conversations")
async def create_conversation(
    body: ConversationCreate,
    user: dict = Depends(get_current_user),
):
    """Create a conversation for the current user."""
    conv_id = secrets.token_urlsafe(12)
    now = datetime.utcnow().isoformat()
    conn = get_db()
    conn.execute(
        "INSERT INTO conversations (id, user_id, title, model, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (conv_id, user["id"], body.title, body.model, now, now),
    )
    conn.commit()
    conn.close()
    return {
        "id": conv_id,
        "title": body.title,
        "model": body.model,
        "created_at": now,
        "updated_at": now,
    }


@app.get("/conversations/{conv_id}")
async def get_conversation(
    conv_id: str,
    user: dict = Depends(get_current_user),
):
    """Return a conversation and its messages."""
    conn = get_db()
    conv = conn.execute(
        "SELECT id, title, model, created_at, updated_at FROM conversations "
        "WHERE id = ? AND user_id = ?",
        (conv_id, user["id"]),
    ).fetchone()
    if not conv:
        conn.close()
        raise HTTPException(status_code=404, detail="Conversation not found")
    messages = conn.execute(
        "SELECT id, role, content, created_at FROM messages "
        "WHERE conversation_id = ? ORDER BY created_at",
        (conv_id,),
    ).fetchall()
    conn.close()
    return {**dict(conv), "messages": [dict(message) for message in messages]}


@app.patch("/conversations/{conv_id}/title")
async def update_conversation_title(
    conv_id: str,
    body: ConversationTitleUpdate,
    user: dict = Depends(get_current_user),
):
    """Rename a conversation owned by the current user."""
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM conversations WHERE id = ? AND user_id = ?",
        (conv_id, user["id"]),
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Conversation not found")
    now = datetime.utcnow().isoformat()
    conn.execute(
        "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ? AND user_id = ?",
        (body.title, now, conv_id, user["id"]),
    )
    conn.commit()
    conn.close()
    return {"status": "ok", "updated_at": now}


@app.delete("/conversations/{conv_id}")
async def delete_conversation(
    conv_id: str,
    user: dict = Depends(get_current_user),
):
    """Delete a conversation owned by the current user."""
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM conversations WHERE id = ? AND user_id = ?",
        (conv_id, user["id"]),
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Conversation not found")
    conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
    conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.post("/chat")
async def chat(
    req: ChatRequest,
    user: dict = Depends(get_current_user),
):
    """Generate or stream a model reply for a chat request."""
    model = resolve_model(req.model)
    if not req.messages:
        raise HTTPException(status_code=400, detail="No messages to answer")
    question = req.messages[-1].content.strip()
    if not question:
        raise HTTPException(status_code=400, detail="The last message is empty")

    context_chunks = await load_visible_context_chunks(user, model)
    safety = await analyze_user_message_safety(question, model)
    ai_messages = build_chat_messages_with_all_visible_chunks(req, user, context_chunks)
    audit_record = build_chat_audit_record(req, user, model, question, safety, context_chunks)

    if not context_chunks:
        reply = build_no_info_reply(question)
        if req.stream:
            return StreamingResponse(
                stream_static_chat_reply_as_json_lines(reply, req, user, audit_record),
                media_type="application/x-ndjson",
            )
        store_chat_messages(req.conversation_id, user["id"], req.messages, reply)
        audit_record["response"] = {
            "tag": response_tag(reply),
            "length": len(reply),
        }
        persist_suspicious_chat_audit_log(audit_record)
        return {
            "model": model["id"],
            "tag": response_tag(reply),
            "message": {"role": "assistant", "content": reply},
        }

    if req.stream:
        return StreamingResponse(
            stream_chat_as_json_lines(model, ai_messages, req, user, audit_record, context_chunks),
            media_type="application/x-ndjson",
        )

    reply = await generate_ai_reply(model, ai_messages)
    reply = ensure_response_tag(reply, context_chunks)
    store_chat_messages(req.conversation_id, user["id"], req.messages, reply)

    audit_record["response"] = {
        "tag": response_tag(reply),
        "length": len(reply),
    }
    persist_suspicious_chat_audit_log(audit_record)

    return {
        "model": model["id"],
        "tag": response_tag(reply),
        "message": {"role": "assistant", "content": reply},
    }





