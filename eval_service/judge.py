"""评分模块：批量调用「满意度评价工作流」做多维评分。

真实模式：POST /v1/workflows/run，从 data.outputs 取 result_json / csi / verdict /
annotation_label / error_cause / badcase。并发由 ThreadPoolExecutor 控制（默认 4），
网络调用在子线程完成，SQLite 写入统一回到主线程，避免跨线程使用同一连接。

mock 模式：使用本地确定性规则评分（字符二元组重合度 + 数字冲突检测 + 回执信号），
无需任何凭据与网络即可端到端跑通，且同一输入永远得到同一结果，适合回归基线对比。
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from collector import is_receipt_only
from config import Config
from grounding import check_grounding
from dify_client import DifyClient, DifyError
from metrics import (
    VALID_ERROR_CAUSES,
    VALID_HALLUCINATION_RISK,
    VALID_LABELS,
    derive,
    normalize_user_score,
)
from store import Store, join_contexts, loads, new_run_id, now_ts

#: 表示「知识库没有覆盖」的措辞
GAP_PATTERNS = [
    "知识库中未收录", "知识库中未找到", "知识库中没有", "暂无相关信息",
    "没有收录", "未收录", "无法确认", "暂无相关记录", "未找到相关",
]

#: 数字（含小数）抽取
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")

#: 高风险数值：带这些单位的数字（次数 / 时长 / 吞吐 / 等级）编造代价高
RISKY_NUMBER_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(MPa|℃|N·m|kg|m/s|L|工作小时|个月|次|QPS|qps|小时|分钟|秒|天|万条|级|个工作日)"
)

#: 长数字串（如证书编号、备案号）同样属于高风险事实
LONG_DIGIT_RE = re.compile(r"\d{8,}")

#: 需要保留的评分字段
RESULT_FIELDS = [
    "factual_accuracy", "knowledge_completeness", "relevance", "clarity",
    "retrieval_quality", "knowledge_gap", "hallucination_risk", "annotation_label",
    "error_cause", "judge_reason", "missing_knowledge", "improvement_suggestions",
]


@dataclass
class JudgeStats:
    """一次评分任务的统计。"""

    total: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    mismatched_verdict: int = 0
    sanitized: int = 0
    badcases: int = 0
    elapsed_sec: float = 0.0
    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        """转为可 JSON 序列化的字典。"""
        return {
            "total": self.total,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "skipped": self.skipped,
            "mismatched_verdict": self.mismatched_verdict,
            "sanitized": self.sanitized,
            "badcases": self.badcases,
            "elapsed_sec": round(self.elapsed_sec, 2),
            "errors": self.errors[:20],
        }


# ---------------------------------------------------------------- mock 规则评分


def _bigrams(text: str) -> set:
    """字符二元组集合（对中文无需分词）。"""
    clean = re.sub(r"[\s\u3000]+", "", text or "")
    if len(clean) < 2:
        return set(clean)
    return {clean[i:i + 2] for i in range(len(clean) - 1)}


def _overlap(answer: str, contexts: str) -> float:
    """回答中有多少比例的字符二元组能在检索片段里找到。"""
    a = _bigrams(answer)
    if not a:
        return 0.0
    c = _bigrams(contexts)
    if not c:
        return 0.0
    return len(a & c) / len(a)


def _numbers(text: str) -> set:
    """抽取文本中的数字。"""
    return set(NUMBER_RE.findall(text or ""))


def _risky_numbers(text: str) -> set:
    """抽取「高风险数值」：带关键单位的数字 + 长数字串。

    只对这些数值做冲突检测，避免把派生值（例如报价差额 300）误判为编造。

    :param text: 待检测文本
    :return: 形如 {"5次", "24小时", "2024110800"} 的集合
    """
    text = text or ""
    out = {f"{value}{unit}" for value, unit in RISKY_NUMBER_RE.findall(text)}
    out.update(LONG_DIGIT_RE.findall(text))
    return out


def _topic_of(question: str) -> str:
    """从问题中提取一个用于聚合的短主题词。"""
    q = (question or "").strip()
    return q[:16] + ("…" if len(q) > 16 else "")


def _missing_knowledge_for(question: str, answer: str, contexts: str) -> List[str]:
    """给出「知识补全清单」条目（mock 版用受控词表，便于聚合去重）。

    词表按装备技术资料的资料类型组织：原理 / 操作规程 / 维修规程 / 故障案例 / 资料管理。
    """
    text = f"{question}{answer}"
    if any(k in text for k in ("力矩", "拧紧", "螺栓", "缸盖")):
        return ["HYS-WX-003《某型液压举升系统维修规程》中油缸缸盖螺栓拧紧力矩与拧紧顺序条目"]
    if any(k in text for k in ("滤芯", "更换周期", "保养周期")):
        return ["HYS-WX-003《某型液压举升系统维修规程》保养周期表（含滤芯 500 工作小时的修订说明）"]
    if any(k in text for k in ("油温", "过热", "冷却器")):
        return ["HYS-CZ-002《某型液压举升系统操作规程》油温监控与超过 75 ℃ 应急处置条款"]
    if any(k in text for k in ("下沉", "内泄漏", "保压", "液压锁")):
        return ["HYS-GZ-004《典型故障案例集》FTC-003 举升到位后缓慢下沉的保压判断方法"]
    if any(k in text for k in ("安全销", "泄压", "挂牌", "带压")):
        return ["HYS-CZ-002《某型液压举升系统操作规程》安全禁止事项全章"]
    if any(k in text for k in ("版本", "作废", "现行", "资料编号", "台账")):
        return ["HYS-ZL-005《装备技术资料清单与版本台账》中资料编号与版本替代关系表"]
    if any(k in text for k in ("蓄能器", "充氮", "原理", "回路")):
        return ["HYS-GY-001《某型液压举升系统工作原理》液压回路与蓄能器作用章节"]
    return [f"《{_topic_of(question)}》对应的装备技术资料条目"]


# ---------------------------------------------------------------- 装备域专项维度

#: 触发安全合规性检查的问题特征：只有「要动手做某件事」的提问才检查安全前置要求。
#: 纯参数类提问（如「滤芯多久换一次」）不触发，避免把周期查询误判为安全警示缺失。
SAFETY_TRIGGER_RE = re.compile(
    r"如何|怎么|怎样|步骤|顺序|流程|操作要点|作业前|维修前|保养时|拆装|拆卸|"
    r"排故|应急处置|故障排除|检修|安全要求|注意事项"
)

#: 装备域归因可覆盖的基础归因。
#: 基础归因为「知识库缺失」等更根本的原因时不覆盖，避免掩盖真实问题。
DOMAIN_CAUSE_OVERRIDE_BASE = {
    "版本失效": ("无", "知识过期", "幻觉编造", "分块不当"),
    "安全警示缺失": ("无", "知识过期", "幻觉编造", "分块不当", "提示词缺陷"),
    "术语不统一": ("无",),
}

#: 回答中出现这些词，视为已履行安全前置要求
SAFETY_KEYWORDS = (
    "停机", "切断电源", "挂牌", "上锁", "泄压", "0 MPa", "机械支撑", "支撑稳固",
    "安全销", "禁止", "警告", "45 ℃", "降至", "确认无压",
)

#: 已作废版本中的数值：回答中出现即视为引用了作废资料（除非同时说明了版本修订）
OBSOLETE_VALUES = {
    "300 工作小时": "滤芯更换周期在 HYS-CZ-002 V4.0 中已由 300 工作小时修订为 500 工作小时",
    "300工作小时": "滤芯更换周期在 HYS-CZ-002 V4.0 中已由 300 工作小时修订为 500 工作小时",
    "14 MPa": "额定工作压力在 HYS-GY-001 V3.0 中已由 14 MPa 修订为 16 MPa",
    "14MPa": "额定工作压力在 HYS-GY-001 V3.0 中已由 14 MPa 修订为 16 MPa",
}

#: 回答中说明「该值已作废」的措辞：出现则不算版本失效
RETRACT_MARKERS = ("作废", "已修订", "旧版", "已被替代", "不再适用", "修订为", "废止")

#: 不规范术语（装备域术语归一）：应使用术语对照表中的标准术语
NONSTANDARD_TERMS = ("油压", "液压缸", "泵浦", "液压阀门", "油泵马达")

#: 与版本相关的提问特征
VERSION_QUESTION_RE = re.compile(r"版本|现行|作废|规程|手册|资料编号|台账|依据哪")


def _domain_dims(question: str, answer: str, contexts: str) -> Dict[str, Any]:
    """装备域专项维度的本地判定规则（mock 模式）。

    - safety_compliance：仅当问题涉及操作/维修/应急处置时计分，否则为 0（不涉及）
    - version_currency ：回答出现已作废版本数值且未说明修订时为 1；版本相关问答题给 4~5；
                         其余为 0（不涉及）
    - terminology_norm ：默认 5，出现不规范术语按数量降档，缩略语未展开给 3

    :return: 含三个维度值、归因建议与判分说明的字典
    """
    q, a, c = question or "", answer or "", contexts or ""
    out: Dict[str, Any] = {
        "safety_compliance": 0,
        "version_currency": 0,
        "terminology_norm": 5,
        "cause": "",
        "notes": [],
    }

    # 1) 版本时效性
    for value, note in OBSOLETE_VALUES.items():
        if value in a and not any(m in a for m in RETRACT_MARKERS):
            out["version_currency"] = 1
            out["cause"] = "版本失效"
            out["notes"].append(f"引用了已作废版本数值「{value}」：{note}。")
            break
    else:
        if VERSION_QUESTION_RE.search(q):
            cited = bool(re.search(r"V\d\.\d", a)) or "HYS-" in a
            out["version_currency"] = 5 if cited else 4

    # 2) 安全合规性
    if SAFETY_TRIGGER_RE.search(q):
        hits = sum(1 for k in SAFETY_KEYWORDS if k in a)
        if hits >= 3:
            out["safety_compliance"] = 5
        elif hits == 2:
            out["safety_compliance"] = 4
        elif hits == 1:
            out["safety_compliance"] = 3
        else:
            out["safety_compliance"] = 1
            out["cause"] = out["cause"] or "安全警示缺失"
            out["notes"].append("问题涉及操作/维修作业，回答未包含停机、泄压、挂牌上锁等安全前置要求。")

    # 3) 术语规范性
    bad = [t for t in NONSTANDARD_TERMS if t in a]
    if bad:
        out["terminology_norm"] = 2 if len(bad) >= 2 else 3
        out["cause"] = out["cause"] or "术语不统一"
        out["notes"].append("出现不规范术语：" + "、".join(bad) + "，应使用《术语与缩略语对照表》中的标准术语。")
    elif re.search(r"HYD-|ELC-", a) and not re.search(r"[（(]", a):
        out["terminology_norm"] = 3
        out["notes"].append("答案直接使用了部件代号，未给出中文全称。")
    return out


def _mock_score_base(
    question: str,
    answer: str,
    contexts: str,
    user_score_raw: str = "",
    receipt_score: Optional[int] = None,
    resources: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """mock 模式的基础规则评分（不含装备域专项维度），输出与评价工作流一致的结果 JSON。

    规则：
      * 命中知识缺失措辞 -> knowledge_gap，FA=3/KC=2，标签为「知识缺失」或「拒答不当」
      * 回答与检索片段二元组重合度低 -> 幻觉编造（FA=1..2）
      * 回答里出现了检索片段中不存在的数字 -> 数字冲突，判为幻觉编造（FA=2）
      * 重合度高 -> 正确（FA/KC 4~5）

    :return: 契约定义的评分 JSON（不含 csi/verdict，由 metrics.derive 派生）
    """
    answer = answer or ""
    contexts = contexts or ""
    ov = _overlap(answer, contexts)
    # 检索命中质量：由 rerank 分数换算到 1-5，无检索时为 0
    rq = 0
    if resources:
        scores = []
        for r in resources:
            try:
                scores.append(float(r.get("score")))
            except (TypeError, ValueError):
                continue
        if scores:
            rq = max(1, min(5, round((sum(scores) / len(scores)) * 5)))

    a_nums, c_nums = _risky_numbers(answer), _risky_numbers(contexts)
    numeric_conflict = bool(a_nums - c_nums)

    # ---------------- 1) 知识缺失
    if any(p in answer for p in GAP_PATTERNS):
        label = "拒答不当" if (receipt_score is not None and receipt_score <= 2) else "知识缺失"
        return {
            "factual_accuracy": 3,
            "knowledge_completeness": 2,
            "relevance": 4,
            "clarity": 4,
            "retrieval_quality": rq,
            "knowledge_gap": True,
            "hallucination_risk": "无",
            "annotation_label": label,
            "error_cause": "知识库缺失",
            "judge_reason": f"回答明确表示知识库未覆盖（重合度 {ov:.2f}），属于知识缺失而非编造。",
            "missing_knowledge": _missing_knowledge_for(question, answer, contexts),
            "improvement_suggestions": [
                "补充对应主题的官方文档并重建索引（create-by-text 后轮询 indexing-status）",
                "为该高频未命中问题新增 FAQ 条目并配置同义问法",
            ],
        }

    # ---------------- 2) 幻觉 / 编造
    if ov < 0.18:
        fa = 1 if ov < 0.12 else 2
        return {
            "factual_accuracy": fa,
            "knowledge_completeness": 2,
            "relevance": 3,
            "clarity": 3,
            "retrieval_quality": min(rq, 3) if rq else rq,
            "knowledge_gap": False,
            "hallucination_risk": "高" if fa == 1 else "中",
            "annotation_label": "错误",
            "error_cause": "幻觉编造",
            "judge_reason": f"回答与检索片段几乎无重合（重合度 {ov:.2f}），且给出了片段中不存在的事实细节。",
            "missing_knowledge": _missing_knowledge_for(question, answer, contexts),
            "improvement_suggestions": [
                "在系统提示词中强约束：仅依据检索片段作答，不得编造编号、日期与数值",
                "对资质/编号类回答要求附带引用来源（文档名 + 段落）",
                "提高检索 top_k 并开启 rerank，确保回答前拿到权威片段",
            ],
        }

    # ---------------- 3) 数字冲突（部分正确但事实有误）
    if numeric_conflict:
        return {
            "factual_accuracy": 2,
            "knowledge_completeness": 3,
            "relevance": 4,
            "clarity": 4,
            "retrieval_quality": rq,
            "knowledge_gap": False,
            "hallucination_risk": "中",
            "annotation_label": "部分正确",
            "error_cause": "幻觉编造",
            "judge_reason": f"回答出现检索片段中不存在的高风险数值（回答 {sorted(a_nums)}，片段 {sorted(c_nums)}），关键事实有误。",
            "missing_knowledge": _missing_knowledge_for(question, answer, contexts),
            "improvement_suggestions": [
                "提示词中要求数值、次数、时长必须原样引用检索片段，不得改写",
                "对含数字的回答增加一致性校验（回答数字需为片段数字子集）",
            ],
        }

    # ---------------- 4) 正确 / 部分正确
    if ov >= 0.60:
        fa, kc, re_, cl = 5, 5, 5, 5
        label, cause, risk = "正确", "无", "无"
        reason = f"回答要点与检索片段高度一致（重合度 {ov:.2f}），无编造。"
    elif ov >= 0.45:
        fa, kc, re_, cl = 4, 4, 5, 4
        label, cause, risk = "正确", "无", "无"
        reason = f"回答与检索片段一致（重合度 {ov:.2f}），要点基本齐全但表述略有简化。"
    elif ov >= 0.30:
        fa, kc, re_, cl = 3, 3, 4, 4
        label, cause, risk = "部分正确", "分块不当", "低"
        reason = f"回答方向正确但覆盖不足（重合度 {ov:.2f}），部分关键细节缺失。"
    else:
        fa, kc, re_, cl = 2, 3, 4, 4
        label, cause, risk = "部分正确", "检索未命中", "低"
        reason = f"回答与检索片段关联较弱（重合度 {ov:.2f}），疑似未召回关键片段。"

    # 回执信号：用户回执低分说明完备性或正确性存疑
    if receipt_score is not None and receipt_score <= 2:
        fa = min(fa, 3)
        kc = min(kc, 3)
        if label == "正确":
            label = "部分正确"
    elif receipt_score is not None and receipt_score >= 4 and ov >= 0.45:
        kc = max(kc, 4)

    return {
        "factual_accuracy": fa,
        "knowledge_completeness": kc,
        "relevance": re_,
        "clarity": cl,
        "retrieval_quality": rq,
        "knowledge_gap": False,
        "hallucination_risk": risk,
        "annotation_label": label,
        "error_cause": cause,
        "judge_reason": reason,
        "missing_knowledge": [] if label == "正确" else _missing_knowledge_for(question, answer, contexts),
        "improvement_suggestions": (
            ["保持当前回答模板与检索配置"] if label == "正确"
            else [
                "优化分块策略：按小节切分并保留标题层级，提升片段自解释性",
                "将 top_k 从 4 提升至 6 并开启 rerank，减少关键要点遗漏",
            ]
        ),
    }


def mock_score_row(
    question: str,
    answer: str,
    contexts: str,
    user_score_raw: str = "",
    receipt_score: Optional[int] = None,
    resources: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """mock 模式的完整评分：基础规则 + 装备域专项维度。

    装备域专项维度（safety_compliance / version_currency / terminology_norm）
    由 _domain_dims 独立判定后叠加，保证与真实工作流的输出结构一致。

    :return: 契约定义的评分 JSON（不含 csi/verdict，由 metrics.derive 派生）
    """
    result = _mock_score_base(question, answer, contexts, user_score_raw, receipt_score, resources)
    dom = _domain_dims(question, answer, contexts)
    result["safety_compliance"] = dom["safety_compliance"]
    result["version_currency"] = dom["version_currency"]
    result["terminology_norm"] = dom["terminology_norm"]
    base_cause = str(result.get("error_cause") or "无") or "无"
    if dom["cause"] and base_cause in DOMAIN_CAUSE_OVERRIDE_BASE.get(dom["cause"], ()):
        result["error_cause"] = dom["cause"]
    if dom["notes"]:
        result["judge_reason"] = ((result.get("judge_reason") or "") + " " + " ".join(dom["notes"])).strip()
    return result


# ---------------------------------------------------------------- 评分器


class Judge:
    """多维评分调度器。"""

    def __init__(
        self,
        client: DifyClient,
        store: Store,
        cfg: Optional[Config] = None,
        concurrency: int = 4,
        verbose: bool = True,
    ):
        """
        :param client: 评价工作流客户端（DIFY_EVAL_API_KEY）
        :param store: Store
        :param cfg: 配置
        :param concurrency: 并发数（mock 模式同样支持，便于验证并发路径）
        :param verbose: 是否打印进度
        """
        self.client = client
        self.store = store
        self.cfg = cfg
        self.concurrency = max(1, int(concurrency))
        self.verbose = verbose

    def _log(self, message: str) -> None:
        """打印评分进度。"""
        if self.verbose:
            print(f"[judge] {message}")

    # ------------------------------------------------ 输入构造
    def build_inputs(
        self,
        message: Dict[str, Any],
        reference_answer: str = "",
        extra_meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, str]:
        """构造评价工作流的输入（全部为字符串）。

        :param message: store 里的 messages 行
        :param reference_answer: 参考标准答案（回归评测可用）
        :param extra_meta: 追加到 meta 的字段
        :return: 工作流 inputs 字典
        """
        resources = loads(message.get("retriever_resources_json"), [])
        contexts = join_contexts(resources)
        meta = {
            "message_id": message.get("id"),
            "conversation_id": message.get("conversation_id"),
            "app_id": extra_meta.get("app_id") if extra_meta else "",
        }
        if extra_meta:
            meta.update({k: v for k, v in extra_meta.items() if k != "app_id"})
        if extra_meta and extra_meta.get("app_id"):
            meta["app_id"] = extra_meta["app_id"]
        suggestion = message.get("receipt_suggestion") or message.get("feedback_content") or ""
        # 主观三项（docs/01）：门户回执当前只采到「满意度」，有用性与体验感待补采，
        # 缺项传空串，DSL 侧按「用户未评分」处理（不参与 SSI，而不是记 0 分）。
        receipt_score = message.get("receipt_score")
        if receipt_score in (None, ""):
            normalized = normalize_user_score(message.get("user_score_raw"))
            satisfaction: Any = normalized if normalized is not None else ""
        else:
            satisfaction = receipt_score
        modality = message.get("modality") or (extra_meta or {}).get("modality") or ""
        return {
            "question": str(message.get("query") or ""),
            "answer": str(message.get("answer") or ""),
            "contexts": contexts,
            "reference_answer": str(reference_answer or ""),
            "modality": str(modality),
            "user_satisfaction": "" if satisfaction in (None, "") else str(satisfaction),
            "user_usefulness": str(message.get("receipt_usefulness") or ""),
            "user_experience": str(message.get("receipt_experience") or ""),
            "user_suggestion": str(suggestion),
        }

    # ------------------------------------------------ 结果校验
    def sanitize(self, payload: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
        """把工作流返回的结果 JSON 清洗到契约取值域内。

        :param payload: 工作流返回的 result_json 解析结果
        :return: (清洗后的字典, 被修正的字段数)
        """
        fixed = 0
        out: Dict[str, Any] = {}
        # 0-5 维度：0 表示「本条不涉及」（RQ 无检索 / SC 不涉及安全内容 / VC 不涉及版本引用）
        for key in (
            "factual_accuracy", "knowledge_completeness", "relevance", "clarity",
            "retrieval_quality", "safety_compliance", "version_currency", "terminology_norm",
        ):
            raw = payload.get(key)
            try:
                val = int(round(float(raw)))
            except (TypeError, ValueError):
                val = 0
            if key in ("retrieval_quality", "safety_compliance", "version_currency"):
                if not (0 <= val <= 5):
                    val, fixed = 0, fixed + 1
            elif not (1 <= val <= 5):
                val, fixed = 3, fixed + 1
            out[key] = val

        gap = payload.get("knowledge_gap")
        out["knowledge_gap"] = bool(gap if isinstance(gap, bool) else str(gap).strip().lower() in ("true", "1", "yes", "是"))

        risk = str(payload.get("hallucination_risk") or "无").strip()
        if risk not in VALID_HALLUCINATION_RISK:
            risk, fixed = "无", fixed + 1
        out["hallucination_risk"] = risk

        label = str(payload.get("annotation_label") or "部分正确").strip()
        if label not in VALID_LABELS:
            label, fixed = "部分正确", fixed + 1
        out["annotation_label"] = label

        cause = str(payload.get("error_cause") or "无").strip()
        if cause not in VALID_ERROR_CAUSES:
            cause, fixed = "无", fixed + 1
        out["error_cause"] = cause

        out["judge_reason"] = str(payload.get("judge_reason") or "").strip()
        for key in ("missing_knowledge", "improvement_suggestions"):
            raw = payload.get(key) or []
            if isinstance(raw, str):
                raw = [raw] if raw.strip() else []
            if not isinstance(raw, list):
                raw, fixed = [], fixed + 1
            out[key] = [str(x).strip() for x in raw if str(x).strip()]
        return out, fixed

    # ------------------------------------------------ 单条评分
    def score_one(self, message: Dict[str, Any], reference_answer: str = "") -> Dict[str, Any]:
        """对单条消息评分（可在线程池中并发调用，只做网络/纯计算，不写库）。"""
        inputs = self.build_inputs(message, reference_answer)
        contexts = inputs["contexts"]
        raw_user_score = str(message.get("user_score_raw") or "").strip()
        resources = loads(message.get("retriever_resources_json"), [])
        workflow_csi: Optional[float] = None
        workflow_verdict: Optional[str] = None
        if self.client.is_mock:
            result = mock_score_row(
                question=inputs["question"],
                answer=inputs["answer"],
                contexts=contexts,
                user_score_raw=raw_user_score,
                receipt_score=message.get("receipt_score"),
                resources=resources,
            )
            source = "mock-rule"
        else:
            resp = self.client.run_workflow(inputs)
            data = resp.get("data") or {}
            if (data.get("status") or "").lower() == "failed":
                raise DifyError(f"工作流执行失败：{data.get('error')}")
            outputs = data.get("outputs") or {}
            raw_json = outputs.get("result_json") or "{}"
            if isinstance(raw_json, str):
                try:
                    parsed = json.loads(raw_json)
                except ValueError as exc:
                    raise DifyError(f"result_json 不是合法 JSON：{exc}") from exc
            elif isinstance(raw_json, dict):
                parsed = raw_json
            else:
                raise DifyError(f"result_json 类型异常：{type(raw_json)}")
            result = dict(parsed)
            # 兼容工作流把部分字段放在 outputs 顶层的情况
            for key in ("annotation_label", "error_cause", "knowledge_gap"):
                if key not in result and key in outputs:
                    result[key] = outputs[key]
            # 工作流自报的 csi / verdict：只用于交叉校验，落库的派生指标一律以 metrics 为准
            try:
                workflow_csi = float(outputs.get("csi")) if outputs.get("csi") is not None else None
            except (TypeError, ValueError):
                workflow_csi = None
            workflow_verdict = str(outputs.get("verdict") or "").strip() or None
            source = "workflow"

        # 生成后确定性校验：答案里的数值/编号/版本/条款是否真出现在命中片段里
        # （v1 只度量、只留证，不参与 verdict/badcase，见 eval_service/grounding.py 头部说明）
        grounding = check_grounding(inputs["answer"], contexts)

        clean, fixed = self.sanitize(result)
        derived = derive(
            clean["factual_accuracy"], clean["knowledge_completeness"],
            clean["relevance"], clean["clarity"], clean["knowledge_gap"], raw_user_score,
            safety_compliance=clean["safety_compliance"],
            version_currency=clean["version_currency"],
            terminology_norm=clean["terminology_norm"],
            error_cause=clean["error_cause"],
        )
        row = {
            "message_id": message.get("id"),
            "conversation_id": message.get("conversation_id"),
            "question": inputs["question"],
            "answer": inputs["answer"],
            "contexts": contexts,
            "user_score_raw": raw_user_score,
            "source": source,
            "grounding_rate": grounding["rate"],
            "grounding_level": grounding["level"],
            "ungrounded": grounding["ungrounded"],
            "result_json": {**clean, **derived, "grounding": grounding},
            "sanitized_fields": fixed,
            "workflow_csi": workflow_csi,
            "workflow_verdict": workflow_verdict,
            **clean,
            **derived,
        }
        return row

    # ------------------------------------------------ 批量评分
    def run(
        self,
        limit: Optional[int] = None,
        message_ids: Optional[Sequence[str]] = None,
        reference_map: Optional[Dict[str, str]] = None,
        include_receipt_only: bool = False,
    ) -> JudgeStats:
        """批量评分并写库。

        :param limit: 最多处理多少条待评价消息
        :param message_ids: 指定消息 ID（回归评测用）
        :param reference_map: message_id -> 参考答案
        :param include_receipt_only: 是否把「评价回执」消息也当作问答样本评分（默认跳过）
        :return: JudgeStats
        """
        import time as _time

        started = _time.time()
        stats = JudgeStats()
        run_id = self.store.start_run(
            "judge",
            {"limit": limit, "concurrency": self.concurrency, "mock": bool(self.client.is_mock)},
        )
        reference_map = reference_map or {}

        if message_ids:
            placeholders = ",".join("?" for _ in message_ids)
            rows = [
                dict(r) for r in self.store.conn.execute(
                    f"SELECT * FROM messages WHERE id IN ({placeholders})", list(message_ids)
                ).fetchall()
            ]
        else:
            rows = self.store.fetch_pending_messages(limit=limit)

        # 「评价回执」消息不是一次问答（它是对上一条回答的评价），默认不参与质量评分
        if rows and not include_receipt_only:
            kept: List[Dict[str, Any]] = []
            for row in rows:
                if is_receipt_only(row.get("answer")):
                    stats.skipped += 1
                else:
                    kept.append(row)
            if stats.skipped:
                self._log(f"跳过 {stats.skipped} 条「评价回执」消息（如需评分请加 --include-receipt-only）")
            rows = kept

        stats.total = len(rows)
        if not rows:
            self._log("没有待评分的消息（可先运行 collect）")
            self.store.finish_run(run_id, "success", stats.as_dict())
            return stats

        self._log(f"待评分 {stats.total} 条，并发 {self.concurrency}，数据源 {'mock' if self.client.is_mock else 'workflow'}")

        results: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = {
                pool.submit(self.score_one, row, reference_map.get(row["id"], "")): row
                for row in rows
            }
            for future in as_completed(futures):
                row = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:  # noqa: BLE001 - 单条失败不能中断整批
                    stats.failed += 1
                    msg = f"{row.get('id')} 评分失败：{exc}"
                    stats.errors.append(msg)
                    self._log(msg)

        # 统一在主线程写库
        for item in results:
            item["run_id"] = run_id
            item["created_at"] = now_ts()
            self.store.upsert_evaluation(item)
            stats.succeeded += 1
            stats.sanitized += int(item.pop("sanitized_fields", 0) or 0)
            stats.badcases += int(bool(item.get("badcase")))
            wf_verdict = item.pop("workflow_verdict", None)
            item.pop("workflow_csi", None)
            if wf_verdict and wf_verdict != item.get("verdict"):
                stats.mismatched_verdict += 1

        stats.elapsed_sec = _time.time() - started
        self.store.finish_run(run_id, "success", stats.as_dict())
        self._log(
            f"完成：成功 {stats.succeeded}，失败 {stats.failed}，badcase {stats.badcases}，"
            f"耗时 {stats.elapsed_sec:.2f}s（run_id={run_id}）"
        )
        return stats


def judge_once(cfg: Config, store: Store, limit: Optional[int] = None, concurrency: Optional[int] = None) -> JudgeStats:
    """便捷入口：按配置构造客户端并批量评分一次。"""
    client = DifyClient.from_config(cfg, "eval")
    return Judge(client, store, cfg, concurrency=concurrency or cfg.concurrency).run(limit=limit)
