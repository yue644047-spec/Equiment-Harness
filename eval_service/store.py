"""SQLite 存储层：表结构、upsert 与查询辅助。

七张表：
    conversations  会话
    messages       消息（含问答对、检索片段、反馈快照）
    feedbacks      应用级反馈（/v1/app/feedbacks 原始记录）
    evaluations    评价工作流的多维评分结果
    runs           一次 collect/judge/regression 运行的元信息
    badcases       挖掘出的 badcase（每次挖掘重建）
    kb_docs        知识库文档导入记录（kb-build 使用）

所有写库操作都是幂等的 upsert（ON CONFLICT DO UPDATE），可重复执行。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

SCHEMA_VERSION = "5"

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- 会话表
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id   TEXT PRIMARY KEY,
    name              TEXT,
    inputs_json       TEXT,
    status            TEXT,
    from_source       TEXT,
    from_end_user_id  TEXT,
    from_account_id   TEXT,
    created_at        INTEGER,          -- Dify 返回的 Unix 秒
    updated_at        INTEGER,
    created_iso       TEXT,             -- 便于人读与周期过滤
    annotated         INTEGER DEFAULT 0,
    raw_json          TEXT,
    fetched_at        INTEGER
);

-- 消息表（一条记录 = 一问一答）
CREATE TABLE IF NOT EXISTS messages (
    id                        TEXT PRIMARY KEY,
    conversation_id           TEXT,
    parent_message_id         TEXT,
    query                     TEXT,
    answer                    TEXT,
    status                    TEXT,
    error                     TEXT,
    workflow_run_id           TEXT,
    elapsed_time              REAL,
    created_at                INTEGER,
    created_iso               TEXT,
    feedback_rating           TEXT,
    feedback_content          TEXT,
    retriever_resources_json  TEXT,
    metadata_json             TEXT,
    receipt_score             INTEGER,   -- 从答案文本里正则抓到的 [[EVAL_RECEIPT]]（= 主观指标1 满意度）
    receipt_suggestion        TEXT,
    receipt_usefulness        INTEGER,   -- 主观指标2 有用性（1-5，未采到为 NULL）
    receipt_experience        INTEGER,   -- 主观指标3 体验感（1-5，未采到为 NULL）
    user_score_raw            TEXT,      -- like/dislike/1..5/空
    user_score                INTEGER,   -- 归一化后的 1..5，空为 NULL
    raw_json                  TEXT,
    fetched_at                INTEGER
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);

-- 应用反馈表
CREATE TABLE IF NOT EXISTS feedbacks (
    id                TEXT PRIMARY KEY,
    app_id            TEXT,
    conversation_id   TEXT,
    message_id        TEXT,
    rating            TEXT,
    content           TEXT,
    from_source       TEXT,
    from_end_user_id  TEXT,
    from_account_id   TEXT,
    created_at        INTEGER,
    updated_at        INTEGER,
    raw_json          TEXT,
    synced_at         INTEGER
);
CREATE INDEX IF NOT EXISTS idx_feedbacks_message ON feedbacks(message_id);

-- 评价结果表（评价工作流输出 + 派生指标）
CREATE TABLE IF NOT EXISTS evaluations (
    message_id                TEXT PRIMARY KEY,
    conversation_id           TEXT,
    run_id                    TEXT,
    question                  TEXT,
    answer                    TEXT,
    contexts                  TEXT,
    factual_accuracy          INTEGER,
    knowledge_completeness    INTEGER,
    relevance                 INTEGER,
    clarity                   INTEGER,
    retrieval_quality         INTEGER,
    safety_compliance         INTEGER,   -- 装备域专项：安全合规性 0-5（0=不涉及）
    version_currency          INTEGER,   -- 装备域专项：版本时效性 0-5（0=不涉及，1=引用作废版本）
    terminology_norm          INTEGER,   -- 装备域专项：术语规范性 1-5
    doc_no                    TEXT,      -- 治理：命中资料编号（台账口径，由归因前置写入）
    doc_version               TEXT,      -- 治理：命中资料版本号
    blame                     TEXT,      -- 治理：doc / system / none
    defect_type               TEXT,      -- 治理：缺陷类型代码（见 attribution.DEFECT_TYPES）
    clause_no                 TEXT,      -- 治理：命中的条款号（解析期写入后可用）
    user_score                INTEGER,
    user_score_raw            TEXT,
    knowledge_gap             INTEGER,
    hallucination_risk        TEXT,
    annotation_label          TEXT,
    grounding_rate            REAL,      -- 生成后可核验性：被片段支撑的事实性 token 占比
    grounding_level           TEXT,      -- ok / warn / fail / n/a（v1 仅度量，不参与判定）
    ungrounded_json           TEXT,      -- 未支撑的数值/编号/版本/条款明细
    error_cause               TEXT,
    judge_reason              TEXT,
    missing_knowledge_json    TEXT,
    improvement_suggestions_json TEXT,
    csi                       REAL,
    verdict                   TEXT,
    badcase                   INTEGER,
    source                    TEXT,      -- workflow / mock-rule
    result_json               TEXT,
    created_at                INTEGER,
    updated_at                INTEGER
);
CREATE INDEX IF NOT EXISTS idx_eval_verdict ON evaluations(verdict);
CREATE INDEX IF NOT EXISTS idx_eval_badcase ON evaluations(badcase);
CREATE INDEX IF NOT EXISTS idx_eval_doc ON evaluations(doc_no);

-- 运行记录表
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    kind          TEXT,      -- collect / judge / report / badcase / regression / kb-build
    status        TEXT,      -- running / success / failed
    started_at    INTEGER,
    finished_at   INTEGER,
    params_json   TEXT,
    stats_json    TEXT,
    notes         TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_kind ON runs(kind);

-- badcase 表
CREATE TABLE IF NOT EXISTS badcases (
    message_id      TEXT PRIMARY KEY,
    severity        TEXT,     -- P0 / P1 / P2
    conversation_id TEXT,
    question        TEXT,
    answer          TEXT,
    csi             REAL,
    factual_accuracy INTEGER,
    knowledge_completeness INTEGER,
    safety_compliance INTEGER,
    version_currency INTEGER,
    terminology_norm INTEGER,
    annotation_label TEXT,
    error_cause     TEXT,
    hallucination_risk TEXT,
    knowledge_gap   INTEGER,
    user_score      INTEGER,
    reasons_json    TEXT,
    created_at      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_badcase_sev ON badcases(severity);

-- 知识库文档表
CREATE TABLE IF NOT EXISTS kb_docs (
    doc_id          TEXT PRIMARY KEY,
    dataset_id      TEXT,
    name            TEXT,
    source_path     TEXT,
    content_hash    TEXT,
    char_count      INTEGER,
    segment_count   INTEGER,
    status          TEXT,     -- pending / indexing / completed / error / skipped
    batch           TEXT,
    error           TEXT,
    created_at      INTEGER,
    updated_at      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_kbdocs_dataset ON kb_docs(dataset_id);

-- 文档缺陷表（治理：docs/10 §7）
CREATE TABLE IF NOT EXISTS defects (
    defect_id       TEXT PRIMARY KEY,   -- doc_no + clause_no + defect_type 的稳定哈希
    message_id      TEXT,               -- 首次发现的证据消息
    doc_no          TEXT,               -- 资料编号（台账口径）
    doc_version     TEXT,               -- 缺陷所在版本
    clause_no       TEXT,               -- 条款号（解析期写入后可用）
    defect_type     TEXT,               -- 缺陷类型代码（D1_* / D2_* ...）
    defect_label    TEXT,               -- 缺陷中文名
    dimension       TEXT,               -- 质量维度 D1~D8
    severity        TEXT,               -- P0 / P1 / P2
    blame           TEXT,               -- doc / system（仅 doc 计入文档质量）
    status          TEXT,               -- open / fixing / verifying / closed / rejected
    evidence_json   TEXT,               -- 证据：命中片段与评分
    question        TEXT,               -- 触发该缺陷的问题
    reasons_json    TEXT,               -- 判定理由
    action          TEXT,               -- 整改动作
    owner           TEXT,               -- 责任单位
    due_date        TEXT,               -- 期限
    verified_by     TEXT,               -- 验证方式（probe_id / manual）
    occurrences     INTEGER DEFAULT 1,  -- 同类缺陷出现次数
    opened_at       INTEGER,
    closed_at       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_defects_doc ON defects(doc_no);
CREATE INDEX IF NOT EXISTS idx_defects_status ON defects(status);
"""


def now_ts() -> int:
    """当前 Unix 秒。"""
    return int(time.time())


def to_iso(ts: Optional[int]) -> Optional[str]:
    """Unix 秒 -> ISO8601（本地时区）。"""
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts)).astimezone().isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def new_run_id(prefix: str = "run") -> str:
    """生成运行 ID。"""
    return f"{prefix}-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"


def dumps(value: Any) -> str:
    """JSON 序列化（中文不转义，便于人工查看）。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(value: Optional[str], default: Any) -> Any:
    """JSON 反序列化，失败返回默认值。"""
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class Store:
    """SQLite 存储门面。"""

    def __init__(self, db_path: Path | str):
        """
        :param db_path: 数据库文件路径，父目录会自动创建
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")

    # -------------------------------------------------- 生命周期
    def init_schema(self) -> None:
        """建表（幂等），并把已存在的旧库补齐到当前 schema 版本。"""
        self.conn.executescript(SCHEMA_SQL)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """为已存在的旧库补充新增列。

        SQLite 的 ADD COLUMN 不支持 IF NOT EXISTS，因此先读 PRAGMA table_info
        再按差集补齐。新库不会走到这里（列已由 SCHEMA_SQL 建出）。

        v1 -> v2：evaluations / badcases 增加装备域专项维度列
        （safety_compliance / version_currency / terminology_norm）。
        v2 -> v3：evaluations 增加治理归因列（doc_no / doc_version / blame /
        defect_type / clause_no）；新增 defects 表（建表由 SCHEMA_SQL 幂等完成）。
        v3 -> v4：messages 增加三项主观指标回执列（receipt_usefulness / receipt_experience，
        receipt_score 沿用为「满意度」），使主观三项能完整落库参与 SSI。
        v4 -> v5：evaluations 增加生成后可核验性列（grounding_rate / grounding_level /
        ungrounded_json），用于「答案是否跑偏」的确定性度量与留证（docs/16 §1）。
        """
        additions = {
            "messages": [
                ("receipt_usefulness", "INTEGER"),
                ("receipt_experience", "INTEGER"),
            ],
            "evaluations": [
                ("safety_compliance", "INTEGER"),
                ("version_currency", "INTEGER"),
                ("terminology_norm", "INTEGER"),
                ("doc_no", "TEXT"),
                ("doc_version", "TEXT"),
                ("blame", "TEXT"),
                ("defect_type", "TEXT"),
                ("clause_no", "TEXT"),
                ("grounding_rate", "REAL"),
                ("grounding_level", "TEXT"),
                ("ungrounded_json", "TEXT"),
            ],
            "badcases": [
                ("safety_compliance", "INTEGER"),
                ("version_currency", "INTEGER"),
                ("terminology_norm", "INTEGER"),
            ],
        }
        for table, columns in additions.items():
            existing = {
                row["name"]
                for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if not existing:
                continue
            for name, decl in columns:
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def close(self) -> None:
        """关闭连接。"""
        try:
            self.conn.commit()
        finally:
            self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """事务上下文。"""
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # -------------------------------------------------- 通用 upsert
    def _upsert(self, table: str, row: Dict[str, Any], pk: str) -> None:
        """按主键 upsert 一行。"""
        cols = list(row.keys())
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != pk)
        sql = (
            f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT({pk}) DO UPDATE SET {updates}"
        )
        self.conn.execute(sql, [row[c] for c in cols])

    # -------------------------------------------------- conversations
    def upsert_conversation(self, item: Dict[str, Any]) -> None:
        """写入/更新一条会话记录。"""
        cid = str(item.get("id") or item.get("conversation_id") or "")
        if not cid:
            return
        row = {
            "conversation_id": cid,
            "name": item.get("name"),
            "inputs_json": dumps(item.get("inputs") or {}),
            "status": item.get("status"),
            "from_source": item.get("from_source"),
            "from_end_user_id": item.get("from_end_user_id"),
            "from_account_id": item.get("from_account_id"),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
            "created_iso": to_iso(item.get("created_at")),
            "annotated": 1 if item.get("annotated") else 0,
            "raw_json": dumps(item),
            "fetched_at": now_ts(),
        }
        self._upsert("conversations", row, "conversation_id")

    def upsert_conversations(self, items: Iterable[Dict[str, Any]]) -> int:
        """批量写入会话，返回条数。"""
        n = 0
        with self.tx():
            for item in items:
                self.upsert_conversation(item)
                n += 1
        return n

    # -------------------------------------------------- messages
    def upsert_message(self, item: Dict[str, Any]) -> None:
        """写入/更新一条消息记录（保留已有反馈字段不覆盖为空）。"""
        mid = str(item.get("id") or "")
        if not mid:
            return
        row = {
            "id": mid,
            "conversation_id": item.get("conversation_id"),
            "parent_message_id": item.get("parent_message_id"),
            "query": item.get("query"),
            "answer": item.get("answer"),
            "status": item.get("status"),
            "error": item.get("error"),
            "workflow_run_id": item.get("workflow_run_id"),
            "elapsed_time": item.get("elapsed_time"),
            "created_at": item.get("created_at"),
            "created_iso": to_iso(item.get("created_at")),
            "feedback_rating": (item.get("feedback") or {}).get("rating") if isinstance(item.get("feedback"), dict) else item.get("feedback_rating"),
            "feedback_content": (item.get("feedback") or {}).get("content") if isinstance(item.get("feedback"), dict) else item.get("feedback_content"),
            "retriever_resources_json": dumps(item.get("retriever_resources") or []),
            "metadata_json": dumps(item.get("metadata") or {}),
            "receipt_score": item.get("receipt_score"),
            "receipt_suggestion": item.get("receipt_suggestion"),
            "receipt_usefulness": item.get("receipt_usefulness"),
            "receipt_experience": item.get("receipt_experience"),
            "user_score_raw": item.get("user_score_raw"),
            "user_score": item.get("user_score"),
            "raw_json": dumps(item),
            "fetched_at": now_ts(),
        }
        self._upsert("messages", row, "id")

    def upsert_messages(self, items: Iterable[Dict[str, Any]]) -> int:
        """批量写入消息，返回条数。"""
        n = 0
        with self.tx():
            for item in items:
                self.upsert_message(item)
                n += 1
        return n

    def update_message_feedback(
        self,
        message_id: str,
        rating: Optional[str],
        content: Optional[str] = None,
        user_score: Optional[int] = None,
        raw: Optional[str] = None,
    ) -> None:
        """更新消息上的反馈字段。"""
        with self.tx():
            self.conn.execute(
                "UPDATE messages SET feedback_rating=?, feedback_content=?, user_score=?, user_score_raw=? WHERE id=?",
                (rating, content, user_score, raw, message_id),
            )

    # -------------------------------------------------- feedbacks
    def upsert_feedback(self, item: Dict[str, Any]) -> None:
        """写入/更新一条应用级反馈。"""
        fid = str(item.get("id") or "")
        if not fid:
            return
        row = {
            "id": fid,
            "app_id": item.get("app_id"),
            "conversation_id": item.get("conversation_id"),
            "message_id": item.get("message_id"),
            "rating": item.get("rating"),
            "content": item.get("content"),
            "from_source": item.get("from_source"),
            "from_end_user_id": item.get("from_end_user_id"),
            "from_account_id": item.get("from_account_id"),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
            "raw_json": dumps(item),
            "synced_at": now_ts(),
        }
        self._upsert("feedbacks", row, "id")

    def upsert_feedbacks(self, items: Iterable[Dict[str, Any]]) -> int:
        """批量写入反馈，返回条数。"""
        n = 0
        with self.tx():
            for item in items:
                self.upsert_feedback(item)
                n += 1
        return n

    # -------------------------------------------------- evaluations
    def upsert_evaluation(self, item: Dict[str, Any]) -> None:
        """写入/更新一条评价结果。"""
        mid = str(item.get("message_id") or "")
        if not mid:
            return
        row = {
            "message_id": mid,
            "conversation_id": item.get("conversation_id"),
            "run_id": item.get("run_id"),
            "question": item.get("question"),
            "answer": item.get("answer"),
            "contexts": item.get("contexts"),
            "factual_accuracy": item.get("factual_accuracy"),
            "knowledge_completeness": item.get("knowledge_completeness"),
            "relevance": item.get("relevance"),
            "clarity": item.get("clarity"),
            "retrieval_quality": item.get("retrieval_quality"),
            "safety_compliance": item.get("safety_compliance"),
            "version_currency": item.get("version_currency"),
            "terminology_norm": item.get("terminology_norm"),
            "user_score": item.get("user_score"),
            "user_score_raw": item.get("user_score_raw"),
            "knowledge_gap": 1 if item.get("knowledge_gap") else 0,
            "hallucination_risk": item.get("hallucination_risk"),
            "annotation_label": item.get("annotation_label"),
            "error_cause": item.get("error_cause"),
            "judge_reason": item.get("judge_reason"),
            "missing_knowledge_json": dumps(item.get("missing_knowledge") or []),
            "improvement_suggestions_json": dumps(item.get("improvement_suggestions") or []),
            "csi": item.get("csi"),
            "verdict": item.get("verdict"),
            "badcase": 1 if item.get("badcase") else 0,
            "source": item.get("source"),
            "grounding_rate": item.get("grounding_rate"),
            "grounding_level": item.get("grounding_level"),
            "ungrounded_json": dumps(item.get("ungrounded") or []),
            "result_json": dumps(item.get("result_json") or {}),
            "created_at": item.get("created_at") or now_ts(),
            "updated_at": now_ts(),
        }
        self._upsert("evaluations", row, "message_id")

    def set_grounding(self, message_id: str, rate: Optional[float], level: str,
                      ungrounded: Iterable[Dict[str, Any]]) -> None:
        """写入生成后可核验性结果（供 judge 与 grounding 回填共用）。"""
        self.conn.execute(
            "UPDATE evaluations SET grounding_rate=?, grounding_level=?, ungrounded_json=?, updated_at=? "
            "WHERE message_id=?",
            (rate, level, dumps(list(ungrounded)), now_ts(), str(message_id)),
        )
        self.conn.commit()

    def upsert_evaluations(self, items: Iterable[Dict[str, Any]]) -> int:
        """批量写入评价结果，返回条数。"""
        n = 0
        with self.tx():
            for item in items:
                self.upsert_evaluation(item)
                n += 1
        return n

    # -------------------------------------------------- runs
    def start_run(self, kind: str, params: Optional[Dict[str, Any]] = None, run_id: Optional[str] = None) -> str:
        """登记一次运行的开始，返回 run_id。"""
        rid = run_id or new_run_id(kind)
        row = {
            "run_id": rid,
            "kind": kind,
            "status": "running",
            "started_at": now_ts(),
            "finished_at": None,
            "params_json": dumps(params or {}),
            "stats_json": None,
            "notes": None,
        }
        with self.tx():
            self._upsert("runs", row, "run_id")
        return rid

    def finish_run(
        self,
        run_id: str,
        status: str = "success",
        stats: Optional[Dict[str, Any]] = None,
        notes: Optional[str] = None,
    ) -> None:
        """登记一次运行的结束。"""
        with self.tx():
            self.conn.execute(
                "UPDATE runs SET status=?, finished_at=?, stats_json=?, notes=? WHERE run_id=?",
                (status, now_ts(), dumps(stats or {}), notes, run_id),
            )

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """按 run_id 读取运行记录。"""
        cur = self.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def list_runs(self, kind: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        """列出最近的运行记录。"""
        if kind:
            cur = self.conn.execute(
                "SELECT * FROM runs WHERE kind=? ORDER BY started_at DESC LIMIT ?", (kind, limit)
            )
        else:
            cur = self.conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]

    # -------------------------------------------------- badcases
    def replace_badcases(self, items: Sequence[Dict[str, Any]]) -> int:
        """重建 badcases 表。"""
        with self.tx():
            self.conn.execute("DELETE FROM badcases")
            for item in items:
                self._upsert(
                    "badcases",
                    {
                        "message_id": item.get("message_id"),
                        "severity": item.get("severity"),
                        "conversation_id": item.get("conversation_id"),
                        "question": item.get("question"),
                        "answer": item.get("answer"),
                        "csi": item.get("csi"),
                        "factual_accuracy": item.get("factual_accuracy"),
                        "knowledge_completeness": item.get("knowledge_completeness"),
                        "safety_compliance": item.get("safety_compliance"),
                        "version_currency": item.get("version_currency"),
                        "terminology_norm": item.get("terminology_norm"),
                        "annotation_label": item.get("annotation_label"),
                        "error_cause": item.get("error_cause"),
                        "hallucination_risk": item.get("hallucination_risk"),
                        "knowledge_gap": 1 if item.get("knowledge_gap") else 0,
                        "user_score": item.get("user_score"),
                        "reasons_json": dumps(item.get("reasons") or []),
                        "created_at": now_ts(),
                    },
                    "message_id",
                )
        return len(items)

    # -------------------------------------------------- kb_docs
    def upsert_kb_doc(self, item: Dict[str, Any]) -> None:
        """写入/更新一条知识库文档记录。"""
        did = str(item.get("doc_id") or "")
        if not did:
            return
        row = {
            "doc_id": did,
            "dataset_id": item.get("dataset_id"),
            "name": item.get("name"),
            "source_path": item.get("source_path"),
            "content_hash": item.get("content_hash"),
            "char_count": item.get("char_count"),
            "segment_count": item.get("segment_count"),
            "status": item.get("status"),
            "batch": item.get("batch"),
            "error": item.get("error"),
            "created_at": item.get("created_at") or now_ts(),
            "updated_at": now_ts(),
        }
        self._upsert("kb_docs", row, "doc_id")

    def upsert_kb_docs(self, items: Iterable[Dict[str, Any]]) -> int:
        """批量写入知识库文档记录。"""
        n = 0
        with self.tx():
            for item in items:
                self.upsert_kb_doc(item)
                n += 1
        return n

    # -------------------------------------------------- 查询辅助
    def count(self, table: str) -> int:
        """统计表行数。"""
        cur = self.conn.execute(f"SELECT COUNT(*) AS c FROM {table}")
        return int(cur.fetchone()["c"])

    def stats_overview(self) -> Dict[str, int]:
        """返回各表行数概览。"""
        tables = ["conversations", "messages", "feedbacks", "evaluations", "runs", "badcases", "kb_docs", "defects"]
        return {t: self.count(t) for t in tables}

    def fetch_pending_messages(self, limit: Optional[int] = None, run_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """取尚未评价（或本次 run 未覆盖）的消息，按时间升序。

        :param limit: 最多返回条数
        :param run_id: 若给出，则排除该 run 已评价的消息
        """
        sql = [
            "SELECT m.* FROM messages m",
            "LEFT JOIN evaluations e ON e.message_id = m.id",
            "WHERE e.message_id IS NULL",
            "AND m.query IS NOT NULL AND TRIM(m.query) <> ''",
        ]
        params: List[Any] = []
        if run_id:
            sql.append("AND (e.run_id IS NULL OR e.run_id <> ?)")
            params.append(run_id)
        sql.append("ORDER BY m.created_at ASC")
        if limit:
            sql.append("LIMIT ?")
            params.append(int(limit))
        cur = self.conn.execute(" ".join(sql), params)
        return [dict(r) for r in cur.fetchall()]

    def fetch_all_messages(self, limit: Optional[int] = None, since_ts: Optional[int] = None) -> List[Dict[str, Any]]:
        """按时间升序取消息。"""
        sql = "SELECT * FROM messages WHERE 1=1"
        params: List[Any] = []
        if since_ts:
            sql += " AND created_at >= ?"
            params.append(int(since_ts))
        sql += " ORDER BY created_at ASC"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def fetch_evaluations(
        self,
        since_ts: Optional[int] = None,
        until_ts: Optional[int] = None,
        only_badcase: bool = False,
        limit: Optional[int] = None,
        join_message: bool = True,
    ) -> List[Dict[str, Any]]:
        """按时间窗取评价结果（默认与 messages 联表以获得 created_at）。

        :param since_ts: 起始 Unix 秒（含）
        :param until_ts: 结束 Unix 秒（不含）
        :param only_badcase: 只取 badcase=1
        :param limit: 限制条数
        :param join_message: 是否联表 messages 取会话/时间字段
        """
        if join_message:
            sql = [
                "SELECT e.*, m.created_at AS msg_created_at, m.created_iso AS msg_created_iso,",
                "       m.conversation_id AS msg_conversation_id, m.feedback_content AS feedback_content,",
                "       m.receipt_score AS receipt_score, m.receipt_suggestion AS receipt_suggestion,",
                "       m.receipt_usefulness AS receipt_usefulness, m.receipt_experience AS receipt_experience",
                "FROM evaluations e LEFT JOIN messages m ON m.id = e.message_id WHERE 1=1",
            ]
        else:
            sql = ["SELECT e.*, e.created_at AS msg_created_at FROM evaluations e WHERE 1=1"]
        params: List[Any] = []
        if since_ts:
            sql.append("AND COALESCE(m.created_at, e.created_at) >= ?")
            params.append(int(since_ts))
        if until_ts:
            sql.append("AND COALESCE(m.created_at, e.created_at) < ?")
            params.append(int(until_ts))
        if only_badcase:
            sql.append("AND e.badcase = 1")
        sql.append("ORDER BY COALESCE(m.created_at, e.created_at) ASC")
        if limit:
            sql.append("LIMIT ?")
            params.append(int(limit))
        rows = [dict(r) for r in self.conn.execute(" ".join(sql), params).fetchall()]
        # 反序列化 JSON 字段，方便上层直接使用
        for r in rows:
            r["missing_knowledge"] = loads(r.get("missing_knowledge_json"), [])
            r["improvement_suggestions"] = loads(r.get("improvement_suggestions_json"), [])
            r["user_score"] = r.get("user_score")
        return rows

    def fetch_badcases(self, min_severity: Optional[str] = None) -> List[Dict[str, Any]]:
        """取 badcase 列表，可按最低严重度过滤（P0 最严重）。"""
        order = {"P0": 0, "P1": 1, "P2": 2}
        rows = [dict(r) for r in self.conn.execute("SELECT * FROM badcases").fetchall()]
        for r in rows:
            r["reasons"] = loads(r.get("reasons_json"), [])
        if min_severity:
            threshold = order.get(min_severity.upper(), 2)
            rows = [r for r in rows if order.get(r.get("severity", "P2"), 2) <= threshold]
        rows.sort(key=lambda r: (order.get(r.get("severity", "P2"), 2), -(r.get("csi") or 0)))
        return rows

    # -------------------------------------------------- 治理：归因与缺陷（v3）
    def update_attribution(self, items: Sequence[Dict[str, Any]]) -> int:
        """把归因前置结果写回 evaluations（doc_no / doc_version / blame / defect_type / clause_no）。

        :param items: attribution.attribute_all 的输出
        :return: 实际更新的条数
        """
        n = 0
        with self.tx():
            for item in items:
                mid = str(item.get("message_id") or "")
                if not mid:
                    continue
                self.conn.execute(
                    "UPDATE evaluations SET doc_no=?, doc_version=?, blame=?, defect_type=?, clause_no=?, updated_at=? "
                    "WHERE message_id=?",
                    (
                        item.get("doc_no") or "",
                        item.get("doc_version") or "",
                        item.get("blame") or "none",
                        item.get("defect_type") or "",
                        item.get("clause_no") or "",
                        now_ts(),
                        mid,
                    ),
                )
                n += 1
        return n

    def replace_defects(self, items: Sequence[Dict[str, Any]]) -> int:
        """整体替换缺陷表。

        同一 (doc_no, clause_no, defect_type) 视为同一条缺陷：保留首条证据并累计出现次数，
        这样记分卡上的「缺陷数」是**去重后的缺陷条目**，而不是问答条数。

        :return: 去重后的缺陷条数
        """
        merged: Dict[str, Dict[str, Any]] = {}
        for item in items:
            did = str(item.get("defect_id") or "")
            if not did:
                continue
            if did in merged:
                merged[did]["occurrences"] = int(merged[did].get("occurrences") or 1) + 1
                continue
            merged[did] = dict(item)
        with self.tx():
            self.conn.execute("DELETE FROM defects")
            for row in merged.values():
                self.conn.execute(
                    "INSERT INTO defects (defect_id, message_id, doc_no, doc_version, clause_no, defect_type,"
                    " defect_label, dimension, severity, blame, status, evidence_json, question, reasons_json,"
                    " action, owner, due_date, verified_by, occurrences, opened_at, closed_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        row.get("defect_id"),
                        row.get("message_id"),
                        row.get("doc_no"),
                        row.get("doc_version"),
                        row.get("clause_no"),
                        row.get("defect_type"),
                        row.get("defect_label"),
                        row.get("dimension"),
                        row.get("severity"),
                        row.get("blame"),
                        row.get("status") or "open",
                        dumps(row.get("evidence") or {}),
                        row.get("question"),
                        dumps(row.get("reasons") or []),
                        row.get("action"),
                        row.get("owner") or "",
                        row.get("due_date") or "",
                        row.get("verified_by") or "",
                        int(row.get("occurrences") or 1),
                        row.get("opened_at") or now_ts(),
                        row.get("closed_at"),
                    ),
                )
        return len(merged)

    def fetch_defects(
        self,
        blame: Optional[str] = None,
        doc_no: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """取缺陷清单，按严重度排序。

        :param blame: 只看 doc（文档侧）或 system（系统侧）
        :param doc_no: 只看某份资料
        :param status: 只看某状态（open / fixing / verifying / closed / rejected）
        """
        sql = "SELECT * FROM defects WHERE 1=1"
        params: List[Any] = []
        if blame:
            sql += " AND blame = ?"
            params.append(blame)
        if doc_no:
            sql += " AND doc_no = ?"
            params.append(doc_no)
        if status:
            sql += " AND status = ?"
            params.append(status)
        rows = [dict(r) for r in self.conn.execute(sql, params).fetchall()]
        for row in rows:
            row["reasons"] = loads(row.get("reasons_json"), [])
            row["evidence"] = loads(row.get("evidence_json"), {})
        order = {"P0": 0, "P1": 1, "P2": 2}
        rows.sort(key=lambda r: (order.get(str(r.get("severity")), 9), str(r.get("doc_no"))))
        return rows

    def update_defect_status(
        self,
        defect_id: str,
        status: str,
        owner: str = "",
        due_date: str = "",
        verified_by: str = "",
    ) -> bool:
        """更新一条缺陷的整改状态（治理看板用）。

        :param defect_id: 缺陷 ID
        :param status: open / fixing / verifying / closed / rejected
        :param owner: 责任单位
        :param due_date: 期限
        :param verified_by: 验证方式（probe_id / manual / 空）
        :return: 是否更新到行
        """
        closed_at = now_ts() if status == "closed" else None
        with self.tx():
            cur = self.conn.execute(
                "UPDATE defects SET status=?, owner=COALESCE(NULLIF(?, ''), owner),"
                " due_date=COALESCE(NULLIF(?, ''), due_date),"
                " verified_by=COALESCE(NULLIF(?, ''), verified_by), closed_at=? WHERE defect_id=?",
                (status, owner, due_date, verified_by, closed_at, defect_id),
            )
        return cur.rowcount > 0

    def fetch_defect(self, defect_id: str) -> Optional[Dict[str, Any]]:
        """按 ID 取单条缺陷。"""
        row = self.conn.execute("SELECT * FROM defects WHERE defect_id = ?", (defect_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["reasons"] = loads(item.get("reasons_json"), [])
        item["evidence"] = loads(item.get("evidence_json"), {})
        return item

    def fetch_resources_map(self) -> Dict[str, List[Dict[str, Any]]]:
        """构建 message_id -> retriever_resources 的映射（治理归因用）。"""
        result: Dict[str, List[Dict[str, Any]]] = {}
        for row in self.conn.execute("SELECT id, retriever_resources_json FROM messages").fetchall():
            result[row["id"]] = loads(row["retriever_resources_json"], [])
        return result

    def fetch_contexts_map(self) -> Dict[str, str]:
        """构建 message_id -> 检索片段拼接文本 的映射。"""
        result: Dict[str, str] = {}
        for r in self.conn.execute("SELECT id, retriever_resources_json FROM messages").fetchall():
            resources = loads(r["retriever_resources_json"], [])
            result[r["id"]] = join_contexts(resources)
        return result


def join_contexts(resources: Optional[Sequence[Dict[str, Any]]]) -> str:
    """把 Dify 的 retriever_resources 拼成工作流约定的 contexts 字符串。

    多段以 "\\n---\\n" 分隔；无检索时返回空字符串。

    :param resources: retriever_resources 数组，元素含 content/score/document_name 等
    :return: 拼接后的上下文字符串
    """
    if not resources:
        return ""
    parts: List[str] = []
    for item in resources:
        if not isinstance(item, dict):
            continue
        content = (item.get("content") or "").strip()
        if not content:
            continue
        name = item.get("document_name") or item.get("title") or ""
        score = item.get("score")
        head = f"[{name}]" if name else ""
        if score is not None:
            try:
                head = f"{head}(score={float(score):.3f})"
            except (TypeError, ValueError):
                pass
        parts.append(f"{head}\n{content}" if head else content)
    return "\n---\n".join(parts)
