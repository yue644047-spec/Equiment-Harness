"""知识引擎内核（直接套用 RAGFlow 源码，不经过 RAGFlow 服务）。

协议：stdin 一行一个 JSON 请求，stdout 一行一个 JSON 响应（JSON-lines）。
  {"id": 1, "op": "health"}
  {"id": 2, "op": "supported"}
  {"id": 3, "op": "chunk", "path": "...docx", "chunk_method": "naive", "parser_config": {...}}
  {"id": 4, "op": "chunk", "name": "a.txt", "text": "正文...", "chunk_method": "naive"}
  {"id": 5, "op": "tokenize", "text": "..."}

依赖：必须用 RAGFlow 的解释器运行（KNOWLEDGE_PYTHON），因为 rag.app / rag.nlp / deepdoc
都来自 RAGFlow 源码树。这里只做"调用与归一化"，解析与分块逻辑一行都不重写。
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------- 源码与运行环境


def _load_env() -> Dict[str, str]:
    """读取统一配置（platform/config/equipment.env）。"""
    here = Path(__file__).resolve().parents[1]
    env_file = Path(os.environ.get("EQUIPMENT_ENV", here / "config" / "equipment.env"))
    values: Dict[str, str] = {}
    if env_file.exists():
        for raw in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    for key, value in os.environ.items():
        if key.startswith(("KNOWLEDGE_", "EQUIPMENT_", "MODEL_")):
            values[key] = value
    return values


# Windows 控制台默认 GBK：内核与网关之间必须强制 UTF-8，否则中文切片内容会被替换成乱码。
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

ENV = _load_env()
SOURCE = Path(ENV.get("KNOWLEDGE_SOURCE", r"E:\llm-stack\src\ragflow-py"))
if ENV.get("KNOWLEDGE_NLTK_DATA"):
    os.environ.setdefault("NLTK_DATA", ENV["KNOWLEDGE_NLTK_DATA"])
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))
os.chdir(SOURCE)

# ---------------------------------------------------------------- 解析器分派

#: chunk_method → RAGFlow 解析器模块（键与 RAGFlow API 的 chunk_method 完全一致）
PARSER_MODULES: Dict[str, str] = {
    "naive": "rag.app.naive",
    "paper": "rag.app.paper",
    "book": "rag.app.book",
    "manual": "rag.app.manual",
    "laws": "rag.app.laws",
    "presentation": "rag.app.presentation",
    "table": "rag.app.table",
    "qa": "rag.app.qa",
    "picture": "rag.app.picture",
    "one": "rag.app.one",
    "email": "rag.app.email",
    "tag": "rag.app.tag",
}

#: 扩展名 → 默认 chunk_method
DEFAULT_BY_SUFFIX: Dict[str, str] = {
    ".pdf": "naive", ".docx": "naive", ".doc": "naive", ".txt": "naive", ".md": "naive",
    ".markdown": "naive", ".html": "naive", ".htm": "naive", ".json": "naive", ".csv": "naive",
    ".xlsx": "naive", ".xls": "naive", ".pptx": "presentation", ".ppt": "presentation",
    ".png": "picture", ".jpg": "picture", ".jpeg": "picture", ".bmp": "picture",
    ".eml": "email", ".msg": "email",
}

DEFAULT_PARSER_CONFIG: Dict[str, Any] = {
    "chunk_token_num": 512,
    "delimiter": "\n!?;。；！？",
    "layout_recognize": "Naive",
    "html4excel": False,
    "auto_keywords": 0,
    "auto_questions": 0,
}


def parser_for(name: str, chunk_method: Optional[str]) -> str:
    """选解析器：显式 chunk_method 优先，否则按扩展名。"""
    if chunk_method and chunk_method in PARSER_MODULES:
        return chunk_method
    return DEFAULT_BY_SUFFIX.get(Path(name).suffix.lower(), "naive")


def _progress(_progress: float, _message: str = "") -> None:
    """RAGFlow 解析器的进度回调；内核模式下不向前端推流，只保证可调用。"""


def _call_chunk(module: Any, name: str, binary: Optional[bytes], text: Optional[str],
                parser_config: Dict[str, Any], from_page: int, to_page: int,
                lang: str) -> List[Dict[str, Any]]:
    """调用 RAGFlow 的 chunk()。不同解析器参数面不同，按"全参数 → 精简参数"降级调用。"""
    rich = {"binary": binary, "from_page": from_page, "to_page": to_page, "lang": lang,
            "callback": _progress, "parser_config": parser_config}
    if text is not None and binary is None:
        rich["binary"] = text.encode("utf-8")
    try:
        return list(module.chunk(name, **rich))
    except TypeError:
        minimal: Dict[str, Any] = {"callback": _progress, "parser_config": parser_config}
        if binary is not None:
            minimal["binary"] = binary
        return list(module.chunk(name, **minimal))


def _normalize(chunks: List[Any], name: str) -> List[Dict[str, Any]]:
    """把 RAGFlow 的片段结构归一化成产品契约（保留原文与可检索字段）。"""
    out: List[Dict[str, Any]] = []
    for index, chunk in enumerate(chunks):
        if not isinstance(chunk, dict):
            chunk = {"content_with_weight": str(chunk)}
        content = str(chunk.get("content_with_weight") or chunk.get("content") or "")
        keywords = chunk.get("important_kwd") or []
        out.append({
            "index": index,
            "content": content,
            "chars": len(content),
            "tokens": len(chunk.get("content_ltks") or []) or None,
            "title": chunk.get("title_tks") or "",
            "doc_name": chunk.get("docnm_kwd") or name,
            "important_keywords": list(keywords) if isinstance(keywords, list) else [],
            "has_table": "<table" in content.lower(),
            "has_image": "<img" in content.lower(),
        })
    return out


# ---------------------------------------------------------------- 操作


def op_health(_req: Dict[str, Any]) -> Dict[str, Any]:
    from rag.nlp import rag_tokenizer  # noqa: F401  证明源码可用
    return {
        "ok": True,
        "source": str(SOURCE),
        "python": sys.version.split()[0],
        "parsers": sorted(PARSER_MODULES),
        "tokenizer": "rag.nlp.rag_tokenizer",
    }


def op_supported(_req: Dict[str, Any]) -> Dict[str, Any]:
    return {"chunk_methods": sorted(PARSER_MODULES), "by_suffix": DEFAULT_BY_SUFFIX,
            "default_parser_config": DEFAULT_PARSER_CONFIG}


def op_tokenize(req: Dict[str, Any]) -> Dict[str, Any]:
    """分词：单条（text）返回词串；批量（texts）返回 results 列表。"""
    from rag.nlp import rag_tokenizer
    if req.get("texts") is not None:
        texts = [str(item or "") for item in (req.get("texts") or [])]
        return {"count": len(texts), "results": [rag_tokenizer.tokenize(text) for text in texts]}
    text = str(req.get("text") or "")
    tokens = rag_tokenizer.tokenize(text)
    return {"count": len(tokens.split()), "tokens": tokens[:2000]}


def op_chunk(req: Dict[str, Any]) -> Dict[str, Any]:
    name = str(req.get("name") or "")
    path = req.get("path")
    text = req.get("text")
    binary: Optional[bytes] = None
    if path:
        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"文件不存在：{file_path}")
        name = name or file_path.name
        binary = file_path.read_bytes()
    elif text is not None:
        name = name or "inline.txt"
    else:
        raise ValueError("必须提供 path 或 text")

    method = parser_for(name, req.get("chunk_method"))
    parser_config = dict(DEFAULT_PARSER_CONFIG)
    parser_config.update(req.get("parser_config") or {})
    module = importlib.import_module(PARSER_MODULES[method])

    started = time.perf_counter()
    raw = _call_chunk(module, name, binary, text, parser_config,
                      int(req.get("from_page") or 0), int(req.get("to_page") or 100000000),
                      str(req.get("lang") or "Chinese"))
    chunks = _normalize(raw, name)
    total_chars = sum(item["chars"] for item in chunks)
    return {
        "name": name,
        "chunk_method": method,
        "parser_module": PARSER_MODULES[method],
        "count": len(chunks),
        "total_chars": total_chars,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "chunks": chunks,
    }


OPS = {"health": op_health, "supported": op_supported, "tokenize": op_tokenize, "chunk": op_chunk}


def handle(req: Dict[str, Any]) -> Dict[str, Any]:
    op = str(req.get("op") or "")
    if op not in OPS:
        return {"ok": False, "error": f"未知操作：{op}", "ops": sorted(OPS)}
    try:
        data = OPS[op](req)
        return {"ok": True, "data": data}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc()[-1200:]}


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError as exc:
            print(json.dumps({"ok": False, "error": f"请求不是合法 JSON：{exc}"}, ensure_ascii=False), flush=True)
            continue
        resp = handle(req)
        resp["id"] = req.get("id")
        print(json.dumps(resp, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
