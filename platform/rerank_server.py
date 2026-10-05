"""本地重排服务（BGE Reranker v2 m3）——OpenAI 兼容的 /v1/rerank。

背景：本机（演示/部署环境）没有可用的重排服务——Ollama 不提供 rerank 接口（实测
/v1/rerank 与 /api/rerank 均 404）、未安装 Xinference/TEI/Infinity、RAGFlow 侧也只注册了
chat/embedding 模型。产品的「模型角色 · 重排」因此在门户系统设置里长期显示为缺失。

本服务只做一件事：把 `BAAI/bge-reranker-v2-m3` 交叉编码器加载起来，用**标准库**
HTTP 服务暴露成模型网关通用的 /v1/rerank 契约，让产品（以及任何 OpenAI 兼容客户端）
按「角色 → base_url + 模型名」把它当成一路模型接入，与既有 Ollama(bge-m3) / DeepSeek
的接入方式完全一致。

契约（Jina / Cohere / vLLM 同形）：

    POST /v1/rerank
    {"model": "bge-reranker-v2-m3", "query": "…", "documents": ["…", "…"], "top_n": 5}
    →  {"model": "bge-reranker-v2-m3", "results": [
            {"index": 3, "relevance_score": 0.87, "document": "…"}, …],
        "usage": {"total_tokens": 1234}}

    GET /health   →  {"ok": true, "model": …, "device": …, "loaded_ms": …}
    GET /v1/models → OpenAI 形模型清单（部分客户端启动时会探它）

配置（命令行优先，其次 equipment.env，其次默认值）：
    RERANK_MODEL_PATH  BAAI/bge-reranker-v2-m3 权重目录
    RERANK_HOST/RERANK_PORT        默认 127.0.0.1:8196
    RERANK_DEVICE      auto（默认）/ cuda / cpu
    RERANK_BATCH       推理批大小（默认 16）
    RERANK_MAX_LENGTH  单对文本截断长度（默认 512）
    RERANK_API_KEY     非空时校验 Authorization: Bearer

运行（独立 venv，不污染产品与内核解释器）：
    E:\\llm-stack\\src\\rerank-py\\.venv\\Scripts\\python.exe platform\\rerank_server.py
或直接 `启动重排服务.bat`。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
DEFAULT_ENV = HERE / "config" / "equipment.env"
DEFAULT_MODEL_PATH = r"E:\llm-stack\data\models\bge-reranker-v2-m3"


def load_env(path: Path) -> Dict[str, str]:
    """与 platform/serve.py 同款的极简 .env 解析（KEY=VALUE）。"""
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


class Reranker:
    """交叉编码器打分器：一次性加载，串行推理（单卡/单进程，串行最稳）。"""

    def __init__(self, model_path: str, device: str, batch_size: int, max_length: int):
        import torch  # 延迟导入，--help 与配置错误时不必等 torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        self.torch = torch
        path = Path(model_path)
        if not path.exists():
            raise SystemExit(f"[rerank] 模型目录不存在：{path}（先跑下载，或用 --model-path 指定）")

        self.name = path.name
        self.batch_size = batch_size
        self.max_length = max_length
        self.device = self._pick_device(device)

        started = time.perf_counter()
        self.tokenizer = AutoTokenizer.from_pretrained(str(path))
        self.model = AutoModelForSequenceClassification.from_pretrained(str(path), num_labels=1)
        self.model.eval()
        self.model.to(self.device)
        if self.device == "cuda":
            self.model.half()
        self.loaded_ms = int((time.perf_counter() - started) * 1000)
        self._lock = threading.Lock()
        print(f"[rerank] 模型 {self.name} 已加载：device={self.device} "
              f"dtype={'fp16' if self.device == 'cuda' else 'fp32'} "
              f"batch={self.batch_size} max_length={self.max_length} 耗时={self.loaded_ms}ms", flush=True)

    def _pick_device(self, wanted: str) -> str:
        torch = self.torch
        wanted = (wanted or "auto").strip().lower()
        available = bool(torch.cuda.is_available())
        if wanted == "cpu" or not available:
            if wanted == "cuda" and not available:
                print("[rerank] 警告：请求 cuda 但 torch.cuda.is_available()=False，回落 CPU", flush=True)
            return "cpu"
        return "cuda"

    def score(self, query: str, documents: List[str], top_n: int | None = None) -> Dict[str, Any]:
        torch = self.torch
        pairs = [[query, doc] for doc in documents]
        scores: List[float] = []
        tokens = 0
        with self._lock:
            for start in range(0, len(pairs), self.batch_size):
                batch = pairs[start:start + self.batch_size]
                encoded = self.tokenizer(batch, padding=True, truncation=True,
                                         max_length=self.max_length, return_tensors="pt")
                tokens += int(encoded["input_ids"].numel())
                encoded = {k: v.to(self.device) for k, v in encoded.items()}
                with torch.no_grad():
                    if self.device == "cuda":
                        with torch.autocast(device_type="cuda", dtype=torch.float16):
                            logits = self.model(**encoded).logits
                    else:
                        logits = self.model(**encoded).logits
                scores.extend(torch.sigmoid(logits.view(-1).float()).cpu().tolist())

        order = sorted(range(len(documents)), key=lambda i: scores[i], reverse=True)
        if top_n:
            order = order[: max(0, int(top_n))]
        return {
            "model": self.name,
            "results": [{"index": i, "relevance_score": round(float(scores[i]), 6),
                         "document": documents[i]} for i in order],
            "usage": {"total_tokens": tokens},
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "equipment-rerank/1.0"
    reranker: Reranker = None  # type: ignore[assignment]
    api_key: str = ""

    # ---------------------------------------------------------------- 工具
    def _send(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not self.api_key:
            return True
        header = self.headers.get("Authorization") or ""
        return header.strip() == f"Bearer {self.api_key}"

    def log_message(self, fmt: str, *args: Any) -> None:  # 默认日志太吵，只留一行摘要
        sys.stderr.write("[rerank] %s - %s\n" % (self.address_string(), fmt % args))

    # ---------------------------------------------------------------- 路由
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path in ("/health", "/v1/health"):
            self._send(200, {"ok": True, "model": self.reranker.name, "device": self.reranker.device,
                             "loaded_ms": self.reranker.loaded_ms,
                             "max_length": self.reranker.max_length,
                             "batch_size": self.reranker.batch_size})
        elif path in ("/v1/models", "/models"):
            self._send(200, {"object": "list", "data": [
                {"id": self.reranker.name, "object": "model", "owned_by": "local"}]})
        else:
            self._send(404, {"error": {"message": f"未知路径 {self.path}", "type": "not_found"}})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path not in ("/v1/rerank", "/rerank"):
            self._send(404, {"error": {"message": f"未知路径 {self.path}", "type": "not_found"}})
            return
        if not self._authorized():
            self._send(401, {"error": {"message": "API key 无效", "type": "invalid_request_error"}})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            self._send(400, {"error": {"message": f"请求体不是合法 JSON：{exc}", "type": "invalid_request_error"}})
            return

        query = payload.get("query") or payload.get("input") or ""
        documents = payload.get("documents") or payload.get("docs") or []
        if isinstance(documents, str):
            documents = [documents]
        if not query or not isinstance(documents, list) or not documents:
            self._send(400, {"error": {"message": "需要 query 与非空 documents 数组", "type": "invalid_request_error"}})
            return
        try:
            result = self.reranker.score(str(query), [str(d) for d in documents], payload.get("top_n"))
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": {"message": f"推理失败：{exc}", "type": "server_error"}})
            return
        self._send(200, result)


def main(argv: List[str] | None = None) -> int:
    env = load_env(Path(os.environ.get("EQUIPMENT_ENV", DEFAULT_ENV)))
    parser = argparse.ArgumentParser(description="装备数据治理 · 本地重排服务（/v1/rerank）")
    parser.add_argument("--host", default=env.get("RERANK_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(env.get("RERANK_PORT", "8196")))
    parser.add_argument("--model-path", default=env.get("RERANK_MODEL_PATH", DEFAULT_MODEL_PATH))
    parser.add_argument("--device", default=env.get("RERANK_DEVICE", "auto"))
    parser.add_argument("--batch-size", type=int, default=int(env.get("RERANK_BATCH", "16")))
    parser.add_argument("--max-length", type=int, default=int(env.get("RERANK_MAX_LENGTH", "512")))
    parser.add_argument("--api-key", default=env.get("RERANK_API_KEY", ""))
    args = parser.parse_args(argv)

    Handler.reranker = Reranker(args.model_path, args.device, args.batch_size, args.max_length)
    Handler.api_key = args.api_key
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[rerank] 监听 http://{args.host}:{args.port}/v1/rerank"
          f"{'（已启用 API key 校验）' if args.api_key else ''}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[rerank] 收到中断，退出", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
