"""知识库构建模块：把本地文本 / Markdown 批量导入 Dify 知识库并跟踪索引状态。

  * 支持 --dry-run：只打印将要上传的文件、字符数、分段预览，不发起任何写请求；
  * 真实模式：create-by-text 建文档 -> 收集 batch -> 轮询 indexing-status -> 落库 kb_docs；
  * 可选 --hit-test QUERY：导入后用 /v1/datasets/{id}/retrieve 做命中测试。

依赖 DIFY_KB_API_KEY（dataset-xxx）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from config import Config
from dify_client import DifyClient, DifyError
from store import Store, now_ts

#: 默认支持的文件后缀
DEFAULT_SUFFIXES = (".md", ".markdown", ".txt")

#: 默认分段规则（与 Dify 控制台的「自定义」模式字段一致）
DEFAULT_PROCESS_RULE: Dict[str, Any] = {
    "mode": "custom",
    "rules": {
        "pre_processing_rules": [
            {"id": "remove_extra_spaces", "enabled": True},
            {"id": "remove_urls_emails", "enabled": False},
        ],
        "segmentation": {"separator": "\n\n", "max_tokens": 500, "chunk_overlap": 50},
        "parent_mode": "paragraph",
    },
}


@dataclass
class SourceFile:
    """待导入的源文件。"""

    path: Path
    name: str
    text: str
    char_count: int
    content_hash: str
    segments: List[str] = field(default_factory=list)


def discover_files(source: Path, suffixes: Sequence[str] = DEFAULT_SUFFIXES) -> List[Path]:
    """递归发现待导入的文本文件。"""
    source = Path(source)
    if source.is_file():
        return [source]
    if not source.exists():
        raise SystemExit(f"[kb-build] 源路径不存在：{source}")
    found: List[Path] = []
    for path in sorted(source.rglob("*")):
        if path.is_file() and path.suffix.lower() in suffixes:
            found.append(path)
    return found


def split_segments(text: str, max_chars: int = 500, overlap: int = 50) -> List[str]:
    """按空行与长度切分文本，用于 dry-run 预览。

    与 Dify 的分段不完全等价（Dify 按 token 计），仅用于人工确认内容切分是否合理。

    :param text: 原文
    :param max_chars: 单段最大字符数
    :param overlap: 相邻段重叠字符数
    :return: 分段列表
    """
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    segments: List[str] = []
    for block in blocks:
        if len(block) <= max_chars:
            segments.append(block)
            continue
        start = 0
        while start < len(block):
            end = min(start + max_chars, len(block))
            segments.append(block[start:end])
            if end >= len(block):
                break
            start = max(end - overlap, start + 1)
    return segments


def load_sources(source: Path, suffixes: Sequence[str] = DEFAULT_SUFFIXES, max_chars: int = 500) -> List[SourceFile]:
    """读取源文件并做分段预览。"""
    files: List[SourceFile] = []
    for path in discover_files(source, suffixes):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"[kb-build] 跳过 {path}：{exc}")
            continue
        files.append(
            SourceFile(
                path=path,
                name=path.stem,
                text=text,
                char_count=len(text),
                content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
                segments=split_segments(text, max_chars=max_chars),
            )
        )
    return files


class KbBuilder:
    """知识库导入器。"""

    def __init__(self, cfg: Config, store: Store, client: Optional[DifyClient] = None, verbose: bool = True):
        """
        :param cfg: 配置
        :param store: Store（记录 kb_docs）
        :param client: 知识库客户端；缺省在首次真正发请求时按 DIFY_KB_API_KEY 构造，
                       这样 --dry-run 无需任何凭据即可运行
        """
        self.cfg = cfg
        self.store = store
        self._client = client
        self.verbose = verbose

    @property
    def client(self) -> DifyClient:
        """惰性创建知识库客户端（dry-run 不会触发凭据校验）。"""
        if self._client is None:
            self._client = DifyClient.from_config(self.cfg, "kb")
        return self._client

    def _log(self, message: str) -> None:
        """打印进度。"""
        if self.verbose:
            print(f"[kb-build] {message}")

    # ------------------------------------------------ dry-run
    def dry_run(self, source: Path, suffixes: Sequence[str], preview_segments: int = 2) -> List[SourceFile]:
        """只打印将要上传的内容与分段预览。"""
        files = load_sources(source, suffixes)
        if not files:
            print(f"[kb-build] 在 {source} 下没有找到可导入的文件（后缀：{', '.join(suffixes)}）")
            return []
        total_chars = sum(f.char_count for f in files)
        total_segments = sum(len(f.segments) for f in files)
        print(f"[kb-build] DRY-RUN：发现 {len(files)} 个文件，共 {total_chars} 字符，预计 {total_segments} 段")
        print("")
        for f in files:
            print(f"- {f.path}")
            print(f"    名称：{f.name}｜字符数：{f.char_count}｜分段数：{len(f.segments)}｜内容哈希：{f.content_hash}")
            for i, seg in enumerate(f.segments[:preview_segments], 1):
                preview = seg[:120].replace("\n", " ")
                print(f"    段 {i} 预览：{preview}…")
            print("")
        print("[kb-build] DRY-RUN 结束：未发起任何写请求。")
        return files

    # ------------------------------------------------ 实际导入
    def build(
        self,
        source: Path,
        dataset_id: str,
        suffixes: Sequence[str] = DEFAULT_SUFFIXES,
        indexing_technique: str = "high_quality",
        create_dataset_name: Optional[str] = None,
        hit_test_query: Optional[str] = None,
        poll_timeout: int = 300,
    ) -> Dict[str, Any]:
        """执行导入。

        :param source: 本地目录或文件
        :param dataset_id: 目标知识库 ID（为空且给了 create_dataset_name 时先建库）
        :param indexing_technique: high_quality / economy
        :param create_dataset_name: 需要新建知识库时的名称
        :param hit_test_query: 导入完成后做一次命中测试的 query
        :param poll_timeout: 轮询索引状态的超时时间（秒）
        :return: 统计字典
        """
        files = load_sources(source, suffixes)
        run_id = self.store.start_run(
            "kb-build",
            {"source": str(source), "dataset_id": dataset_id, "files": len(files)},
        )
        if not files:
            self.store.finish_run(run_id, "success", {"files": 0}, notes="未发现可导入文件")
            print(f"[kb-build] 在 {source} 下没有找到可导入的文件")
            return {"files": 0, "documents": 0, "skipped": 0, "errors": []}

        if not dataset_id:
            if not create_dataset_name:
                raise SystemExit("[kb-build] 需要 --dataset-id，或用 --create-dataset NAME 先创建知识库")
            created = self.client.create_dataset(create_dataset_name)
            dataset_id = created.get("id") or ""
            self._log(f"已创建知识库 {create_dataset_name} -> {dataset_id}")

        stats = {"files": len(files), "documents": 0, "skipped": 0, "errors": [], "dataset_id": dataset_id}
        batches: List[Tuple[str, SourceFile]] = []

        for f in files:
            existing = self.store.conn.execute(
                "SELECT doc_id, content_hash FROM kb_docs WHERE source_path=? AND dataset_id=?",
                (str(f.path), dataset_id),
            ).fetchone()
            if existing and existing["content_hash"] == f.content_hash:
                stats["skipped"] += 1
                self._log(f"跳过（内容未变）：{f.path.name}")
                continue
            try:
                resp = self.client.create_document_by_text(
                    dataset_id,
                    f.name,
                    f.text,
                    indexing_technique=indexing_technique,
                    process_rule=DEFAULT_PROCESS_RULE,
                )
            except DifyError as exc:
                stats["errors"].append(f"{f.path.name}: {exc}")
                self._log(f"上传失败：{f.path.name} -> {exc}")
                self.store.upsert_kb_doc({
                    "doc_id": f"local-{f.content_hash}-{dataset_id}",
                    "dataset_id": dataset_id,
                    "name": f.name,
                    "source_path": str(f.path),
                    "content_hash": f.content_hash,
                    "char_count": f.char_count,
                    "segment_count": len(f.segments),
                    "status": "error",
                    "error": str(exc),
                })
                continue

            document = (resp or {}).get("document") or {}
            doc_id = document.get("id") or ""
            batch = (resp or {}).get("batch") or ""
            stats["documents"] += 1
            self.store.upsert_kb_doc({
                "doc_id": doc_id or f"local-{f.content_hash}",
                "dataset_id": dataset_id,
                "name": f.name,
                "source_path": str(f.path),
                "content_hash": f.content_hash,
                "char_count": f.char_count,
                "segment_count": len(f.segments),
                "status": "indexing",
                "batch": batch,
            })
            if batch:
                batches.append((batch, f))
            self._log(f"已提交：{f.path.name}（doc_id={doc_id}，batch={batch}）")

        # 轮询索引状态
        for batch, f in batches:
            try:
                status = self.client.wait_indexing(dataset_id, batch, timeout=poll_timeout)
            except DifyError as exc:
                stats["errors"].append(f"{f.path.name} 索引状态查询失败：{exc}")
                continue
            items = status.get("data") or []
            done = all((it.get("indexing_status") == "completed") for it in items) if items else False
            final = "completed" if done else ("indexing" if not status.get("timed_out") else "timeout")
            self.store.upsert_kb_doc({
                "doc_id": (items[0].get("id") if items else "") or f"local-{f.content_hash}",
                "dataset_id": dataset_id,
                "name": f.name,
                "source_path": str(f.path),
                "content_hash": f.content_hash,
                "char_count": f.char_count,
                "segment_count": len(f.segments),
                "status": final,
                "batch": batch,
                "error": None if done else "索引未在超时时间内完成",
            })
            self._log(f"索引状态：{f.path.name} -> {final}")

        if hit_test_query:
            try:
                result = self.client.retrieve(dataset_id, hit_test_query, top_k=3)
                records = result.get("records") or []
                self._log(f"命中测试 '{hit_test_query}'：{len(records)} 条")
                for rec in records[:3]:
                    seg = rec.get("segment") or {}
                    self._log(f"  - score={rec.get('score')} doc={seg.get('document_id')} {str(seg.get('content'))[:60]}…")
                stats["hit_test"] = {"query": hit_test_query, "hits": len(records)}
            except DifyError as exc:
                stats["errors"].append(f"命中测试失败：{exc}")

        self.store.finish_run(run_id, "success", stats)
        self._log(
            f"完成：提交 {stats['documents']} 个文档，跳过 {stats['skipped']} 个，失败 {len(stats['errors'])} 个"
        )
        return stats


def build_knowledge_base(
    cfg: Config,
    store: Store,
    source: Path,
    dataset_id: str,
    dry_run: bool = False,
    suffixes: Sequence[str] = DEFAULT_SUFFIXES,
    create_dataset_name: Optional[str] = None,
    hit_test_query: Optional[str] = None,
    concurrency_unused: int = 1,
) -> Dict[str, Any]:
    """便捷入口：dry-run 或真实导入。"""
    builder = KbBuilder(cfg, store)
    if dry_run:
        files = builder.dry_run(source, suffixes)
        return {"dry_run": True, "files": len(files), "chars": sum(f.char_count for f in files)}
    return builder.build(
        source=source,
        dataset_id=dataset_id,
        suffixes=suffixes,
        create_dataset_name=create_dataset_name,
        hit_test_query=hit_test_query,
    )
