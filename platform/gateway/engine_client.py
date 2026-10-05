"""引擎内核进程管理（网关侧）。

把两个内核 worker 作为常驻子进程拉起，用 JSON-lines 协议调用：
  · 知识内核 → RAGFlow 解释器（ragflow 源码）
  · 编排内核 → Dify 解释器（graphon 引擎）

设计要点：
  * 一次调用一个往返，串行化（加锁）—— 内核本身有状态（源码导入、模型缓存），不做并发复用；
  * 读超时由后台线程 + 队列实现，避免 readline 永久阻塞；
  * 子进程崩了自动重启一次，并把 stderr 尾部带给调用方，便于定位（内核崩溃在 Windows 上会直接杀进程）。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

HOME = Path(__file__).resolve().parents[1]


def load_env() -> Dict[str, str]:
    """统一配置：platform/config/equipment.env（进程环境优先）。"""
    values: Dict[str, str] = {}
    env_file = Path(os.environ.get("EQUIPMENT_ENV", HOME / "config" / "equipment.env"))
    if env_file.exists():
        for raw in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    values.update({k: v for k, v in os.environ.items() if k.startswith(("KNOWLEDGE_", "ORCHESTRATION_", "EQUIPMENT_", "MODEL_", "CODE_EXEC_", "GATEWAY_"))})
    return values


ENV = load_env()


class EngineWorker:
    """一个内核 worker 的常驻进程 + 请求队列。"""

    def __init__(self, name: str, python: str, script: Path, timeout: float = 900.0):
        self.name = name
        self.python = python
        self.script = script
        self.timeout = timeout
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._stderr: List[str] = []
        self.restarts = 0

    # ------------------------------------------------------------ 进程
    def _spawn(self) -> None:
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", EQUIPMENT_ENV=str(HOME / "config" / "equipment.env"))
        self._proc = subprocess.Popen(
            [self.python, str(self.script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1, env=env, cwd=str(HOME),
        )
        self._queue = queue.Queue()
        threading.Thread(target=self._pump_stdout, daemon=True, name=f"{self.name}-stdout").start()
        threading.Thread(target=self._pump_stderr, daemon=True, name=f"{self.name}-stderr").start()

    def _pump_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            self._queue.put(line)
        self._queue.put(None)

    def _pump_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            self._stderr.append(line.rstrip())
            del self._stderr[:-40]

    def ensure(self) -> None:
        if self._proc is None or self._proc.poll() is not None:
            if self._proc is not None:
                self.restarts += 1
            self._spawn()

    def start(self) -> Optional[int]:
        """幂等拉起常驻进程（已存活则直接返回现有 PID，不计入重启次数）。"""
        with self._lock:
            self.ensure()
        return self.pid

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def pid(self) -> Optional[int]:
        """常驻子进程的 PID（未启动或已退出时为 None）。"""
        return self._proc.pid if self._proc is not None and self._proc.poll() is None else None

    def stderr_tail(self, lines: int = 12) -> str:
        return "\n".join(self._stderr[-lines:])

    def restart(self) -> Dict[str, Any]:
        """优雅停止并重新拉起 worker；返回新旧 PID 与累计重启次数。"""
        with self._lock:
            old_pid = self.pid
            proc = self._proc
            if proc is not None:
                try:
                    if proc.stdin:
                        proc.stdin.close()
                    proc.terminate()
                    proc.wait(timeout=10)
                except Exception:  # noqa: BLE001
                    try:
                        proc.kill()
                    except Exception:  # noqa: BLE001
                        pass
                self._proc = None
            self.restarts += 1
            self._spawn()
            assert self._proc is not None
            return {"name": self.name, "old_pid": old_pid, "pid": self._proc.pid, "restarts": self.restarts}

    # ------------------------------------------------------------ 调用
    def call(self, op: str, timeout: Optional[float] = None, **payload: Any) -> Dict[str, Any]:
        request = {"op": op, **payload}
        with self._lock:
            self.ensure()
            assert self._proc is not None and self._proc.stdin is not None
            request["id"] = int(time.time() * 1000)
            try:
                self._proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                self._spawn()
                raise RuntimeError(f"{self.name} 内核写入失败并已重启：{exc}") from exc

            deadline = time.time() + (timeout or self.timeout)
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError(f"{self.name} 内核调用超时（{op}）")
                try:
                    line = self._queue.get(timeout=min(remaining, 1.0))
                except queue.Empty:
                    if self._proc.poll() is not None:
                        tail = self.stderr_tail()
                        self._spawn()
                        raise RuntimeError(f"{self.name} 内核进程退出（exit={self._proc.returncode if self._proc else None}）：{tail[-600:]}")
                    continue
                if line is None:
                    tail = self.stderr_tail()
                    self._spawn()
                    raise RuntimeError(f"{self.name} 内核输出结束，已重启。stderr：{tail[-600:]}")
                line = line.strip()
                if not line:
                    continue
                try:
                    response = json.loads(line)
                except ValueError:
                    continue  # 内核的调试输出，忽略
                if response.get("id") != request["id"]:
                    continue
                return response

    def stop(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass
        self._proc = None


def build_workers() -> Dict[str, EngineWorker]:
    """按统一配置创建两个内核 worker。"""
    home = HOME
    return {
        "knowledge": EngineWorker(
            "knowledge",
            ENV.get("KNOWLEDGE_PYTHON", r"E:\llm-stack\src\ragflow-py\.venv\Scripts\python.exe"),
            home / "engine_knowledge" / "worker.py",
        ),
        "orchestration": EngineWorker(
            "orchestration",
            ENV.get("ORCHESTRATION_PYTHON", r"E:\llm-stack\src\dify\api\.venv\Scripts\python.exe"),
            home / "engine_orchestration" / "worker.py",
        ),
    }


def masked_config() -> Dict[str, Any]:
    """对外暴露的统一配置（api key 打码）。"""
    def mask(value: str) -> str:
        if not value:
            return ""
        return value[:4] + "****" + value[-4:] if len(value) > 8 else "****"

    keys = [k for k in ENV if k.startswith("MODEL_")]
    config: Dict[str, Any] = {"models": {}, "engines": {}}
    for key in sorted(keys):
        if key.endswith("_API_KEY"):
            config["models"][key] = mask(ENV[key])
        else:
            config["models"][key] = ENV[key]
    config["engines"] = {
        "knowledge": {"source": ENV.get("KNOWLEDGE_SOURCE", ""), "python": ENV.get("KNOWLEDGE_PYTHON", "")},
        "orchestration": {"source": ENV.get("ORCHESTRATION_SOURCE", ""), "python": ENV.get("ORCHESTRATION_PYTHON", "")},
    }
    config["code_exec"] = ENV.get("CODE_EXEC_ENDPOINT", "")
    return config
