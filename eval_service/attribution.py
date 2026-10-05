"""归因前置模块（docs/10 §4）：判定一条低分回答的问题出在资料还是系统。

核心纪律：**只有「检索到了、忠实引用了、却仍然答不好」才判定为文档缺陷**；
其余问题一律归因到系统（分块 / 索引 / 提示词 / 模型），不得记到资料头上。

blame 取值：
    doc     可定位到具体某份资料的缺陷（进入该资料的记分卡）
    corpus  资料整体层面的覆盖缺口（无法定位到某一份资料，进入覆盖缺口清单）
    system  系统侧问题，不计入资料质量
    none    未见异常

规则（R1~R9，确定性优先，LLM 只做兜底）：
    R1 无任何检索片段                -> 系统（检索失败）
    R2 命中片段但检索质量差          -> 系统（检索失准 / 分块）
    R3 幻觉风险中高或归因幻觉编造    -> 系统（提示词 / 模型）
    R4 命中且忠实，但内容不全        -> 文档（D1 覆盖/完整性）
    R5 命中且忠实，但事实错误        -> 文档（D2 内容准确性）
    R6 归因跨文档冲突                -> 文档（D3 一致性，需技术核定）
    R7 引用作废版本或非现行版本      -> 文档/索引（D4 版本时效性）
    R8 命中片段结构不可检索          -> 文档（D7 结构可检索性）
    R9 无命中且确认资料给出缺失      -> 文档（D1 覆盖完备性）
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from corpus import CorpusIndex

#: 从 Dify 的 document_name 中抽取资料编号与版本号
#: 形如：HYS-WX-003 某型液压举升系统维修规程 V2.0
DOC_REF_RE = re.compile(r"(HYS-[A-Z]{2}-\d{3})\D{0,24}?(V\d+(?:\.\d+)?)")

#: 文档缺陷类型字典：code -> (质量维度, 中文名, 默认严重度)
DEFECT_TYPES: Dict[str, Tuple[str, str, str]] = {
    "D1_MISSING": ("D1", "覆盖缺口", "P2"),
    "D1_INCOMPLETE": ("D1", "内容不全", "P2"),
    "D2_WRONG": ("D2", "内容错误", "P0"),
    "D2_ADAPT": ("D2", "型号适配错误", "P1"),
    "D3_CONFLICT": ("D3", "跨文档冲突", "P1"),
    "D4_STALE": ("D4", "知识过期", "P1"),
    "D4_OBSOLETE": ("D4", "引用作废版本", "P1"),
    "D5_TERM": ("D5", "术语不统一", "P2"),
    "D6_SAFETY": ("D6", "安全警示缺失", "P0"),
    "D7_STRUCT": ("D7", "结构不可检索", "P2"),
    "D8_TRACE": ("D8", "引用不可追溯", "P2"),
}

#: 文档缺陷的整改动作（供缺陷整改单直接引用）
DEFECT_ACTION: Dict[str, str] = {
    "D1_MISSING": "补充对应资料的正文段落并重建索引（kb-build），必要时新增 FAQ 条目",
    "D1_INCOMPLETE": "补全该条款内容：补步骤、补适用条件、补边界值",
    "D2_WRONG": "核对权威值并更正资料正文，标注修订记录与版本",
    "D2_ADAPT": "在资料中明确适用型号与工况，必要时按型号启用元数据过滤",
    "D3_CONFLICT": "组织技术部门核定权威值，统一各资料表述并标注版本",
    "D4_STALE": "更新资料正文并作废旧段落，重建索引",
    "D4_OBSOLETE": "清除知识库中残留的作废版本段落，并在提示词中要求标注资料编号与版本号",
    "D5_TERM": "以《术语与缩略语对照表》为准统一术语，缩略语首次出现展开",
    "D6_SAFETY": "把安全警示与禁止事项作为独立段落入库，提示词中禁止为简洁而删减",
    "D7_STRUCT": "重构标题层级与条款编号、重做跨页表格，使其可被检索",
    "D8_TRACE": "解析期写入条款号与页码，保证答案可回溯到具体条款",
}

#: 系统侧问题类型：code -> (中文名, 默认严重度)
SYSTEM_TYPES: Dict[str, Tuple[str, str]] = {
    "S_NO_RETRIEVAL": ("检索失败", "P1"),
    "S_MISS": ("检索未命中", "P1"),
    "S_CHUNK": ("分块不当", "P1"),
    "S_HALLUCINATION": ("幻觉编造", "P0"),
    "S_PROMPT": ("提示词缺陷", "P2"),
    "S_MODEL": ("模型能力不足", "P2"),
    "S_AMBIG": ("问题歧义", "P2"),
}

SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}


def parse_doc_refs(resources: Optional[Sequence[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """从 retriever_resources 解析命中的资料编号与版本。

    :param resources: Dify 的 retriever_resources 数组
    :return: [{doc_no, version, score, segment_id, name}]
    """
    refs: List[Dict[str, Any]] = []
    for item in resources or []:
        name = str(item.get("document_name") or item.get("title") or "")
        matched = DOC_REF_RE.search(name)
        doc_no = matched.group(1) if matched else ""
        version = matched.group(2) if matched else ""
        refs.append(
            {
                "doc_no": doc_no,
                "version": version,
                "score": item.get("score"),
                "segment_id": item.get("segment_id"),
                "name": name,
            }
        )
    return refs


def _int(value: Any, default: int = 0) -> int:
    """安全转 int（None / 非数字回退默认值）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _primary_ref(refs: Sequence[Dict[str, Any]], corpus: Optional[CorpusIndex]) -> Dict[str, Any]:
    """选出代表本次回答的资料：优先「版本与台账不一致」的那份，否则取第一条。"""
    if not refs:
        return {}
    if corpus is not None:
        for ref in refs:
            if ref.get("doc_no") and corpus.is_current(ref["doc_no"], ref.get("version")) is False:
                return ref
    return refs[0]


def attribute_evaluation(
    row: Dict[str, Any],
    resources: Optional[Sequence[Dict[str, Any]]] = None,
    corpus: Optional[CorpusIndex] = None,
) -> Dict[str, Any]:
    """对一条评价结果做归因前置判定。

    :param row: evaluations 表的一行（含六维评分与装备域专项维度）
    :param resources: 该消息的 retriever_resources
    :param corpus: 语料索引（用于版本时效性与条款存在性判定，可为 None）
    :return: {blame, defect_type, dimension, severity, doc_no, doc_version, reasons, evidence}
    """
    fa = _int(row.get("factual_accuracy"))
    kc = _int(row.get("knowledge_completeness"))
    rq = _int(row.get("retrieval_quality"))
    sc = _int(row.get("safety_compliance"))
    vc = _int(row.get("version_currency"))
    tn = _int(row.get("terminology_norm"), 5)
    hallu = str(row.get("hallucination_risk") or "无").strip()
    cause = str(row.get("error_cause") or "无").strip()
    gap = bool(row.get("knowledge_gap"))
    is_badcase = bool(row.get("badcase"))

    refs = parse_doc_refs(resources)
    primary = _primary_ref(refs, corpus)
    doc_no = primary.get("doc_no") or ""
    doc_version = primary.get("version") or ""

    # 检索是否命中正确条款；回答是否忠实于片段
    matched = bool(refs) and rq >= 3
    faithful = hallu not in ("中", "高") and cause != "幻觉编造"

    reasons: List[str] = []
    result: Dict[str, Any] = {
        "message_id": row.get("message_id"),
        "blame": "none",
        "defect_type": "",
        "dimension": "",
        "severity": None,
        "doc_no": doc_no,
        "doc_version": doc_version,
        "clause_no": "",
        "reasons": reasons,
        "evidence": {
            "retrieved": [
                {"doc_no": r["doc_no"], "version": r["version"], "score": r["score"], "segment_id": r["segment_id"]}
                for r in refs
            ],
            "retrieval_quality": rq,
            "hallucination_risk": hallu,
            "error_cause": cause,
        },
    }

    def doc_defect(code: str, reason: str, severity: Optional[str] = None) -> Dict[str, Any]:
        dimension, label, default_sev = DEFECT_TYPES[code]
        result["blame"] = "doc"
        result["defect_type"] = code
        result["dimension"] = dimension
        result["severity"] = severity or default_sev
        result["defect_label"] = label
        reasons.append(reason)
        return result

    def corpus_defect(code: str, reason: str, severity: Optional[str] = None) -> Dict[str, Any]:
        """资料整体层面的缺陷（覆盖缺口）：是文档侧问题，但无法归到某一份资料。"""
        dimension, label, default_sev = DEFECT_TYPES[code]
        result["blame"] = "corpus"
        result["defect_type"] = code
        result["dimension"] = dimension
        result["severity"] = severity or default_sev
        result["defect_label"] = label
        reasons.append(reason)
        return result

    def system_defect(code: str, reason: str, severity: Optional[str] = None) -> Dict[str, Any]:
        label, default_sev = SYSTEM_TYPES[code]
        result["blame"] = "system"
        result["defect_type"] = code
        result["dimension"] = "D7"
        result["severity"] = severity or default_sev
        result["defect_label"] = label
        reasons.append(reason)
        return result

    # ---------- 版本线（最高优先：作废版本是确定性缺陷，与评分无关）
    if vc == 1:
        return doc_defect("D4_OBSOLETE", "引用了已作废版本资料（version_currency = 1）", "P1")

    if corpus is not None and doc_no:
        current = corpus.current_version(doc_no)
        if current and doc_version and current != doc_version:
            obsolete = corpus.obsolete_versions(doc_no)
            note = f"（台账作废版本：{'、'.join(obsolete)}）" if obsolete else ""
            return doc_defect(
                "D4_OBSOLETE",
                f"命中片段版本 {doc_version} 与台账现行版本 {current} 不一致{note}",
                "P1",
            )

    # ---------- 安全线
    if 0 < sc <= 2:
        if matched and faithful:
            return doc_defect("D6_SAFETY", "检索命中且回答忠实，但安全警示 / 禁止事项缺失", "P0")
        return system_defect("S_MISS", "安全维度不达标，但检索未命中正确条款", "P0")

    # ---------- 幻觉线
    if hallu in ("中", "高") or cause == "幻觉编造":
        return system_defect("S_HALLUCINATION", f"幻觉风险 {hallu}，属提示词 / 模型问题", "P0")

    # ---------- 无检索片段
    if not refs:
        if gap or cause == "知识库缺失":
            # 没有命中任何片段，无法定位到具体资料：计入「覆盖缺口清单」而非某份资料的记分卡
            return corpus_defect("D1_MISSING", "资料中无对应内容（知识库缺失），且未命中任何片段", "P2")
        if cause == "问题歧义":
            return system_defect("S_AMBIG", "问题表述有歧义，未形成有效检索", "P2")
        return system_defect("S_NO_RETRIEVAL", "本次回答没有任何检索片段，属检索链路问题", "P1")

    # ---------- 检索失准 / 分块
    if rq <= 2:
        if cause == "分块不当":
            return system_defect("S_CHUNK", "命中片段被切断（分块不当）", "P1")
        return system_defect("S_MISS", f"检索命中质量偏低（retrieval_quality = {rq}）", "P1")

    # ---------- 以下分支：检索命中且回答忠实，问题指向资料本身
    if matched and faithful:
        if cause == "跨文档冲突":
            return doc_defect("D3_CONFLICT", "命中多份资料的片段且结论冲突，需技术核定", "P1")
        if cause == "知识过期":
            return doc_defect("D4_STALE", "资料内容已过期", "P1")
        if cause == "型号适配错误":
            return doc_defect("D2_ADAPT", "资料未明确适用型号", "P1")
        if fa <= 2:
            return doc_defect("D2_WRONG", f"检索命中且回答忠实于片段，但片段内容与权威值不符（FA={fa}）", "P0")
        if kc <= 3:
            return doc_defect("D1_INCOMPLETE", f"检索命中且回答忠实，但内容不全（KC={kc}）", "P2")
        if gap:
            return doc_defect("D1_MISSING", "用户所需知识在资料中不完整", "P2")

    # ---------- 术语线（不依赖检索命中）
    if 0 < tn <= 2:
        return doc_defect("D5_TERM", f"术语或缩略语不规范（TN={tn}）", "P2")

    # ---------- 其他系统侧归因
    if cause == "提示词缺陷":
        return system_defect("S_PROMPT", "系统提示词约束不足", "P2")
    if cause == "模型能力不足":
        return system_defect("S_MODEL", "模型能力不足", "P2")
    if cause == "问题歧义":
        return system_defect("S_AMBIG", "问题表述有歧义", "P2")
    if cause == "分块不当":
        return system_defect("S_CHUNK", "分块不当", "P1")
    if cause == "检索未命中":
        return system_defect("S_MISS", "检索未命中", "P1")

    if is_badcase:
        reasons.append("低分但未命中确定性规则，需人工判定")
    return result


def build_defect(
    attributed: Dict[str, Any],
    row: Dict[str, Any],
    actions: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """把归因结果整理成 defects 表的一行（同一文档+条款+类型视为同一条缺陷）。"""
    import hashlib
    import time

    blame = str(attributed.get("blame") or "none")
    if attributed.get("doc_no"):
        doc_no = str(attributed["doc_no"])
    elif blame == "corpus":
        doc_no = "（资料整体）"
    else:
        doc_no = "（未归因）"
    clause_no = attributed.get("clause_no") or ""
    defect_type = attributed.get("defect_type") or ""
    action = ""
    if actions:
        action = actions.get(defect_type, actions.get(str(row.get("error_cause") or "无"), ""))
    key = f"{doc_no}|{clause_no}|{defect_type}"
    defect_id = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    return {
        "defect_id": defect_id,
        "message_id": row.get("message_id"),
        "doc_no": doc_no,
        "doc_version": attributed.get("doc_version") or "",
        "clause_no": clause_no,
        "defect_type": defect_type,
        "defect_label": attributed.get("defect_label") or "",
        "dimension": attributed.get("dimension") or "",
        "severity": attributed.get("severity") or "P2",
        "blame": attributed.get("blame") or "none",
        "status": "open",
        "evidence": attributed.get("evidence") or {},
        "question": str(row.get("question") or "")[:200],
        "reasons": attributed.get("reasons") or [],
        "action": action,
        "owner": "",
        "due_date": "",
        "verified_by": "",
        "occurrences": 1,
        "opened_at": int(time.time()),
        "closed_at": None,
    }


def attribute_all(
    rows: Sequence[Dict[str, Any]],
    resources_map: Dict[str, Sequence[Dict[str, Any]]],
    corpus: Optional[CorpusIndex] = None,
) -> List[Dict[str, Any]]:
    """批量归因，返回与输入等长的结果列表。"""
    return [attribute_evaluation(row, resources_map.get(str(row.get("message_id"))), corpus) for row in rows]
