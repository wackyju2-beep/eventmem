"""eventmem / Hermes — turn-end extraction: ask the auxiliary model what to remember.

A pure Q&A turn never calls a tool, so ``evt_add`` never runs (design §4.5).  After the
turn ends the provider calls one auxiliary completion over (user message, reply), gets
strict JSON back, and writes 0–3 events through the core.  This is host work — it needs
*the host's* model route — so it lives in the adapter; the core only sees final events.

``_aux_llm`` is the single seam tests monkeypatch.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from eventmem_core.i18n import tr

from . import host

logger = logging.getLogger(__name__)

__all__ = ["AUX_TASK", "extract_system", "extract_events", "parse_events", "squash",
           "first_sentence"]

#: ``register()`` registers this as an auxiliary task, so it appears in the config
#: (``auxiliary.eventmem_extract.*``) and can be pointed at its own model.
AUX_TASK = "eventmem_extract"


def extract_system() -> str:
    """The extraction prompt, in the language active right now (not at import time)."""
    return tr(
        "你是事件记忆抽取器。读一轮对话，抽出 0–3 条值得长期记住的事件。\n"
        "每条一句话（第一人称，≤120 字），kind 只能是 决定 / 尝试 / 结果 / 变更 / 教训。\n"
        "记：结论、决定及理由、持久改变、踩过的坑；不记寒暄、过程性步骤、复述。\n"
        "实质对话（不是寒暄/确认）至少 1 条；只有纯寒暄才返回空数组。\n"
        '只输出严格 JSON，不要代码块、不要解释：{"events":[{"kind":"结果","text":"…"}]}',
        "You extract event memory. Read one conversation turn and pull out 0–3 events worth\n"
        "remembering long-term.\n"
        "One sentence per event (first person, ≤120 characters); kind is one of\n"
        "决定 / 尝试 / 结果 / 变更 / 教训.\n"
        "Record conclusions, decisions and their reasons, lasting changes, dead ends and the\n"
        "traps hit on the way; skip greetings, step-by-step narration and restatements.\n"
        "A substantive turn needs at least 1 event; an empty array is only for small talk.\n"
        'Output strict JSON only, no code fence, no prose: {"events":[{"kind":"结果","text":"…"}]}',
    )


def _aux_llm(messages: list[dict[str, Any]], *, max_tokens: int = 500,
             timeout: float = 60.0) -> Any:
    """One auxiliary-model call for post-turn extraction (tests monkeypatch this)."""
    return host.call_llm(messages, task=AUX_TASK, max_tokens=max_tokens, timeout=timeout)


def squash(text: Any, limit: int = 0) -> str:
    """Collapse whitespace (and optionally truncate) before storing a turn excerpt."""
    out = " ".join(str(text or "").split())
    if limit and len(out) > limit:
        out = out[: max(0, int(limit) - 1)] + "…"
    return out


def first_sentence(text: Any, limit: int) -> str:
    """First sentence/clause of a reply, whitespace-collapsed and capped at ``limit``."""
    out = " ".join(str(text or "").split())
    if not out:
        return ""
    cut = len(out)
    for mark in ("。", "！", "？", "!", "?", "；"):
        i = out.find(mark)
        if 0 <= i < cut:
            cut = i + 1
    if cut < 20:  # 首句太短（"好。"）时不要只留两个字
        cut = len(out)
    out = out[:cut]
    if len(out) > limit:
        out = out[: max(0, int(limit) - 1)] + "…"
    return out


def parse_events(raw: str, kinds: tuple[str, ...], limit: int = 3) -> Optional[list[dict[str, Any]]]:
    """Parse the extractor's JSON; ``[]`` for "nothing to record", ``None`` when unusable."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start:end + 1])
    except (TypeError, ValueError):
        return None
    items = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return None
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip()
        body = str(item.get("text") or "").strip()
        if kind in kinds and body:
            out.append({"kind": kind, "text": body})
        if len(out) >= limit:
            break
    return out


def extract_events(question: str, answer: str, *, kinds: tuple[str, ...],
                   limit: int = 3) -> Optional[list[dict[str, Any]]]:
    """Ask the auxiliary model to distill the turn; ``None`` when the call/parse failed."""
    try:
        response = _aux_llm([
            {"role": "system", "content": extract_system()},
            {"role": "user", "content": tr("用户：%s\n\n助手：%s", "User: %s\n\nAssistant: %s")
             % (question, answer)},
        ])
        raw = response.choices[0].message.content or ""
    except Exception as exc:  # noqa: BLE001 - caller falls back to the mechanical record
        logger.warning("eventmem: extraction call failed (%s)", exc)
        return None
    parsed = parse_events(raw, kinds, limit)
    if parsed is None:
        logger.warning("eventmem: extraction returned unparsable output")
    return parsed
