"""领域词典：从语料自带的对照表/台账里解析实体种子与关系（**不依赖任何大模型**）。

数据来源就是语料自己：
  * 《术语与缩略语对照表》(HYS-TY-005)：缩略语→含义、部件→代号；
  * 《装备技术资料清单与版本台账》(HYS-ZL-005)：资料编号→版本→现行/作废状态。

产出三类东西，供建图时使用：
  1. terms   —— 领域词条（强制进图的实体种子，不依赖分词器是否切对）；
  2. edges   —— 词典级关系（代号 / 含义 / 现行版本 / 已作废），建图时**钉住**不参与稀疏化；
  3. aliases —— 同义映射（当前只收"全称→缩略语"这类高置信对，宁缺毋滥）。

判据见 docs/16 §7 D29（2026-09-28：词典驱动图谱）。
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Sequence, Set, Tuple

#: 表格里不该当实体收进来的表头/占位词
STOP_CELLS = {
    "序号", "项目", "内容", "名称", "说明", "备注", "主要功能", "部件", "代号", "缩略语", "全称",
    "资料编号", "资料名称", "版本", "版本号", "发布日期", "状态", "参数名称", "数值", "单位", "序号 ",
}

#: 现行/作废状态词
ACTIVE_WORDS = ("现行有效", "现行", "有效")
OBSOLETE_WORDS = ("已作废", "作废", "失效")

_CODE_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:[-_][A-Z0-9]+)+$")
_SPACED_CODE_RE = re.compile(r"^[A-Z][A-Z0-9]*(?: [0-9]+)+$")   # NAS 1638
_ABBR_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")                   # HYD / PU / VL / RV（缩略语列专用）
_VERSION_NODE_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:[-_][A-Z0-9]+)+ V?\d+(?:\.\d+){1,2}$")  # HYS-CZ-002 V4.0
_VER_RE = re.compile(r"^[Vv]?\d+(?:\.\d+){1,2}$")
_CN_RE = re.compile(r"^[\u4e00-\u9fff][\u4e00-\u9fffA-Za-z0-9\-]{1,15}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _clean_cell(value: str) -> str:
    text = (value or "").strip()
    text = text.replace("**", "").replace("`", "")
    return text.strip()


def _split_row(line: str) -> List[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [_clean_cell(cell) for cell in body.split("|")]


def _is_separator(line: str) -> bool:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return False
    return all(set(cell) <= set("-: ") for cell in _split_row(stripped))


def iter_tables(text: str) -> Iterable[Tuple[List[str], List[List[str]]]]:
    """从一段文本里逐个取出 Markdown 表格：(表头, 数据行)。"""
    lines = text.splitlines()
    index = 0
    while index < len(lines) - 1:
        head = lines[index].strip()
        sep = lines[index + 1].strip()
        if head.startswith("|") and _is_separator(sep):
            header = _split_row(head)
            rows: List[List[str]] = []
            cursor = index + 2
            gaps = 0
            while cursor < len(lines):
                body = lines[cursor].strip()
                if not body.startswith("|"):
                    # 切片边界可能插进一行噪声（页码/标题碎片），容忍 1 行；
                    # 连续两行非表格即判定这张表结束，避免把内容不同的两张表并成一张。
                    gaps += 1
                    if gaps > 1:
                        break
                    cursor += 1
                    continue
                cells = _split_row(body)
                if len(cells) != len(header):
                    break
                gaps = 0
                rows.append(cells)
                cursor += 1
            if rows:
                yield header, rows
            index = cursor
            continue
        index += 1


def _header_index(header: Sequence[str], *keywords: str) -> int:
    for position, name in enumerate(header):
        if any(key in name for key in keywords):
            return position
    return -1


def _is_entity(value: str) -> bool:
    """实体判定（比 _is_term 宽）：允许缩略语列（HYD/RV）与版本节点（HYS-CZ-002 V4.0）。"""
    return bool(_is_term(value) or _ABBR_RE.match(value or "") or _VERSION_NODE_RE.match(value or ""))


def _is_term(value: str) -> bool:
    if not value or value in STOP_CELLS or len(value) < 2 or len(value) > 24:
        return False
    if _DATE_RE.match(value):
        return False
    return bool(_CN_RE.match(value) or _CODE_RE.match(value) or _SPACED_CODE_RE.match(value)
                or _VER_RE.match(value))


class Lexicon:
    """词典解析结果。"""

    def __init__(self) -> None:
        self.terms: Set[str] = set()
        self.edges: List[Dict[str, Any]] = []
        self.aliases: Dict[str, str] = {}
        self.documents: Dict[str, Dict[str, Any]] = {}
        #: 实体类别覆盖：资料编号→资料、版本节点→版本、缩略语→缩略语（其余交给 graph.classify）
        self.categories: Dict[str, str] = {}
        self.stats: Dict[str, int] = defaultdict(int)
        self._seen: Set[Tuple[str, str, str]] = set()

    def add_term(self, term: str) -> None:
        if _is_entity(term):
            self.terms.add(term)

    def add_edge(self, source: str, target: str, relation: str, weight: float = 6.0) -> None:
        if not _is_entity(source) or not _is_entity(target) or source == target:
            return
        key = (source, target, relation)
        if key in self._seen:
            return
        self._seen.add(key)
        self.edges.append({"source": source, "target": target, "relation": relation,
                           "weight": float(weight), "documents": 1, "pinned": True})
        self.add_term(source)
        self.add_term(target)
        self.stats[relation] += 1


def _parse_ledger(header: Sequence[str], rows: Sequence[Sequence[str]], lexicon: Lexicon) -> None:
    """资料清单与版本台账 → 资料节点 + 版本节点 + 「现行版本 / 已作废」关系。

    同一资料编号只保留一个现行版本；已作废版本单独成点并连到资料上，
    这样"V3.0 已作废、V4.0 现行"在图上是可查的，而不是两个都当"版本号"糊在一起。
    """
    no_at = _header_index(header, "资料编号")
    ver_at = _header_index(header, "版本")
    state_at = _header_index(header, "状态")
    name_at = _header_index(header, "资料名称")
    if no_at < 0 or ver_at < 0 or state_at < 0:
        return
    for row in rows:
        if max(no_at, ver_at, state_at) >= len(row):
            continue
        doc_no, version, state = row[no_at], row[ver_at], row[state_at]
        if not _CODE_RE.match(doc_no) or not _VER_RE.match(version):
            continue
        record = lexicon.documents.setdefault(doc_no, {"current": "", "current_state": "", "obsolete": [], "name": ""})
        if 0 <= name_at < len(row) and _CN_RE.match(row[name_at]):
            record["name"] = row[name_at]
        version_node = f"{doc_no} {version}"
        lexicon.categories.setdefault(doc_no, "资料")
        lexicon.categories.setdefault(version_node, "版本")
        lexicon.add_term(doc_no)
        lexicon.add_term(version_node)
        if any(word in state for word in OBSOLETE_WORDS):
            if version_node not in record["obsolete"]:
                record["obsolete"].append(version_node)
            lexicon.add_edge(version_node, doc_no, "已作废")
        else:
            record["current"] = version_node
            record["current_state"] = state
            lexicon.add_edge(doc_no, version_node, "现行版本")
        lexicon.stats["ledger_rows"] += 1


def _parse_glossary(header: Sequence[str], rows: Sequence[Sequence[str]], lexicon: Lexicon) -> None:
    abbr_at = _header_index(header, "缩略语", "缩写")
    candidates = [at for at in (_header_index(header, "全称"), _header_index(header, "含义"),
                                _header_index(header, "说明")) if at >= 0]
    if abbr_at < 0 or not candidates:
        return
    for row in rows:
        if abbr_at >= len(row):
            continue
        abbr = row[abbr_at]
        # 缩略语列允许 2~10 位大写字母数字（HYD / PU / RV / NAS 1638），
        # 这类短代号在正文里会被通用噪声规则挡掉，但在对照表语境下是有意义的实体
        if not (_is_term(abbr) or _ABBR_RE.match(abbr) or _SPACED_CODE_RE.match(abbr)):
            continue
        # 全称列可能是英文（Hydraulic / Relief Valve），此时取中文的"说明"列做含义，
        # 保证图谱里连到的是中文术语（RV → 溢流阀），而不是英文词组。
        target = ""
        for at in candidates:
            if at < len(row) and _CN_RE.match(row[at]):
                target = row[at]
                break
        if not target:
            for at in candidates:
                if at < len(row) and _is_term(row[at]):
                    target = row[at]
                    break
        if not target:
            continue
        lexicon.categories.setdefault(abbr, "缩略语")
        lexicon.add_edge(abbr, target, "含义", weight=4.0)
        lexicon.stats["glossary_rows"] += 1


def _parse_parts(header: Sequence[str], rows: Sequence[Sequence[str]], lexicon: Lexicon) -> None:
    part_at = _header_index(header, "部件", "部组件")
    code_at = _header_index(header, "代号")
    if part_at < 0 or code_at < 0 or part_at == code_at:
        return
    for row in rows:
        if max(part_at, code_at) >= len(row):
            continue
        part, code = row[part_at], row[code_at]
        part = re.sub(r"[（(].*?[)）]", "", part).strip()
        if not _CN_RE.match(part) or not _CODE_RE.match(code):
            continue
        # 部件名与代号粒度不同（"液压泵站" vs "HYD-PU-01"），只连关系、不做实体合并
        lexicon.categories.setdefault(part, "部件")
        lexicon.categories.setdefault(code, "代号")
        lexicon.add_edge(part, code, "代号", weight=5.0)
        lexicon.stats["part_rows"] += 1


def _parse_params(header: Sequence[str], rows: Sequence[Sequence[str]], lexicon: Lexicon) -> None:
    """《技术参数表》的"参数名称"列 → **权威参数实体**。

    语料自带这张表（序号 | 参数名称 | 数值），给的是规范名称：额定工作压力 / 溢流阀开启压力 /
    系统清洁度要求 / 油温停机检查阈值。比字符窗口可靠得多——既不会切出"流阀开启压力"这种
    左截断碎片，也让参数名在图上被钉住、不被排序和配额挤掉（判据见 docs/16 §7，2026-09-28）。
    """
    name_at = _header_index(header, "参数名称", "参数名")
    if name_at < 0:
        return
    for row in rows:
        if name_at >= len(row):
            continue
        name = row[name_at]
        if not _CN_RE.match(name):
            continue
        lexicon.categories.setdefault(name, "参数")
        lexicon.add_term(name)
        lexicon.stats["param_rows"] += 1


def parse_lexicon(chunks: Sequence[Dict[str, Any]]) -> Lexicon:
    """按**文档**重组正文后解析表格：切片可能把一张表切断，逐片解析会漏行。"""
    lexicon = Lexicon()
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for chunk in chunks:
        key = str(chunk.get("document_id") or chunk.get("doc_name") or "")
        grouped[key].append(chunk)
    for _, items in grouped.items():
        items.sort(key=lambda item: int(item.get("seq") or 0))
        text = "\n".join(str(item.get("content") or "") for item in items)
        for header, rows in iter_tables(text):
            _parse_ledger(header, rows, lexicon)
            _parse_glossary(header, rows, lexicon)
            _parse_parts(header, rows, lexicon)
            _parse_params(header, rows, lexicon)
    lexicon.stats["terms"] = len(lexicon.terms)
    lexicon.stats["edges"] = len(lexicon.edges)
    return lexicon
