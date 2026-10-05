"""语料索引模块：从 samples/ 提取每份资料的编号 / 版本 / 状态 / 条款清单。

治理度量（见 docs/10）需要知道「每份资料有多少条款、现行版本是什么」，
才能计算探针覆盖率与版本时效性缺陷。本模块只读语料，不修改任何资料。

设计要点：
1. 只依赖 Python 标准库；
2. 文档头字段与条款正则与 tools/check_corpus.py 保持同一口径；
3. 版本台账（HYS-ZL-005）是版本权威源，文档级结论一律以台账为准。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

#: 文档头必备字段（与 tools/check_corpus.py 一致）
HEADER_FIELDS = ("资料编号", "资料名称", "版本号", "发布日期", "适用型号", "密级", "现行状态")

#: 版本台账的资料编号（版本权威源）
LEDGER_DOC_NO = "HYS-ZL-005"

#: 台账行：序号 | 编号 | 名称 | 版本 | 日期 | 状态 |
LEDGER_ROW_RE = re.compile(
    r"^\|\s*\d+\s*\|\s*(HYS-[A-Z]{2}-\d{3})\s*\|([^|]*)\|\s*(V[\d.]+)\s*\|"
    r"\s*([\d-]+)\s*\|\s*\**([^|*]+?)\**\s*\|"
)

#: 条款编号（如 4.2.1）出现在行首
CLAUSE_RE = re.compile(r"^\s*(\d+(?:\.\d+){1,3})\s")


def parse_header(text: str) -> Dict[str, str]:
    """解析文档头的字段表格，返回字段名 -> 值。"""
    header: Dict[str, str] = {}
    for field_name in HEADER_FIELDS:
        matched = re.search(rf"\|\s*{field_name}\s*\|\s*([^|]+?)\s*\|", text)
        if matched:
            header[field_name] = matched.group(1).strip().strip("*").strip()
    return header


def parse_ledger(text: str) -> List[Dict[str, str]]:
    """解析版本台账的资料清单总表。"""
    rows: List[Dict[str, str]] = []
    for line in text.splitlines():
        matched = LEDGER_ROW_RE.match(line)
        if not matched:
            continue
        rows.append(
            {
                "doc_no": matched.group(1).strip(),
                "name": matched.group(2).strip(),
                "version": matched.group(3).strip(),
                "date": matched.group(4).strip(),
                "status": matched.group(5).strip(),
            }
        )
    return rows


def iter_clauses(text: str) -> List[str]:
    """抽取正文中的条款编号（行首数字编号），保持出现顺序。"""
    return [m.group(1) for line in text.splitlines() if (m := CLAUSE_RE.match(line))]


@dataclass
class DocInfo:
    """一份资料的索引信息。"""

    doc_no: str
    title: str = ""
    version: str = ""
    status: str = ""
    effective_date: str = ""
    clause_list: List[str] = field(default_factory=list)
    path: str = ""

    @property
    def clauses(self) -> int:
        """条款数量。"""
        return len(self.clause_list)


@dataclass
class CorpusIndex:
    """语料索引：文档信息 + 台账现行版本。"""

    docs: Dict[str, DocInfo] = field(default_factory=dict)
    #: 台账按资料编号分组：一个编号可能有多行（现行 + 历次作废）
    ledger: Dict[str, List[Dict[str, str]]] = field(default_factory=dict)
    samples_dir: Optional[Path] = None

    def current_version(self, doc_no: str) -> Optional[str]:
        """台账登记的现行版本；台账未登记时返回 None。

        注意：台账同一编号存在多行（现行 + 历次作废），必须按「现行状态」挑，
        否则会把作废版本误判成现行版本，进而产生假的版本失效缺陷。
        """
        for row in self.ledger.get(doc_no) or []:
            if "现行" in str(row.get("status", "")):
                return row.get("version")
        return None

    def obsolete_versions(self, doc_no: str) -> List[str]:
        """台账登记的作废版本清单（用于版本失效类缺陷的判定与证据展示）。"""
        return [
            str(row.get("version"))
            for row in self.ledger.get(doc_no) or []
            if "现行" not in str(row.get("status", ""))
        ]

    def is_current(self, doc_no: str, version: Optional[str]) -> Optional[bool]:
        """给定版本是否为台账现行版本；无法判定时返回 None。"""
        current = self.current_version(doc_no)
        if not current or not version:
            return None
        return current.strip() == version.strip()

    def clauses_total(self, doc_no: str) -> Optional[int]:
        """该资料的条款总数；未知返回 None。"""
        info = self.docs.get(doc_no)
        return info.clauses if info else None

    def clause_numbers(self, doc_no: str) -> List[str]:
        """该资料的条款编号清单。"""
        info = self.docs.get(doc_no)
        return list(info.clause_list) if info else []

    def summary_lines(self) -> List[str]:
        """可打印的索引摘要。"""
        lines = [f"  语料目录 : {self.samples_dir}"]
        total_rows = sum(len(v) for v in self.ledger.values())
        lines.append(f"  资料数量 : {len(self.docs)} 份，台账登记 {len(self.ledger)} 个编号 / {total_rows} 行")
        for doc_no in sorted(self.docs):
            info = self.docs[doc_no]
            current = self.current_version(doc_no) or "未登记"
            lines.append(
                f"    - {doc_no} {info.version}（{info.status}）条款 {info.clauses} 条，台账现行版本 {current}"
            )
        return lines


def load_corpus(samples_dir: Path | str) -> CorpusIndex:
    """扫描目录，建立语料索引。

    :param samples_dir: 语料目录（默认项目下的 samples/）
    :return: CorpusIndex
    """
    root = Path(samples_dir)
    index = CorpusIndex(samples_dir=root)
    if not root.exists():
        return index

    texts: List[tuple] = []
    for path in sorted(root.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        texts.append((path, text))

    # 先解析台账（版本权威源）
    for path, text in texts:
        header = parse_header(text)
        if header.get("资料编号") == LEDGER_DOC_NO:
            for row in parse_ledger(text):
                index.ledger.setdefault(row["doc_no"], []).append(row)
            break

    for path, text in texts:
        header = parse_header(text)
        doc_no = header.get("资料编号")
        if not doc_no:
            continue
        index.docs[doc_no] = DocInfo(
            doc_no=doc_no,
            title=header.get("资料名称", ""),
            version=header.get("版本号", ""),
            status=header.get("现行状态", ""),
            effective_date=header.get("发布日期", ""),
            clause_list=iter_clauses(text),
            path=str(path),
        )
    return index
