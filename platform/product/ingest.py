"""入库流水线：调知识内核解析分块 → 嵌入 → 落产品库。

每一步都留状态：document.status（UNSTART/RUNNING/DONE/FAIL）+ progress + message，
门户按这个显示解析进度；失败信息原样带给用户，不吞错。
"""

from __future__ import annotations

import html as html_lib
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import kernels
from .embedder import ModelGateway
from .store import Store, new_id, now

ProgressFn = Optional[Callable[[float, str], None]]

# ---------------------------------------------------------------- 语料清洗
# 语料里的表格是 HTML 写法（<table><thead><tr><th>…），直接交给分词器会把
# tabl/thead/tbodi/tr/th/td 当成词切出来，关键词又被"取最长词"的兜底逻辑顶到前面，
# 连带污染实体抽取。这里在入库前把表格还原成"键：值 / 单元格 | 单元格"的纯文本。
# 判据见 docs/16 §7 D29（2026-09-28）。
_TABLE_RE = re.compile(r"<table[^>]*>(.*?)</table>", re.S | re.I)
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL_RE = re.compile(r"<t[hd][^>]*>(.*?)</t[hd]>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_HTML_HINT_RE = re.compile(r"</?(table|thead|tbody|tr|t[hd]|p|div|br|ul|ol|li|strong|em|span)\b", re.I)
#: 纯标记/结构词：分词器把 HTML 标签切出来的碎片，关键词里必须排除
_MARKUP_TOKENS = {"tabl", "table", "thead", "tbody", "tbodi", "tr", "th", "td", "br", "nbsp", "amp", "quot", "lt", "gt"}


def _cell_text(raw: str) -> str:
    return html_lib.unescape(_TAG_RE.sub("", raw)).strip()


def html_to_text(text: str) -> str:
    """把 HTML 片段转成可读纯文本；表格按行还原（两列 → 「键：值」）。"""
    def table_block(match: "re.Match[str]") -> str:
        lines: List[str] = []
        for row in _ROW_RE.findall(match.group(1)):
            cells = [cell for cell in (_cell_text(item) for item in _CELL_RE.findall(row)) if cell]
            if not cells:
                continue
            lines.append(f"{cells[0]}：{' '.join(cells[1:])}" if len(cells) == 2 else " | ".join(cells))
        return "\n".join(lines)

    out = _TABLE_RE.sub(table_block, text)
    out = re.sub(r"<br\s*/?>", "\n", out, flags=re.I)
    out = _TAG_RE.sub("", out)
    out = html_lib.unescape(out)
    out = re.sub(r"[ \t]+", " ", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def clean_chunk_text(text: str) -> str:
    """切片正文清洗：只在检测到 HTML 结构时才改写，纯 Markdown 原样返回。"""
    if not text or not _HTML_HINT_RE.search(text):
        return text
    return html_to_text(text) or text


def _is_markup_token(word: str) -> bool:
    lowered = word.lower().strip()
    return lowered in _MARKUP_TOKENS or lowered.startswith("&")


def ingest_document(store: Store, gateway: ModelGateway, document_id: str,
                    progress: ProgressFn = None, embed_batch: int = 16) -> Dict[str, Any]:
    """解析一份已登记的文档并建立向量索引。"""
    document = store.get_document(document_id)
    if not document:
        raise ValueError(f"文档不存在：{document_id}")
    dataset = store.get_dataset(document["dataset_id"]) or {}

    def report(value: float, message: str) -> None:
        store.update_document(document_id, {"status": "RUNNING", "progress": round(value, 4), "message": message})
        if progress:
            progress(value, message)

    started = time.perf_counter()
    report(0.02, "已进入解析队列")
    try:
        result = kernels.knowledge(
            "chunk", timeout=3600.0,
            path=document.get("stored_path") or None,
            name=document["name"],
            chunk_method=dataset.get("chunk_method") or "naive",
            parser_config=dataset.get("parser_config") or {},
        )
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or "解析失败")
        data = result.get("data") or {}
        chunks: List[Dict[str, Any]] = data.get("chunks") or []
        if not chunks:
            raise RuntimeError("解析结果为空（0 个切片）")
        report(0.45, f"解析完成：{len(chunks)} 个切片，开始向量化")

        texts = [clean_chunk_text(item.get("content") or "") for item in chunks]
        tokens = kernels.tokenize_batch(texts)
        vectors = gateway.embed(texts, batch_size=embed_batch)
        report(0.9, f"向量化完成：{len(vectors)} 条")

        payload = []
        for index, item in enumerate(chunks):
            payload.append({
                "id": new_id("ck"),
                "content": texts[index],
                "tokens": tokens[index] if index < len(tokens) else "",
                "keywords": _keywords(item, tokens[index] if index < len(tokens) else ""),
                "embedding": vectors[index] if index < len(vectors) else None,
            })
        store.replace_chunks(document["dataset_id"], document_id, payload)
        elapsed = int((time.perf_counter() - started) * 1000)
        store.update_document(document_id, {
            "status": "DONE", "progress": 1.0,
            "message": f"完成：{len(payload)} 切片 / 用时 {elapsed / 1000:.1f}s",
        })
        return {"document_id": document_id, "status": "DONE", "chunk_count": len(payload),
                "elapsed_ms": elapsed, "parser": data.get("parser_module")}
    except Exception as exc:  # noqa: BLE001
        store.update_document(document_id, {
            "status": "FAIL", "progress": 0.0, "message": f"{type(exc).__name__}: {exc}"[:400],
        })
        return {"document_id": document_id, "status": "FAIL", "error": f"{type(exc).__name__}: {exc}"[:400]}


def _keywords(item: Dict[str, Any], tokens: str, limit: int = 8) -> List[str]:
    """关键词：优先用解析器给的 important_kwd，否则从分词结果里取最长的若干词。

    兜底路径要排除 HTML 标记碎片（tabl/thead/tbodi/tr/th/td），否则关键词会被标记词占满。
    """
    raw = [str(word) for word in (item.get("important_keywords") or []) if not _is_markup_token(str(word))]
    if raw:
        return raw[:limit]
    words = [word for word in (tokens or "").split() if len(word) > 1 and not _is_markup_token(word)]
    words.sort(key=len, reverse=True)
    seen: List[str] = []
    for word in words:
        if word not in seen:
            seen.append(word)
        if len(seen) >= limit:
            break
    return seen


def save_upload(destination: Path, name: str, content: bytes) -> Path:
    """把上传的原件落盘（产品自持存储，默认 platform/data/files/<dataset>/）。"""
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / name
    stem, suffix = target.stem, target.suffix
    counter = 1
    while target.exists():
        target = destination / f"{stem}_{counter}{suffix}"
        counter += 1
    target.write_bytes(content)
    return target
