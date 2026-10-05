"""采集模块：从 Dify Service API 拉取会话 / 消息 / 反馈，解析用户评分并写入 SQLite。

user_score 归一化优先级（写入 user_score_raw，再由 metrics.normalize_user_score 转 1..5）：
    1) 消息反馈为 dislike          -> "dislike"（最强负向信号，优先保留）
    2) 对话内回执 [[EVAL_RECEIPT]]score=N,suggestion=...[[/EVAL_RECEIPT]] -> "N"
    3) 消息反馈为 like              -> "like"
    4) 应用反馈内容里的数字          -> "N"
    5) 都没有                       -> ""（缺失）
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from config import Config
from dify_client import DifyClient, DifyError
from store import Store, dumps, join_contexts, loads, now_ts
from metrics import normalize_user_score

#: 规范回执块（v2，三项主观指标）：
#:   [[EVAL_RECEIPT]]satisfaction=4,usefulness=5,experience=5,suggestion=...[[/EVAL_RECEIPT]]
#: 兼容 v1 单分回执：[[EVAL_RECEIPT]]score=4,suggestion=...[[/EVAL_RECEIPT]]
#: 采用键值对而非 JSON，是因为 Jinja2 模板无法对用户建议做 JSON 转义，
#: 建议里的引号/换行会把 JSON 打碎。改动 Dify 回执模板时必须同步本文件。
RECEIPT_BLOCK_RE = re.compile(r"\[\[EVAL_RECEIPT\]\](.*?)\[\[/EVAL_RECEIPT\]\]", re.S)
#: 历史 JSON 回执（无结束标记）：[[EVAL_RECEIPT]]{"score":5,"suggestion":"..."}
RECEIPT_JSON_RE = re.compile(r"\[\[EVAL_RECEIPT\]\]\s*(\{[^{}]*\})")
#: 人类可读回退（机器标记被误删时的兜底）：必须出现「满意度：N / 5」这类行才认定是回执，避免误判普通回答
RECEIPT_TEXT_SCORE_RE = re.compile(r"(?:满意度评分|主观指标1\s*满意度|满意度)\s*[:：]\s*(\d)\s*/\s*5")
RECEIPT_TEXT_USEFULNESS_RE = re.compile(r"(?:主观指标2\s*有用性|有用性)\s*[:：]\s*(\d)\s*/\s*5")
RECEIPT_TEXT_EXPERIENCE_RE = re.compile(r"(?:主观指标3\s*体验感|体验感)\s*[:：]\s*(\d)\s*/\s*5")
RECEIPT_TEXT_SUGGESTION_RE = re.compile(r"改进建议\s*[:：]\s*([^\n]*)")
#: 回执模板自身的行（用于「仅回执」识别）：三项主观指标 + 建议行
RECEIPT_TEMPLATE_LINE_RES = (
    RECEIPT_TEXT_SCORE_RE,
    RECEIPT_TEXT_USEFULNESS_RE,
    RECEIPT_TEXT_EXPERIENCE_RE,
    RECEIPT_TEXT_SUGGESTION_RE,
)
#: 兼容旧引用名
RECEIPT_RE = RECEIPT_BLOCK_RE
#: 键值对拆分（v1）：score=N,suggestion=<剩余全部内容，允许换行与逗号>
RECEIPT_KV_RE = re.compile(r"^\s*score\s*=\s*(-?\d+)\s*,\s*suggestion\s*=", re.S)
#: 键值对拆分（v2，三项主观指标）：satisfaction=N,usefulness=N,experience=N,suggestion=...
RECEIPT_KV3_RE = re.compile(
    r"^\s*satisfaction\s*=\s*(-?\d+)\s*,\s*usefulness\s*=\s*(-?\d+)\s*,"
    r"\s*experience\s*=\s*(-?\d+)\s*,\s*suggestion\s*=",
    re.S,
)
#: 回执正文特征词（用于识别「仅回执」消息）
#: 同时收录历史企业域话术与现行装备域话术，避免历史会话被漏采。
RECEIPT_BODY_MARKERS = (
    "已记录你的评价",
    "我们会据此补充知识库内容",
    "我们会据此补充装备技术资料",
    "校正术语与版本引用",
)
#: 机器标记常量（strip 时用于兜底清理残缺标记）
RECEIPT_START_MARK = "[[EVAL_RECEIPT]]"
RECEIPT_END_MARK = "[[/EVAL_RECEIPT]]"

#: 从反馈文本里提取数字评分
NUMERIC_RE = re.compile(r"(?:^|\D)([1-5])(?:\s*分|$|\D)")


@dataclass
class CollectStats:
    """一次采集的统计结果。"""

    conversations: int = 0
    messages: int = 0
    feedbacks: int = 0
    with_feedback: int = 0
    with_receipt: int = 0
    with_retrieval: int = 0
    user_score_coverage: float = 0.0
    skipped: int = 0
    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        """转为可 JSON 序列化的字典。"""
        return {
            "conversations": self.conversations,
            "messages": self.messages,
            "feedbacks": self.feedbacks,
            "with_feedback": self.with_feedback,
            "with_receipt": self.with_receipt,
            "with_retrieval": self.with_retrieval,
            "user_score_coverage": round(self.user_score_coverage, 4),
            "skipped": self.skipped,
            "errors": self.errors[:20],
        }


def _valid_score(value: Any) -> Optional[int]:
    """把任意输入收敛成 1-5 的整数，非法返回 None。"""
    try:
        score_val = int(value)
    except (TypeError, ValueError):
        return None
    return score_val if 1 <= score_val <= 5 else None


def _parse_json_payload(body: str) -> Tuple[Optional[int], str]:
    """解析历史 JSON 形式的回执载荷。"""
    try:
        payload = json.loads(body)
    except ValueError:
        return None, ""
    if not isinstance(payload, dict):
        return None, ""
    return _valid_score(payload.get("score")), str(payload.get("suggestion") or "")


def _parse_kv_form(text: str) -> Optional[Dict[str, Any]]:
    """(a) 解析规范键值对回执，返回带 form 的字典；不是该形式时返回 None。

    解析规则（与 Dify 回执模板强绑定，必须严格照此实现）：
        score      = "score=" 之后、第一个 ",suggestion=" 之前的整数
        suggestion = ",suggestion=" 之后、直到 "[[/EVAL_RECEIPT]]" 之前的全部内容（允许换行），strip 后返回

    因为 suggestion 是「取到块尾」，所以建议原文里的逗号、换行、引号、冒号，
    甚至再次出现 "score=9" 这样的干扰字样，都能被原样保留。
    """
    block = RECEIPT_BLOCK_RE.search(text)
    if not block:
        return None
    body = block.group(1)
    head, sep, tail = body.partition(",suggestion=")
    matched3 = RECEIPT_KV3_RE.match(body)
    if matched3:
        # 规范写法（v2，三项主观指标）：satisfaction=N,usefulness=N,experience=N,suggestion=...
        return {
            "score": _valid_score(matched3.group(1)),
            "satisfaction": _valid_score(matched3.group(1)),
            "usefulness": _valid_score(matched3.group(2)),
            "experience": _valid_score(matched3.group(3)),
            "suggestion": body[matched3.end():].strip(),
            "form": "kv3",
        }
    matched = RECEIPT_KV_RE.match(body)
    if matched:
        # 规范写法（v1）：score=N,suggestion=...（允许两侧空白）
        return {
            "score": _valid_score(matched.group(1)),
            "satisfaction": _valid_score(matched.group(1)),
            "usefulness": None,
            "experience": None,
            "suggestion": body[matched.end():].strip(),
            "form": "kv",
        }
    # 容错：三项键齐全但 ",suggestion=" 缺失/带空白
    loose3 = re.search(r"satisfaction\s*=\s*(-?\d+)\s*,\s*usefulness\s*=\s*(-?\d+)\s*,"
                       r"\s*experience\s*=\s*(-?\d+)", head)
    if loose3:
        return {
            "score": _valid_score(loose3.group(1)),
            "satisfaction": _valid_score(loose3.group(1)),
            "usefulness": _valid_score(loose3.group(2)),
            "experience": _valid_score(loose3.group(3)),
            "suggestion": (tail if sep else "").strip(),
            "form": "kv3",
        }
    score_match = re.search(r"(?:score|satisfaction)\s*=\s*(-?\d+)", head)
    if score_match:
        # 容错写法（v1）：缺少 ",suggestion=" 或分隔符两侧有空白
        return {
            "score": _valid_score(score_match.group(1)),
            "satisfaction": _valid_score(score_match.group(1)),
            "usefulness": None,
            "experience": None,
            "suggestion": (tail if sep else "").strip(),
            "form": "kv",
        }
    return None


def _parse_json_form(text: str) -> Optional[Dict[str, Any]]:
    """(b) 兼容历史 JSON 回执 [[EVAL_RECEIPT]]{"score":4,"suggestion":"..."}。"""
    block = RECEIPT_BLOCK_RE.search(text)
    if block and block.group(1).strip().startswith("{"):
        score_val, suggestion = _parse_json_payload(block.group(1).strip())
        if score_val is not None or suggestion:
            return {"score": score_val, "suggestion": suggestion.strip(), "form": "json"}
    legacy = RECEIPT_JSON_RE.search(text)
    if legacy:
        score_val, suggestion = _parse_json_payload(legacy.group(1))
        if score_val is not None or suggestion:
            return {"score": score_val, "suggestion": suggestion.strip(), "form": "json"}
    return None


def _parse_text_form(text: str) -> Optional[Dict[str, Any]]:
    """(c) 人类可读回退：机器标记被误删时的兜底。

    只有正文确实出现「满意度评分：N / 5」才认定是回执（避免把普通回答误判成评价）；
    「改进建议：xxx」取到行尾，该行为空时 suggestion 为空字符串。
    """
    score_match = RECEIPT_TEXT_SCORE_RE.search(text)
    if not score_match:
        return None
    suggestion_match = RECEIPT_TEXT_SUGGESTION_RE.search(text)
    usefulness_match = RECEIPT_TEXT_USEFULNESS_RE.search(text)
    experience_match = RECEIPT_TEXT_EXPERIENCE_RE.search(text)
    return {
        "score": _valid_score(score_match.group(1)),
        "satisfaction": _valid_score(score_match.group(1)),
        "usefulness": _valid_score(usefulness_match.group(1)) if usefulness_match else None,
        "experience": _valid_score(experience_match.group(1)) if experience_match else None,
        "suggestion": suggestion_match.group(1).strip() if suggestion_match else "",
        "form": "text",
    }


#: 回执统一结构的字段（三项主观指标 + 建议）
RECEIPT_KEYS = ("score", "satisfaction", "usefulness", "experience")


def _normalize_receipt(result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """把各种回执形式补齐成统一结构（缺项为 None，suggestion 为空串）。"""
    out: Dict[str, Any] = {key: None for key in RECEIPT_KEYS}
    out["suggestion"] = ""
    out["form"] = "none"
    if not result:
        return out
    for key in RECEIPT_KEYS:
        out[key] = result.get(key)
    # v1 回执只有 score，语义等同「满意度」
    if out["satisfaction"] is None and out["score"] is not None:
        out["satisfaction"] = out["score"]
    out["suggestion"] = str(result.get("suggestion") or "")
    out["form"] = str(result.get("form") or "none")
    return out


def parse_eval_receipt(text: str) -> Dict[str, Any]:
    """按优先级解析评价回执，返回统一结构（评测服务的唯一入口）。

    依次尝试三种形式，任一成功即返回：
        (a) 键值对：[[EVAL_RECEIPT]]score=N,suggestion=...[[/EVAL_RECEIPT]]   ← 规范格式
        (b) JSON：[[EVAL_RECEIPT]]{"score":N,"suggestion":"..."}             ← 兼容历史数据
        (c) 人类可读回退：「满意度评分：N / 5」+「改进建议：...」

    :param text: 助手消息全文（可能是回执，也可能是普通回答）
    :return: {"score": int|None, "satisfaction": int|None, "usefulness": int|None,
              "experience": int|None, "suggestion": str, "form": "kv3"|"kv"|"json"|"text"|"none"}
    """
    if not text:
        return _normalize_receipt(None)
    source = str(text)
    for parser in (_parse_kv_form, _parse_json_form, _parse_text_form):
        try:
            result = parser(source)
        except Exception:  # noqa: BLE001 - 回执解析异常不得影响采集主流程
            result = None
        if result is not None:
            return _normalize_receipt(result)
    return _normalize_receipt(None)


def parse_receipt(answer: str) -> Tuple[Optional[int], str]:
    """兼容旧接口：返回 (score, suggestion)。

    :param answer: 助手回答全文
    :return: (score, suggestion)；未命中时返回 (None, "")
    """
    result = parse_eval_receipt(answer)
    return result["score"], result["suggestion"]


def strip_receipt(answer: str) -> str:
    """去掉回答中的回执标记（键值对形式与历史 JSON 形式），避免污染评分输入。"""
    if not answer:
        return ""
    text = RECEIPT_BLOCK_RE.sub("", str(answer))
    text = RECEIPT_JSON_RE.sub("", text)
    text = text.replace(RECEIPT_START_MARK, "").replace(RECEIPT_END_MARK, "")
    return text.strip()


def is_receipt_only(answer: str) -> bool:
    """判断该消息是否只是「评价回执」而不是一次真正的问答。

    判定方式：先确认消息里出现回执特征（机器标记或回执正文特征词），
    再逐行剔除回执模板自身的行，若剩余实质内容不超过 20 个字符，即认为是「仅回执」。
    这样「正常回答 + 追加回执」的消息仍会被正常评分。

    回执是对上一条回答的评价结果，不应作为问答质量样本参与评分；
    judge 默认跳过这类消息（可用 --include-receipt-only 强制纳入）。
    """
    if not answer:
        return False
    source = str(answer)
    if not any(m in source for m in RECEIPT_BODY_MARKERS) and not RECEIPT_BLOCK_RE.search(source):
        return False
    body = strip_receipt(source)
    if not body:
        return True
    substantive: List[str] = []
    for line in body.splitlines():
        text_line = line.strip().lstrip("-•* ").strip()
        if not text_line:
            continue
        if any(m in text_line for m in RECEIPT_BODY_MARKERS):
            continue
        if any(rx.search(text_line) for rx in RECEIPT_TEMPLATE_LINE_RES):
            continue
        substantive.append(text_line)
    return len("".join(substantive)) <= 20


def resolve_user_score(
    feedback_rating: Optional[str],
    feedback_content: Optional[str],
    receipt_score: Optional[int],
) -> str:
    """按优先级合并出用户评分的原始值。

    :return: "like" / "dislike" / "1".."5" / ""
    """
    rating = (feedback_rating or "").strip().lower()
    if rating == "dislike":
        return "dislike"
    if receipt_score is not None:
        return str(int(receipt_score))
    if rating == "like":
        return "like"
    if feedback_content:
        match = NUMERIC_RE.search(feedback_content)
        if match:
            return match.group(1)
    return ""


class Collector:
    """会话/消息/反馈采集器。"""

    def __init__(self, client: DifyClient, store: Store, cfg: Optional[Config] = None, verbose: bool = True):
        """
        :param client: 已构造的 DifyClient（问答应用 Key）
        :param store: 已初始化的 Store
        :param cfg: 配置对象（可选，用于读取 user 标识等）
        :param verbose: 是否打印进度
        """
        self.client = client
        self.store = store
        self.cfg = cfg
        self.verbose = verbose

    def _log(self, message: str) -> None:
        """打印采集进度。"""
        if self.verbose:
            print(f"[collect] {message}")

    def collect(
        self,
        limit: Optional[int] = None,
        since_ts: Optional[int] = None,
        fetch_feedbacks: bool = True,
        user: Optional[str] = None,
    ) -> CollectStats:
        """执行一次完整采集。

        :param limit: 最多采集多少条消息
        :param since_ts: 只采集该时间戳（Unix 秒）之后的消息与会话
        :param fetch_feedbacks: 是否同时拉取 /v1/app/feedbacks
        :param user: 会话归属的 user 标识
        :return: CollectStats
        """
        stats = CollectStats()
        run_id = self.store.start_run(
            "collect",
            {"limit": limit, "since_ts": since_ts, "mock": bool(self.client.is_mock)},
        )
        try:
            conversations = self.client.iter_conversations(user=user)
            if since_ts:
                conversations = [
                    c for c in conversations
                    if int(c.get("updated_at") or c.get("created_at") or 0) >= since_ts
                ]
            self.store.upsert_conversations(conversations)
            stats.conversations = len(conversations)
            self._log(f"会话 {stats.conversations} 个")

            all_messages: List[Dict[str, Any]] = []
            for conv in conversations:
                cid = conv.get("id")
                if not cid:
                    continue
                try:
                    items = self.client.iter_messages(cid, user=user)
                except DifyError as exc:
                    stats.errors.append(f"messages({cid}) 失败：{exc}")
                    self._log(f"拉取会话 {cid} 的消息失败：{exc}")
                    continue
                all_messages.extend(items)

            if since_ts:
                all_messages = [m for m in all_messages if int(m.get("created_at") or 0) >= since_ts]
            all_messages.sort(key=lambda m: m.get("created_at") or 0)
            if limit:
                all_messages = all_messages[-int(limit):]

            normalized: List[Dict[str, Any]] = []
            for item in all_messages:
                row, has_fb, has_receipt, has_retrieval = self.normalize_message(item)
                normalized.append(row)
                stats.with_feedback += int(has_fb)
                stats.with_receipt += int(has_receipt)
                stats.with_retrieval += int(has_retrieval)
            self.store.upsert_messages(normalized)
            stats.messages = len(normalized)
            self._log(f"消息 {stats.messages} 条（含反馈 {stats.with_feedback}，含回执 {stats.with_receipt}，含检索 {stats.with_retrieval}）")

            if fetch_feedbacks:
                try:
                    feedbacks = self.client.iter_app_feedbacks()
                    self.store.upsert_feedbacks(feedbacks)
                    stats.feedbacks = len(feedbacks)
                    self._merge_feedbacks_into_messages(feedbacks)
                    self._log(f"应用反馈 {stats.feedbacks} 条")
                except DifyError as exc:
                    stats.errors.append(f"app/feedbacks 失败：{exc}")
                    self._log(f"拉取应用反馈失败：{exc}")

            scored = sum(1 for m in normalized if m.get("user_score") is not None)
            stats.user_score_coverage = (scored / len(normalized)) if normalized else 0.0
            self.store.finish_run(run_id, "success", stats.as_dict())
            return stats
        except Exception as exc:  # noqa: BLE001 - 需要把失败登记进 runs 表
            self.store.finish_run(run_id, "failed", stats.as_dict(), notes=str(exc))
            raise

    def normalize_message(self, item: Dict[str, Any]) -> Tuple[Dict[str, Any], bool, bool, bool]:
        """把 Dify 原始消息对象规范化成 store 行。

        :return: (row, 是否有反馈, 是否有回执, 是否有检索片段)
        """
        answer_raw = item.get("answer") or ""
        receipt = parse_eval_receipt(answer_raw)
        receipt_score = receipt["score"]
        receipt_suggestion = receipt["suggestion"]
        feedback = item.get("feedback") or {}
        rating = feedback.get("rating") if isinstance(feedback, dict) else None
        content = feedback.get("content") if isinstance(feedback, dict) else None
        raw_us = resolve_user_score(rating, content, receipt_score)

        row = dict(item)
        row["answer"] = strip_receipt(answer_raw)
        row["feedback_rating"] = rating
        row["feedback_content"] = content
        row["receipt_score"] = receipt_score
        row["receipt_suggestion"] = receipt_suggestion
        # 主观指标 2/3（有用性 / 体验感）：v2 回执才有，未采到为 None
        row["receipt_usefulness"] = receipt["usefulness"]
        row["receipt_experience"] = receipt["experience"]
        row["user_score_raw"] = raw_us
        row["user_score"] = normalize_user_score(raw_us)
        resources = item.get("retriever_resources") or []
        return row, bool(rating), receipt_score is not None, bool(resources)

    def _merge_feedbacks_into_messages(self, feedbacks: List[Dict[str, Any]]) -> None:
        """把应用反馈里的评分补写到消息上（消息自身没有反馈时）。"""
        for fb in feedbacks:
            mid = fb.get("message_id")
            if not mid:
                continue
            cur = self.store.conn.execute(
                "SELECT feedback_rating, receipt_score, user_score_raw FROM messages WHERE id=?", (mid,)
            ).fetchone()
            if cur is None:
                continue
            if cur["feedback_rating"]:
                continue  # 消息自带的反馈优先级更高
            rating = fb.get("rating")
            content = fb.get("content") or ""
            raw_us = resolve_user_score(rating, content, cur["receipt_score"])
            self.store.update_message_feedback(
                mid, rating, content, normalize_user_score(raw_us), raw_us
            )

    def set_feedback(self, message_id: str, rating: Optional[str], content: str = "") -> Dict[str, Any]:
        """回写反馈到 Dify（POST /v1/messages/{id}/feedbacks）。"""
        return self.client.set_message_feedback(message_id, rating, content)


def collect_once(cfg: Config, store: Store, limit: Optional[int] = None, since_ts: Optional[int] = None) -> CollectStats:
    """便捷入口：按配置构造客户端并采集一次。"""
    client = DifyClient.from_config(cfg, "app")
    return Collector(client, store, cfg).collect(limit=limit, since_ts=since_ts)
