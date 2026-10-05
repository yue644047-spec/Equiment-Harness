"""数据库交互查询服务：把自然语言问题映射为**白名单查询模板**并执行。

对应设计报告（docs/11）3.7 数据库交互查询模块：
1. 业务库（装备台账、履历、检修记录）由甲方提供，系统使用**只读账号**；
2. 查询逻辑为确定性模板 + 参数绑定，不拼接 SQL，不接受任意 SQL；
3. 结果同时返回命中的模板、参数与解释，便于人工核对与溯源；
4. 与知识库问答并列，构成「资料问答」与「台账查询」两条链路。

离线演示：
    python -c "import sys; sys.path.insert(0,'eval_service'); import db_query as d; \
               r=d.query('EQ-1002 的履历', 'demo_data/business.db'); print(r.intent, r.rows)"
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

#: 装备编号形态：EQ-1001 / ZB-2001
EQUIPMENT_RE = re.compile(r"\b([A-Z]{2}-\d{4})\b")
#: 型号：HYS-1 / HYS-2
MODEL_RE = re.compile(r"\b(HYS-\d)\b")

MAX_ROWS = 200


@dataclass
class QueryResult:
    """一次查询的结果（含模板、参数与解释，便于核对）。"""

    intent: str
    label: str
    explain: str
    sql: str = ""
    params: Tuple[Any, ...] = ()
    columns: List[str] = field(default_factory=list)
    rows: List[Dict[str, Any]] = field(default_factory=list)
    message: str = ""
    refused: bool = False

    @property
    def count(self) -> int:
        """结果行数。"""
        return len(self.rows)

    def to_dict(self) -> Dict[str, Any]:
        """转为字典。"""
        return {
            "intent": self.intent,
            "label": self.label,
            "explain": self.explain,
            "sql": self.sql,
            "params": list(self.params),
            "columns": self.columns,
            "rows": self.rows,
            "count": self.count,
            "message": self.message,
            "refused": self.refused,
        }


# ---------------------------------------------------------------- 参数抽取


#: 中文数字（用于「近三个月」这类表述）
_CN_DIGITS = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _normalize_cn_number(text: str) -> str:
    """把「近三个月」里的中文数字归一化为阿拉伯数字，便于统一解析。"""
    def repl(match: "re.Match[str]") -> str:
        word = match.group(1)
        if word in _CN_DIGITS:
            return f"近{_CN_DIGITS[word]}"
        if word.startswith("十") and len(word) == 2 and word[1] in _CN_DIGITS:
            return f"近{10 + _CN_DIGITS[word[1]]}"
        if len(word) == 2 and word[0] in _CN_DIGITS and word[1] == "十":
            return f"近{_CN_DIGITS[word[0]] * 10}"
        return match.group(0)

    return re.sub(r"近([一两二三四五六七八九十]+)", repl, text)


def parse_period(question: str, today: Optional[date] = None) -> Optional[Tuple[str, str]]:
    """从问题中抽取时间范围，返回 (起始日, 结束日) 的 ISO 字符串。

    支持：近 N 个月 / 近 N 天 / 今年 / 去年 / 2025 年 / 2025-06 / 本月。
    """
    today = today or date.today()
    question = _normalize_cn_number(question)
    m = re.search(r"近\s*(\d+)\s*个?月", question)
    if m:
        months = int(m.group(1))
        start = today - timedelta(days=30 * months)
        return start.isoformat(), today.isoformat()
    m = re.search(r"近\s*(\d+)\s*(天|日)", question)
    if m:
        days = int(m.group(1))
        return (today - timedelta(days=days)).isoformat(), today.isoformat()
    if "本月" in question:
        return today.replace(day=1).isoformat(), today.isoformat()
    if "今年" in question:
        return date(today.year, 1, 1).isoformat(), today.isoformat()
    if "去年" in question:
        return date(today.year - 1, 1, 1).isoformat(), date(today.year - 1, 12, 31).isoformat()
    m = re.search(r"(20\d{2})\s*年", question)
    if m:
        year = int(m.group(1))
        return date(year, 1, 1).isoformat(), date(year, 12, 31).isoformat()
    m = re.search(r"(20\d{2})-(\d{2})", question)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        last = (date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)).isoformat()
        return date(year, month, 1).isoformat(), last
    return None


def detect(question: str, today: Optional[date] = None) -> Dict[str, Any]:
    """抽取问题中的结构化线索（装备编号、型号、时间范围）。"""
    eq = EQUIPMENT_RE.search(question or "")
    model = MODEL_RE.search(question or "")
    return {
        "equipment_no": eq.group(1) if eq else "",
        "model": model.group(1) if model else "",
        "period": parse_period(question or "", today),
    }


# ---------------------------------------------------------------- 查询模板


def _t_equipment_by_model(ctx: Dict[str, Any]) -> QueryResult:
    """按型号查装备台账。"""
    model = ctx["model"] or "HYS-1"
    sql = ("SELECT equipment_no AS 装备编号, model AS 型号, name AS 名称, unit AS 所属单位,"
           " commission_date AS 列装日期, status AS 状态, runtime_hours AS 累计工作小时"
           " FROM equipment WHERE model = ? ORDER BY equipment_no")
    return QueryResult(
        intent="equipment_by_model",
        label="按型号查装备台账",
        explain=f"查询型号 {model} 的全部装备及其状态与累计工作小时。",
        sql=sql,
        params=(model,),
    )


def _t_equipment_history(ctx: Dict[str, Any]) -> QueryResult:
    """按装备编号查履历。"""
    eq = ctx["equipment_no"]
    period = ctx["period"]
    if period:
        sql = ("SELECT event_date AS 日期, event_type AS 事件类型, description AS 说明, operator AS 经办人,"
               " doc_ref AS 关联资料 FROM service_history WHERE equipment_no = ?"
               " AND event_date BETWEEN ? AND ? ORDER BY event_date")
        return QueryResult(
            intent="equipment_history",
            label="按装备编号与时间段查履历",
            explain=f"查询装备 {eq} 在 {period[0]} 至 {period[1]} 期间的履历事件。",
            sql=sql,
            params=(eq, period[0], period[1]),
        )
    sql = ("SELECT event_date AS 日期, event_type AS 事件类型, description AS 说明, operator AS 经办人,"
           " doc_ref AS 关联资料 FROM service_history WHERE equipment_no = ? ORDER BY event_date")
    return QueryResult(
        intent="equipment_history",
        label="按装备编号查履历",
        explain=f"查询装备 {eq} 的全部履历事件（按时间升序）。",
        sql=sql,
        params=(eq,),
    )


def _t_maintenance(ctx: Dict[str, Any]) -> QueryResult:
    """查检修记录（可按时间段与装备过滤）。"""
    eq = ctx["equipment_no"]
    period = ctx["period"]
    conds: List[str] = []
    params: List[Any] = []
    if eq:
        conds.append("m.equipment_no = ?")
        params.append(eq)
    if period:
        conds.append("m.maint_date BETWEEN ? AND ?")
        params.extend(period)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    sql = ("SELECT m.equipment_no AS 装备编号, m.maint_date AS 检修日期, m.item AS 项目, m.result AS 结论,"
           " m.next_due_date AS 下次到期, m.maintainer AS 检修人"
           " FROM maintenance m" + where + " ORDER BY m.maint_date DESC")
    scope = "、".join(filter(None, [eq or "", f"{period[0]}~{period[1]}" if period else ""])) or "全部装备"
    return QueryResult(
        intent="maintenance",
        label="查检修记录",
        explain=f"查询 {scope} 的检修记录，并给出下次到期日期。",
        sql=sql,
        params=tuple(params),
    )


def _t_fault_history(ctx: Dict[str, Any]) -> QueryResult:
    """查故障类履历。"""
    eq = ctx["equipment_no"]
    conds = ["event_type = '故障'"]
    params: List[Any] = []
    if eq:
        conds.append("equipment_no = ?")
        params.append(eq)
    sql = ("SELECT equipment_no AS 装备编号, event_date AS 日期, description AS 故障描述,"
           " operator AS 经办人, doc_ref AS 关联资料 FROM service_history WHERE "
           + " AND ".join(conds) + " ORDER BY event_date DESC")
    return QueryResult(
        intent="fault_history",
        label="查故障履历",
        explain=f"查询{'装备 ' + eq + ' 的' if eq else '全部装备的'}故障类履历，并可据「关联资料」跳转维修规程。",
        sql=sql,
        params=tuple(params),
    )


def _t_parts_replacement(ctx: Dict[str, Any]) -> QueryResult:
    """查换件记录。"""
    eq = ctx["equipment_no"]
    conds = ["event_type = '换件'"]
    params: List[Any] = []
    if eq:
        conds.append("equipment_no = ?")
        params.append(eq)
    sql = ("SELECT equipment_no AS 装备编号, event_date AS 日期, description AS 更换内容,"
           " operator AS 经办人 FROM service_history WHERE " + " AND ".join(conds) + " ORDER BY event_date DESC")
    return QueryResult(
        intent="parts_replacement",
        label="查换件记录",
        explain="查询换件类履历，用于备件消耗与周期分析。",
        sql=sql,
        params=tuple(params),
    )


def _t_due_maintenance(ctx: Dict[str, Any], today: Optional[date] = None) -> QueryResult:
    """查即将到期或已逾期的保养。"""
    today = today or date.today()
    horizon = (today + timedelta(days=90)).isoformat()
    sql = ("SELECT equipment_no AS 装备编号, maint_date AS 上次检修, item AS 项目, result AS 结论,"
           " next_due_date AS 下次到期, maintainer AS 检修人 FROM maintenance"
           " WHERE next_due_date <= ? ORDER BY next_due_date")
    return QueryResult(
        intent="due_maintenance",
        label="查保养到期",
        explain=f"列出下次到期日在 {horizon} 之前（含已逾期）的装备，用于提前安排检修。",
        sql=sql,
        params=(horizon,),
    )


def _t_status_summary(ctx: Dict[str, Any]) -> QueryResult:
    """按状态统计装备。"""
    sql = ("SELECT status AS 状态, COUNT(*) AS 台数, ROUND(AVG(runtime_hours), 1) AS 平均工作小时"
           " FROM equipment GROUP BY status ORDER BY 台数 DESC")
    return QueryResult(
        intent="status_summary",
        label="按状态统计装备",
        explain="按在用 / 待修等状态统计装备台数与平均累计工作小时。",
        sql=sql,
        params=(),
    )


def _t_overview(ctx: Dict[str, Any]) -> QueryResult:
    """台账总览（兜底模板）。"""
    sql = ("SELECT equipment_no AS 装备编号, model AS 型号, unit AS 所属单位, status AS 状态,"
           " runtime_hours AS 累计工作小时 FROM equipment ORDER BY equipment_no")
    return QueryResult(
        intent="overview",
        label="装备台账总览",
        explain="未识别到明确的查询意图，返回台账总览；可换用「某装备的履历」「近三个月检修记录」等说法。",
        sql=sql,
        params=(),
    )


#: 意图路由（顺序即优先级；每条为 (意图键, 触发词列表, 构造函数)）
RULES: Sequence[Tuple[str, Sequence[str], Callable[[Dict[str, Any]], QueryResult]]] = (
    ("due_maintenance", ("到期", "将要保养", "该保养", "逾期", "临期"), _t_due_maintenance),
    ("fault_history", ("故障", "坏了", "异常现象"), _t_fault_history),
    ("parts_replacement", ("换件", "更换了", "更换记录", "备件消耗"), _t_parts_replacement),
    ("maintenance", ("检修", "保养记录", "维护记录", "年检", "半年检"), _t_maintenance),
    ("status_summary", ("多少台", "台数", "统计", "几台"), _t_status_summary),
    ("equipment_history", ("履历", "经历", "历史事件", "大事记"), _t_equipment_history),
    ("equipment_by_model", ("台账", "型号", "装备清单", "有哪些装备"), _t_equipment_by_model),
)


# ---------------------------------------------------------------- 执行


def _connect_ro(db_path: Path | str) -> sqlite3.Connection:
    """以只读方式打开业务库（对应「只读账号」约束）。"""
    path = Path(db_path).resolve()
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = 1")
    return conn


def query(question: str, db_path: Path | str, today: Optional[date] = None) -> QueryResult:
    """执行一次自然语言 → 白名单模板查询。

    :param question: 用户问题
    :param db_path: 业务库路径（只读访问）
    :param today: 参考日期（便于测试）
    :return: QueryResult
    """
    text = question or ""
    ctx = detect(text, today=today)

    # 装备编号优先：带编号的问题一律走履历/检修/故障模板
    chosen: Optional[QueryResult] = None
    for key, keywords, builder in RULES:
        if any(k in text for k in keywords):
            chosen = builder(ctx)
            break
    if chosen is None:
        if ctx["equipment_no"]:
            chosen = _t_equipment_history(ctx)
        else:
            chosen = _t_overview(ctx)

    if not Path(db_path).exists():
        return QueryResult(
            intent=chosen.intent,
            label=chosen.label,
            explain=chosen.explain,
            refused=True,
            message=f"业务库不存在：{db_path}（先运行 python tools/make_demo_business_db.py）",
        )

    try:
        conn = _connect_ro(db_path)
    except sqlite3.Error as exc:
        return QueryResult(
            intent=chosen.intent, label=chosen.label, explain=chosen.explain,
            refused=True, message=f"无法以只读方式打开业务库：{exc}",
        )

    try:
        cur = conn.execute(chosen.sql, chosen.params)
        rows = [dict(r) for r in cur.fetchmany(MAX_ROWS)]
        columns = [d[0] for d in (cur.description or [])]
    except sqlite3.Error as exc:
        conn.close()
        return QueryResult(
            intent=chosen.intent, label=chosen.label, explain=chosen.explain,
            refused=True, message=f"查询失败（模板已拒绝执行）：{exc}",
        )
    conn.close()

    chosen.rows = rows
    chosen.columns = columns
    if not rows:
        chosen.message = "模板执行成功，但没有匹配记录。"
    return chosen


def available_intents() -> List[Dict[str, str]]:
    """列出可用查询意图与示例问法（用于界面提示）。"""
    examples = {
        "equipment_by_model": "HYS-1 的装备台账",
        "equipment_history": "EQ-1002 的履历",
        "maintenance": "近三个月检修记录",
        "fault_history": "有哪些故障记录",
        "parts_replacement": "换件记录",
        "due_maintenance": "哪些装备保养要到期了",
        "status_summary": "各状态各有多少台",
        "overview": "装备台账总览",
    }
    return [{"intent": key, "example": examples.get(key, "")} for key in examples]
