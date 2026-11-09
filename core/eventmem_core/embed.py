"""eventmem.embed — 事件向量编码器（ONNX 真实实现 + 纯 Python 假实现）。

真实路径与 localmem 共用 Qwen3-Embedding-0.6B ONNX int8 模型：
last-token pooling → 截断到 ``dim``（MRL）→ **重新** L2 归一化。截断后不重新归一化，
"余弦"就变成 ``cos x ||v[:d]||``，排的是向量前段能量而不是语义相似度。

硬约束：``encode()`` 永不抛异常。模型缺失、onnxruntime/tokenizers/numpy 缺失、
ONNX 报错、空输入——一律返回 ``None``，由 recall 降级成纯 FTS。
"""

from __future__ import annotations

import hashlib
import importlib.util
import logging
import math
import os
import threading
from pathlib import Path
from typing import Any

try:  # numpy 不是硬依赖：没有它时真实编码器禁用、假编码器走纯 Python
    import numpy as _np
except Exception:  # noqa: BLE001
    _np = None

logger = logging.getLogger(__name__)

DEFAULT_DIM = 512
THREADS = 4
MODEL_FILES = ("model_int8.onnx", "model_quantized.onnx", "model.onnx")
INSTRUCTION = "Given a search query, retrieve relevant memories from past conversations: "


def _models_root() -> Path:
    """Where the ONNX model lives.  ``EVENTMEM_MODELS_ROOT`` wins; the default is the
    path the Hermes deployment has always used, kept so an adapter that says nothing
    keeps working.  A new host should pass ``CoreSettings.models_root`` instead."""
    explicit = os.environ.get("EVENTMEM_MODELS_ROOT")
    if explicit:
        return Path(explicit).expanduser()
    home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
    return home / "localmem" / "models"


def _missing_runtime() -> str:
    """Name of the first missing optional runtime, or ``""`` when all are importable."""
    for name in ("numpy", "onnxruntime", "tokenizers"):
        try:
            if importlib.util.find_spec(name) is None:
                return name
        except (ImportError, ValueError):
            return name
    return ""


def _finish(values: list[float]):
    """L2 归一化；numpy 存在时返回 float32 ndarray，否则返回 list[float]。"""
    norm = math.sqrt(sum(v * v for v in values))
    if not norm or not math.isfinite(norm):
        return None
    out = [v / norm for v in values]
    if _np is not None:
        return _np.asarray(out, dtype=_np.float32)
    return out


class Encoder:
    """Qwen3-Embedding-0.6B ONNX 编码器（懒加载，失败即降级）。"""

    def __init__(self, model_dir: str | Path | None = None, dim: int = DEFAULT_DIM,
                 threads: int = THREADS) -> None:
        self._dim = max(32, int(dim))
        self._root = Path(model_dir) if model_dir else _models_root()
        self._threads = max(1, int(threads))
        self._dir: Path | None = None
        self._resolved = False
        self._sess: Any = None
        self._tok: Any = None
        self._error = ""
        self._load_lock = threading.Lock()
        self._run_lock = threading.Lock()

    # -- discovery / lifecycle

    @staticmethod
    def _find(root: Path) -> Path | None:
        def ok(d: Path) -> bool:
            return (d / "tokenizer.json").is_file() and any(
                (d / name).is_file() for name in MODEL_FILES
            )

        try:
            if ok(root):
                return root
            if not root.is_dir():
                return None
            for sub in sorted(p for p in root.iterdir() if p.is_dir()):
                if ok(sub):
                    return sub
        except OSError:
            return None
        return None

    def _resolve(self) -> Path | None:
        if self._resolved:
            return self._dir
        self._resolved = True
        try:
            self._dir = self._find(self._root)
        except Exception as exc:  # noqa: BLE001
            self._error = f"{type(exc).__name__}: {exc}"
            self._dir = None
        return self._dir

    def available(self) -> bool:
        """True only when numpy *and* the ONNX runtime are importable and the model resolves.

        Checking the imports here (not only at ``load()``) is what makes a degraded install
        visible: without it ``available()`` says yes while every ``encode()`` returns ``None``.
        """
        missing = _missing_runtime()
        if missing:
            self._error = f"{missing} missing"
            return False
        return _np is not None and self._resolve() is not None

    def reason(self) -> str:
        """Why :meth:`available` is False (``""`` when it is True)."""
        if self.available():
            return ""
        return self._error or "encoder unavailable"

    def load(self) -> bool:
        if self._sess is not None:
            return True
        if _np is None:
            self._error = "numpy missing"
            return False
        model_dir = self._resolve()
        if model_dir is None:
            if not self._error:
                self._error = f"model not found under {self._root}"
            return False
        with self._load_lock:
            if self._sess is not None:
                return True
            try:
                import onnxruntime as ort
                from tokenizers import Tokenizer

                model_file = next(
                    (model_dir / name for name in MODEL_FILES if (model_dir / name).is_file()), None
                )
                if model_file is None:
                    self._error = f"no onnx file in {model_dir}"
                    return False
                opts = ort.SessionOptions()
                opts.intra_op_num_threads = self._threads
                opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
                self._sess = ort.InferenceSession(
                    str(model_file), opts, providers=["CPUExecutionProvider"]
                )
                self._tok = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
                logger.info("eventmem: encoder ready (%s)", model_file)
                return True
            except Exception as exc:  # noqa: BLE001 - 激活失败不能拖垮 agent
                self._error = f"{type(exc).__name__}: {exc}"
                self._sess = None
                self._tok = None
                logger.warning("eventmem: encoder load failed: %s", self._error)
                return False

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def error(self) -> str:
        return self._error

    # -- encode

    def encode(self, text: str, is_query: bool = False):
        """float32 / L2 归一化、长度 ``dim`` 的向量；任何失败返回 ``None``。"""
        try:
            if not isinstance(text, str) or not text.strip():
                return None
            if _np is None or not self.load():
                return None
            payload = (INSTRUCTION + text) if is_query else text
            ids = list(self._tok.encode(payload).ids)
            if not ids:
                return None
            arr = _np.asarray([ids], dtype=_np.int64)
            feeds: dict[str, Any] = {
                "input_ids": arr,
                "attention_mask": _np.ones_like(arr),
                "position_ids": _np.arange(arr.shape[1], dtype=_np.int64)[None, :],
            }
            names = {i.name for i in self._sess.get_inputs()}
            for name in names:
                if name.startswith("past_key_values."):
                    feeds[name] = _np.zeros((1, 8, 0, 128), dtype=_np.float32)
            feeds = {k: v for k, v in feeds.items() if k in names}
            with self._run_lock:
                out = self._sess.run(None, feeds)[0]
            vec = _np.asarray(out[0, -1, :], dtype=_np.float32)  # last-token pooling
            if vec.size == 0:
                return None
            vec = vec[: min(self._dim, vec.size)]
            norm = float(_np.linalg.norm(vec))
            if not norm or not math.isfinite(norm):
                return None
            return (vec / norm).astype(_np.float32)
        except Exception as exc:  # noqa: BLE001 - 调用方只看到 None
            self._error = f"{type(exc).__name__}: {exc}"
            logger.warning("eventmem: encode failed: %s", self._error)
            return None


class FakeEncoder:
    """测试用：字符 1/2/3-gram 稳定哈希到 dim 维，确定性、纯 Python、无依赖。

    用 blake2b 而不是内置 ``hash()``——后者按进程加盐，跑两次结果不同。
    """

    _WEIGHTS = ((1, 0.5), (2, 1.0), (3, 1.0))

    def __init__(self, dim: int = DEFAULT_DIM) -> None:
        self._dim = max(8, int(dim))

    @property
    def dim(self) -> int:
        return self._dim

    def encode(self, text: str, is_query: bool = False):
        if not isinstance(text, str):
            return None
        s = text.strip()
        if not s:
            return None
        vec = [0.0] * self._dim
        for n, weight in self._WEIGHTS:
            if len(s) < n:
                continue
            for i in range(len(s) - n + 1):
                gram = s[i:i + n].encode("utf-8")
                digest = hashlib.blake2b(gram, digest_size=8).digest()
                x = int.from_bytes(digest, "little")
                vec[x % self._dim] += weight if (x >> 63) & 1 else -weight
        return _finish(vec)
