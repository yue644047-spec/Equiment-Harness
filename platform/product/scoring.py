"""三主观 + 三客观的评价落点（ADR-D25）。

主观三项（用户在门户打分）：满意度 / 有用性 / 体验感（docs/01 回执三项）。
客观三项（系统自动算，人工可复核覆盖）：FA 事实准确性 / KC 知识完备性 / CL 表达清晰度。

核心一致性要求（甲方 2026-09-27 明确）：
  **检索不到就是检索不到**——主观侧要能反映"未检索"，客观侧同样判为未命中，
  并且必须告诉用户"这份资料缺了什么"，而不是给一个假分数糊过去。

因此：
  * 命中为空 → 客观三项返回 no_hit=True 且三项为 None（不参与均值，不补 0）；
  * 命中有但要点缺失 → 客观分照打，并把缺口写入覆盖缺口表（维度 D1/D7）。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from .embedder import ModelError, ModelGateway
from .store import Store
from .textutil import normalize_for_prompt

FENCE = chr(96) * 3

STOPWORDS = {
    "如何", "怎么", "怎样", "什么", "哪些", "哪个", "是否", "能否", "可以", "为什么", "多少", "多久",
    "请", "说明", "介绍", "告诉我", "一下", "以及", "还有", "需要", "应该", "必须", "这个", "那个",
    "我们", "你们", "他们", "的", "了", "吗", "呢", "和", "与", "或", "在", "对", "把", "被", "是",
    "有", "无", "中", "上", "下", "里", "时", "后", "前", "等", "根据", "按照", "关于",
}

JUDGE_PROMPT = (
    "你是装备技术资料问答的严格判官。依据【检索片段】给【回答】打三项客观分，1–5 整数：\n"
    "FA 事实准确性：回答里的事实是否都能在片段中找到依据；出现片段中没有的数值/编号/结论记 1。\n"
    "KC 知识完备性：**先看片段里有没有问题所问的信息**——片段里根本没有所问内容（无法据资料作答）时 KC 记 1–2；"
    "片段有该信息但回答漏了要点，按缺口大小扣分。\n"
    "CL 表达清晰度：结构是否清楚、术语是否规范、是否便于按步骤执行。\n"
    "只输出 JSON，不要任何解释文字："
    '{"fa": 1, "kc": 1, "cl": 1, "reason": "一句话判据"}'
)

#: 判分输出本身很短（一个 JSON），但推理类模型（如 deepseek-flash）会先花预算输出思维链，
#: 预算过小会把 content 挤空（finish_reason=length → content=""，判分解析失败、三项变 null）。
#: 因此给足预算，并在 content 仍为空时放大预算再判一次。
JUDGE_MAX_TOKENS = 1024
JUDGE_RETRY_TOKENS = 4096


def question_terms(question: str, limit: int = 6) -> List[str]:
    """从问题里抽取要素（去疑问词与停用词，保留 2–12 字的中文词块与英文代号）。"""
    text = question or ""
    text = re.sub(r"[？?！!。，,；;：:、（）()\[\]【】]", " ", text)
    text = text.replace(chr(34), " ").replace(chr(39), " ")
    terms: List[str] = []
    for piece in text.split():
        if not piece:
            continue
        if re.fullmatch(r"[0-9A-Za-z_.\-]{2,}", piece):
            terms.append(piece)
            continue
        chunk = piece
        for stop in sorted(STOPWORDS, key=len, reverse=True):
            chunk = chunk.replace(stop, " ")
        for word in chunk.split():
            if 2 <= len(word) <= 12:
                terms.append(word)
    seen: List[str] = []
    for term in terms:
        if term not in seen:
            seen.append(term)
    return seen[:limit]


def detect_no_hit(hits: List[Dict[str, Any]]) -> bool:
    return not hits


def coverage_miss(question: str, hits: List[Dict[str, Any]], min_rate: float = 0.34) -> Dict[str, Any]:
    """命中片段是否真的回答了问题：用「问题要素在片段里的出现比例」判定。

    检索器总会返回 top_k 条（哪怕都不相关），所以「检索不到」不能只看 hits 是否为空。
    覆盖率低于阈值时判为**未命中**：客观三项不适用、给出缺口清单，
    与主观侧的「未检索」保持一致（ADR-D25）。
    """
    if not hits:
        return {"miss": True, "rate": 0.0, "reason": "检索命中为空"}
    terms = question_terms(question)
    if not terms:
        return {"miss": False, "rate": 0.0, "reason": ""}
    best = 0.0
    for hit in hits[:6]:
        text = normalize_for_prompt(str(hit.get("content") or ""), limit=2000)
        hit_rate = sum(1 for term in terms if term in text) / len(terms)
        best = max(best, hit_rate)
    return {"miss": best < min_rate, "rate": round(best, 3),
            "reason": "命中片段的要素覆盖率仅 " + str(round(best * 100)) + "%（阈值 " + str(int(min_rate * 100)) + "%）"}


def build_gaps(store: Store, dataset_ids: List[str], question: str,
               hits: List[Dict[str, Any]], reason: str = "") -> List[Dict[str, Any]]:
    """把「差什么」写成可整改的缺口条目（含已入库资料清单，便于判断该补哪一份）。"""
    terms = question_terms(question)
    datasets = [item for item in store.list_datasets() if item["id"] in dataset_ids] or store.list_datasets()
    inventory: List[str] = []
    for dataset in datasets:
        for document in store.list_documents(dataset["id"])[:12]:
            inventory.append(dataset["name"] + " / " + document["name"])
    gaps: List[Dict[str, Any]] = []
    for term in terms:
        gaps.append({
            "term": term,
            "missing": "已入库资料中未检索到与「" + term + "」相关的条款（" + (reason or "检索命中为空") + "）",
            "suggestion": "补充「" + term + "」相关条款并确认标题层级与编号可解析（维度 D1 覆盖完备性 / D7 结构可检索性）",
            "dimension": "D1",
            "inventory": inventory[:8],
        })
    if not gaps:
        gaps.append({
            "term": question[:20],
            "missing": "已入库资料中未检索到与该问题相关的条款",
            "suggestion": "补充对应条款或新增资料后重新入库（维度 D1 覆盖完备性）",
            "dimension": "D1",
            "inventory": inventory[:8],
        })
    return gaps


def judge_objective(gateway: ModelGateway, question: str, answer: str,
                    hits: List[Dict[str, Any]]) -> Dict[str, Any]:
    """自动客观三项。未命中直接返回 no_hit，不调用模型、不给假分。"""
    if not hits:
        return {"no_hit": True, "fa": None, "kc": None, "cl": None,
                "reason": "本次未检索到任何资料片段，客观三项不适用（不参与均值）", "source": "auto"}

    blocks = []
    for index, hit in enumerate(hits[:8], 1):
        blocks.append("[" + str(index) + "] " + normalize_for_prompt(str(hit.get("content") or ""), limit=800))
    user = ("【检索片段】\n" + "\n\n".join(blocks)
            + "\n\n【用户问题】\n" + question
            + "\n\n【回答】\n" + (answer or "")[:2000])
    messages = [{"role": "system", "content": JUDGE_PROMPT}, {"role": "user", "content": user}]
    try:
        raw = gateway.chat(messages, temperature=0.0, max_tokens=JUDGE_MAX_TOKENS)
        if not (raw or "").strip():
            # 推理类模型的思维链把预算吃满、content 被挤空：放大预算再判一次
            raw = gateway.chat(messages, temperature=0.0, max_tokens=JUDGE_RETRY_TOKENS)
    except ModelError as exc:
        return {"no_hit": False, "fa": None, "kc": None, "cl": None,
                "reason": "自动判分未完成：" + str(exc)[:200], "source": "auto"}
    parsed = _parse_json(raw)
    if not parsed:
        return {"no_hit": False, "fa": None, "kc": None, "cl": None,
                "reason": "自动判分返回无法解析：" + (raw or "")[:150], "source": "auto"}
    return {
        "no_hit": False,
        "fa": _clip(parsed.get("fa")),
        "kc": _clip(parsed.get("kc")),
        "cl": _clip(parsed.get("cl")),
        "reason": str(parsed.get("reason") or "")[:300],
        "source": "auto",
    }


def _clip(value: Any) -> Optional[int]:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return max(1, min(5, number))


def _parse_json(raw: str) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    text = raw.strip()
    if FENCE in text:
        text = text.split(FENCE, 1)[1]
        text = text.split(FENCE, 1)[0]
        text = text.replace("json", "", 1) if text.strip().lower().startswith("json") else text
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        return None


def miss_answer(question: str, gaps: List[Dict[str, Any]], datasets: List[str]) -> str:
    """未命中时的回答：明确说"没检索到"，并列出缺什么、建议补什么（不编造）。"""
    terms = "、".join(item["term"] for item in gaps[:6])
    lines = [
        "**资料中未收录**：本次在已入库资料（" + "、".join(datasets[:5]) + "）中未检索到与「" + terms
        + "」相关的内容，因此无法依据资料作答。",
        "",
        "**缺口要素**",
    ]
    for item in gaps[:6]:
        lines.append("- " + item["term"] + "：" + item["missing"])
    lines.append("")
    lines.append("**建议补充**")
    for item in gaps[:4]:
        lines.append("- " + item["suggestion"])
    lines.append("")
    lines.append("> 本条已记入「覆盖缺口」，可在数据治理台或缺陷整改单中指派补充资料后重新入库。")
    return "\n".join(lines)
