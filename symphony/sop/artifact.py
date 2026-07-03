"""Artifact domain model — structured node inputs/outputs for SOPs.

Each SOP node declares the *type* of artifact it consumes (input) and produces
(output), plus free-form natural-language conditions. Types are a small preset
enum so the UI can render dropdowns and the executor can format-check values
(e.g. a Feishu doc must be a valid Feishu URL). Content conditions (e.g. "must
contain 背景/SQL/DAG") are NOT hard-checked here — they are passed into the pi
prompt for the agent to self-verify.

This module deliberately does NOT import ``sop_definition`` to avoid a circular
import (``sop_definition`` imports ``ArtifactType`` from here).
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import BaseModel


class ArtifactType(str, Enum):
    """Preset artifact types a node can consume/produce."""

    FEISHU_DOC = "feishu_doc"
    SQL = "sql"
    TASK_ID = "task_id"
    LINK = "link"
    TEXT = "text"  # default / fallback — keeps old SOPs and ad-hoc Q&A working


class Artifact(BaseModel):
    """A single structured node artifact."""

    type: ArtifactType = ArtifactType.TEXT
    value: str = ""
    label: str | None = None


# Feishu/Lark doc URLs: docx/docs/sheets/wiki/base/file/drive on the known hosts.
_FEISHU_URL = re.compile(
    r"https?://[\w.-]*(?:feishu\.cn|larksuite\.com|feishu-pre\.net)/"
    r"(?:docx|docs|sheets|wiki|base|file|drive|sheet|mindnote)/\S+",
    re.IGNORECASE,
)
_GENERIC_URL = re.compile(r"https?://\S+", re.IGNORECASE)


def validate_artifact_format(atype: ArtifactType, value: str) -> tuple[bool, str]:
    """Format-only validation (no content / pi judgement).

    Returns ``(ok, error_message)``. Empty ``error_message`` when ok.
    """
    v = (value or "").strip()
    if not v:
        return False, "产物值不能为空"
    if atype == ArtifactType.FEISHU_DOC:
        return (True, "") if _FEISHU_URL.fullmatch(v) else (False, "必须是合法的飞书文档链接")
    if atype == ArtifactType.LINK:
        return (True, "") if _GENERIC_URL.fullmatch(v) else (False, "必须是合法的 URL")
    # sql / task_id / text: any non-empty value is acceptable.
    return True, ""


def extract_artifact(result: Any, atype: ArtifactType) -> Artifact | None:
    """Extract a structured artifact from a node result (pi/LLM output).

    Resolution order:
      1. An explicit ``{"artifact": {type,value,label}}`` JSON object in the
         node's ``output`` text (raw JSON / ```json fenced / first balanced obj).
      2. Type-specific fallback: for feishu_doc/link, scan the body for a URL.
      3. For text, take the whole trimmed body.
    Returns ``None`` when nothing usable can be recovered.
    """
    from symphony.sop.schema_validator import _parse_json_text

    text = result.get("output") if isinstance(result, dict) else result
    if not isinstance(text, str):
        text = ""

    parsed = _parse_json_text(text) if text else None
    if isinstance(parsed, dict) and isinstance(parsed.get("artifact"), dict):
        a = parsed["artifact"]
        raw_type = a.get("type") or atype.value
        try:
            resolved = ArtifactType(raw_type)
        except ValueError:
            resolved = atype
        return Artifact(
            type=resolved,
            value=str(a.get("value", "")),
            label=a.get("label"),
        )

    if text:
        if atype in (ArtifactType.FEISHU_DOC, ArtifactType.LINK):
            m = (_FEISHU_URL.search(text) if atype == ArtifactType.FEISHU_DOC else None) \
                or _GENERIC_URL.search(text)
            if m:
                return Artifact(type=atype, value=m.group(0))
        if atype == ArtifactType.TEXT:
            return Artifact(type=ArtifactType.TEXT, value=text.strip())

    return None


def _iter_balanced_objects(text: str):
    """Yield every top-level balanced ``{...}`` substring in ``text``.

    Unlike schema_validator._first_json_object (which returns only the first),
    this scans the whole body so a ``needs_user_input`` block can be found even
    when it appears after other JSON (e.g. a fenced artifact block)."""
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "{":
            depth = 0
            for j in range(i, n):
                c = text[j]
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        yield text[i:j + 1]
                        i = j + 1
                        break
            else:
                return
        else:
            i += 1


def extract_needs_user_input(result: Any) -> dict | None:
    """Extract a ``needs_user_input`` request from a node's output.

    The prompt contract asks the model, when it must obtain information from the
    user before continuing, to emit a JSON block:
        {"needs_user_input": {"questions": [{"key","question","type"}], "reason"}}

    Returns the inner dict ``{"questions": [...], "reason": ...}`` (with a
    normalized non-empty ``questions`` list) or ``None`` when absent.
    """
    import json

    text = result.get("output") if isinstance(result, dict) else result
    if not isinstance(text, str) or "needs_user_input" not in text:
        return None

    for candidate in _iter_balanced_objects(text):
        if "needs_user_input" not in candidate:
            continue
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        block = obj.get("needs_user_input") if isinstance(obj, dict) else None
        if not isinstance(block, dict):
            continue
        questions = block.get("questions")
        if not isinstance(questions, list) or not questions:
            continue
        norm = []
        for q in questions:
            if isinstance(q, dict) and q.get("question"):
                norm.append({
                    "key": str(q.get("key") or f"q{len(norm) + 1}"),
                    "question": str(q["question"]),
                    "type": str(q.get("type") or "text"),
                })
        if not norm:
            continue
        return {"questions": norm, "reason": str(block.get("reason") or "")}
    return None
