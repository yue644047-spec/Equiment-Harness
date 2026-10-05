"""指标计算模块：严格实现统一契约中的 CSI / verdict / badcase 规则。

契约（不得改动）：
  CSI（有 US）：0.30*US + 0.25*FA + 0.20*KC + 0.15*RE + 0.10*CL
  CSI（无 US）：(0.25*FA + 0.20*KC + 0.15*RE + 0.10*CL) / 0.70
  verdict：CSI>=4.2 优秀；>=3.5 良好；>=2.8 待改进；<2.8 不合格
  红线：FA<=2 直接不合格；US=dislike 且 CSI>=3.5 时降级为待改进
      装备域安全红线：safety_compliance<=2 直接不合格
  badcase：CSI<3.0 或 FA<=2 或 US=dislike 或（knowledge_gap 且 KC<=2）
          或 safety_compliance<=2 或 version_currency=1 或 terminology_norm<=2
          或 error_cause 命中装备域归因（安全警示缺失/版本失效/型号适配错误/术语不统一/跨文档冲突）

装备域专项维度（不进入 CSI 权重，保持主指数可纵向对比）：
  safety_compliance 0-5，0 表示本条不涉及安全内容
  version_currency  0-5，0 表示本条不涉及版本引用；1 表示引用了已作废版本且未提示
  terminology_norm  1-5
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

#: CSI 权重（有用户评分时）
WEIGHTS_WITH_US: Dict[str, float] = {
    "user_score": 0.30,
    "factual_accuracy": 0.25,
    "knowledge_completeness": 0.20,
    "relevance": 0.15,
    "clarity": 0.10,
}

#: 无用户评分时参与归一化的维度权重
WEIGHTS_NO_US: Dict[str, float] = {
    "factual_accuracy": 0.25,
    "knowledge_completeness": 0.20,
    "relevance": 0.15,
    "clarity": 0.10,
}

#: 无 US 时的权重总和（0.70）
NO_US_TOTAL = round(sum(WEIGHTS_NO_US.values()), 10)

VERDICT_EXCELLENT = "优秀"
VERDICT_GOOD = "良好"
VERDICT_FAIR = "待改进"
VERDICT_POOR = "不合格"

VERDICT_ORDER = [VERDICT_EXCELLENT, VERDICT_GOOD, VERDICT_FAIR, VERDICT_POOR]

SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}

VALID_LABELS = ["正确", "部分正确", "错误", "知识缺失", "拒答不当", "该拒答未拒答"]
VALID_ERROR_CAUSES = [
    "无", "知识库缺失", "检索未命中", "分块不当", "提示词缺陷",
    "模型能力不足", "幻觉编造", "知识过期", "版本失效", "问题歧义",
    # 装备技术资料域新增归因
    "型号适配错误", "术语不统一", "安全警示缺失", "跨文档冲突",
]
VALID_HALLUCINATION_RISK = ["无", "低", "中", "高"]

#: 装备域专项维度键（不进入 CSI 权重，以专项达标与红线方式参与判定）
DOMAIN_DIMENSION_KEYS = ("safety_compliance", "version_currency", "terminology_norm")

#: 安全合规性 <= 该值即触发装备域安全红线（直接判不合格）
SAFETY_REDLINE = 2
#: version_currency = 1 表示引用了已作废版本资料
VERSION_OBSOLETE = 1
#: 触发 badcase 的装备域归因
DOMAIN_BADCASE_CAUSES = ("安全警示缺失", "版本失效", "型号适配错误", "术语不统一", "跨文档冲突")


# ---------------------------------------------------------------- 归一化


def normalize_user_score(raw: Any) -> Optional[int]:
    """把原始用户评分归一化为 1..5 的整数。

    规则：like -> 5；dislike -> 1；"1".."5" -> 对应整数；空 -> None（缺失）。

    :param raw: 原始值，可能是 like/dislike/1..5/None/空串
    :return: 1..5 或 None
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = int(raw)
        return value if 1 <= value <= 5 else None
    text = str(raw).strip().lower()
    if text in ("", "none", "null", "nan"):
        return None
    if text == "like":
        return 5
    if text == "dislike":
        return 1
    if text in ("1", "2", "3", "4", "5"):
        return int(text)
    # 兼容 "5 分"、"score=4" 之类的脏数据
    for ch in text:
        if ch in "12345":
            return int(ch)
    return None


def csi_from_scores(
    factual_accuracy: Optional[int],
    knowledge_completeness: Optional[int],
    relevance: Optional[int],
    clarity: Optional[int],
    user_score: Optional[int],
) -> Optional[float]:
    """计算 CSI 综合满意度指数（0-5，保留两位小数）。

    :return: CSI；必需维度缺失时返回 None
    """
    dims = [factual_accuracy, knowledge_completeness, relevance, clarity]
    if any(d is None for d in dims):
        return None
    fa, kc, re_, cl = (float(d) for d in dims)
    if user_score is not None:
        value = (
            WEIGHTS_WITH_US["user_score"] * float(user_score)
            + WEIGHTS_WITH_US["factual_accuracy"] * fa
            + WEIGHTS_WITH_US["knowledge_completeness"] * kc
            + WEIGHTS_WITH_US["relevance"] * re_
            + WEIGHTS_WITH_US["clarity"] * cl
        )
    else:
        value = (
            WEIGHTS_NO_US["factual_accuracy"] * fa
            + WEIGHTS_NO_US["knowledge_completeness"] * kc
            + WEIGHTS_NO_US["relevance"] * re_
            + WEIGHTS_NO_US["clarity"] * cl
        ) / NO_US_TOTAL
    return round(value, 2)


def llm_quality_score(
    factual_accuracy: Optional[int],
    knowledge_completeness: Optional[int],
    relevance: Optional[int],
    clarity: Optional[int],
) -> Optional[float]:
    """仅由 LLM 维度构成的 0-5 质量分（用于与用户评分做相关性分析）。"""
    if any(d is None for d in (factual_accuracy, knowledge_completeness, relevance, clarity)):
        return None
    value = (
        WEIGHTS_NO_US["factual_accuracy"] * float(factual_accuracy)
        + WEIGHTS_NO_US["knowledge_completeness"] * float(knowledge_completeness)
        + WEIGHTS_NO_US["relevance"] * float(relevance)
        + WEIGHTS_NO_US["clarity"] * float(clarity)
    ) / NO_US_TOTAL
    return round(value, 2)


def decide_verdict(
    csi: Optional[float],
    factual_accuracy: Optional[int],
    user_score_raw: Any = None,
    *,
    safety_compliance: Optional[int] = 0,
    version_currency: Optional[int] = 0,
) -> str:
    """依据 CSI 与红线规则判定 verdict。

    判定顺序：CSI 阈值 -> dislike 降级 -> 版本降级 -> FA 红线 -> 装备域安全红线（最强）。

    :param csi: 综合满意度指数
    :param factual_accuracy: 事实准确性得分
    :param user_score_raw: 原始用户评分（用于识别 dislike）
    :param safety_compliance: 装备域安全合规性（0 表示本条不涉及安全内容）
    :param version_currency: 装备域版本时效性（1 表示引用了已作废版本资料）
    :return: 优秀 / 良好 / 待改进 / 不合格
    """
    if csi is None:
        return VERDICT_POOR
    if csi >= 4.2:
        verdict = VERDICT_EXCELLENT
    elif csi >= 3.5:
        verdict = VERDICT_GOOD
    elif csi >= 2.8:
        verdict = VERDICT_FAIR
    else:
        verdict = VERDICT_POOR

    raw = "" if user_score_raw is None else str(user_score_raw).strip().lower()
    if raw == "dislike" and csi >= 3.5:
        verdict = VERDICT_FAIR

    # 装备域版本降级：引用了已作废版本资料且未提示，最多只能判「待改进」。
    # 与 dislike 降级同级，弱于 FA 红线与安全红线。
    try:
        version = int(version_currency) if version_currency is not None else 0
    except (TypeError, ValueError):
        version = 0
    if version == VERSION_OBSOLETE and verdict in (VERDICT_EXCELLENT, VERDICT_GOOD):
        verdict = VERDICT_FAIR

    if factual_accuracy is not None and factual_accuracy <= 2:
        verdict = VERDICT_POOR

    # 装备域安全红线：装备操作/维修类回答若遗漏泄压、挂牌上锁、禁止带压拆卸等
    # 禁止性要求，后果不可接受，与 FA<=2 同级直接判不合格。
    # safety_compliance=0 表示本条不涉及安全内容，不触发。
    try:
        safety = int(safety_compliance) if safety_compliance is not None else 0
    except (TypeError, ValueError):
        safety = 0
    if 0 < safety <= SAFETY_REDLINE:
        verdict = VERDICT_POOR
    return verdict


def is_badcase(
    csi: Optional[float],
    factual_accuracy: Optional[int],
    knowledge_completeness: Optional[int],
    knowledge_gap: Any,
    user_score_raw: Any = None,
    *,
    safety_compliance: Optional[int] = 0,
    version_currency: Optional[int] = 0,
    terminology_norm: Optional[int] = 5,
    error_cause: str = "",
) -> bool:
    """badcase 判定。

    基础规则：CSI<3.0 或 FA<=2 或 US=dislike 或（knowledge_gap 且 KC<=2）。
    装备域追加：safety_compliance<=2 或 version_currency=1 或 terminology_norm<=2
    或 error_cause 命中装备域归因。
    """
    if csi is not None and csi < 3.0:
        return True
    if factual_accuracy is not None and factual_accuracy <= 2:
        return True
    raw = "" if user_score_raw is None else str(user_score_raw).strip().lower()
    if raw == "dislike":
        return True
    if bool(knowledge_gap) and knowledge_completeness is not None and knowledge_completeness <= 2:
        return True

    def _int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    safety = _int(safety_compliance, 0)
    version = _int(version_currency, 0)
    terminology = _int(terminology_norm, 5)
    if 0 < safety <= SAFETY_REDLINE:
        return True
    if version == VERSION_OBSOLETE:
        return True
    if terminology <= 2:
        return True
    if str(error_cause or "").strip() in DOMAIN_BADCASE_CAUSES:
        return True
    return False


def derive(
    factual_accuracy: Optional[int],
    knowledge_completeness: Optional[int],
    relevance: Optional[int],
    clarity: Optional[int],
    knowledge_gap: Any,
    user_score_raw: Any,
    *,
    safety_compliance: Optional[int] = 0,
    version_currency: Optional[int] = 0,
    terminology_norm: Optional[int] = 5,
    error_cause: str = "",
) -> Dict[str, Any]:
    """由原始维度分派生 CSI / verdict / badcase。

    装备域专项维度为可选关键字参数，缺省时不改变原有行为，
    保证旧调用点与历史数据的判定结果完全不变。

    :return: 含 csi / verdict / badcase / user_score 的字典
    """
    us = normalize_user_score(user_score_raw)
    csi = csi_from_scores(factual_accuracy, knowledge_completeness, relevance, clarity, us)
    return {
        "user_score": us,
        "csi": csi,
        "verdict": decide_verdict(
            csi, factual_accuracy, user_score_raw,
            safety_compliance=safety_compliance,
            version_currency=version_currency,
        ),
        "badcase": is_badcase(
            csi, factual_accuracy, knowledge_completeness, knowledge_gap, user_score_raw,
            safety_compliance=safety_compliance,
            version_currency=version_currency,
            terminology_norm=terminology_norm,
            error_cause=error_cause,
        ),
    }


# ---------------------------------------------------------------- 周期过滤


def period_start(period: Optional[str], now: Optional[datetime] = None) -> Optional[int]:
    """把 period 关键字换算成起始时间戳（Unix 秒）。

    支持：day/日、week/周、month/月、all/全部（None 表示不限制）。

    :param period: 周期关键字
    :param now: 当前时间，默认取本地时间
    :return: 起始 Unix 秒；all 或未知值返回 None
    """
    if not period:
        return None
    key = str(period).strip().lower()
    if key in ("all", "全部", "none", ""):
        return None
    base = now or datetime.now()
    if key in ("day", "daily", "日", "today", "1d"):
        delta = timedelta(days=1)
    elif key in ("week", "weekly", "周", "7d"):
        delta = timedelta(days=7)
    elif key in ("month", "monthly", "月", "30d"):
        delta = timedelta(days=30)
    else:
        raise ValueError(f"未知的 period：{period}（可用：day/week/month/all）")
    return int((base - delta).timestamp())


def filter_by_period(
    rows: Sequence[Dict[str, Any]],
    period: Optional[str],
    ts_key: str = "msg_created_at",
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """按周期过滤行。"""
    start = period_start(period, now)
    if start is None:
        return list(rows)
    out: List[Dict[str, Any]] = []
    for r in rows:
        ts = r.get(ts_key) or r.get("created_at") or 0
        try:
            if int(ts) >= start:
                out.append(r)
        except (TypeError, ValueError):
            continue
    return out


# ---------------------------------------------------------------- 统计工具


def mean(values: Sequence[float]) -> Optional[float]:
    """算术平均值，空序列返回 None。"""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 2)


def percentile(values: Sequence[float], p: float) -> Optional[float]:
    """线性插值分位数（p 取 0-100）。"""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return round(float(vals[0]), 2)
    k = (len(vals) - 1) * (p / 100.0)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return round(float(vals[int(k)]), 2)
    return round(float(vals[lo] * (hi - k) + vals[hi] * (k - lo)), 2)


def _rank(values: Sequence[float]) -> List[float]:
    """返回平均秩（处理并列）。"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def pearson(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    """皮尔逊相关系数；样本不足或方差为 0 时返回 None。"""
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return None
    return round(cov / math.sqrt(vx * vy), 4)


def spearman(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    """斯皮尔曼等级相关系数。"""
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    return pearson(_rank(list(xs)), _rank(list(ys)))


def distribution(values: Iterable[Any]) -> Dict[str, int]:
    """计数分布（按键名排序以稳定输出）。"""
    result: Dict[str, int] = {}
    for v in values:
        key = "(空)" if v is None or v == "" else str(v)
        result[key] = result.get(key, 0) + 1
    return dict(sorted(result.items(), key=lambda kv: (-kv[1], kv[0])))


# ---------------------------------------------------------------- 聚合


def aggregate(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """对评价行集合做聚合统计。

    :param rows: fetch_evaluations 返回的行（或等价字典）
    :return: 指标字典，供报表使用
    """
    n = len(rows)
    result: Dict[str, Any] = {"count": n}
    if n == 0:
        return {
            "count": 0,
            "dimension_means": {},
            "csi": {"mean": None, "median": None, "p50": None, "p90": None, "min": None, "max": None, "std": None},
            "verdict_distribution": {},
            "verdict_ratio": {},
            "pass_rate_35": None,
            "pass_rate_42": None,
            "badcase_count": 0,
            "badcase_rate": None,
            "knowledge_gap_rate": None,
            "user_score": {"count": 0, "mean": None, "distribution": {}},
            "llm_quality_mean": None,
            "correlation": {"pearson": None, "spearman": None, "n": 0},
            "annotation_label_distribution": {},
            "error_cause_distribution": {},
            "hallucination_risk_distribution": {},
            "severity_distribution": {},
            "domain": {
                "safety_compliance": None,
                "safety_compliance_coverage": None,
                "safety_redline_count": 0,
                "version_currency": None,
                "version_currency_coverage": None,
                "version_obsolete_count": 0,
                "terminology_norm": None,
                "terminology_low_count": 0,
            },
        }

    def col(name: str) -> List[Any]:
        return [r.get(name) for r in rows]

    csi_vals = [float(v) for v in col("csi") if v is not None]
    us_vals = [int(v) for v in col("user_score") if v is not None]
    llm_vals = [
        llm_quality_score(r.get("factual_accuracy"), r.get("knowledge_completeness"), r.get("relevance"), r.get("clarity"))
        for r in rows
    ]
    llm_vals = [v for v in llm_vals if v is not None]

    verdicts = [r.get("verdict") or "(空)" for r in rows]
    verdict_dist = distribution(verdicts)
    verdict_ratio = {k: round(v / n, 4) for k, v in verdict_dist.items()}

    badcount = sum(1 for r in rows if r.get("badcase"))
    gapcount = sum(1 for r in rows if r.get("knowledge_gap"))

    # 用户评分与 LLM 质量分的相关性（仅取两者都有的样本）
    paired = [
        (int(r["user_score"]), llm)
        for r, llm in zip(rows, [
            llm_quality_score(r.get("factual_accuracy"), r.get("knowledge_completeness"), r.get("relevance"), r.get("clarity"))
            for r in rows
        ])
        if r.get("user_score") is not None and llm is not None
    ]
    px = [p[0] for p in paired]
    py = [p[1] for p in paired]

    std = None
    if len(csi_vals) > 1:
        m = sum(csi_vals) / len(csi_vals)
        std = round(math.sqrt(sum((v - m) ** 2 for v in csi_vals) / (len(csi_vals) - 1)), 3)

    result.update(
        {
            "dimension_means": {
                "factual_accuracy": mean([v for v in col("factual_accuracy") if v is not None]),
                "knowledge_completeness": mean([v for v in col("knowledge_completeness") if v is not None]),
                "relevance": mean([v for v in col("relevance") if v is not None]),
                "clarity": mean([v for v in col("clarity") if v is not None]),
                # RQ 仅在真正发生检索（>0）时参与均值，否则会把「无检索」误算成低分
                "retrieval_quality": mean([v for v in col("retrieval_quality") if v is not None and v > 0]),
                "retrieval_quality_coverage": round(
                    sum(1 for v in col("retrieval_quality") if v is not None and v > 0) / n, 4
                ),
            },
            "csi": {
                "mean": mean(csi_vals),
                "median": percentile(csi_vals, 50),
                "p50": percentile(csi_vals, 50),
                "p90": percentile(csi_vals, 90),
                "min": round(min(csi_vals), 2) if csi_vals else None,
                "max": round(max(csi_vals), 2) if csi_vals else None,
                "std": std,
            },
            "verdict_distribution": verdict_dist,
            "verdict_ratio": verdict_ratio,
            "pass_rate_35": round(sum(1 for v in csi_vals if v >= 3.5) / len(csi_vals), 4) if csi_vals else None,
            "pass_rate_42": round(sum(1 for v in csi_vals if v >= 4.2) / len(csi_vals), 4) if csi_vals else None,
            "badcase_count": badcount,
            "badcase_rate": round(badcount / n, 4),
            "knowledge_gap_rate": round(gapcount / n, 4),
            "user_score": {
                "count": len(us_vals),
                "coverage": round(len(us_vals) / n, 4),
                "mean": mean(us_vals),
                "distribution": distribution(us_vals),
            },
            "llm_quality_mean": mean(llm_vals),
            "correlation": {
                "pearson": pearson(px, py),
                "spearman": spearman(px, py),
                "n": len(paired),
            },
            "annotation_label_distribution": distribution(col("annotation_label")),
            "error_cause_distribution": distribution(col("error_cause")),
            "hallucination_risk_distribution": distribution(col("hallucination_risk")),
            "severity_distribution": distribution(
                [r.get("severity") for r in rows if r.get("severity")]
            ),
            # 装备域专项维度：安全合规性 / 版本时效性 / 术语规范性
            # 三项均只统计"本条真正涉及"的样本（值为 0 表示不涉及，不参与均值），
            # 否则会把「不涉及安全内容」的原理类问答误算成低分。
            "domain": {
                "safety_compliance": mean([v for v in col("safety_compliance") if v is not None and v > 0]),
                "safety_compliance_coverage": round(
                    sum(1 for v in col("safety_compliance") if v is not None and v > 0) / n, 4
                ),
                "safety_redline_count": sum(
                    1 for v in col("safety_compliance") if v is not None and 0 < v <= SAFETY_REDLINE
                ),
                "version_currency": mean([v for v in col("version_currency") if v is not None and v > 0]),
                "version_currency_coverage": round(
                    sum(1 for v in col("version_currency") if v is not None and v > 0) / n, 4
                ),
                "version_obsolete_count": sum(1 for v in col("version_currency") if v == VERSION_OBSOLETE),
                "terminology_norm": mean([v for v in col("terminology_norm") if v is not None]),
                "terminology_low_count": sum(
                    1 for v in col("terminology_norm") if v is not None and v <= 2
                ),
            },
        }
    )
    return result


def severity_of(row: Dict[str, Any]) -> Optional[str]:
    """按契约给 badcase 行分级。

    P0：FA<=2 或 幻觉风险为中/高（或 annotation_label/error_cause 指向幻觉编造）
    P1：CSI<3.0 或 US=dislike
    P2：KC<=3 或 knowledge_gap

    :return: P0/P1/P2；不属于 badcase 时返回 None
    """
    fa = row.get("factual_accuracy")
    kc = row.get("knowledge_completeness")
    csi = row.get("csi")
    gap = bool(row.get("knowledge_gap"))
    raw_us = row.get("user_score_raw")
    risk = str(row.get("hallucination_risk") or "").strip()
    label = str(row.get("annotation_label") or "")
    cause = str(row.get("error_cause") or "")

    safety = row.get("safety_compliance")
    try:
        safety_val = int(safety) if safety is not None else 0
    except (TypeError, ValueError):
        safety_val = 0
    version = row.get("version_currency")
    try:
        version_val = int(version) if version is not None else 0
    except (TypeError, ValueError):
        version_val = 0
    terminology = row.get("terminology_norm")
    try:
        terminology_val = int(terminology) if terminology is not None else 5
    except (TypeError, ValueError):
        terminology_val = 5

    safety_fail = 0 < safety_val <= SAFETY_REDLINE or cause == "安全警示缺失"
    hallucination = risk in ("中", "高") or cause == "幻觉编造" or label == "错误"
    if (fa is not None and fa <= 2) or hallucination or safety_fail:
        return "P0"
    if (
        (csi is not None and csi < 3.0)
        or (raw_us is not None and str(raw_us).strip().lower() == "dislike")
        or version_val == VERSION_OBSOLETE
        or cause == "版本失效"
    ):
        return "P1"
    if (kc is not None and kc <= 3) or gap or terminology_val <= 2 or cause in DOMAIN_BADCASE_CAUSES:
        return "P2"
    return None


def severity_reasons(row: Dict[str, Any]) -> List[str]:
    """给出触发该分级的具体原因，供人工复核。"""
    reasons: List[str] = []
    fa = row.get("factual_accuracy")
    kc = row.get("knowledge_completeness")
    csi = row.get("csi")
    risk = str(row.get("hallucination_risk") or "")
    if fa is not None and fa <= 2:
        reasons.append(f"事实准确性 FA={fa}（<=2，红线）")
    if risk in ("中", "高"):
        reasons.append(f"幻觉风险={risk}")
    if str(row.get("error_cause") or "") == "幻觉编造":
        reasons.append("归因为幻觉编造")
    if csi is not None and csi < 3.0:
        reasons.append(f"CSI={csi}（<3.0）")
    if row.get("user_score_raw") is not None and str(row["user_score_raw"]).strip().lower() == "dislike":
        reasons.append("用户点了 dislike")
    if row.get("knowledge_gap"):
        reasons.append("命中知识缺失")
    if kc is not None and kc <= 3:
        reasons.append(f"知识完备性 KC={kc}（<=3）")

    # 装备域专项维度
    safety = row.get("safety_compliance")
    try:
        safety_val = int(safety) if safety is not None else 0
    except (TypeError, ValueError):
        safety_val = 0
    if 0 < safety_val <= SAFETY_REDLINE:
        reasons.append(f"安全合规性 SC={safety_val}（<={SAFETY_REDLINE}，装备域安全红线）")
    if str(row.get("error_cause") or "") == "安全警示缺失":
        reasons.append("归因为安全警示缺失")
    version = row.get("version_currency")
    try:
        version_val = int(version) if version is not None else 0
    except (TypeError, ValueError):
        version_val = 0
    if version_val == VERSION_OBSOLETE:
        reasons.append("引用了已作废版本资料（VC=1）")
    terminology = row.get("terminology_norm")
    try:
        terminology_val = int(terminology) if terminology is not None else 5
    except (TypeError, ValueError):
        terminology_val = 5
    if terminology_val <= 2:
        reasons.append(f"术语规范性 TN={terminology_val}（<=2）")
    return reasons
