# -*- coding: utf-8 -*-
"""装备技术资料语料与版本治理校验器。

面向"数据管理集中化"：把资料入库前的**规范性与版本一致性**检查固化为可重复执行的工具，
避免作废版本数值、非规范术语、编号版本不一致的资料进入知识库。

用法：
    python tools/check_corpus.py                    # 校验 samples/ 下的全部语料
    python tools/check_corpus.py --dir samples
    python tools/check_corpus.py --strict           # 参数一致性的告警也计为失败
    python tools/check_corpus.py --ref <基准目录>                        # 资料规范符合性检查：对基准集做自洽性体检
    python tools/check_corpus.py --ref <基准目录> --target <目标文档>     # 额外对目标文档输出违规清单（.docx/.md/.txt）

校验内容（错误级）：
1. 文档头完整性：每份资料必须具备 8 字段文档头（资料编号/资料名称/版本号/发布日期/适用型号/密级/编制单位/现行状态）
2. 台账一致性：文档头的资料编号、版本号、现行状态必须与《装备技术资料清单与版本台账》一致
3. 唯一现行版本：同一资料编号只允许一个"现行有效"版本
4. 作废值隔离：已作废版本的数值只允许出现在版本台账中，不得泄漏到其他资料
5. 术语规范：不得使用《术语与缩略语对照表》以外的非规范说法

校验内容（告警级，--strict 下计为失败）：
6. 参数一致性：关键技术参数在各资料间取值必须一致（数值 + 单位逐字相同）
7. 免责声明：示例语料必须声明为演示数据

版本台账本身是版本权威源，因此豁免第 2、4、6 项检查（它必须引用作废值来说明修订历史）。

退出码：0 全部通过；1 存在错误（或 --strict 下存在告警）；2 用法错误。
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent

#: 文档头必备字段
HEADER_FIELDS = ("资料编号", "资料名称", "版本号", "发布日期", "适用型号", "密级", "编制单位", "现行状态")

#: 版本台账的资料编号（版本权威源，豁免部分检查）
LEDGER_DOC_NO = "HYS-ZL-005"

#: 已作废版本的数值：只允许出现在版本台账中
OBSOLETE_VALUES = {
    "300 工作小时": "HYS-CZ-002 V3.0 作废值，现行 V4.0 为 500 工作小时",
    "14 MPa": "HYS-GY-001 V2.0 作废值，现行 V3.0 为 16 MPa",
}

#: 非规范术语：应使用《术语与缩略语对照表》中的标准术语
NONSTANDARD_TERMS = {
    "油压": "应为「系统压力」或「液压油压力」",
    "液压缸": "应为「举升油缸」",
    "泵浦": "应为「液压泵站」",
    "液压阀门": "应为「电磁换向阀」或「溢流阀」",
}

#: 参数一致性规则：(参数别名, 允许出现的「数值 单位」集合)
#: 允许集合需要覆盖同一参数的合法分次值（如螺栓分三次拧紧的中间力矩）。
PARAM_RULES: Sequence[Tuple[Tuple[str, ...], frozenset]] = (
    (("额定工作压力",), frozenset({"16 MPa"})),
    (("最高允许压力",), frozenset({"18 MPa"})),
    (("溢流阀开启压力", "开启压力"), frozenset({"17.5 MPa"})),
    (("充氮压力",), frozenset({"10 MPa"})),
    (("举升额定负载", "额定举升负载"), frozenset({"3000 kg"})),
    (("举升速度",), frozenset({"0.05 m/s"})),
    (("举升最大高度",), frozenset({"1.8 m"})),
    (("油箱容量",), frozenset({"60 L"})),
    (("滤芯更换",), frozenset({"500 工作小时", "12 个月"})),
    (("缸盖螺栓",), frozenset({"180 N·m", "120 N·m", "60 N·m"})),
    (("软管接头",), frozenset({"90 N·m"})),
)

#: 参数值 = 数字 + 单位，必须紧跟在参数名之后（中间只允许少量非数字字符）
UNIT_GROUP = "MPa|℃|N·m|kg|m/s|L|工作小时|个月"

LEDGER_ROW_RE = re.compile(
    r"^\|\s*\d+\s*\|\s*(HYS-[A-Z]{2}-\d{3})\s*\|([^|]*)\|\s*(V[\d.]+)\s*\|"
    r"\s*([\d-]+)\s*\|\s*\**([^|*]+?)\**\s*\|"
)
CLAUSE_RE = re.compile(r"^\s*(\d+(?:\.\d+){1,3})\s")

# --------------------------------------------------------------------------
# 基准比对模式（--ref / --target）专用常量
# 仅在传入 --ref 时生效；未传 --ref 时上述默认校验行为逐字不变。
# --------------------------------------------------------------------------

#: 文档信息表字段别名 → 规范字段名（去空白后匹配），用于把各文档的字段口径归一
INFO_FIELD_ALIASES = {
    "文档编号": "文档编号", "资料编号": "文档编号", "文档编码": "文档编号", "文件编号": "文档编号",
    "版本": "版本号", "版本号": "版本号", "版本状态": "版本号",
    "编制单位": "编制单位", "编写单位": "编制单位", "编制部门": "编制单位", "编写部门": "编制单位",
    "发布日期": "发布日期", "出版日期": "发布日期",
    "密级": "密级", "保密等级": "密级",
    "文档名称": "文档名称", "资料名称": "文档名称", "文件名称": "文档名称",
    "适用型号": "适用型号",
    "现行状态": "现行状态",
}

#: 某字段在基准文档集中出现比例 ≥ 该阈值，即视为「基准必需字段」
REQUIRED_FIELD_RATIO = 0.8

#: 版本号规范形态：V<数字>[.<数字>[.<数字>]]
VERSION_RE = re.compile(r"^V\d+(?:\.\d+){0,2}$")

#: 占位值字面量（与「字段名自身」同值也算占位，如「编制单位：编制单位」）
PLACEHOLDER_TOKENS = frozenset({"待填", "待补充", "待完善", "略", "xxx", "todo", "tbd", "n/a", "na"})

#: docx 标题样式：Heading N / 标题 N（目录项样式 toc* 需跳过）
HEADING_STYLE_RE = re.compile(r"^(?:heading|标题)\s*(\d+)$", re.IGNORECASE)

#: 编号标题：1 / 1.1 / 1.1.1（后随标题文字）
CLAUSE_HEAD_RE = re.compile(r"^(\d+(?:\.\d+){0,3})[\s\u3000]+(\S.*)$")

#: 目录行（尾随页码，需过滤，避免把目录当正文标题）
TOC_LINE_RE = re.compile(r"^\d+(?:\.\d+)*[\s\u3000]+\S.*[\s\u3000]+\d+$")

#: 量化口径：「参数名：数值 单位」（单位取自白名单，降低误报）
QUANTITY_UNIT = (
    r"(?:个|台|套|件|组|人|天|次|页|条|项|种|层|米|毫米|公斤|公斤/立方米|"
    r"MPa|℃|kg|L|m/s|N·m|MB|GB|TB|KB|%|秒|分钟|小时|月|工作日)"
)
LABEL_VALUE_RE = re.compile(r"([\u4e00-\u9fffA-Za-z（）()·\-]{2,20}?)\s*[:：]\s*(\d+(?:\.\d+)?)\s*(" + QUANTITY_UNIT + r")")


class Report:
    """单个语料文件的校验结果。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.header: Dict[str, str] = {}
        self.clauses = 0

    @property
    def is_ledger(self) -> bool:
        """本文件是否为版本台账（版本权威源）。"""
        return self.header.get("资料编号") == LEDGER_DOC_NO

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def parse_header(text: str) -> Dict[str, str]:
    """解析文档头的 8 字段表格。"""
    header: Dict[str, str] = {}
    for field in HEADER_FIELDS:
        matched = re.search(rf"\|\s*{field}\s*\|\s*([^|]+?)\s*\|", text)
        if matched:
            header[field] = matched.group(1).strip().strip("*").strip()
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


def check_parameters(rep: Report, text: str) -> None:
    """参数一致性：只比对「参数名之后紧邻的数值」。

    不能简单地把一行里的所有数值都当作该参数的取值——技术资料常在同一行并列多个参数
    （如「额定工作压力 16 MPa、最高允许压力 18 MPa」），那样会产生大量误报。
    """
    lines = text.splitlines()
    for aliases, allowed in PARAM_RULES:
        for alias in aliases:
            pattern = re.compile(
                re.escape(alias) + r"[^0-9\n]{0,12}?(\d+(?:\.\d+)?)\s*(" + UNIT_GROUP + r")"
            )
            for matched in pattern.finditer(text):
                observed = f"{matched.group(1)} {matched.group(2)}"
                if observed in allowed:
                    continue
                lineno = text.count("\n", 0, matched.start()) + 1
                snippet = lines[lineno - 1].strip()[:70] if lineno <= len(lines) else ""
                rep.warn(f"参数「{alias}」取值「{observed}」不在允许集合 {sorted(allowed)}：第 {lineno} 行 {snippet}")


def check_document(rep: Report, text: str, ledger: List[Dict[str, str]]) -> None:
    """对单份语料执行全部校验项。"""
    # 1. 文档头完整性
    missing = [f for f in HEADER_FIELDS if not rep.header.get(f)]
    if missing:
        rep.error("文档头缺少字段：" + "、".join(missing))

    doc_no = rep.header.get("资料编号", "")
    version = rep.header.get("版本号", "")
    status = rep.header.get("现行状态", "")
    is_ledger = rep.is_ledger

    # 2. 台账一致性（版本台账自身是权威源，豁免）
    if doc_no and ledger and not is_ledger:
        matches = [r for r in ledger if r["doc_no"] == doc_no]
        if not matches:
            rep.error(f"资料编号 {doc_no} 未登记在版本台账 {LEDGER_DOC_NO} 中")
        else:
            current = [r for r in matches if "现行" in r["status"]]
            if not current:
                rep.error(f"资料编号 {doc_no} 在台账中没有现行有效版本")
            elif version and version not in {r["version"] for r in current}:
                rep.error(
                    f"版本号 {version} 与台账的现行版本 "
                    f"{'、'.join(sorted(r['version'] for r in current))} 不一致"
                )
            if status and current and status != current[0]["status"]:
                rep.error(f"现行状态 {status} 与台账的 {current[0]['status']} 不一致")

    # 3. 作废值隔离（版本台账需要引用作废值说明修订历史，豁免）
    if not is_ledger:
        for value, note in OBSOLETE_VALUES.items():
            if value in text:
                rep.error(f"出现已作废版本数值「{value}」（{note}），作废值只允许出现在版本台账中")

    # 4. 非规范术语
    for term, advice in NONSTANDARD_TERMS.items():
        if term in text:
            rep.error(f"使用了非规范术语「{term}」：{advice}")

    # 5. 参数一致性（告警级）
    if not is_ledger:
        check_parameters(rep, text)

    # 6. 免责声明与条款统计
    if "本文档为示例语料" not in text:
        rep.warn("缺少「示例语料/教学演示值」免责声明")
    rep.clauses = sum(1 for line in text.splitlines() if CLAUSE_RE.match(line))


# ==========================================================================
# 基准比对模式：以一批「规范文档」为样板，归纳规范并对目标文档指出不规范处
# ==========================================================================


@dataclass
class SpecDoc:
    """基准/目标文档的统一表示（docx 与 md/txt 归一）。"""

    path: Path
    text: str
    headings: List[Tuple[int, str]]
    info: Dict[str, str]
    has_toc: bool

    @property
    def doc_no(self) -> str:
        return self.info.get("文档编号", "")

    @property
    def version(self) -> str:
        return self.info.get("版本号", "")


@dataclass
class SpecProfile:
    """从基准文档集归纳出的规范画像。"""

    docs: List[SpecDoc]
    info_fields: Dict[str, int]
    doc_no_signatures: Dict[str, int]
    label_values: Dict[str, Dict[str, int]]

    @property
    def n_docs(self) -> int:
        return len(self.docs)

    @property
    def required_fields(self) -> List[str]:
        """基准文档集中出现比例 ≥ REQUIRED_FIELD_RATIO 的字段。"""
        threshold = self.n_docs * REQUIRED_FIELD_RATIO
        return sorted(f for f, c in self.info_fields.items() if c >= threshold)

    @property
    def main_signatures(self) -> List[str]:
        """出现次数最多的文档编号形态（并列全取）。"""
        if not self.doc_no_signatures:
            return []
        top = max(self.doc_no_signatures.values())
        return sorted(s for s, c in self.doc_no_signatures.items() if c == top)

    @property
    def toc_present(self) -> bool:
        """基准集是否全部含目录。"""
        return bool(self.docs) and all(d.has_toc for d in self.docs)

    @property
    def stable_labels(self) -> Dict[str, Set[str]]:
        """出现比例 ≥ REQUIRED_FIELD_RATIO 且取值唯一的量化口径（label → 取值）。"""
        threshold = self.n_docs * REQUIRED_FIELD_RATIO
        out: Dict[str, Set[str]] = {}
        for label, values in self.label_values.items():
            if len(values) == 1 and sum(values.values()) >= threshold:
                out[label] = set(values)
        return out

    @property
    def conflicts(self) -> Dict[str, Set[str]]:
        """基准集内部取值冲突的量化口径（需人工确认）。"""
        return {label: set(values) for label, values in self.label_values.items() if len(values) > 1}


def _norm_key(value: str) -> str:
    """去掉全部空白（含全角空格），用于字段名/占位值比较。"""
    return re.sub(r"\s+", "", value or "")


def canon_info_label(raw: str) -> str:
    """把文档信息表字段名归一到规范字段名；不认识则返回空串。"""
    return INFO_FIELD_ALIASES.get(_norm_key(raw), "")


def _extract_headings(paragraphs) -> Tuple[List[Tuple[int, str]], bool]:
    """从 docx 段落抽取编号标题（style 优先），并判断是否含目录。"""
    headings: List[Tuple[int, str]] = []
    has_toc = False
    for para in paragraphs:
        text = (para.text or "").strip()
        style = (para.style.name if para.style is not None else "") or ""
        if style.lower().startswith("toc"):
            has_toc = True
            continue
        if not text:
            continue
        if _norm_key(text).startswith(("目录", "目次")):
            has_toc = True
            continue
        matched = HEADING_STYLE_RE.match(style)
        if not matched or TOC_LINE_RE.match(text):
            continue
        headings.append((int(matched.group(1)), text))
    return headings, has_toc


def _extract_headings_md(text: str) -> Tuple[List[Tuple[int, str]], bool]:
    """从 md/txt 抽取 `#` 标题，并判断是否含目录。"""
    headings: List[Tuple[int, str]] = []
    has_toc = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _norm_key(stripped).startswith(("目录", "目次")):
            has_toc = True
            continue
        matched = re.match(r"^(#{1,4})\s+(\S.*)$", stripped)
        if matched:
            headings.append((len(matched.group(1)), matched.group(2).strip()))
    return headings, has_toc


def _docx_info_table(document) -> Dict[str, str]:
    """解析文首「文档信息表」：单元格(标签, 值)，字段名归一，取首次出现。"""
    if not document.tables:
        return {}
    info: Dict[str, str] = {}
    for row in document.tables[0].rows:
        cells = [cell.text.strip() for cell in row.cells]
        if len(cells) < 2:
            continue
        key = canon_info_label(cells[0])
        if key and key not in info:
            info[key] = cells[1]
    return info


def load_docx(path: Path) -> Tuple[str, List[Tuple[int, str]], Dict[str, str], bool]:
    """加载 .docx：lazy import python-docx，缺库时给出明确指引。"""
    try:
        from docx import Document  # 延迟导入：仅 --ref 模式需要
    except ImportError as exc:  # pragma: no cover - 依赖环境
        raise RuntimeError("解析 .docx 需要 python-docx，请先执行：pip install python-docx") from exc
    document = Document(str(path))
    paragraphs = list(document.paragraphs)
    lines = [para.text for para in paragraphs]
    for table in document.tables:  # 表格文本一并纳入，供术语/量化口径检查
        for row in table.rows:
            lines.append(" | ".join(cell.text.strip() for cell in row.cells))
    headings, has_toc = _extract_headings(paragraphs)
    return "\n".join(lines), headings, _docx_info_table(document), has_toc


def load_md(path: Path) -> Tuple[str, List[Tuple[int, str]], Dict[str, str], bool]:
    """加载 .md/.txt：复用既有 8 字段文档头解析。"""
    text = path.read_text(encoding="utf-8")
    headings, has_toc = _extract_headings_md(text)
    info = {canon_info_label(k): v for k, v in parse_header(text).items() if canon_info_label(k)}
    return text, headings, info, has_toc


def load_spec_doc(path: Path) -> SpecDoc:
    """按后缀分流加载文档。"""
    suffix = path.suffix.lower()
    if suffix == ".docx":
        text, headings, info, has_toc = load_docx(path)
    elif suffix in (".md", ".txt"):
        text, headings, info, has_toc = load_md(path)
    else:
        raise RuntimeError(f"暂不支持的文档类型：{path.name}（仅支持 .docx/.md/.txt）")
    return SpecDoc(path=path, text=text, headings=headings, info=info, has_toc=has_toc)


def doc_no_signature(doc_no: str) -> str:
    """把文档编号抽象为形态签名：各段「字母/数字 + 长度」拼串，如 A2N3。"""
    cleaned = (doc_no or "").strip()
    if not cleaned:
        return ""
    parts = re.split(r"[-－—–_/]", cleaned)
    return "".join(("N" if part.isdigit() else "A") + str(len(part)) for part in parts if part)


def _chapter_issues(headings: Sequence[Tuple[int, str]]) -> List[str]:
    """章号检查：重号 / 跳级 / 一级章号不连续（均为 WARN 级）。"""
    issues: List[str] = []
    seen: Set[str] = set()
    numbered: List[Tuple[int, str]] = []
    for level, text in headings:
        matched = CLAUSE_HEAD_RE.match(text)
        if not matched:
            continue
        num = matched.group(1)
        if num in seen:
            issues.append(f"章号「{num}」重复出现")
        seen.add(num)
        numbered.append((level, num))
    for (prev_level, prev_num), (level, num) in zip(numbered, numbered[1:]):
        if level > prev_level + 1:
            issues.append(f"标题层级跳级：{prev_num}(H{prev_level}) → {num}(H{level})")
    level1 = [int(num) for level, num in numbered if level == 1 and num.isdigit()]
    for prev, cur in zip(level1, level1[1:]):
        if cur != prev + 1:
            issues.append(f"一级章号不连续：{prev} → {cur}")
    # 去重且保序
    deduped: List[str] = []
    for msg in issues:
        if msg not in deduped:
            deduped.append(msg)
    return deduped


def _label_values(text: str) -> Dict[str, Set[str]]:
    """抽取「参数名：数值 单位」量化口径（label → 取值集合）。"""
    out: Dict[str, Set[str]] = {}
    for matched in LABEL_VALUE_RE.finditer(text):
        label = _norm_key(matched.group(1))
        if len(label) < 2:
            continue
        out.setdefault(label, set()).add(f"{matched.group(2)} {matched.group(3)}")
    return out


def build_profile(ref_dir: Path) -> SpecProfile:
    """加载基准目录下全部文档并归纳规范画像。"""
    suffixes = (".docx", ".md", ".txt")
    files = sorted(
        p for p in ref_dir.iterdir()
        if p.is_file() and p.suffix.lower() in suffixes and not p.name.startswith("~$")
    )
    docs = [load_spec_doc(p) for p in files]

    info_fields: Dict[str, int] = {}
    doc_no_signatures: Dict[str, int] = {}
    label_values: Dict[str, Dict[str, int]] = {}
    for doc in docs:
        for field in doc.info:
            info_fields[field] = info_fields.get(field, 0) + 1
        if doc.doc_no:
            sig = doc_no_signature(doc.doc_no)
            doc_no_signatures[sig] = doc_no_signatures.get(sig, 0) + 1
        for label, values in _label_values(doc.text).items():
            bucket = label_values.setdefault(label, {})
            for value in values:
                bucket[value] = bucket.get(value, 0) + 1
    return SpecProfile(docs, info_fields, doc_no_signatures, label_values)


def check_against_profile(doc: SpecDoc, profile: SpecProfile) -> Tuple[List[str], List[str]]:
    """对单份文档按基准画像做符合性检查，返回 (errors, warnings)。"""
    errors: List[str] = []
    warnings: List[str] = []

    # —— 结构：文档信息表 ——
    if not doc.info:
        errors.append("缺少「文档信息表」（应为文首表格，含文档编号 / 版本号 / 编制单位 等字段）")
    else:
        missing = [f for f in profile.required_fields if not doc.info.get(f)]
        if missing:
            errors.append("文档信息表缺少基准必需字段：" + "、".join(missing))
        for key, value in doc.info.items():
            normalized = _norm_key(value)
            if not normalized:
                errors.append(f"文档信息表字段「{key}」为空")
            elif normalized == _norm_key(key) or normalized.lower() in PLACEHOLDER_TOKENS:
                warnings.append(f"文档信息表字段「{key}」仍为占位值「{value}」")

    # —— 文档头与版本 ——
    if doc.version and not VERSION_RE.match(doc.version):
        errors.append(f"版本号「{doc.version}」形态不符合基准（应形如 V1.0 / V1.0.0）")
    if doc.doc_no:
        signature = doc_no_signature(doc.doc_no)
        if profile.main_signatures and signature not in profile.main_signatures:
            warnings.append(
                f"文档编号「{doc.doc_no}」形态与基准主流不符"
                f"（基准主流：{'、'.join(profile.main_signatures)}，本文件：{signature}）"
            )

    # —— 结构：目录与章号 ——
    if profile.toc_present and not doc.has_toc:
        warnings.append("缺少「目录」")
    for msg in _chapter_issues(doc.headings):
        warnings.append("章号问题：" + msg)

    # —— 术语规范（复用既有非规范术语表） ——
    for term, advice in NONSTANDARD_TERMS.items():
        if term in doc.text:
            errors.append(f"使用了非规范术语「{term}」：{advice}")

    # —— 技术内容：只报与基准多数取值冲突，需人工确认，不自动判对错 ——
    labels = _label_values(doc.text)
    for label, allowed in profile.stable_labels.items():
        observed = labels.get(label)
        if not observed:
            continue
        bad = sorted(v for v in observed if v not in allowed)
        if bad:
            warnings.append(
                f"量化口径「{label}」取值「{'、'.join(bad)}」"
                f"与基准「{'、'.join(sorted(allowed))}」冲突，需人工确认"
            )
    return errors, warnings


def _print_profile(profile: SpecProfile) -> None:
    print(f"基准文档集：{len(profile.docs)} 份")
    for doc in profile.docs:
        print(
            f"  - {doc.path.name}｜编号 {doc.doc_no or '?'}｜版本 {doc.version or '?'}"
            f"｜标题 {len(doc.headings)} 条｜目录 {'有' if doc.has_toc else '无'}"
        )
    print("\n从基准集归纳出的规范（比对依据）：")
    print(f"  必需字段（≥{int(REQUIRED_FIELD_RATIO * 100)}% 基准文档齐备）："
          + ("、".join(profile.required_fields) or "（无）"))
    print("  文档编号主流形态：" + ("、".join(profile.main_signatures) or "（无）"))
    print("  版本号形态：V<数字>[.<数字>[.<数字>]]")
    print("  目录：" + ("基准均含目录" if profile.toc_present else "基准并非均含目录（不检查此项）"))
    if profile.conflicts:
        print("  基准内部取值冲突（需人工确认，不作为判据）：")
        for label, values in sorted(profile.conflicts.items()):
            print(f"    - {label}：" + " / ".join(sorted(values)))


def _print_selfcheck(profile: SpecProfile) -> Tuple[int, int]:
    print("\n" + "-" * 74)
    print("基准自洽性体检（逐份按归纳规范复核）：")
    total_errors = total_warnings = 0
    for doc in profile.docs:
        errors, warnings = check_against_profile(doc, profile)
        print(f"\n[{'FAIL' if errors else 'PASS'}] {doc.path.name}")
        for msg in errors:
            print(f"  [ERROR] {msg}")
        for msg in warnings:
            print(f"  [WARN ] {msg}")
        total_errors += len(errors)
        total_warnings += len(warnings)
    return total_errors, total_warnings


def _print_target(doc: SpecDoc, profile: SpecProfile) -> Tuple[int, int]:
    print("\n" + "-" * 74)
    print(f"目标文档规范符合性检查：{doc.path.name}")
    errors, warnings = check_against_profile(doc, profile)
    print(f"\n[{'FAIL' if errors else 'PASS'}] {doc.path.name}")
    for msg in errors:
        print(f"  [ERROR] {msg}")
    for msg in warnings:
        print(f"  [WARN ] {msg}")
    return len(errors), len(warnings)


def run_spec_check(ref_arg: str, target_arg: Optional[str]) -> int:
    """基准符合性检查主流程：--ref 体检；再加 --target 输出违规清单。"""
    ref_dir = Path(ref_arg)
    if not ref_dir.is_absolute():
        ref_dir = ROOT / ref_dir
    if not ref_dir.is_dir():
        print(f"[FATAL] 基准目录不存在：{ref_dir}")
        return 2

    print("资料规范符合性检查（以基准文档集为规范样板）")
    print("=" * 74)
    try:
        profile = build_profile(ref_dir)
    except RuntimeError as exc:
        print(f"[FATAL] {exc}")
        return 2
    if not profile.docs:
        print(f"[FATAL] 基准目录 {ref_dir} 下没有找到 .docx/.md/.txt 文档")
        return 2

    _print_profile(profile)
    total_errors, total_warnings = _print_selfcheck(profile)

    if target_arg:
        target_path = Path(target_arg)
        if not target_path.is_absolute():
            target_path = ROOT / target_path
        if not target_path.is_file():
            print(f"[FATAL] 目标文件不存在：{target_path}")
            return 2
        try:
            target = load_spec_doc(target_path)
        except RuntimeError as exc:
            print(f"[FATAL] {exc}")
            return 2
        target_errors, target_warnings = _print_target(target, profile)
        total_errors += target_errors
        total_warnings += target_warnings

    print("\n" + "=" * 74)
    if target_arg:
        print(f"基准比对完成（含基准自检）：错误 {total_errors} 个，告警 {total_warnings} 个")
        if total_errors:
            print("结论：目标文档存在不符合基准规范的 ERROR 项，请按上方清单修正后重跑。")
            return 1
        print("结论：目标文档未发现 ERROR 级不规范（WARN 项需人工确认）。")
        return 0
    print(f"基准自洽性体检完成：错误 {total_errors} 个，告警 {total_warnings} 个")
    if total_errors:
        print("结论：基准集自身存在不一致，建议先统一基准口径，再用于比对目标文档。")
    else:
        print("结论：基准集自洽（WARN 为基准内部差异项，供人工确认）。")
    return 0


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="装备技术资料语料与版本治理校验器")
    parser.add_argument("--dir", default="samples", help="语料目录（默认 samples）")
    parser.add_argument("--strict", action="store_true", help="把参数一致性告警也计为失败")
    parser.add_argument("--ref", default=None, help="基准目录：启用「资料规范符合性检查」模式（对基准做自洽性体检）")
    parser.add_argument("--target", default=None, help="目标文件：与 --ref 同用时，对目标文档输出违规清单")
    args = parser.parse_args(argv[1:])

    if args.ref:
        return run_spec_check(args.ref, args.target)

    corpus_dir = Path(args.dir)
    if not corpus_dir.is_absolute():
        corpus_dir = ROOT / corpus_dir
    if not corpus_dir.is_dir():
        print(f"[FATAL] 目录不存在：{corpus_dir}")
        return 2

    files = sorted(p for p in corpus_dir.glob("*.md") if p.name != "冒烟测试问题清单.md")
    if not files:
        print(f"[FATAL] {corpus_dir} 下没有找到语料文件")
        return 2

    texts = {p: p.read_text(encoding="utf-8") for p in files}

    # 先解析版本台账（版本权威源）
    ledger: List[Dict[str, str]] = []
    for path, text in texts.items():
        if parse_header(text).get("资料编号") == LEDGER_DOC_NO:
            ledger = parse_ledger(text)
            break

    print("装备技术资料语料与版本治理校验")
    print("=" * 74)
    if not ledger:
        print(f"[WARN] 未找到版本台账 {LEDGER_DOC_NO}，台账一致性检查已跳过")
    else:
        current = [r for r in ledger if "现行" in r["status"]]
        print(
            f"版本台账 {LEDGER_DOC_NO} 共登记 {len(ledger)} 条版本记录"
            f"（现行有效 {len(current)} 条，已作废 {len(ledger) - len(current)} 条）"
        )

    total_errors = 0
    total_warnings = 0
    total_clauses = 0
    for path in files:
        rep = Report(path)
        rep.header = parse_header(texts[path])
        check_document(rep, texts[path], ledger)
        total_clauses += rep.clauses
        status = "PASS" if not rep.errors else "FAIL"
        print(f"\n[{status}] {path.name}")
        print(
            f"  {rep.header.get('资料编号', '?')} {rep.header.get('版本号', '?')} "
            f"{rep.header.get('现行状态', '?')} · 条款 {rep.clauses} 条"
            + ("（版本权威源）" if rep.is_ledger else "")
        )
        for msg in rep.errors:
            print(f"  [ERROR] {msg}")
        for msg in rep.warnings:
            print(f"  [WARN ] {msg}")
        total_errors += len(rep.errors)
        total_warnings += len(rep.warnings)

    # 唯一现行版本检查
    by_no: Dict[str, List[Dict[str, str]]] = {}
    for row in ledger:
        by_no.setdefault(row["doc_no"], []).append(row)
    for doc_no, rows in sorted(by_no.items()):
        current = [r for r in rows if "现行" in r["status"]]
        if len(current) > 1:
            print(f"  [ERROR] 资料编号 {doc_no} 存在 {len(current)} 个现行有效版本，必须唯一")
            total_errors += 1

    print("\n" + "=" * 74)
    print(
        f"共校验 {len(files)} 份资料，条款 {total_clauses} 条，"
        f"错误 {total_errors} 个，告警 {total_warnings} 个"
    )
    if total_errors:
        print("结论：存在错误，资料不得入库，请修正后重跑。")
    elif args.strict and total_warnings:
        print("结论：错误为 0，但存在告警（--strict 下判失败）。")
    else:
        print("结论：语料规范性、编号版本一致性与作废值隔离全部通过。")
    return 1 if (total_errors or (args.strict and total_warnings)) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
