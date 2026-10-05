# -*- coding: utf-8 -*-
"""生成后确定性校验（grounding）：答案里的数值 / 编号 / 版本 / 条款是否真的出现在被引片段里。

为什么需要（docs/16 §1 目标判据、docs/10 归因前置）：
    提示词只能"要求"模型不编造；本模块用**确定性比对**回答"到底有没有编"。
    未支撑的数值与编号是最常见、也最可判定的跑偏形态，可作为：
      * 用户侧「当前引用资料是否健全/合规」反馈的自动证据；
      * badcase 与缺陷整改单的证据字段；
      * 质量报表的「事实可核验率」。

口径纪律（重要）：
    v1 **只度量、只留证，不参与 verdict / badcase 判定**。
    要把它变成红线（例如"存在未支撑数值 → 直接判不合格"）必须先走 docs/16 §4 R3 流程：
    改 docs/01 + 登记决策，再改代码并重算历史。

规则（全部确定性，不调用 LLM）：
    R-NUM     答案中的数值未被任何命中片段支撑 → 未支撑数值
    R-DOC     资料编号（HYS-XX-000）未被片段支撑 → 未支撑编号
    R-VER     版本号（Vx.y）未被片段支撑 → 未支撑版本
    R-CLAUSE  条款号（§x.y / 第 x.y 条）未被片段支撑 → 未支撑条款

判定：
    n/a    没有命中片段（库外兜底等），无法校验，不算跑偏
    ok     未支撑项为 0
    warn   1~2 项，且不含编号/版本/条款类
    fail   出现未支撑的编号/版本/条款，或未支撑项 ≥ 3
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

#: 带单位的数值（单位只用于"这是个数"的判断，不参与比对）
UNIT_RE = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)\s*"
    r"(MPa|kPa|℃|°C|mm|cm|m/s|N·m|N\.m|kg|t|L|mL|小时|分钟|个月|年|天|次|%|V|A|kW|r/min|滴/分钟|工作小时)"
)
#: 纯数字（≥2 位，用于抓 500 / 3000 / 17.5 这类；过滤 1. 2. 这类序号）
NUMBER_RE = re.compile(r"(?<![\d.])(\d{2,}(?:\.\d+)?)(?![\d])")
#: 资料编号：HYS-XX-000
DOC_NO_RE = re.compile(r"\b(HYS-[A-Z]{2}-\d{3})\b")
#: 版本号：V2.0 / V3
VERSION_RE = re.compile(r"\b(V\d+(?:\.\d+)?)\b")
#: 条款号：§2.5.2 或 第 2.5.2 条
CLAUSE_RE = re.compile(r"(?:§\s*(\d+(?:\.\d+){1,3}))|(?:第\s*(\d+(?:\.\d+){1,3})\s*[条款])")

#: 严重类（一旦未支撑即判 fail）
SERIOUS_KINDS = ("doc_no", "version", "clause")


def _normalize(text: str) -> str:
    """归一化：去空白、统一温度符号与大小写，便于"是否出现"的比对。"""
    out = str(text or "")
    out = out.replace("°C", "℃").replace("°c", "℃")
    out = re.sub(r"\s+", "", out)
    return out


def _extract(answer: str) -> List[Tuple[str, str]]:
    """从答案里抽取需要校验的事实性 token：[(kind, value)]。"""
    tokens: List[Tuple[str, str]] = []
    seen = set()

    def add(kind: str, value: str) -> None:
        key = (kind, value)
        if value and key not in seen:
            seen.add(key)
            tokens.append(key)

    for match in UNIT_RE.finditer(answer):
        add("number", match.group(1))
    for match in NUMBER_RE.finditer(answer):
        add("number", match.group(1))
    for match in DOC_NO_RE.finditer(answer):
        add("doc_no", match.group(1))
    for match in VERSION_RE.finditer(answer):
        add("version", match.group(1))
    for match in CLAUSE_RE.finditer(answer):
        add("clause", match.group(1) or match.group(2))
    return tokens


def check_grounding(answer: str, contexts: str) -> Dict[str, Any]:
    """校验答案中的事实性 token 是否被命中片段支撑。

    :param answer: 系统回答全文（建议已剥离评价回执）
    :param contexts: 命中片段的拼接文本（store.join_contexts 的输出）
    :return: {"checked": int, "ungrounded": [...], "grounded": int, "rate": float, "level": str, "note": str}
    """
    corpus = _normalize(contexts)
    tokens = _extract(answer or "")
    if not corpus or not tokens:
        return {
            "checked": 0, "grounded": 0, "ungrounded": [], "rate": 1.0, "level": "n/a",
            "note": "无命中片段或答案中无事实性 token，未做校验",
        }

    ungrounded: List[Dict[str, str]] = []
    for kind, value in tokens:
        if _normalize(value) in corpus:
            continue
        ungrounded.append({
            "kind": kind,
            "value": value,
            "reason": {
                "number": "该数值未出现在任何命中片段中（疑似编造或来自片段之外）",
                "doc_no": "该资料编号未被命中片段支撑（引用来源可疑）",
                "version": "该版本号未被命中片段支撑（疑似版本错引）",
                "clause": "该条款号未被命中片段支撑（溯源不可靠）",
            }.get(kind, "未被命中片段支撑"),
        })

    checked = len(tokens)
    grounded = checked - len(ungrounded)
    rate = round(grounded / float(checked), 4) if checked else 1.0
    serious = [u for u in ungrounded if u["kind"] in SERIOUS_KINDS]
    if not ungrounded:
        level = "ok"
    elif serious or len(ungrounded) >= 3:
        level = "fail"
    else:
        level = "warn"
    return {
        "checked": checked,
        "grounded": grounded,
        "ungrounded": ungrounded,
        "rate": rate,
        "level": level,
        "note": "",
    }


def summarize(level: str) -> str:
    """给报表用的中文说明。"""
    return {
        "ok": "全部事实性内容可在命中片段中找到依据",
        "warn": "存在少量未支撑数值，建议人工复核",
        "fail": "存在未支撑的编号/版本/条款或多项未支撑数值，判定为跑偏风险高",
        "n/a": "无命中片段，未做可核验性校验",
    }.get(level, level)
