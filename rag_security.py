import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from prompts import build_rag_security_prompt


@dataclass
class SecurityMessage:
    """Minimal message shape used by RAG security model calls."""
    role: str
    content: str


def prune_security_index_entries(base_dir: Path) -> dict:
    """Remove security-index entries whose source text files no longer exist."""
    idx_path = base_dir / "security_index.json"
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


def normalize_rag_security_assessment(parsed: dict | None, raw_reply: str = "") -> dict:
    """Normalize raw model output into Emma's persisted RAG security schema."""
    if not isinstance(parsed, dict):
        return {
            "has_any": True,
            "risk": "medium",
            "matches": [
                {
                    "signal": "model_security_parse_error",
                    "severity": "medium",
                    "excerpt": (raw_reply or "Model did not return valid JSON")[:500],
                }
            ],
            "status": "checked",
        }
    raw_risk = str(parsed.get("risk") or "").strip().lower()
    risk = raw_risk if raw_risk in {"none", "medium", "high"} else "medium"
    raw_matches = parsed.get("matches") if isinstance(parsed.get("matches"), list) else []
    matches = []
    for item in raw_matches[:10]:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity") or risk).strip().lower()
        if severity not in {"medium", "high"}:
            severity = "medium" if risk == "medium" else "high"
        signal = str(item.get("signal") or "model_detected_prompt_injection").strip()
        excerpt = str(item.get("excerpt") or parsed.get("summary") or signal).strip()
        matches.append(
            {
                "signal": signal[:120] or "model_detected_prompt_injection",
                "severity": severity,
                "excerpt": excerpt[:500],
            }
        )
    has_any = bool(parsed.get("has_any")) or risk in {"medium", "high"} or bool(matches)
    if has_any and risk == "none":
        risk = "medium"
    if has_any and not matches:
        matches.append(
            {
                "signal": "model_detected_prompt_injection",
                "severity": "medium" if risk == "medium" else "high",
                "excerpt": str(parsed.get("summary") or "Model detected prompt-injection risk")[:500],
            }
        )
    if not has_any:
        risk = "none"
        matches = []
    return {
        "has_any": has_any,
        "risk": risk,
        "matches": matches,
        "status": "checked",
    }


def resolve_rag_security_model(
    model: dict | None,
    available_models_func: Callable[[], list[dict]],
    resolve_model_func: Callable[[str], dict],
) -> dict | None:
    """Choose the model used for RAG security checks."""
    if isinstance(model, dict):
        return model
    models = available_models_func()
    if not models:
        return None
    return resolve_model_func(models[0]["id"])


async def assess_rag_prompt_injection(
    text: str,
    file_name: str,
    model: dict | None,
    available_models_func: Callable[[], list[dict]],
    resolve_model_func: Callable[[str], dict],
    generate_ai_reply_func: Callable[[dict, list[SecurityMessage]], Awaitable[str]],
) -> dict:
    """Run model-based prompt-injection screening for a RAG document."""
    security_model = resolve_rag_security_model(model, available_models_func, resolve_model_func)
    if security_model is None:
        return {
            "has_any": False,
            "risk": "none",
            "matches": [],
            "status": "unavailable",
        }
    prompt = build_rag_security_prompt(file_name, text or "")
    reply = await generate_ai_reply_func(security_model, [SecurityMessage(role="user", content=prompt)])
    return normalize_rag_security_assessment(extract_json_object(reply), reply)


def save_security_to_index(base_dir: Path, stem: str, assessment: dict) -> None:
    """Persist a RAG security assessment next to the source files."""
    if not (base_dir / f"{stem}.txt").exists():
        return
    idx_path = base_dir / "security_index.json"
    index = prune_security_index_entries(base_dir)
    index[stem] = {
        "has_any": bool(assessment.get("has_any")),
        "risk": assessment.get("risk") or "none",
        "matches": assessment.get("matches") if isinstance(assessment.get("matches"), list) else [],
        "status": "checked",
        "checked_at": datetime.utcnow().isoformat(),
    }
    idx_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


def security_response(record: dict | None, status: str = "unchecked") -> dict:
    """Return a frontend-safe representation of a security record."""
    if not isinstance(record, dict):
        return {"has_any": False, "risk": "none", "matches": [], "status": status}
    matches = record.get("matches") if isinstance(record.get("matches"), list) else []
    response = {
        "has_any": bool(record.get("has_any")) and bool(matches),
        "risk": record.get("risk") or ("medium" if matches else "none"),
        "matches": matches,
        "status": record.get("status") or status,
    }
    if record.get("checked_at"):
        response["checked_at"] = record["checked_at"]
    return response


def is_high_risk_rag_security(record: dict | None) -> bool:
    """Return whether a RAG security record must be excluded from chat."""
    security = security_response(record)
    return bool(security.get("has_any")) and security.get("risk") == "high"


def rotate_rag_audit_logs(audit_dir: Path, max_files: int = 500, delete_count: int = 50) -> None:
    """Keep the suspicious RAG audit directory within its retention limit."""
    try:
        files = sorted(audit_dir.glob("*.json"), key=lambda path: path.stat().st_mtime)
        if len(files) < max_files:
            return
        for path in files[:delete_count]:
            path.unlink(missing_ok=True)
    except Exception as exc:
        print(f"[rag-audit] failed to rotate audit logs: {exc}")


def build_rag_audit_record(
    txt_path: Path,
    scope: str,
    owner_id: int | None,
    assessment: dict,
) -> dict:
    """Build a JSON-serializable suspicious RAG audit record."""
    return {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "audit_type": "suspicious_rag",
        "file": {
            "name": txt_path.name,
            "stem": txt_path.stem,
            "scope": scope,
            "owner_id": owner_id,
            "path": str(txt_path),
        },
        "security": security_response(assessment, "checked"),
    }


def persist_suspicious_rag_audit_log(
    audit_dir: Path,
    txt_path: Path,
    scope: str,
    owner_id: int | None,
    assessment: dict,
) -> None:
    """Write an audit log for a suspicious RAG assessment."""
    if not assessment.get("has_any"):
        return
    try:
        audit_dir.mkdir(parents=True, exist_ok=True)
        rotate_rag_audit_logs(audit_dir)
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S%fZ")
        safe_stem = re.sub(r"[^\w.-]+", "_", txt_path.stem).strip("_") or "rag"
        path = audit_dir / f"suspicious_rag_{ts}_{safe_stem}_{secrets.token_hex(4)}.json"
        record = build_rag_audit_record(txt_path, scope, owner_id, assessment)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"[rag-audit] failed to persist audit log: {exc}")


async def get_or_create_rag_security_record(
    txt_path: Path,
    scope: str,
    owner_id: int | None,
    model: dict | None,
    audit_dir: Path,
    available_models_func: Callable[[], list[dict]],
    resolve_model_func: Callable[[str], dict],
    generate_ai_reply_func: Callable[[dict, list[SecurityMessage]], Awaitable[str]],
    exception_logger: Callable[[BaseException, dict], None] | None = None,
) -> dict:
    """Load or lazily create the security record for a RAG file."""
    files_dir = txt_path.parent
    security = prune_security_index_entries(files_dir)
    existing = security.get(txt_path.stem)
    if isinstance(existing, dict):
        return existing
    if not txt_path.exists():
        return {}
    try:
        assessment = await assess_rag_prompt_injection(
            txt_path.read_text(encoding="utf-8", errors="ignore"),
            txt_path.name,
            model,
            available_models_func,
            resolve_model_func,
            generate_ai_reply_func,
        )
        save_security_to_index(files_dir, txt_path.stem, assessment)
        persist_suspicious_rag_audit_log(audit_dir, txt_path, scope, owner_id, assessment)
        return assessment
    except Exception as exc:
        if exception_logger is not None:
            exception_logger(
                exc,
                {
                    "operation": "rag_security_assessment_for_chat",
                    "path": str(txt_path),
                    "scope": scope,
                    "owner_id": owner_id,
                },
            )
        return {}


async def should_exclude_rag_from_chat(
    txt_path: Path,
    scope: str,
    owner_id: int | None,
    model: dict | None,
    audit_dir: Path,
    available_models_func: Callable[[], list[dict]],
    resolve_model_func: Callable[[str], dict],
    generate_ai_reply_func: Callable[[dict, list[SecurityMessage]], Awaitable[str]],
    exception_logger: Callable[[BaseException, dict], None] | None = None,
) -> bool:
    """Return whether a RAG file should be withheld from chat context."""
    record = await get_or_create_rag_security_record(
        txt_path,
        scope,
        owner_id,
        model,
        audit_dir,
        available_models_func,
        resolve_model_func,
        generate_ai_reply_func,
        exception_logger,
    )
    return is_high_risk_rag_security(record)
