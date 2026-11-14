"""eventmem / Hermes — the ``MemoryProvider`` surface.

This is the host contract: lifecycle hooks, the recall injection, the turn-end
extraction and the tool surface.  Every one of them is a thin shell around
:class:`eventmem_core.EventMem`; the memory logic lives there and is shared with any
other adapter.

The provider is created per agent (one profile per instance, per the multiplex
contract), so it binds its home, settings and core in ``initialize()`` and never keeps
profile state at module level.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from agent.memory_provider import MemoryProvider, RecallStatus

from eventmem_core import EventMem
from eventmem_core.i18n import tr
from eventmem_core.settings import CoreSettings

from . import settings as S
from . import tools
from .extract import ExtractError, extract_events, squash
from .host import spawn_context_thread

logger = logging.getLogger(__name__)

__all__ = ["EventMemProvider"]

PROVIDER_NAME = "eventmem"

#: 写进 ``events.agent`` 的宿主标签，也用作会话 id 的前缀。共用一个库时，这是「这条是谁记的」
#: 的第一手答案：dsh 那边用 ``dsh``，两边会话 id 也不会撞在一起（宿主自己的 session id 是
#: ``20261111_235957_fb7a8c`` 这种形式，不会和前缀冲突）。
AGENT = "hermes"


class EventMemProvider(MemoryProvider):
    """Event memory provider: append-only events, project skeleton, hybrid recall."""

    def __init__(self, *, settings_loader: Optional[Callable[..., CoreSettings]] = None) -> None:
        self._home = S.hermes_home()
        self._session_id = ""
        self._primary = True
        self._core: Optional[EventMem] = None
        self._settings: CoreSettings = CoreSettings()
        self._settings_loader = settings_loader or S.build
        self._lock = threading.RLock()
        self._session_project = ""
        self._last_recall: Optional[RecallStatus] = None
        # 跨轮召回状态（已给过正文的 id / 已给过骨架的项目）。会话边界、压缩、撤销时丢弃重建。
        self._recall_state: Optional[Any] = None
        self._init_error = ""
        self._warm_thread: Optional[threading.Thread] = None
        # 本回合是否已经写过事件（on_turn_start 置 False，evt_add 成功置 True）。
        # sync_turn 在回合结束后读它，决定要不要做回合末抽取（设计 §4.5）。
        self._turn_wrote = False

    # -- identity / lifecycle ------------------------------------------------

    def _sid(self, raw: str = "") -> Optional[str]:
        """会话 id 带上宿主前缀（``hermes:<宿主会话 id>``）。

        共用一个库时这是「谁是哪个 agent 的会话」的答案，也让 ``session_project`` 的粘性不会
        被另一个宿主里同名的会话抢走。前缀只加在这里——落库的会话字符串全程一致。
        """
        value = (raw or self._session_id or "").strip()
        if not value:
            return None
        return value if value.startswith(AGENT + ":") else "%s:%s" % (AGENT, value)

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    def is_available(self) -> bool:
        """A database path is the only hard requirement; embeddings degrade to FTS."""
        try:
            import eventmem_core.store  # noqa: F401
        except Exception:  # noqa: BLE001
            return False
        return True

    def unavailable_reason(self) -> str:
        if not self.is_available():
            return tr("eventmem: 无法导入 eventmem_core（core 未安装或损坏）",
                      "eventmem: cannot import eventmem_core (core missing or broken)")
        return ""

    def initialize(self, session_id: str, **kwargs) -> None:
        self._home = Path(kwargs.get("hermes_home") or self._home)
        self._session_id = session_id or ""
        context = str(kwargs.get("agent_context") or "primary")
        self._primary = context in ("primary", "")
        self._session_project = ""
        self._recall_state = None
        S.install_language_provider(self._home)
        self._settings = self._settings_loader(self._home)
        self._ensure_core()
        self._warm_encoder()

    def shutdown(self) -> None:
        with self._lock:
            core, self._core = self._core, None
        if core is not None:
            core.close()

    def on_session_switch(self, new_session_id: str, *, parent_session_id: str = "",
                          reset: bool = False, rewound: bool = False, **kwargs: Any) -> None:
        switched = bool(new_session_id) and new_session_id != self._session_id
        self._session_id = new_session_id or self._session_id
        if reset:
            self._session_project = ""
        # 新会话 / 压缩（就地压缩时 session_id 不变，靠 reason 识别）/ 撤销 之后，
        # 「上一轮给过什么」都不再成立：计数归零，下一轮按新会话给骨架。
        if reset or rewound or switched or str(kwargs.get("reason") or "") == "compression":
            self._recall_state = None

    def on_turn_start(self, turn_number: int, message: str, **kwargs: Any) -> None:
        """New turn: the write flag starts clean (design §4.5)."""
        self._turn_wrote = False

    def system_prompt_block(self) -> str:
        # Design §4.2: the write rules live in evt_add's tool description, not in a new
        # system-prompt section. Returning "" also keeps the cached prefix byte-stable.
        return ""

    def recall_status(self) -> Optional[RecallStatus]:
        return self._last_recall

    # -- core ----------------------------------------------------------------

    def _ensure_core(self) -> Optional[EventMem]:
        if self._core is not None:
            return self._core
        with self._lock:
            if self._core is not None:
                return self._core
            try:
                if not self._settings.db_path:
                    from dataclasses import replace

                    self._settings = replace(self._settings, db_path=S.db_path(self._home))
                Path(self._settings.db_path).parent.mkdir(parents=True, exist_ok=True)
                self._core = EventMem(self._settings, agent=AGENT)
                self._init_error = ""
            except Exception as exc:  # noqa: BLE001 - never crash a turn over storage
                self._init_error = f"{type(exc).__name__}: {exc}"
                logger.warning("eventmem: store init failed (%s)", self._init_error)
                self._core = None
            return self._core

    def _warm_encoder(self) -> None:
        """Load the ONNX session off the turn's critical path (~1.3 s cold)."""
        if self._warm_thread is not None and self._warm_thread.is_alive():
            return
        core = self._core
        if core is None:
            return
        try:
            thread = spawn_context_thread(core.load_encoder, name="eventmem-warm")
            thread.start()
            self._warm_thread = thread
        except Exception as exc:  # noqa: BLE001 - warm-up is optional
            logger.debug("eventmem: encoder warm-up skipped (%s)", exc)

    def _context(self) -> Optional[tools.ToolContext]:
        core = self._ensure_core()
        if core is None:
            return None
        if not self._session_project and self._session_id:
            self._session_project = core.session_project(self._sid() or "")
        return tools.ToolContext(core=core, settings=self._settings,
                                 session_id=self._sid() or "", primary=self._primary,
                                 session_project=self._session_project)

    # -- recall --------------------------------------------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> Any:
        """Recall for the upcoming turn; ``None`` when nothing matches."""
        self._last_recall = None
        text = (query or "").strip()
        if not text:
            return None
        ctx = self._context()
        if ctx is None:
            return None
        core = ctx.core

        if self._recall_state is None:
            self._recall_state = core.new_recall_state()
        state = self._recall_state
        # 一次编码，两处共用：recall 的向量臂与项目候选都要 query 向量，各编一次就是 60 ms。
        try:
            qvec = core.query_vector(text)
        except Exception:  # noqa: BLE001 - a broken encoder means "no vector arm"
            qvec = None
        try:
            block = core.recall(text, state=state, qvec=qvec)
        except Exception as exc:  # noqa: BLE001 - recall must never fail a turn
            logger.warning("eventmem: recall failed (%s)", exc)
            block = ""
        if not isinstance(block, str):
            block = ""
        # 项目候选（方案：向量匹配 → 只给标题，模型只能选不能造）。recall 空时也单独给这一行：
        # 「这一回合没有任何命中」正是模型最不知道该往哪个项目里写的时候。
        try:
            hint = core.project_hint(text, qvec=qvec) if text else ""
        except Exception as exc:  # noqa: BLE001 - candidates are a nicety, not a guarantee
            logger.debug("eventmem: project hint failed (%s)", exc)
            hint = ""
        if hint:
            block = (block.rstrip() + "\n\n" + hint) if block.strip() else hint
        if not block.strip():
            return None
        self._last_recall = RecallStatus(
            provider_label=PROVIDER_NAME,
            # Count rendered event lines (each ends with `#<id>`), not the block header.
            count=sum(1 for line in block.splitlines() if re.search(r"#\d+", line)),
        )
        return block

    # -- turn end ------------------------------------------------------------

    def sync_turn(self, user_content: str, assistant_content: str, *,
                  session_id: str = "", messages: Optional[list[dict[str, Any]]] = None,
                  turn_author: Optional[dict[str, Any]] = None) -> None:
        """Turn end: distill a turn that wrote no events into 0–3 events via the aux model.

        The host has no channel to call the main model after it stops, so this runs on the
        manager's background worker: one auxiliary completion over the user message and the
        reply, then normal event writes (project / session / vector included).
        """
        from .host import is_trivial

        if not self._primary or self._turn_wrote:
            return
        question = squash(user_content, 2000)
        answer = squash(assistant_content, 3000)
        if not question or not answer:
            return
        if is_trivial(user_content or ""):
            return
        core = self._ensure_core()
        if core is None:
            return

        limit = int(self._settings.max_events_per_call)
        try:
            events = extract_events(question, answer,
                                    kinds=("决定", "尝试", "结果", "变更", "教训"), limit=limit)
        except ExtractError as exc:
            # 抽取失败就**不写记忆**：这一轮没有值得长期保存的结论，机械复述一遍问题只是
            # 往库里灌噪声。失败本身留一行警告给控制台（概览页），由人去修模型配置。
            core.note_extract_failure(exc.reason, exc.detail,
                                      session=session_id or self._session_id or None)
            return
        if not events:
            logger.info("eventmem: extractor found nothing worth recording")
            return

        project = self._session_project or core.session_project(self._sid(session_id) or "") or None
        for item in events[:limit]:
            text = str(item.get("text") or "").strip()
            kind = str(item.get("kind") or "").strip()
            if not text or kind not in ("决定", "尝试", "结果", "变更", "教训"):
                continue
            try:
                core.add_event(kind, text, project=project, session=self._sid(session_id))
            except Exception as exc:  # noqa: BLE001 - never fail a turn over housekeeping
                logger.warning("eventmem: auto-record failed (%s)", exc)

    # -- tools ---------------------------------------------------------------

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return tools.schemas(self._settings)

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> str:
        ctx = self._context()
        if ctx is None:
            return json.dumps({"error": tr("eventmem 存储不可用", "eventmem storage unavailable"),
                               "detail": self._init_error}, ensure_ascii=False)
        out = tools.handle(tool_name, args or {}, ctx)
        if tool_name == "evt_add":
            self._turn_wrote = self._turn_wrote or _wrote_something(out)
        if ctx.session_project != self._session_project:
            self._session_project = ctx.session_project
            ctx.core.set_session_project(self._sid() or "", self._session_project)
        return out


def _wrote_something(payload: str) -> bool:
    try:
        data = json.loads(payload)
    except Exception:  # noqa: BLE001
        return False
    return bool(isinstance(data, dict) and data.get("ok") and int(data.get("count") or 0) > 0)
