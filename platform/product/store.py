"""产品数据面（SQLite / MySQL 双后端）：数据集 / 文档 / 切片 / 向量 / 标注 / 工作流 / 运行 / 模型提供商 / 长期记忆。

后端由配置决定（ADR-D27）：
  * sqlite（默认兜底）：单文件 platform/data/equipment.db，随产品搬走即可，离线零运维；
  * mysql（正式部署）：库 equipment_governance（utf8mb4），与图数据库 Neo4j 配合使用。

两种后端共用同一份建表语句与同一套方法：DDL 采用两边都认的类型（VARCHAR/INT/DOUBLE/LONGTEXT），
SQL 方言差异（占位符、INSERT OR REPLACE、CREATE INDEX IF NOT EXISTS、information_schema）
统一由下面的 SqlConnection 适配，业务方法一行都不用改。
向量以 float32 二进制存在切片行里，检索时整库加载进内存（万级切片以内足够快）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import numpy as np

HOME = Path(__file__).resolve().parents[1]
DEFAULT_DB = HOME / "data" / "equipment.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    description VARCHAR(1000) DEFAULT '',
    chunk_method VARCHAR(32) DEFAULT 'naive',
    parser_config LONGTEXT,
    embedding_model VARCHAR(128) DEFAULT '',
    created_at VARCHAR(32), updated_at VARCHAR(32)
);
CREATE TABLE IF NOT EXISTS documents (
    id VARCHAR(64) PRIMARY KEY,
    dataset_id VARCHAR(64) NOT NULL,
    name VARCHAR(255) NOT NULL,
    stored_path VARCHAR(1000) DEFAULT '',
    size INTEGER DEFAULT 0,
    status VARCHAR(16) DEFAULT 'UNSTART',
    progress DOUBLE DEFAULT 0,
    message VARCHAR(1000) DEFAULT '',
    chunk_count INTEGER DEFAULT 0,
    meta LONGTEXT,
    enabled INTEGER DEFAULT 1,
    created_at VARCHAR(32), updated_at VARCHAR(32)
);
CREATE INDEX IF NOT EXISTS idx_documents_dataset ON documents(dataset_id);
CREATE TABLE IF NOT EXISTS chunks (
    id VARCHAR(64) PRIMARY KEY,
    dataset_id VARCHAR(64) NOT NULL,
    document_id VARCHAR(64) NOT NULL,
    seq INTEGER DEFAULT 0,
    content LONGTEXT NOT NULL,
    tokens LONGTEXT,
    keywords VARCHAR(1000) DEFAULT '[]',
    embedding LONGBLOB,
    dim INTEGER DEFAULT 0,
    available INTEGER DEFAULT 1,
    created_at VARCHAR(32)
);
CREATE INDEX IF NOT EXISTS idx_chunks_dataset ON chunks(dataset_id);
CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);
CREATE TABLE IF NOT EXISTS graph_nodes (
    dataset_id VARCHAR(64) NOT NULL,
    id VARCHAR(255) NOT NULL,
    name VARCHAR(255) NOT NULL,
    weight DOUBLE DEFAULT 0,
    category VARCHAR(32) DEFAULT '术语',
    PRIMARY KEY (dataset_id, id)
);
CREATE TABLE IF NOT EXISTS graph_edges (
    dataset_id VARCHAR(64) NOT NULL,
    source VARCHAR(255) NOT NULL,
    target VARCHAR(255) NOT NULL,
    relation VARCHAR(32) DEFAULT '共现',
    weight DOUBLE DEFAULT 0,
    PRIMARY KEY (dataset_id, source, target)
);
CREATE TABLE IF NOT EXISTS anno_tasks (
    id VARCHAR(64) PRIMARY KEY, name VARCHAR(255), dataset_id VARCHAR(64), note VARCHAR(1000) DEFAULT '',
    created_by VARCHAR(64), created_at VARCHAR(32)
);
CREATE TABLE IF NOT EXISTS anno_items (
    id VARCHAR(64) PRIMARY KEY, task_id VARCHAR(64) NOT NULL, chunk_id VARCHAR(64), document_id VARCHAR(64),
    doc_name VARCHAR(255), dataset_id VARCHAR(64), content LONGTEXT, status VARCHAR(16) DEFAULT 'pending',
    labels VARCHAR(4000) DEFAULT '{}', comment VARCHAR(2000) DEFAULT '', annotator VARCHAR(64), updated_at VARCHAR(32)
);
CREATE INDEX IF NOT EXISTS idx_anno_task ON anno_items(task_id, status);
CREATE TABLE IF NOT EXISTS workflows (
    id VARCHAR(64) PRIMARY KEY, name VARCHAR(255), mode VARCHAR(32) DEFAULT 'workflow', note VARCHAR(1000) DEFAULT '',
    dsl LONGTEXT, created_at VARCHAR(32), updated_at VARCHAR(32)
);
CREATE TABLE IF NOT EXISTS runs (
    id VARCHAR(64) PRIMARY KEY, workflow_id VARCHAR(64), status VARCHAR(32), inputs LONGTEXT, outputs LONGTEXT,
    events LONGTEXT, error VARCHAR(2000) DEFAULT '', elapsed_ms INTEGER DEFAULT 0, created_at VARCHAR(32)
);
-- 模型提供商：一条记录 = 一份接入配置（厂商 + base_url + api_key + 模型名），不按角色划分。
-- role 列是历史遗留（旧库迁移用），新记录一律写空串，取值请走 model_roles 绑定。
CREATE TABLE IF NOT EXISTS providers (
    id VARCHAR(64) PRIMARY KEY, role VARCHAR(32) DEFAULT '', provider VARCHAR(64), name VARCHAR(255), base_url VARCHAR(500),
    api_key VARCHAR(500), extra VARCHAR(4000) DEFAULT '{}', enabled INTEGER DEFAULT 1,
    created_at VARCHAR(32), updated_at VARCHAR(32)
);
-- 模型角色 → 提供商绑定；角色级参数（chat.max_tokens / embedding.dim）放 extra
CREATE TABLE IF NOT EXISTS model_roles (
    role VARCHAR(32) PRIMARY KEY, provider_id VARCHAR(64) DEFAULT '',
    extra VARCHAR(4000) DEFAULT '{}', updated_at VARCHAR(32)
);
CREATE TABLE IF NOT EXISTS embed_cache (
    hash VARCHAR(64) PRIMARY KEY, model VARCHAR(128), dim INTEGER, vector LONGBLOB
);
CREATE TABLE IF NOT EXISTS conversations (
    id VARCHAR(64) PRIMARY KEY, name VARCHAR(255), user VARCHAR(64), created_at VARCHAR(32), updated_at VARCHAR(32)
);
CREATE TABLE IF NOT EXISTS messages (
    id VARCHAR(64) PRIMARY KEY, conversation_id VARCHAR(64), role VARCHAR(16), content LONGTEXT,
    resources LONGTEXT, feedback VARCHAR(16) DEFAULT '', created_at VARCHAR(32),
    -- 主观三项（docs/01：满意度 / 有用性 / 体验感），1–5，未评价为 NULL
    us_satisfaction INTEGER, us_usefulness INTEGER, us_experience INTEGER,
    -- 客观三项（docs/01：FA 事实准确性 / KC 知识完备性 / CL 表达清晰度），自动算，可人工覆盖
    obj_fa INTEGER, obj_kc INTEGER, obj_cl INTEGER,
    obj_source VARCHAR(16) DEFAULT '',   -- auto / human
    obj_reason VARCHAR(2000) DEFAULT '', -- 判据（可追溯到命中片段）
    no_hit INTEGER DEFAULT 0,            -- 1 = 本次没检索到资料（客观三项不参与均值）
    gap_json LONGTEXT,                   -- 缺口要素
    review_by VARCHAR(64) DEFAULT '', review_at VARCHAR(32) DEFAULT '', review_note VARCHAR(1000) DEFAULT ''
);
CREATE TABLE IF NOT EXISTS gaps (
    id VARCHAR(64) PRIMARY KEY, dataset_id VARCHAR(64), question LONGTEXT, keywords VARCHAR(1000),
    missing LONGTEXT, suggestion LONGTEXT, dimension VARCHAR(8) DEFAULT 'D1',
    message_id VARCHAR(64), status VARCHAR(16) DEFAULT '待整改', owner VARCHAR(64) DEFAULT '',
    created_at VARCHAR(32), updated_at VARCHAR(32)
);
CREATE INDEX IF NOT EXISTS idx_gaps_status ON gaps(status);
-- 缺陷整改处置：覆盖「覆盖缺口(gaps)」与「人工标注(anno_items)」两类来源的整改状态
CREATE TABLE IF NOT EXISTS defect_states (
    defect_id VARCHAR(64) PRIMARY KEY, status VARCHAR(16) DEFAULT '待整改',
    owner VARCHAR(64) DEFAULT '', due_date VARCHAR(32) DEFAULT '', updated_at VARCHAR(32)
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);
CREATE TABLE IF NOT EXISTS receipts (
    id VARCHAR(64) PRIMARY KEY, conversation_id VARCHAR(64), message_id VARCHAR(64), score DOUBLE,
    comment VARCHAR(2000), created_at VARCHAR(32)
);
CREATE TABLE IF NOT EXISTS memories (
    id VARCHAR(64) PRIMARY KEY, scope VARCHAR(16) DEFAULT 'global', project VARCHAR(128) DEFAULT '',
    title VARCHAR(255) DEFAULT '', content LONGTEXT, tags VARCHAR(500) DEFAULT '',
    enabled INTEGER DEFAULT 1, source VARCHAR(16) DEFAULT 'manual',
    created_at VARCHAR(32), updated_at VARCHAR(32)
);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope, project);
"""

_lock = threading.Lock()


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def new_id(prefix: str = "") -> str:
    return (prefix + uuid.uuid4().hex)[:32]


class SqlConnection:
    """跨后端连接包装：把 SQLite 写法的 SQL 适配到 MySQL，业务代码零改动。

    适配项：
      * 占位符 ? → %s；
      * INSERT OR REPLACE → REPLACE；
      * CREATE INDEX IF NOT EXISTS → MySQL 不支持，去掉后忽略重名（1061）；
      * executescript → 按分号拆开逐条执行（MySQL 驱动不支持多语句）；
      * 行对象统一支持 row["col"] 取值（SQLite Row / pymysql DictCursor）。
    """

    def __init__(self, raw: Any, dialect: str):
        self.raw = raw
        self.dialect = dialect
        self._pending: List[str] = []

    # ---- 方言翻译
    def _translate(self, sql: str) -> str:
        if self.dialect != "mysql":
            return sql
        text = sql.replace("INSERT OR REPLACE INTO", "REPLACE INTO")
        text = text.replace("INSERT OR IGNORE INTO", "INSERT IGNORE INTO")
        text = text.replace("?", "%s")
        return text

    def execute(self, sql: str, params: Any = None) -> Any:
        statement = self._translate(sql)
        index_sql = statement.strip().upper().startswith("CREATE INDEX IF NOT EXISTS")
        if self.dialect == "mysql" and index_sql:
            statement = statement.replace("IF NOT EXISTS", "", 1)
            try:
                return self.raw.cursor().execute(statement, params)
            except Exception as exc:  # noqa: BLE001  1061 = 索引已存在
                if "1061" in str(exc) or "Duplicate key name" in str(exc):
                    return None
                raise
        cursor = self.raw.cursor()
        cursor.execute(statement, params) if params is not None else cursor.execute(statement)
        return cursor

    def executemany(self, sql: str, seq: Any) -> Any:
        statement = self._translate(sql)
        cursor = self.raw.cursor()
        cursor.executemany(statement, list(seq))
        return cursor

    def executescript(self, script: str) -> None:
        if self.dialect != "mysql":
            self.raw.executescript(script)
            return
        for statement in [part.strip() for part in script.split(";")]:
            if statement:
                self.execute(statement)

    def commit(self) -> None:
        self.raw.commit()

    def close(self) -> None:
        try:
            self.raw.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "SqlConnection":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is None:
            self.raw.commit()
        else:
            try:
                self.raw.rollback()
            except Exception:  # noqa: BLE001
                pass


class Store:
    """产品数据面。线程内复用连接，跨线程各用各的（FastAPI 线程池）。"""

    def __init__(self, path: Optional[Path] = None, backend: str = "sqlite",
                 mysql: Optional[Dict[str, Any]] = None):
        self.backend = (backend or "sqlite").lower()
        self.mysql = mysql or {}
        self.path = Path(path or DEFAULT_DB)
        if self.backend == "sqlite":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)
            conn.commit()

    #: 后加的列（旧库升级用）：CREATE TABLE IF NOT EXISTS 不会补列，必须显式 ALTER
    ADDED_COLUMNS = {
        "documents": [("enabled", "INTEGER DEFAULT 1")],
        "graph_nodes": [("category", "VARCHAR(32) DEFAULT '术语'"),
                        ("community", "VARCHAR(8) DEFAULT ''")],
        "messages": [
            ("us_satisfaction", "INTEGER"), ("us_usefulness", "INTEGER"), ("us_experience", "INTEGER"),
            ("obj_fa", "INTEGER"), ("obj_kc", "INTEGER"), ("obj_cl", "INTEGER"),
            ("obj_source", "VARCHAR(16) DEFAULT ''"), ("obj_reason", "VARCHAR(2000) DEFAULT ''"),
            ("no_hit", "INTEGER DEFAULT 0"), ("gap_json", "LONGTEXT"),
            ("review_by", "VARCHAR(64) DEFAULT ''"), ("review_at", "VARCHAR(32) DEFAULT ''"),
            ("review_note", "VARCHAR(1000) DEFAULT ''"),
        ],
    }

    def _columns(self, conn: SqlConnection, table: str) -> set:
        """取表的列名（两种后端统一）。"""
        if conn.dialect == "mysql":
            rows = conn.execute(
                "SELECT COLUMN_NAME AS name FROM information_schema.columns"
                " WHERE table_schema = DATABASE() AND table_name = %s", (table,)).fetchall()
        else:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {row["name"] for row in rows}

    def _migrate(self, conn: SqlConnection) -> None:
        """就地升级旧库：缺失的列自动补上（数据不丢，重启即生效）。"""
        for table, columns in self.ADDED_COLUMNS.items():
            existing = self._columns(conn, table)
            if not existing:
                continue
            for name, ddl in columns:
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        self._migrate_providers(conn)

    def _migrate_providers(self, conn: SqlConnection) -> None:
        """providers 独立化：旧库以 role 为记录维度，升级为「提供商记录 + 角色绑定」。

        每个旧角色各绑定到它原来的那条记录（与旧 resolve 取首条的行为一致），
        角色级参数（max_tokens/dim）随 extra 一起搬到绑定上，随后清空 role 列。
        """
        if "role" not in self._columns(conn, "providers"):
            return
        rows = conn.execute(
            "SELECT id, role, extra FROM providers WHERE role IS NOT NULL AND role <> ''"
        ).fetchall()
        if not rows:
            return
        for row in rows:
            if conn.execute("SELECT role FROM model_roles WHERE role = ?", (row["role"],)).fetchone():
                continue
            conn.execute(
                "INSERT INTO model_roles (role, provider_id, extra, updated_at) VALUES (?,?,?,?)",
                (row["role"], row["id"], row["extra"] or "{}", now()),
            )
        conn.execute("UPDATE providers SET role = '' WHERE role IS NOT NULL AND role <> ''")

    def connect(self) -> SqlConnection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            if self.backend == "mysql":
                import pymysql
                import pymysql.cursors
                raw = pymysql.connect(
                    host=self.mysql.get("host", "127.0.0.1"),
                    port=int(self.mysql.get("port", 3306)),
                    user=self.mysql.get("user", "root"),
                    password=self.mysql.get("password", ""),
                    database=self.mysql.get("database", "equipment_governance"),
                    charset="utf8mb4",
                    cursorclass=pymysql.cursors.DictCursor,
                    autocommit=False,
                    connect_timeout=15,
                )
                conn = SqlConnection(raw, "mysql")
            else:
                raw = sqlite3.connect(self.path, timeout=30)
                raw.row_factory = sqlite3.Row
                raw.execute("PRAGMA journal_mode=WAL")
                conn = SqlConnection(raw, "sqlite")
            self._local.conn = conn
        return conn

    def describe(self) -> Dict[str, Any]:
        """数据面自述（页面/自检展示用）。"""
        if self.backend == "mysql":
            return {"backend": "mysql",
                    "target": f"{self.mysql.get('host')}:{self.mysql.get('port')}/{self.mysql.get('database')}",
                    "user": self.mysql.get("user")}
        return {"backend": "sqlite", "target": str(self.path)}

    # ------------------------------------------------------------ 数据集
    def create_dataset(self, name: str, description: str = "", chunk_method: str = "naive",
                       parser_config: Optional[Dict[str, Any]] = None,
                       embedding_model: str = "") -> Dict[str, Any]:
        dataset_id = new_id("ds")
        stamp = now()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO datasets (id,name,description,chunk_method,parser_config,embedding_model,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (dataset_id, name, description, chunk_method, json.dumps(parser_config or {}, ensure_ascii=False),
                 embedding_model, stamp, stamp),
            )
            conn.commit()
        return self.get_dataset(dataset_id) or {}

    def get_dataset(self, dataset_id: str) -> Optional[Dict[str, Any]]:
        row = self.connect().execute("SELECT * FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
        return self._dataset_row(row) if row else None

    def list_datasets(self, name: str = "") -> List[Dict[str, Any]]:
        sql = "SELECT * FROM datasets"
        params: List[Any] = []
        if name:
            sql += " WHERE name LIKE ?"
            params.append(f"%{name}%")
        sql += " ORDER BY created_at DESC"
        return [self._dataset_row(row) for row in self.connect().execute(sql, params).fetchall()]

    def _dataset_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["parser_config"] = json.loads(item.get("parser_config") or "{}")
        counts = self.connect().execute(
            "SELECT (SELECT COUNT(*) FROM documents WHERE dataset_id = ?) AS document_count,"
            " (SELECT COUNT(*) FROM chunks WHERE dataset_id = ? AND available = 1) AS chunk_count",
            (row["id"], row["id"]),
        ).fetchone()
        item["document_count"] = int(counts["document_count"] or 0)
        item["chunk_count"] = int(counts["chunk_count"] or 0)
        return item

    def update_dataset(self, dataset_id: str, patch: Dict[str, Any]) -> bool:
        fields: List[str] = []
        values: List[Any] = []
        for key in ("name", "description", "chunk_method", "embedding_model"):
            if key in patch:
                fields.append(f"{key} = ?")
                values.append(patch[key])
        if "parser_config" in patch:
            fields.append("parser_config = ?")
            values.append(json.dumps(patch["parser_config"], ensure_ascii=False))
        if not fields:
            return False
        fields.append("updated_at = ?")
        values.extend([now(), dataset_id])
        with self.connect() as conn:
            conn.execute(f"UPDATE datasets SET {', '.join(fields)} WHERE id = ?", values)
            conn.commit()
        return True

    def delete_datasets(self, ids: Iterable[str]) -> int:
        ids = list(ids)
        with self.connect() as conn:
            for dataset_id in ids:
                # 缺陷来源（覆盖缺口 gaps / 人工标注 anno_items）随库删除，
                # 并清掉它们的整改处置记录（defect_states 仅按 defect_id 关联，必要时先取 id）
                defect_ids = [r[0] for r in conn.execute(
                    "SELECT id FROM gaps WHERE dataset_id = ?", (dataset_id,)).fetchall()]
                defect_ids += [r[0] for r in conn.execute(
                    "SELECT id FROM anno_items WHERE dataset_id = ?", (dataset_id,)).fetchall()]
                for defect_id in defect_ids:
                    conn.execute("DELETE FROM defect_states WHERE defect_id = ?", (defect_id,))
                conn.execute("DELETE FROM gaps WHERE dataset_id = ?", (dataset_id,))
                conn.execute("DELETE FROM chunks WHERE dataset_id = ?", (dataset_id,))
                conn.execute("DELETE FROM documents WHERE dataset_id = ?", (dataset_id,))
                conn.execute("DELETE FROM graph_nodes WHERE dataset_id = ?", (dataset_id,))
                conn.execute("DELETE FROM graph_edges WHERE dataset_id = ?", (dataset_id,))
                conn.execute("DELETE FROM anno_items WHERE dataset_id = ?", (dataset_id,))
                conn.execute("DELETE FROM datasets WHERE id = ?", (dataset_id,))
            conn.commit()
        return len(ids)

    # ------------------------------------------------------------ 文档
    def add_document(self, dataset_id: str, name: str, stored_path: str, size: int) -> Dict[str, Any]:
        doc_id = new_id("doc")
        stamp = now()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO documents (id,dataset_id,name,stored_path,size,status,progress,message,created_at,updated_at)"
                " VALUES (?,?,?,?,?,'UNSTART',0,'',?,?)",
                (doc_id, dataset_id, name, stored_path, size, stamp, stamp),
            )
            conn.commit()
        return self.get_document(doc_id) or {}

    def get_document(self, document_id: str) -> Optional[Dict[str, Any]]:
        row = self.connect().execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
        return self._document_row(row) if row else None

    def list_documents(self, dataset_id: str, keywords: str = "") -> List[Dict[str, Any]]:
        sql = "SELECT * FROM documents WHERE dataset_id = ?"
        params: List[Any] = [dataset_id]
        if keywords:
            sql += " AND name LIKE ?"
            params.append(f"%{keywords}%")
        sql += " ORDER BY created_at DESC"
        return [self._document_row(row) for row in self.connect().execute(sql, params).fetchall()]

    def _document_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["meta_fields"] = json.loads(item.get("meta") or "{}")
        dataset = self.get_dataset(item["dataset_id"]) or {}
        item["chunk_method"] = dataset.get("chunk_method", "naive")
        item["meta"] = item["meta_fields"]
        return item

    def update_document(self, document_id: str, patch: Dict[str, Any]) -> bool:
        fields: List[str] = []
        values: List[Any] = []
        for key in ("name", "status", "progress", "message", "chunk_count", "stored_path"):
            if key in patch:
                fields.append(f"{key} = ?")
                values.append(patch[key])
        if "enabled" in patch:
            fields.append("enabled = ?")
            values.append(1 if patch["enabled"] else 0)
        if "meta_fields" in patch:
            fields.append("meta = ?")
            values.append(json.dumps(patch["meta_fields"], ensure_ascii=False))
        if not fields:
            return False
        fields.append("updated_at = ?")
        values.extend([now(), document_id])
        with self.connect() as conn:
            conn.execute(f"UPDATE documents SET {', '.join(fields)} WHERE id = ?", values)
            conn.commit()
        return True

    def delete_documents(self, ids: Iterable[str]) -> int:
        ids = list(ids)
        with self.connect() as conn:
            for document_id in ids:
                conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
                conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
            conn.commit()
        return len(ids)

    # ------------------------------------------------------------ 切片与向量
    def replace_chunks(self, dataset_id: str, document_id: str, chunks: List[Dict[str, Any]]) -> int:
        with self.connect() as conn:
            conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
            stamp = now()
            for seq, chunk in enumerate(chunks):
                vector = chunk.get("embedding")
                blob = np.asarray(vector, dtype=np.float32).tobytes() if vector else None
                conn.execute(
                    "INSERT INTO chunks (id,dataset_id,document_id,seq,content,tokens,keywords,embedding,dim,available,created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,1,?)",
                    (chunk.get("id") or new_id("ck"), dataset_id, document_id, seq, chunk["content"],
                     chunk.get("tokens") or "", json.dumps(chunk.get("keywords") or [], ensure_ascii=False),
                     blob, len(vector) if vector else 0, stamp),
                )
            conn.execute("UPDATE documents SET chunk_count = ?, updated_at = ? WHERE id = ?",
                          (len(chunks), stamp, document_id))
            conn.commit()
        return len(chunks)

    def list_chunks(self, dataset_id: str = "", document_id: str = "", keywords: str = "",
                    limit: int = 200, offset: int = 0) -> List[Dict[str, Any]]:
        sql = "SELECT id,dataset_id,document_id,seq,content,tokens,keywords,available,created_at FROM chunks WHERE 1=1"
        params: List[Any] = []
        if dataset_id:
            sql += " AND dataset_id = ?"
            params.append(dataset_id)
        if document_id:
            sql += " AND document_id = ?"
            params.append(document_id)
        if keywords:
            sql += " AND content LIKE ?"
            params.append(f"%{keywords}%")
        sql += " ORDER BY seq LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = self.connect().execute(sql, params).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["important_keywords"] = json.loads(item.pop("keywords") or "[]")
            item["available"] = bool(item["available"])
            out.append(item)
        return out

    def get_chunk(self, chunk_id: str) -> Optional[Dict[str, Any]]:
        row = self.connect().execute("SELECT * FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["important_keywords"] = json.loads(item.pop("keywords") or "[]")
        return item

    def add_chunk(self, dataset_id: str, document_id: str, content: str, embedding: Optional[List[float]],
                  keywords: Optional[List[str]] = None, tokens: str = "") -> Dict[str, Any]:
        chunk_id = new_id("ck")
        seq_row = self.connect().execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 AS next FROM chunks WHERE document_id = ?", (document_id,)
        ).fetchone()
        blob = np.asarray(embedding, dtype=np.float32).tobytes() if embedding else None
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO chunks (id,dataset_id,document_id,seq,content,tokens,keywords,embedding,dim,available,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,1,?)",
                (chunk_id, dataset_id, document_id, int(seq_row["next"] or 0), content, tokens,
                 json.dumps(keywords or [], ensure_ascii=False), blob, len(embedding) if embedding else 0, now()),
            )
            conn.execute("UPDATE documents SET chunk_count = chunk_count + 1, updated_at = ? WHERE id = ?",
                          (now(), document_id))
            conn.commit()
        return self.get_chunk(chunk_id) or {}

    def update_chunk(self, chunk_id: str, patch: Dict[str, Any]) -> bool:
        fields: List[str] = []
        values: List[Any] = []
        if "content" in patch:
            fields.append("content = ?")
            values.append(patch["content"])
        if "available" in patch:
            fields.append("available = ?")
            values.append(1 if patch["available"] else 0)
        if "important_keywords" in patch:
            fields.append("keywords = ?")
            values.append(json.dumps(patch["important_keywords"], ensure_ascii=False))
        if "embedding" in patch and patch["embedding"] is not None:
            fields.append("embedding = ?")
            values.append(np.asarray(patch["embedding"], dtype=np.float32).tobytes())
            fields.append("dim = ?")
            values.append(len(patch["embedding"]))
        if not fields:
            return False
        values.append(chunk_id)
        with self.connect() as conn:
            conn.execute(f"UPDATE chunks SET {', '.join(fields)} WHERE id = ?", values)
            conn.commit()
        return True

    def delete_chunks(self, ids: Iterable[str]) -> int:
        ids = list(ids)
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT document_id, COUNT(*) AS n FROM chunks WHERE id IN ({placeholders}) GROUP BY document_id",
                ids,
            ).fetchall()
            for chunk_id in ids:
                conn.execute("DELETE FROM chunks WHERE id = ?", (chunk_id,))
            for row in rows:
                conn.execute(
                    "UPDATE documents SET chunk_count = MAX(chunk_count - ?, 0), updated_at = ? WHERE id = ?",
                    (int(row["n"]), now(), row["document_id"]),
                )
            conn.commit()
        return len(ids)

    def vectors(self, dataset_ids: List[str]) -> List[Dict[str, Any]]:
        """把指定数据集的可用切片连同向量载入内存（检索用）。"""
        if not dataset_ids:
            return []
        placeholders = ",".join("?" for _ in dataset_ids)
        rows = self.connect().execute(
            f"SELECT c.id, c.dataset_id, c.document_id, c.content, c.tokens, c.keywords, c.embedding, c.dim,"
            f" d.name AS doc_name, d.meta AS doc_meta FROM chunks c JOIN documents d ON d.id = c.document_id"
            f" WHERE c.dataset_id IN ({placeholders}) AND c.available = 1"
            f" AND d.enabled = 1 AND c.embedding IS NOT NULL",
            dataset_ids,
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["vector"] = np.frombuffer(item.pop("embedding"), dtype=np.float32)
            item["meta"] = json.loads(item.pop("doc_meta") or "{}")
            item["keywords"] = json.loads(item.pop("keywords") or "[]")
            out.append(item)
        return out

    # ------------------------------------------------------------ 图谱
    def replace_graph(self, dataset_id: str, nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM graph_nodes WHERE dataset_id = ?", (dataset_id,))
            conn.execute("DELETE FROM graph_edges WHERE dataset_id = ?", (dataset_id,))
            conn.executemany(
                "INSERT OR REPLACE INTO graph_nodes (dataset_id,id,name,weight,category,community)"
                " VALUES (?,?,?,?,?,?)",
                [(dataset_id, n["id"], n["name"], float(n.get("weight") or 0),
                  n.get("category") or "术语", n.get("community") or "") for n in nodes],
            )
            conn.executemany(
                "INSERT OR REPLACE INTO graph_edges (dataset_id,source,target,relation,weight) VALUES (?,?,?,?,?)",
                [(dataset_id, e["source"], e["target"], e.get("relation") or "共现", float(e.get("weight") or 0))
                 for e in edges],
            )
            conn.commit()

    def graph_counts(self, dataset_id: str) -> Dict[str, int]:
        """图谱规模（不带 LIMIT）。

        界面上的"实体/关系"数字必须走这里：get_graph 为了安全带了 LIMIT 256/128，
        拿它的数组长度当规模会少报（实测治理台显示"关系 128"、图谱页显示"关系 280"）。
        """
        conn = self.connect()
        nodes = conn.execute("SELECT COUNT(*) AS n FROM graph_nodes WHERE dataset_id = ?",
                             (dataset_id,)).fetchone()
        edges = conn.execute("SELECT COUNT(*) AS n FROM graph_edges WHERE dataset_id = ?",
                             (dataset_id,)).fetchone()
        return {"nodes": int(nodes["n"] if nodes else 0), "edges": int(edges["n"] if edges else 0)}

    def get_graph(self, dataset_id: str) -> Dict[str, Any]:
        nodes = [dict(r) for r in self.connect().execute(
            "SELECT id,name,weight,category,community FROM graph_nodes WHERE dataset_id = ?"
            " ORDER BY weight DESC LIMIT 256",
            (dataset_id,)).fetchall()]
        edges = [dict(r) for r in self.connect().execute(
            "SELECT source,target,relation,weight FROM graph_edges WHERE dataset_id = ? ORDER BY weight DESC LIMIT 128",
            (dataset_id,)).fetchall()]
        return {"nodes": nodes, "edges": edges}

    # ------------------------------------------------------------ 标注
    def create_anno_task(self, name: str, dataset_id: str, note: str, created_by: str) -> Dict[str, Any]:
        task_id = new_id("at")
        with self.connect() as conn:
            conn.execute("INSERT INTO anno_tasks (id,name,dataset_id,note,created_by,created_at) VALUES (?,?,?,?,?,?)",
                         (task_id, name, dataset_id, note, created_by, now()))
            conn.commit()
        return self.get_anno_task(task_id) or {}

    def get_anno_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        row = self.connect().execute("SELECT * FROM anno_tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item.update(self.anno_progress(task_id))
        return item

    def list_anno_tasks(self) -> List[Dict[str, Any]]:
        rows = self.connect().execute("SELECT * FROM anno_tasks ORDER BY created_at DESC").fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item.update(self.anno_progress(row["id"]))
            out.append(item)
        return out

    def anno_progress(self, task_id: str) -> Dict[str, Any]:
        row = self.connect().execute(
            "SELECT COUNT(*) AS total, SUM(status = 'done') AS done, SUM(status = 'pending') AS pending,"
            " SUM(status = 'skipped') AS skipped FROM anno_items WHERE task_id = ?", (task_id,)).fetchone()
        total = int(row["total"] or 0)
        done = int(row["done"] or 0)
        return {"total": total, "done": done, "pending": int(row["pending"] or 0),
                "skipped": int(row["skipped"] or 0), "progress": (done / total) if total else 0.0}

    def seed_anno_items(self, task_id: str, dataset_id: str, limit: int = 20) -> int:
        existing = {r["chunk_id"] for r in self.connect().execute(
            "SELECT chunk_id FROM anno_items WHERE task_id = ?", (task_id,)).fetchall()}
        rows = self.connect().execute(
            "SELECT c.id, c.document_id, c.content, d.name AS doc_name FROM chunks c"
            " JOIN documents d ON d.id = c.document_id WHERE c.dataset_id = ? AND c.available = 1"
            " ORDER BY c.document_id, c.seq LIMIT ?", (dataset_id, max(1, limit * 4))).fetchall()
        added = 0
        with self.connect() as conn:
            for row in rows:
                if added >= limit:
                    break
                if row["id"] in existing:
                    continue
                conn.execute(
                    "INSERT INTO anno_items (id,task_id,chunk_id,document_id,doc_name,dataset_id,content)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (new_id("ai"), task_id, row["id"], row["document_id"], row["doc_name"], dataset_id,
                     row["content"]),
                )
                added += 1
            conn.commit()
        return added

    def list_anno_items(self, task_id: str, status: str = "", limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        # 关联 documents 取 meta，补齐前端契约字段（ref_type/ref_id/doc_id/doc_no/version）
        sql = ("SELECT a.*, d.meta AS doc_meta FROM anno_items a"
               " LEFT JOIN documents d ON d.id = a.document_id WHERE a.task_id = ?")
        params: List[Any] = [task_id]
        if status:
            sql += " AND a.status = ?"
            params.append(status)
        # rowid 是 SQLite 专有列，MySQL 没有；改用稳定主键排序（两种后端通用）
        sql += " ORDER BY a.doc_name, a.id LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        out = []
        for row in self.connect().execute(sql, params).fetchall():
            item = dict(row)
            meta = json.loads(item.pop("doc_meta") or "{}")
            item["labels"] = json.loads(item.get("labels") or "{}")
            item["ref_type"] = "chunk"
            item["ref_id"] = item.get("chunk_id") or ""
            item["doc_id"] = item.get("document_id") or ""
            item["doc_no"] = meta.get("doc_no") or ""
            item["version"] = meta.get("version") or ""
            out.append(item)
        return out

    def save_anno_item(self, item_id: str, status: str, labels: Dict[str, Any], comment: str,
                       annotator: str) -> bool:
        with self.connect() as conn:
            conn.execute(
                "UPDATE anno_items SET status = ?, labels = ?, comment = ?, annotator = ?, updated_at = ? WHERE id = ?",
                (status, json.dumps(labels, ensure_ascii=False), comment, annotator, now(), item_id),
            )
            conn.commit()
        return True

    def anno_stats(self, task_id: str) -> Dict[str, Any]:
        dims: Dict[str, int] = {}
        completeness: Dict[str, int] = {}
        compliance: Dict[str, int] = {}
        severity: Dict[str, int] = {}
        for row in self.connect().execute("SELECT labels FROM anno_items WHERE task_id = ?", (task_id,)).fetchall():
            labels = json.loads(row["labels"] or "{}")
            for code in labels.get("defect_dimensions") or []:
                dims[code] = dims.get(code, 0) + 1
            for key, sink in (("completeness", completeness), ("compliance", compliance), ("severity", severity)):
                value = labels.get(key)
                if value:
                    sink[value] = sink.get(value, 0) + 1
        return {"progress": self.anno_progress(task_id), "defect_dimensions": dims,
                "completeness": completeness, "compliance": compliance, "severity": severity}

    def anno_export_rows(self, task_id: str, only_done: bool = True) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM anno_items WHERE task_id = ?"
        if only_done:
            sql += " AND status = 'done'"
        out = []
        for row in self.connect().execute(sql, (task_id,)).fetchall():
            item = dict(row)
            item["labels"] = json.loads(item.get("labels") or "{}")
            out.append(item)
        return out

    def delete_anno_task(self, task_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM anno_items WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM anno_tasks WHERE id = ?", (task_id,))
            conn.commit()

    # ------------------------------------------------------------ 工作流与运行
    def save_workflow(self, name: str, dsl: str, mode: str = "workflow", note: str = "",
                      workflow_id: str = "") -> Dict[str, Any]:
        stamp = now()
        with self.connect() as conn:
            if workflow_id:
                conn.execute("UPDATE workflows SET name = ?, dsl = ?, mode = ?, note = ?, updated_at = ? WHERE id = ?",
                             (name, dsl, mode, note, stamp, workflow_id))
            else:
                workflow_id = new_id("wf")
                conn.execute(
                    "INSERT INTO workflows (id,name,mode,note,dsl,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                    (workflow_id, name, mode, note, dsl, stamp, stamp),
                )
            conn.commit()
        return self.get_workflow(workflow_id) or {}

    def get_workflow(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        row = self.connect().execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["dsl_parsed"] = json.loads(item["dsl"] or "{}")
        return item

    def list_workflows(self) -> List[Dict[str, Any]]:
        rows = self.connect().execute("SELECT * FROM workflows ORDER BY updated_at DESC").fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["dsl_parsed"] = json.loads(item["dsl"] or "{}")
            out.append(item)
        return out

    def delete_workflow(self, workflow_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM workflows WHERE id = ?", (workflow_id,))
            conn.execute("DELETE FROM runs WHERE workflow_id = ?", (workflow_id,))
            conn.commit()

    def add_run(self, workflow_id: str, status: str, inputs: Dict[str, Any], outputs: Dict[str, Any],
                events: List[Dict[str, Any]], error: str, elapsed_ms: int) -> Dict[str, Any]:
        run_id = new_id("run")
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO runs (id,workflow_id,status,inputs,outputs,events,error,elapsed_ms,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, workflow_id, status, json.dumps(inputs, ensure_ascii=False),
                 json.dumps(outputs, ensure_ascii=False), json.dumps(events[-60:], ensure_ascii=False),
                 error, elapsed_ms, now()),
            )
            conn.commit()
        return {"id": run_id, "status": status, "outputs": outputs, "error": error, "elapsed_ms": elapsed_ms,
                "events": events[-60:]}

    def list_runs(self, workflow_id: str = "", limit: int = 20) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM runs"
        params: List[Any] = []
        if workflow_id:
            sql += " WHERE workflow_id = ?"
            params.append(workflow_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        out = []
        for row in self.connect().execute(sql, params).fetchall():
            item = dict(row)
            for key in ("inputs", "outputs", "events"):
                try:
                    item[key] = json.loads(item[key] or "null")
                except ValueError:
                    item[key] = None
            out.append(item)
        return out

    # ------------------------------------------------------------ 模型提供商
    def upsert_provider(self, provider: str, name: str, base_url: str, api_key: str,
                        extra: Optional[Dict[str, Any]] = None, provider_id: str = "") -> Dict[str, Any]:
        """新建 / 更新一条模型提供商接入配置（不含角色）。"""
        stamp = now()
        with self.connect() as conn:
            if provider_id:
                current = conn.execute("SELECT api_key FROM providers WHERE id = ?", (provider_id,)).fetchone()
                # 前端加载时 api_key 一律留空，保存时若未重新填写则沿用旧密钥，避免误清空
                effective_key = api_key or (current["api_key"] if current else "") or ""
                conn.execute(
                    "UPDATE providers SET provider=?, name=?, base_url=?, api_key=?, extra=?, updated_at=? WHERE id=?",
                    (provider, name, base_url, effective_key, json.dumps(extra or {}, ensure_ascii=False), stamp, provider_id),
                )
            else:
                existing = conn.execute(
                    "SELECT id, api_key FROM providers WHERE provider = ? AND name = ?", (provider, name)
                ).fetchone()
                if existing:
                    provider_id = existing["id"]
                    effective_key = api_key or (existing["api_key"] or "")
                    conn.execute(
                        "UPDATE providers SET base_url=?, api_key=?, extra=?, updated_at=? WHERE id=?",
                        (base_url, effective_key, json.dumps(extra or {}, ensure_ascii=False), stamp, provider_id),
                    )
                else:
                    provider_id = new_id("pv")
                    conn.execute(
                        "INSERT INTO providers (id,role,provider,name,base_url,api_key,extra,enabled,created_at,updated_at)"
                        " VALUES (?,?,?,?,?,?,?,1,?,?)",
                        (provider_id, "", provider, name, base_url, api_key,
                         json.dumps(extra or {}, ensure_ascii=False), stamp, stamp),
                    )
            conn.commit()
        return self.get_provider(provider_id) or {}

    def get_provider(self, provider_id: str) -> Optional[Dict[str, Any]]:
        if not provider_id:
            return None
        row = self.connect().execute("SELECT * FROM providers WHERE id = ?", (provider_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["extra"] = json.loads(item.get("extra") or "{}")
        return item

    def list_providers(self) -> List[Dict[str, Any]]:
        out = []
        for row in self.connect().execute("SELECT * FROM providers ORDER BY provider, name").fetchall():
            item = dict(row)
            item["extra"] = json.loads(item.get("extra") or "{}")
            out.append(item)
        return out

    def delete_provider(self, provider_id: str) -> None:
        """删除接入配置，并解除引用它的角色绑定（角色回到配置文件默认值）。"""
        with self.connect() as conn:
            conn.execute("DELETE FROM providers WHERE id = ?", (provider_id,))
            conn.execute("UPDATE model_roles SET provider_id = '', updated_at = ? WHERE provider_id = ?",
                         (now(), provider_id))
            conn.commit()

    # ------------------------------------------------------------ 模型角色绑定
    def get_role_binding(self, role: str) -> Optional[Dict[str, Any]]:
        row = self.connect().execute("SELECT * FROM model_roles WHERE role = ?", (role,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["extra"] = json.loads(item.get("extra") or "{}")
        return item

    def list_role_bindings(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for row in self.connect().execute("SELECT * FROM model_roles ORDER BY role").fetchall():
            item = dict(row)
            item["extra"] = json.loads(item.get("extra") or "{}")
            out[item["role"]] = item
        return out

    def set_role_binding(self, role: str, provider_id: str,
                         extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """把角色绑定到某条提供商记录；extra 存角色级参数（max_tokens / dim）。"""
        stamp = now()
        payload = json.dumps(extra or {}, ensure_ascii=False)
        with self.connect() as conn:
            if conn.execute("SELECT role FROM model_roles WHERE role = ?", (role,)).fetchone():
                conn.execute("UPDATE model_roles SET provider_id=?, extra=?, updated_at=? WHERE role=?",
                             (provider_id, payload, stamp, role))
            else:
                conn.execute("INSERT INTO model_roles (role, provider_id, extra, updated_at) VALUES (?,?,?,?)",
                             (role, provider_id, payload, stamp))
            conn.commit()
        return self.get_role_binding(role) or {}

    # ------------------------------------------------------------ 会话与消息
    def ensure_conversation(self, conversation_id: str, user: str, question: str) -> str:
        stamp = now()
        with self.connect() as conn:
            row = conn.execute("SELECT id FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
            if row:
                conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (stamp, conversation_id))
            else:
                conn.execute(
                    "INSERT INTO conversations (id,name,user,created_at,updated_at) VALUES (?,?,?,?,?)",
                    (conversation_id, (question or "新会话")[:40], user, stamp, stamp))
            conn.commit()
        return conversation_id

    def list_conversations(self, user: str = "", limit: int = 30) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM conversations"
        params: List[Any] = []
        if user:
            sql += " WHERE user = ?"
            params.append(user)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        out = []
        for row in self.connect().execute(sql, params).fetchall():
            item = dict(row)
            count = self.connect().execute(
                "SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ?", (row["id"],)).fetchone()
            item["message_count"] = int(count["n"] or 0)
            out.append(item)
        return out

    def add_message(self, conversation_id: str, role: str, content: str,
                    resources: Optional[List[Dict[str, Any]]] = None, message_id: str = "") -> Dict[str, Any]:
        message_id = message_id or new_id("msg")
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO messages (id,conversation_id,role,content,resources,created_at) VALUES (?,?,?,?,?,?)",
                (message_id, conversation_id, role, content, json.dumps(resources or [], ensure_ascii=False), now()),
            )
            conn.commit()
        return {"id": message_id}

    def list_messages(self, conversation_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self.connect().execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at LIMIT ?",
            (conversation_id, limit)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["resources"] = json.loads(item.get("resources") or "[]")
            out.append(item)
        return out

    # ------------------------------------------------------------ 评价（3 主观 + 3 客观）
    def save_subjective(self, message_id: str, satisfaction: Optional[int], usefulness: Optional[int],
                        experience: Optional[int], comment: str = "") -> bool:
        """主观三项落库（docs/01：US 缺失不补默认值，这里允许 NULL）。"""
        with self.connect() as conn:
            conn.execute(
                "UPDATE messages SET us_satisfaction = ?, us_usefulness = ?, us_experience = ? WHERE id = ?",
                (satisfaction, usefulness, experience, message_id),
            )
            conn.commit()
        if comment:
            self.add_receipt("", message_id, float(satisfaction or 0), comment)
        return True

    def save_objective(self, message_id: str, fa: Optional[int], kc: Optional[int], cl: Optional[int],
                       no_hit: bool, reason: str, source: str = "auto",
                       gaps: Optional[List[Dict[str, Any]]] = None) -> bool:
        """客观三项落库；no_hit=1 表示本次没检索到资料（三项不参与均值）。"""
        with self.connect() as conn:
            conn.execute(
                "UPDATE messages SET obj_fa = ?, obj_kc = ?, obj_cl = ?, obj_source = ?, obj_reason = ?,"
                " no_hit = ?, gap_json = ? WHERE id = ?",
                (fa, kc, cl, source, reason[:2000], 1 if no_hit else 0,
                 json.dumps(gaps or [], ensure_ascii=False), message_id),
            )
            conn.commit()
        return True

    def override_objective(self, message_id: str, fa: Optional[int], kc: Optional[int], cl: Optional[int],
                           reviewer: str, note: str = "") -> bool:
        """人工复核覆盖自动客观分（ADR-D25：自动先算、人工可覆盖）。"""
        with self.connect() as conn:
            conn.execute(
                "UPDATE messages SET obj_fa = ?, obj_kc = ?, obj_cl = ?, obj_source = 'human',"
                " review_by = ?, review_at = ?, review_note = ? WHERE id = ?",
                (fa, kc, cl, reviewer, now(), note[:500], message_id),
            )
            conn.commit()
        return True

    def get_evaluation(self, message_id: str) -> Optional[Dict[str, Any]]:
        row = self.connect().execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["gaps"] = json.loads(item.pop("gap_json") or "[]")
        item["no_hit"] = bool(item.get("no_hit"))
        return item

    def list_evaluations(self, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self.connect().execute(
            "SELECT * FROM messages WHERE role = 'assistant' ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["gaps"] = json.loads(item.pop("gap_json") or "[]")
            item["no_hit"] = bool(item.get("no_hit"))
            out.append(item)
        return out

    def evaluation_summary(self) -> Dict[str, Any]:
        """三主观 + 三客观的汇总（未命中单独计数，不混入均值）。"""
        row = self.connect().execute(
            "SELECT COUNT(*) AS total,"
            " SUM(no_hit = 1) AS no_hit,"
            # 别名避开 MySQL 保留字（AS use 会报语法错，实测踩过）
            " AVG(CASE WHEN no_hit = 0 THEN us_satisfaction END) AS us_mean,"
            " AVG(CASE WHEN no_hit = 0 THEN us_usefulness END) AS ue_mean,"
            " AVG(CASE WHEN no_hit = 0 THEN us_experience END) AS ex_mean,"
            " AVG(CASE WHEN no_hit = 0 THEN obj_fa END) AS fa_mean,"
            " AVG(CASE WHEN no_hit = 0 THEN obj_kc END) AS kc_mean,"
            " AVG(CASE WHEN no_hit = 0 THEN obj_cl END) AS cl_mean,"
            " SUM(obj_source = 'human') AS reviewed"
            " FROM messages WHERE role = 'assistant'").fetchone()
        def avg(value: Any) -> Optional[float]:
            return round(float(value), 2) if value is not None else None
        return {
            "total": int(row["total"] or 0),
            "no_hit": int(row["no_hit"] or 0),
            "subjected": {"satisfaction": avg(row["us_mean"]), "usefulness": avg(row["ue_mean"]),
                          "experience": avg(row["ex_mean"])},
            "objective": {"fa": avg(row["fa_mean"]), "kc": avg(row["kc_mean"]),
                          "cl": avg(row["cl_mean"])},
            "reviewed": int(row["reviewed"] or 0),
        }

    # ------------------------------------------------------------ 覆盖缺口
    def add_gap(self, dataset_id: str, question: str, keywords: List[str], missing: str,
                suggestion: str, dimension: str = "D1", message_id: str = "") -> Dict[str, Any]:
        gap_id = new_id("gp")
        stamp = now()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO gaps (id,dataset_id,question,keywords,missing,suggestion,dimension,message_id,"
                " status,owner,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,'待整改','',?,?)",
                (gap_id, dataset_id, question, json.dumps(keywords, ensure_ascii=False), missing,
                 suggestion, dimension, message_id, stamp, stamp),
            )
            conn.commit()
        return {"id": gap_id, "question": question, "keywords": keywords, "missing": missing,
                "suggestion": suggestion, "dimension": dimension, "status": "待整改",
                "dataset_id": dataset_id, "created_at": stamp}

    def list_gaps(self, status: str = "", limit: int = 200) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM gaps"
        params: List[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        out = []
        for row in self.connect().execute(sql, params).fetchall():
            item = dict(row)
            item["keywords"] = json.loads(item.get("keywords") or "[]")
            out.append(item)
        return out

    def update_gap(self, gap_id: str, status: str, owner: str = "") -> bool:
        with self.connect() as conn:
            conn.execute("UPDATE gaps SET status = ?, owner = ?, updated_at = ? WHERE id = ?",
                         (status, owner, now(), gap_id))
            conn.commit()
        return True

    def defect_states(self) -> Dict[str, Dict[str, str]]:
        """缺陷整改处置表：defect_id -> {status, owner, due_date}。"""
        out: Dict[str, Dict[str, str]] = {}
        for row in self.connect().execute(
                "SELECT defect_id, status, owner, due_date FROM defect_states").fetchall():
            item = dict(row)
            out[item["defect_id"]] = {"status": item["status"] or "待整改",
                                      "owner": item["owner"] or "",
                                      "due_date": item["due_date"] or ""}
        return out

    def upsert_defect_state(self, defect_id: str, status: str, owner: str = "",
                            due_date: str = "") -> bool:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO defect_states (defect_id, status, owner, due_date, updated_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(defect_id) DO UPDATE SET status = excluded.status,"
                " owner = excluded.owner, due_date = excluded.due_date, updated_at = excluded.updated_at",
                (defect_id, status, owner, due_date, now()))
            conn.commit()
        return True

    def set_feedback(self, message_id: str, feedback: str) -> bool:
        with self.connect() as conn:
            conn.execute("UPDATE messages SET feedback = ? WHERE id = ?", (feedback, message_id))
            conn.commit()
        return True

    # ------------------------------------------------------------ 回执
    def add_receipt(self, conversation_id: str, message_id: str, score: float, comment: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO receipts (id,conversation_id,message_id,score,comment,created_at) VALUES (?,?,?,?,?,?)",
                (new_id("rc"), conversation_id, message_id, score, comment, now()),
            )
            conn.commit()

    def receipt_summary(self) -> Dict[str, Any]:
        row = self.connect().execute("SELECT COUNT(*) AS n, AVG(score) AS avg FROM receipts").fetchone()
        return {"count": int(row["n"] or 0), "mean": round(float(row["avg"] or 0), 2)}

    # ------------------------------------------------------------ 嵌入缓存
    def cached_vectors(self, hashes: List[str], model: str) -> Dict[str, List[float]]:
        if not hashes:
            return {}
        placeholders = ",".join("?" for _ in hashes)
        rows = self.connect().execute(
            f"SELECT hash, vector, dim FROM embed_cache WHERE model = ? AND hash IN ({placeholders})",
            [model, *hashes],
        ).fetchall()
        return {row["hash"]: np.frombuffer(row["vector"], dtype=np.float32).tolist() for row in rows}

    def cache_vectors(self, items: List[Dict[str, Any]], model: str) -> None:
        if not items:
            return
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO embed_cache (hash,model,dim,vector) VALUES (?,?,?,?)",
                [(item["hash"], model, len(item["vector"]),
                  np.asarray(item["vector"], dtype=np.float32).tobytes()) for item in items],
            )
            conn.commit()

    # ------------------------------------------------------------ 长期记忆
    def list_memories(self, scope: str = "", project: str = "", keyword: str = "") -> List[Dict[str, Any]]:
        sql = "SELECT * FROM memories"
        clauses: List[str] = []
        params: List[Any] = []
        if scope:
            clauses.append("scope = ?")
            params.append(scope)
        if project:
            clauses.append("project = ?")
            params.append(project)
        if keyword:
            clauses.append("(title LIKE ? OR content LIKE ? OR tags LIKE ?)")
            params.extend([f"%{keyword}%"] * 3)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY scope, updated_at DESC"
        return [self._memory_row(row) for row in self.connect().execute(sql, params).fetchall()]

    def get_memory(self, memory_id: str) -> Optional[Dict[str, Any]]:
        row = self.connect().execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return self._memory_row(row) if row else None

    def upsert_memory(self, scope: str, title: str, content: str, project: str = "",
                      tags: str = "", source: str = "manual", enabled: bool = True,
                      memory_id: str = "") -> Dict[str, Any]:
        stamp = now()
        with self.connect() as conn:
            if memory_id:
                conn.execute(
                    "UPDATE memories SET scope=?, project=?, title=?, content=?, tags=?, enabled=?,"
                    " source=?, updated_at=? WHERE id=?",
                    (scope, project, title, content, tags, 1 if enabled else 0, source, stamp, memory_id),
                )
            else:
                memory_id = new_id("mem")
                conn.execute(
                    "INSERT INTO memories (id,scope,project,title,content,tags,enabled,source,created_at,updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (memory_id, scope, project, title, content, tags, 1 if enabled else 0, source, stamp, stamp),
                )
            conn.commit()
        return self.get_memory(memory_id) or {}

    def delete_memory(self, memory_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            conn.commit()

    def memories_for_chat(self, projects: List[str], limit: int = 20,
                          max_chars: int = 4000) -> List[Dict[str, Any]]:
        """问答时自动读取：全局记忆全部生效，项目记忆仅在该项目在检索范围内时生效。"""
        rows = self.connect().execute("SELECT * FROM memories WHERE enabled = 1 ORDER BY updated_at DESC").fetchall()
        names = {str(name) for name in projects if name}
        picked: List[Dict[str, Any]] = []
        used = 0
        for row in rows:
            item = self._memory_row(row)
            if item["scope"] == "project" and item["project"] not in names:
                continue
            if len(picked) >= limit:
                break
            body = str(item["content"])[:500]
            if used + len(body) > max_chars:
                break
            used += len(body)
            picked.append({**item, "content": body})
        return picked

    def _memory_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["enabled"] = bool(item.get("enabled"))
        item["project"] = item.get("project") or ""
        item["tags"] = item.get("tags") or ""
        return item


def content_hash(model: str, text: str) -> str:
    return hashlib.sha1((model + "\u0000" + text).encode("utf-8")).hexdigest()
