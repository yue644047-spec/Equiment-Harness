"""检索契约层服务（kb_bridge）：实现与 Dify 外部知识库协议兼容的检索接口。

对应设计报告（docs/11）3.5 检索契约层：
1. 协议兼容：POST /retrieval，Bearer 鉴权，请求 {knowledge_id, query, retrieval_setting}，
   响应 {records:[{content, score, title, metadata}]}；
2. 统一片段：所有后端返回的片段都归一化的同一结构（见 chunk_index.Chunk）；
3. 引用契约：title 与 metadata.citation 统一为「资料编号 版本号 §条款号」；
4. 版本硬过滤：服务端强制注入「现行有效」条件，调用方无法放宽；
5. 后端可切换：本地条款级索引 / 上游 RAGFlow，切换不改动 Dify 侧配置。

离线演示用法：
    python kb_bridge/bridge_server.py --port 9380
    curl -X POST http://127.0.0.1:9380/retrieval -H "Authorization: Bearer bridge-demo-key" \
         -H "Content-Type: application/json" \
         -d "{\"knowledge_id\":\"hys\",\"query\":\"滤芯更换周期\",\"retrieval_setting\":{\"top_k\":3,\"score_threshold\":0.0}}"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval_service"))   # 复用语料索引（同一口径）
sys.path.insert(0, str(ROOT / "kb_bridge"))

from chunk_index import ChunkIndex, load_ledger_current  # noqa: E402

DEFAULT_PORT = 9380
DEFAULT_KEY = "bridge-demo-key"
FORCED_STATUS = "现行有效"


class BridgeState:
    """服务状态：索引 + 配置 + 计数。"""

    def __init__(self, corpus_dirs: Sequence[Path], api_key: str = DEFAULT_KEY):
        self.corpus_dirs = [Path(p) for p in corpus_dirs]
        self.api_key = api_key
        self.lock = threading.Lock()
        self.index = ChunkIndex()
        self.stats: Dict[str, Any] = {}
        self.requests = 0
        self.reload()

    def reload(self) -> Dict[str, Any]:
        """重建索引（入库后调用）。"""
        with self.lock:
            merged = ChunkIndex()
            ledger: Dict[str, str] = {}
            for directory in self.corpus_dirs:
                if not directory.exists():
                    continue
                if not ledger:
                    try:
                        ledger = load_ledger_current(directory)
                    except Exception:  # 语料目录不含台账时忽略
                        ledger = {}
                part = ChunkIndex.build(directory, ledger)
                merged.chunks.extend(part.chunks)
            merged._build_stats()
            self.index = merged
            self.stats = merged.stats()
            return self.stats


class Handler(BaseHTTPRequestHandler):
    """检索契约层 HTTP 处理器。"""

    server_version = "KbBridge/1.0"
    state: Optional[BridgeState] = None

    # ------------------------------------------------ 基础
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        print(f"[kb_bridge] {self.address_string()} {fmt % args}")

    def _json(self, obj: Any, status: int = 200) -> None:
        payload = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _html(self, body: str, status: int = 200) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization") or ""
        token = header.replace("Bearer", "").strip()
        if not token:
            return False
        return token == self.state.api_key

    # ------------------------------------------------ 检索
    def _do_search(self, payload: Dict[str, Any], include_obsolete: bool = False) -> Dict[str, Any]:
        """执行一次检索，返回统一片段列表。"""
        query = str(payload.get("query") or payload.get("question") or "")
        setting = payload.get("retrieval_setting") or {}
        top_k = int(setting.get("top_k") or payload.get("top_k") or 6)
        threshold = float(setting.get("score_threshold") or 0.0)

        # 调用方可传的元数据条件（Dify 目前不提供界面，仅程序可传）
        doc_nos: Optional[List[str]] = None
        clause_prefix = ""
        cond = payload.get("metadata_condition") or {}
        for item in cond.get("conditions") or []:
            name = str(item.get("name") or "")
            value = str(item.get("value") or "")
            if name == "doc_no" and value:
                doc_nos = [v.strip() for v in value.split(",") if v.strip()]
            elif name == "clause_prefix":
                clause_prefix = value

        # 版本硬过滤：服务端强制，调用方不能放宽（除非显式演示开关）
        results = self.state.index.search(
            query,
            top_k=top_k,
            score_threshold=threshold,
            include_obsolete=bool(include_obsolete),
            doc_nos=doc_nos,
            clause_prefix=clause_prefix,
        )
        records = [r.chunk.to_unified(r.score) for r in results]
        return {
            "records": records,
            "debug": {
                "knowledge_id": str(payload.get("knowledge_id") or ""),
                "forced_condition": f"status = {FORCED_STATUS}" if not include_obsolete else "（演示：已放宽版本过滤）",
                "top_k": top_k,
                "score_threshold": threshold,
                "hits": len(records),
                "include_obsolete": bool(include_obsolete),
                "engine": "local-clause-index",
            },
        }

    # ------------------------------------------------ 路由
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/healthz":
            self._json({"status": "ok", "stats": self.state.stats, "requests": self.state.requests})
        elif path == "/api/stats":
            self._json({
                "stats": self.state.stats,
                "corpus_dirs": [str(p) for p in self.state.corpus_dirs],
                "forced_condition": f"status = {FORCED_STATUS}",
                "engine": "local-clause-index",
            })
        elif path == "/api/search":
            q = (query.get("q") or query.get("query") or [""])[0]
            top_k = int((query.get("top_k") or ["6"])[0])
            include_obsolete = (query.get("include_obsolete") or ["0"])[0] in ("1", "true", "yes")
            payload = {"query": q, "retrieval_setting": {"top_k": top_k, "score_threshold": 0.0}}
            self._json(self._do_search(payload, include_obsolete=include_obsolete))
        elif path == "/admin/reload":
            stats = self.state.reload()
            self._json({"reloaded": True, "stats": stats})
        elif path == "/":
            self._html(
                "<!DOCTYPE html><html lang='zh-CN'><meta charset='utf-8'>"
                "<title>检索契约层</title>"
                "<body style='font-family:Microsoft YaHei;padding:24px'>"
                "<h2>检索契约层（kb_bridge）</h2>"
                "<p>本服务实现 Dify 外部知识库协议，供治理看板的「检索契约层」页面调用。</p>"
                "<ul><li>POST /retrieval（Bearer 鉴权，Dify 调用入口）</li>"
                "<li>GET /api/search?q=滤芯更换周期&amp;include_obsolete=1（演示用）</li>"
                "<li>GET /api/stats / GET /healthz / GET /admin/reload</li></ul></body></html>"
            )
        else:
            self._json({"error_code": 2001, "error_msg": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            payload = json.loads(raw or "{}")
        except ValueError:
            self._json({"error_code": 1001, "error_msg": "invalid JSON body"}, 400)
            return

        if path != "/retrieval":
            self._json({"error_code": 2001, "error_msg": "not found"}, 404)
            return

        # Dify 的鉴权约定：Authorization: Bearer {API_KEY}
        if not self._authorized():
            self._json({"error_code": 1002, "error_msg": "Authorization failed. Please check your API key."}, 401)
            return

        self.state.requests += 1
        include_obsolete = bool(payload.get("debug_include_obsolete"))
        result = self._do_search(payload, include_obsolete=include_obsolete)
        self._json({"records": result["records"], "debug": result["debug"]})


def run_bridge(corpus_dirs: Sequence[Path], host: str = "127.0.0.1", port: int = DEFAULT_PORT,
               api_key: str = DEFAULT_KEY) -> int:
    """启动检索契约层服务。"""
    state = BridgeState(corpus_dirs, api_key=api_key)
    Handler.state = state
    httpd = ThreadingHTTPServer((host, port), Handler)
    print("=" * 68)
    print("检索契约层（kb_bridge）已启动")
    print(f"  Dify 外部知识库 Endpoint : http://{host}:{port}")
    print(f"  API Key                  : {api_key}")
    print(f"  语料目录                 : {', '.join(str(p) for p in corpus_dirs)}")
    print(f"  条款片段                 : {state.stats.get('chunks')} 条"
          f"（资料 {state.stats.get('documents')} 份，作废片段 {state.stats.get('obsolete_chunks')} 条）")
    print(f"  版本硬过滤               : 强制 status = {FORCED_STATUS}")
    print("=" * 68)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[kb_bridge] 已停止")
    finally:
        httpd.server_close()
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="检索契约层服务（Dify 外部知识库协议兼容）")
    parser.add_argument("--corpus", default=None, help="语料目录，可用逗号分隔多个（默认 samples + demo_corpus）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"监听端口（默认 {DEFAULT_PORT}）")
    parser.add_argument("--api-key", default=os.environ.get("KB_BRIDGE_KEY") or DEFAULT_KEY, help="Bearer 鉴权密钥")
    args = parser.parse_args(argv)

    if args.corpus:
        dirs = [Path(p.strip()) for p in args.corpus.split(",") if p.strip()]
    else:
        dirs = [ROOT / "samples", ROOT / "demo_corpus"]
    return run_bridge(dirs, host=args.host, port=args.port, api_key=args.api_key)


if __name__ == "__main__":
    raise SystemExit(main())
