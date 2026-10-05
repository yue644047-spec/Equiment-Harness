"""条款级片段索引：检索契约层的本地后端（离线可跑，不依赖 RAGFlow / Dify）。

对应设计报告（docs/11）3.5 检索契约层与 4.7 条款级溯源：
1. 统一片段结构：chunk_id / doc_no / version / status / clause_no / section / content / score；
2. 引用契约：回答与界面统一使用「资料编号 版本号 §章节」定位到条款；
3. 版本硬过滤：默认只召回台账登记的现行有效版本，作废版本物理隔离（可在演示中对比开关效果）；
4. 检索：字符二元组 TF-IDF 打分（纯标准库、确定性、可解释），分数归一化到 0~1。

本模块是「后端可切换」中的本地实现：换成上游 RAGFlow 时只需替换 search() 的数据来源，
片段结构与引用契约保持不变。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

#: 条款行：行首为 1 / 1.1 / 1.1.1 形式的编号
CLAUSE_RE = re.compile(r"^\s*(\d+(?:\.\d+){1,3})\s+(\S.*)$")
#: 章节标题
SECTION_RE = re.compile(r"^\s*(#{2,4})\s+(.+?)\s*$")


def bigrams(text: str) -> List[str]:
    """字符二元组切分（中文无需分词器，且与评测服务的既有做法一致）。"""
    cleaned = re.sub(r"[\s\u3000]+", "", text or "")
    cleaned = re.sub(r"[，。；：、（）()\[\]【】\"'“”‘’,.;:!?？！\-—_/\\]", "", cleaned)
    if len(cleaned) < 2:
        return [cleaned] if cleaned else []
    return [cleaned[i : i + 2] for i in range(len(cleaned) - 1)]


@dataclass
class Chunk:
    """统一片段结构（检索契约的 C2：任何后端都必须归一化成这一结构）。"""

    chunk_id: str
    doc_no: str
    version: str
    status: str
    clause_no: str
    section: str
    content: str
    source: str = ""
    page: str = ""

    @property
    def citation(self) -> str:
        """引用契约（C3）：资料编号 版本号 §章节。"""
        parts = [self.doc_no, self.version]
        if self.clause_no:
            parts.append(f"§{self.clause_no}")
        return " ".join(p for p in parts if p)

    def to_unified(self, score: float) -> Dict[str, Any]:
        """转成统一片段（对外返回给 Dify 的 record）。"""
        return {
            "content": self.content,
            "score": round(score, 4),
            "title": f"{self.doc_no} {self.version}",
            "metadata": {
                "chunk_id": self.chunk_id,
                "doc_no": self.doc_no,
                "version": self.version,
                "status": self.status,
                "clause_no": self.clause_no,
                "section": self.section,
                "citation": self.citation,
                "page": self.page or "",
                "engine": "local-clause-index",
            },
        }


@dataclass
class Scored:
    """带分数的片段。"""

    chunk: Chunk
    score: float
    matched_terms: int = 0


class ChunkIndex:
    """条款级索引：构建 + 检索 + 版本硬过滤。"""

    def __init__(self) -> None:
        self.chunks: List[Chunk] = []
        self.df: Dict[str, int] = {}
        self._norms: List[float] = []

    # ------------------------------------------------ 构建
    @classmethod
    def build(cls, samples_dir: Path | str, ledger_current: Optional[Dict[str, str]] = None) -> "ChunkIndex":
        """扫描语料目录，切分为条款级片段。

        :param samples_dir: 语料目录
        :param ledger_current: doc_no -> 台账现行版本（用于标记作废版本）
        """
        root = Path(samples_dir)
        index = cls()
        current = dict(ledger_current or {})

        for path in sorted(root.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            header = _parse_header(text)
            doc_no = header.get("资料编号", "")
            if not doc_no:
                continue
            version = header.get("版本号", "")
            status = header.get("现行状态", "")
            ledger_version = current.get(doc_no, "")
            if ledger_version and ledger_version != version:
                status = "已作废"          # 台账有登记且版本不同 → 以台账为准
            elif not status:
                status = "现行有效"

            section = ""
            for lineno, line in enumerate(text.splitlines(), 1):
                m_section = SECTION_RE.match(line)
                if m_section:
                    section = m_section.group(2).strip()
                    continue
                m_clause = CLAUSE_RE.match(line)
                if not m_clause:
                    continue
                clause_no, body = m_clause.group(1), m_clause.group(2).strip()
                if len(body) < 8:
                    continue
                chunk = Chunk(
                    chunk_id=f"{doc_no}-{version}-{clause_no}",
                    doc_no=doc_no,
                    version=version,
                    status=status,
                    clause_no=clause_no,
                    section=section,
                    content=f"{clause_no} {body}",
                    source=f"{path.name}:{lineno}",
                )
                index.chunks.append(chunk)

        index._build_stats()
        return index

    def _build_stats(self) -> None:
        """统计文档频率与片段长度，供 TF-IDF 打分。"""
        self.df = {}
        self._norms = []
        for chunk in self.chunks:
            terms = set(bigrams(chunk.content))
            for t in terms:
                self.df[t] = self.df.get(t, 0) + 1
        for chunk in self.chunks:
            self._norms.append(self._norm(chunk))

    def _norm(self, chunk: Chunk) -> float:
        """片段向量模长（用于余弦式归一化）。"""
        counts: Dict[str, int] = {}
        for t in bigrams(chunk.content):
            counts[t] = counts.get(t, 0) + 1
        total = 0.0
        n = max(len(self.chunks), 1)
        for t, c in counts.items():
            idf = math.log((n + 1) / (self.df.get(t, 0) + 1)) + 1.0
            total += (c * idf) ** 2
        return math.sqrt(total) or 1.0

    # ------------------------------------------------ 检索
    def search(
        self,
        question: str,
        top_k: int = 6,
        score_threshold: float = 0.0,
        include_obsolete: bool = False,
        doc_nos: Optional[Sequence[str]] = None,
        clause_prefix: str = "",
    ) -> List[Scored]:
        """检索片段。

        :param question: 查询文本
        :param top_k: 返回条数
        :param score_threshold: 最低分（0 表示不过滤）
        :param include_obsolete: 是否包含作废版本（仅演示对比用；正式链路恒为 False）
        :param doc_nos: 限定资料编号
        :param clause_prefix: 限定条款号前缀（如 "4."）
        :return: Scored 列表，按分数降序
        """
        q_terms = bigrams(question)
        if not q_terms:
            return []
        q_counts: Dict[str, int] = {}
        for t in q_terms:
            q_counts[t] = q_counts.get(t, 0) + 1
        n = max(len(self.chunks), 1)
        q_norm = math.sqrt(sum(((c * (math.log((n + 1) / (self.df.get(t, 0) + 1)) + 1.0)) ** 2) for t, c in q_counts.items())) or 1.0

        results: List[Scored] = []
        for idx, chunk in enumerate(self.chunks):
            if not include_obsolete and chunk.status != "现行有效":
                continue
            if doc_nos and chunk.doc_no not in doc_nos:
                continue
            if clause_prefix and not chunk.clause_no.startswith(clause_prefix):
                continue
            c_counts: Dict[str, int] = {}
            for t in bigrams(chunk.content):
                c_counts[t] = c_counts.get(t, 0) + 1
            dot = 0.0
            matched = 0
            for t, qc in q_counts.items():
                cc = c_counts.get(t)
                if not cc:
                    continue
                matched += 1
                idf = math.log((n + 1) / (self.df.get(t, 0) + 1)) + 1.0
                dot += (qc * idf) * (cc * idf)
            if dot <= 0:
                continue
            score = dot / (q_norm * self._norms[idx])
            results.append(Scored(chunk=chunk, score=max(0.0, min(1.0, score)), matched_terms=matched))

        results.sort(key=lambda s: (-s.score, s.chunk.chunk_id))
        if score_threshold > 0:
            results = [r for r in results if r.score >= score_threshold]
        return results[: max(1, top_k)]

    # ------------------------------------------------ 摘要
    def stats(self) -> Dict[str, Any]:
        """索引概览。"""
        by_doc: Dict[str, int] = {}
        obsolete = 0
        for chunk in self.chunks:
            by_doc[chunk.doc_no] = by_doc.get(chunk.doc_no, 0) + 1
            if chunk.status != "现行有效":
                obsolete += 1
        return {
            "chunks": len(self.chunks),
            "documents": len(by_doc),
            "obsolete_chunks": obsolete,
            "by_doc": by_doc,
        }


def _parse_header(text: str) -> Dict[str, str]:
    """解析文档头字段（与语料治理模块同一口径）。"""
    fields = ("资料编号", "资料名称", "版本号", "现行状态", "发布日期")
    header: Dict[str, str] = {}
    for name in fields:
        m = re.search(rf"\|\s*{name}\s*\|\s*([^|]+?)\s*\|", text)
        if m:
            header[name] = m.group(1).strip().strip("*").strip()
    return header


def load_ledger_current(samples_dir: Path | str) -> Dict[str, str]:
    """从版本台账读取各资料的现行版本（doc_no -> version）。"""
    from corpus import load_corpus  # 复用评测服务的语料索引，避免两套口径

    corpus = load_corpus(Path(samples_dir))
    return {doc_no: (corpus.current_version(doc_no) or "") for doc_no in corpus.docs}
