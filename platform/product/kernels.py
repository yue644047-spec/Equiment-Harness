"""两个引擎内核的进程管理（产品侧单例）。

复用 gateway/engine_client.py 的 EngineWorker：知识内核 = RAGFlow 源码，编排内核 = Dify graphon。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

HOME = Path(__file__).resolve().parents[1]
if str(HOME / "gateway") not in sys.path:
    sys.path.insert(0, str(HOME / "gateway"))

from engine_client import ENV, build_workers  # noqa: E402

_workers = build_workers()


def knowledge(op: str, timeout: float = 900.0, **payload: Any) -> Dict[str, Any]:
    return _workers["knowledge"].call(op, timeout=timeout, **payload)


def orchestration(op: str, timeout: float = 900.0, **payload: Any) -> Dict[str, Any]:
    return _workers["orchestration"].call(op, timeout=timeout, **payload)


def health() -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name, worker in _workers.items():
        try:
            response = worker.call("health", timeout=180.0)
            out[name] = {"ok": bool(response.get("ok")), "alive": worker.alive,
                         "restarts": worker.restarts, "data": response.get("data"),
                         "error": response.get("error")}
        except Exception as exc:  # noqa: BLE001
            out[name] = {"ok": False, "alive": worker.alive, "error": str(exc)[:300],
                         "stderr": worker.stderr_tail()}
    return out


def stop() -> None:
    for worker in _workers.values():
        worker.stop()


def workers_info() -> List[Dict[str, Any]]:
    """内核 worker 的进程信息（不做调用，仅看进程本身）。"""
    out: List[Dict[str, Any]] = []
    for name, worker in _workers.items():
        out.append({
            "name": name,
            "pid": worker.pid,
            "alive": worker.alive,
            "restarts": worker.restarts,
            "python": worker.python,
            "script": str(worker.script),
        })
    return out


def warmup() -> Dict[str, Any]:
    """启动时后台预拉起两个内核进程（内核本身按需常驻，不预拉起则首次调用才出现进程）。

    逐项静默容错：任一内核拉起失败都不影响产品服务启动，进程页会如实显示 stopped。
    """
    out: Dict[str, Any] = {}
    for name, worker in _workers.items():
        try:
            out[name] = {"ok": True, "pid": worker.start()}
        except Exception as exc:  # noqa: BLE001
            out[name] = {"ok": False, "error": str(exc)[:300]}
    return out


def restart(name: str) -> Dict[str, Any]:
    """重启指定内核 worker，返回新旧 PID 与累计重启次数。"""
    worker = _workers.get(name)
    if worker is None:
        raise KeyError(name)
    return worker.restart()


def tokenize_batch(texts: List[str]) -> List[str]:
    """用 RAGFlow 的分词器批量切词（保证与知识内核同口径）；失败时退化为简单切分。"""
    if not texts:
        return []
    out: List[str] = []
    chunk_size = 64
    for start in range(0, len(texts), chunk_size):
        batch = texts[start:start + chunk_size]
        try:
            response = knowledge("tokenize", timeout=300.0, texts=batch)
        except Exception:  # noqa: BLE001
            response = None
        if response and response.get("ok"):
            out.extend(response["data"].get("results") or ["" for _ in batch])
        else:
            out.extend(" ".join(_simple_tokens(text)) for text in batch)
    return out


def _simple_tokens(text: str) -> List[str]:
    import re
    tokens: List[str] = []
    for piece in re.findall(r"[0-9A-Za-z_]+|[\u4e00-\u9fff]+", text or ""):
        if piece.isascii():
            tokens.append(piece.lower())
        else:
            tokens.extend(piece[i:i + 2] for i in range(max(1, len(piece) - 1)))
    return tokens
