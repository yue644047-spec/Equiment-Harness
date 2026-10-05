"""图数据库后端（Neo4j）：把实体与关系落到真正的图库里，SQLite 仍保留为离线兜底。

按 Neo4j 的建模惯例组织（2026-09-28 重做，判据见 docs/16 §7 D30）：

  * **标签分域**：所有实体都带 :Entity，再按语义叠加领域标签
    （:Part / :Parameter / :Code / :Document / :Version / :Term / :Abbreviation）；
  * **关系分型**：不再是一个 :RELATED 加字符串属性，而是真正的类型
    （:HAS_CODE / :MEANS / :CURRENT_VERSION / :OBSOLETE / :CO_OCCURS），
    同时保留 relation 属性保存中文名供界面显示；
  * **约束与索引**：(dataset,id) 唯一约束、(dataset)/(dataset,name) 范围索引、
    name 全文索引（cjk 分词，中文实体检索走 db.index.fulltext.queryNodes）；
  * **命名空间**：Community 版不支持多库，仍用 dataset 属性隔离；额外建 (:Dataset) 节点并用
    :IN_DATASET 挂接，便于在 Neo4j Browser 里按知识库浏览；
  * 访问用官方 HTTP Query API v2 + Basic Auth，不引入额外驱动依赖。

选型与国产化适配结论见 docs/17。
"""

from __future__ import annotations

import base64
import time
from typing import Any, Dict, List, Optional

import httpx

#: Neo4j 里给动态标签/关系类型加引号用的反引号（这里用 chr 拼，避免源码里出现裸反引号）
BT = chr(96)

#: 语义类别 → 领域标签（Neo4j 标签用英文，中文类别仍存在 category 属性里）
LABEL_BY_CATEGORY: Dict[str, str] = {
    "部件": "Part",
    "参数": "Parameter",
    "代号": "Code",
    "资料": "Document",
    "版本": "Version",
    "术语": "Term",
    "缩略语": "Abbreviation",
}
#: 中文关系 → 关系类型（界面仍显示中文，图库里用类型表达语义）
TYPE_BY_RELATION: Dict[str, str] = {
    "代号": "HAS_CODE",
    "含义": "MEANS",
    "现行版本": "CURRENT_VERSION",
    "已作废": "OBSOLETE",
    "同文档": "CO_OCCURS",
    "跨资料": "CO_OCCURS",
}
DEFAULT_TYPE = "RELATED"
#: 共现边按 scope 再分，便于 Cypher 按"同文档/跨资料"过滤
SCOPE_BY_RELATION: Dict[str, str] = {"同文档": "same_document", "跨资料": "cross_document"}


class GraphDBError(RuntimeError):
    """图库调用失败（错误信息可直接展示）。"""


class Neo4jGraphDB:
    """Neo4j HTTP 客户端：Cypher 执行 + 建模 + 查询。"""

    def __init__(self, uri: str, user: str, password: str, database: str = "neo4j",
                 timeout: float = 120.0):
        self.uri = (uri or "").rstrip("/")
        self.user = user
        self.password = password
        self.database = database
        self.timeout = timeout
        self._schema_ready = False
        # 复用一条持久连接：Neo4j HTTP 单次请求每次都要重新建连 + Basic 认证，
        # 实测每条 Cypher 约 1.5s 都是连接开销；共享 Client 后同一批查询能省下大部分。
        # httpx.Client 自身线程安全，可在 FastAPI 线程池并发复用。
        self._client = httpx.Client(timeout=timeout)

    # ------------------------------------------------------------ 基础
    @property
    def enabled(self) -> bool:
        return bool(self.uri and self.user)

    def _headers(self) -> Dict[str, str]:
        token = base64.b64encode(f"{self.user}:{self.password}".encode("utf-8")).decode("ascii")
        return {"Authorization": "Basic " + token, "Content-Type": "application/json"}

    def cypher(self, statement: str, parameters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not self.enabled:
            raise GraphDBError("图数据库未配置（NEO4J_URI / NEO4J_USER）")
        url = f"{self.uri}/db/{self.database}/query/v2"
        try:
            response = self._client.post(url, headers=self._headers(),
                                         json={"statement": statement, "parameters": parameters or {}})
        except httpx.HTTPError as exc:
            raise GraphDBError(f"图数据库不可达（{self.uri}）：{exc}") from exc
        if response.status_code >= 400:
            detail = response.text[:300]
            try:
                errors = response.json().get("errors") or []
                if errors:
                    detail = errors[0].get("message") or detail
            except ValueError:
                pass
            raise GraphDBError(f"Cypher 执行失败（HTTP {response.status_code}）：{detail}")
        return response.json()

    def rows(self, statement: str, parameters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        return _rows(self.cypher(statement, parameters))

    def health(self) -> Dict[str, Any]:
        try:
            info = self._client.get(self.uri + "/", timeout=10.0).json()
        except (httpx.HTTPError, ValueError) as exc:
            return {"reachable": False, "error": str(exc)[:200], "uri": self.uri}
        return {"reachable": True, "uri": self.uri, "database": self.database,
                "version": info.get("neo4j_version"), "edition": info.get("neo4j_edition")}

    # ------------------------------------------------------------ 建模：约束与索引
    def ensure_schema(self, force: bool = False) -> None:
        """建约束与索引（幂等）。"""
        if self._schema_ready and not force:
            return
        self.cypher("CREATE CONSTRAINT entity_id IF NOT EXISTS "
                    "FOR (e:Entity) REQUIRE (e.dataset, e.id) IS UNIQUE")
        self.cypher("CREATE CONSTRAINT dataset_id IF NOT EXISTS "
                    "FOR (d:Dataset) REQUIRE d.id IS UNIQUE")
        self.cypher("CREATE INDEX entity_dataset IF NOT EXISTS FOR (e:Entity) ON (e.dataset)")
        self.cypher("CREATE INDEX entity_name IF NOT EXISTS FOR (e:Entity) ON (e.dataset, e.name)")
        for label in LABEL_BY_CATEGORY.values():
            self.cypher(f"CREATE INDEX entity_{label.lower()}_name IF NOT EXISTS "
                        f"FOR (e:{label}) ON (e.name)")
        # 全文索引：中文用 cjk 分词；若该分析器不可用则退回默认分词
        analyzer = "OPTIONS { indexConfig: { " + BT + "fulltext.analyzer" + BT + ": 'cjk' } }"
        try:
            self.cypher("CREATE FULLTEXT INDEX entity_fulltext IF NOT EXISTS FOR (e:Entity) "
                        "ON EACH [e.name] " + analyzer)
        except GraphDBError:
            self.cypher("CREATE FULLTEXT INDEX entity_fulltext IF NOT EXISTS "
                        "FOR (e:Entity) ON EACH [e.name]")
        self._schema_ready = True

    def model(self) -> Dict[str, Any]:
        """图模型自述：标签 / 关系类型 / 约束 / 索引（供门户「图模型」面板）。"""
        labels = [row.get("label") for row in self.rows("CALL db.labels() YIELD label RETURN label")]
        types = [row.get("relationshipType") for row in
                 self.rows("CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType")]
        constraints = self.rows(
            "SHOW CONSTRAINTS YIELD name, type, labelsOrTypes, properties "
            "RETURN name, type, labelsOrTypes AS labels, properties AS props")
        indexes = self.rows(
            "SHOW INDEXES YIELD name, type, labelsOrTypes AS labels, properties AS props, state "
            "WHERE type IN ['RANGE','FULLTEXT','TEXT','VECTOR'] "
            "RETURN name, type, labels, props, state")
        return {"labels": sorted(labels), "relationship_types": sorted(types),
                "constraints": constraints, "indexes": indexes,
                "label_by_category": LABEL_BY_CATEGORY, "type_by_relation": TYPE_BY_RELATION}

    # ------------------------------------------------------------ 写图
    def replace_dataset_graph(self, dataset_id: str, dataset_name: str,
                             nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]]) -> Dict[str, Any]:
        """整库替换某个知识库的图：先按 dataset 删除旧图，再按标签/关系类型分组批量写入。"""
        self.ensure_schema()
        self.cypher("MATCH (e:Entity {dataset: $dataset}) DETACH DELETE e", {"dataset": dataset_id})
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")

        if nodes:
            grouped: Dict[str, List[Dict[str, Any]]] = {}
            for node in nodes:
                category = node.get("category") or "术语"
                label = LABEL_BY_CATEGORY.get(category, "Entity")
                grouped.setdefault(label, []).append({
                    "id": node["id"], "name": node["name"], "category": category,
                    "weight": float(node.get("value") or node.get("weight") or 0),
                    "community": node.get("community") or "",
                })
            for label, rows in grouped.items():
                self.cypher(
                    "UNWIND $rows AS row "
                    f"MERGE (e:Entity:{BT}{label}{BT} {{dataset: $dataset, id: row.id}}) "
                    "SET e.name = row.name, e.category = row.category, e.weight = row.weight, "
                    "    e.community = row.community, e.dataset_name = $name",
                    {"rows": rows, "dataset": dataset_id, "name": dataset_name},
                )

        if edges:
            grouped_edges: Dict[str, List[Dict[str, Any]]] = {}
            for edge in edges:
                relation = edge.get("relation") or "共现"
                rtype = TYPE_BY_RELATION.get(relation, DEFAULT_TYPE)
                grouped_edges.setdefault(rtype, []).append({
                    "source": edge["source"], "target": edge["target"], "relation": relation,
                    "weight": float(edge.get("weight") or 1),
                    "scope": SCOPE_BY_RELATION.get(relation, "asserted"),
                })
            for rtype, rows in grouped_edges.items():
                self.cypher(
                    "UNWIND $rows AS row "
                    "MATCH (a:Entity {dataset: $dataset, id: row.source}) "
                    "MATCH (b:Entity {dataset: $dataset, id: row.target}) "
                    f"MERGE (a)-[r:{BT}{rtype}{BT} {{dataset: $dataset}}]->(b) "
                    "SET r.relation = row.relation, r.weight = row.weight, r.scope = row.scope",
                    {"rows": rows, "dataset": dataset_id},
                )

        # 知识库节点 + 挂接：在 Neo4j Browser 里可以从 (:Dataset) 直接展开整张图
        self.cypher(
            "MERGE (d:Dataset {id: $dataset}) SET d.name = $name, d.updated_at = $stamp "
            "WITH d MATCH (e:Entity {dataset: $dataset}) MERGE (e)-[:IN_DATASET]->(d)",
            {"dataset": dataset_id, "name": dataset_name, "stamp": stamp},
        )
        return self.stats(dataset_id)

    def stats(self, dataset_id: str) -> Dict[str, Any]:
        node_rows = self.rows("MATCH (e:Entity {dataset: $dataset}) RETURN count(e) AS nodes",
                              {"dataset": dataset_id})
        rel_rows = self.rows(
            "MATCH (:Entity {dataset: $dataset})-[r]->(:Entity) WHERE type(r) <> 'IN_DATASET' "
            "RETURN count(r) AS rels", {"dataset": dataset_id})
        # 最近构建时间取自 (:Dataset).updated_at（每次 replace_dataset_graph 都会盖时间戳），
        # 门户"最近构建时间"卡片因此显示真实时间，而不是永远"未构建"。
        stamp_rows = self.rows(
            "MATCH (d:Dataset {id: $dataset}) RETURN coalesce(d.updated_at, '') AS built_at",
            {"dataset": dataset_id})
        return {"nodes": _first(node_rows, "nodes", 0), "relationships": _first(rel_rows, "rels", 0),
                "built_at": _first(stamp_rows, "built_at", "")}

    def delete_dataset(self, dataset_id: str) -> None:
        self.cypher("MATCH (e:Entity {dataset: $dataset}) DETACH DELETE e", {"dataset": dataset_id})
        self.cypher("MATCH (d:Dataset {id: $dataset}) DETACH DELETE d", {"dataset": dataset_id})

    # ------------------------------------------------------------ 读图（门户数据面）
    def graph_payload(self, dataset_id: str, limit: int = 400) -> Dict[str, Any]:
        """整图读回（门户图谱页用）：节点带类别/社区/度数，边带中文关系名与关系类型。"""
        node_rows = self.rows(
            "MATCH (e:Entity {dataset: $dataset}) "
            "OPTIONAL MATCH (e)-[r]-(:Entity) WHERE type(r) <> 'IN_DATASET' "
            "RETURN e.id AS id, e.name AS name, coalesce(e.category, '术语') AS category, "
            "       coalesce(e.community, '') AS community, coalesce(e.weight, 0) AS value, "
            "       count(r) AS degree ORDER BY value DESC LIMIT $limit",
            {"dataset": dataset_id, "limit": limit})
        edge_rows = self.rows(
            "MATCH (a:Entity {dataset: $dataset})-[r]->(b:Entity {dataset: $dataset}) "
            "WHERE type(r) <> 'IN_DATASET' "
            "RETURN a.id AS source, b.id AS target, coalesce(r.relation, type(r)) AS relation, "
            "       type(r) AS type, coalesce(r.weight, 1) AS weight "
            "ORDER BY weight DESC LIMIT 800",
            {"dataset": dataset_id})
        return {"nodes": node_rows, "edges": edge_rows}

    # ------------------------------------------------------------ 查询
    def search_entities(self, dataset_id: str, keyword: str = "", limit: int = 50) -> List[Dict[str, Any]]:
        """实体检索：有关键字走全文索引（cjk 分词），否则退回 CONTAINS。"""
        if keyword.strip():
            try:
                rows = self.rows(
                    "CALL db.index.fulltext.queryNodes('entity_fulltext', $keyword, {limit: $limit}) "
                    "YIELD node, score "
                    "WHERE node.dataset = $dataset "
                    "OPTIONAL MATCH (node)-[r]-(:Entity) WHERE type(r) <> 'IN_DATASET' "
                    "RETURN node.id AS id, node.name AS name, coalesce(node.category, '术语') AS category, "
                    "       coalesce(node.weight, 0) AS weight, count(r) AS degree, score "
                    "ORDER BY score DESC, weight DESC LIMIT $limit",
                    {"dataset": dataset_id, "keyword": keyword, "limit": limit})
                if rows:
                    return rows
            except GraphDBError:
                pass
        return self.rows(
            "MATCH (e:Entity {dataset: $dataset}) "
            "WHERE $keyword = '' OR e.name CONTAINS $keyword "
            "OPTIONAL MATCH (e)-[r]-(:Entity) WHERE type(r) <> 'IN_DATASET' "
            "RETURN e.id AS id, e.name AS name, coalesce(e.category, '术语') AS category, "
            "       coalesce(e.weight, 0) AS weight, count(r) AS degree "
            "ORDER BY weight DESC, degree DESC LIMIT $limit",
            {"dataset": dataset_id, "keyword": keyword, "limit": limit})

    def neighbours(self, dataset_id: str, entity: str, limit: int = 30) -> List[Dict[str, Any]]:
        """邻居：带关系类型与中文关系名。"""
        return self.rows(
            "MATCH (a:Entity {dataset: $dataset, id: $entity})-[r]-(b:Entity {dataset: $dataset}) "
            "WHERE type(r) <> 'IN_DATASET' "
            "RETURN b.name AS name, coalesce(b.category, '术语') AS category, type(r) AS type, "
            "       coalesce(r.relation, type(r)) AS relation, coalesce(r.weight, 1) AS weight "
            "ORDER BY weight DESC LIMIT $limit",
            {"dataset": dataset_id, "entity": entity, "limit": limit})

    def path_between(self, dataset_id: str, left: str, right: str, limit: int = 3) -> List[Dict[str, Any]]:
        """两个实体之间的最短路径（跨任意关系类型）。"""
        return self.rows(
            "MATCH p = shortestPath((a:Entity {dataset: $dataset, id: $left})"
            "-[*..6]-(b:Entity {dataset: $dataset, id: $right})) "
            "WHERE all(rel IN relationships(p) WHERE type(rel) <> 'IN_DATASET') "
            "RETURN [n IN nodes(p) | n.name] AS names, "
            "       [rel IN relationships(p) | coalesce(rel.relation, type(rel))] AS relations, "
            "       length(p) AS hops LIMIT $limit",
            {"dataset": dataset_id, "left": left, "right": right, "limit": limit})

    def central_entities(self, dataset_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        """度中心性排名（Cypher 现场算，不依赖 GDS 插件）。"""
        return self.rows(
            "MATCH (e:Entity {dataset: $dataset}) "
            "OPTIONAL MATCH (e)-[r]-(:Entity) WHERE type(r) <> 'IN_DATASET' "
            "WITH e, count(r) AS degree "
            "RETURN e.id AS id, e.name AS name, coalesce(e.category, '术语') AS category, "
            "       degree, coalesce(e.weight, 0) AS weight "
            "ORDER BY degree DESC, weight DESC LIMIT $limit",
            {"dataset": dataset_id, "limit": limit})

    def version_board(self, dataset_id: str) -> List[Dict[str, Any]]:
        """资料—版本—状态：哪份资料的现行版本是哪个、有哪些已作废版本。"""
        return self.rows(
            "MATCH (d:Document {dataset: $dataset}) "
            "OPTIONAL MATCH (d)-[:CURRENT_VERSION]->(cur:Version) "
            "OPTIONAL MATCH (old:Version)-[:OBSOLETE]->(d) "
            "RETURN d.name AS doc_no, coalesce(cur.name, '') AS current_version, "
            "       collect(DISTINCT old.name) AS obsolete_versions "
            "ORDER BY doc_no",
            {"dataset": dataset_id})

    def composition(self, dataset_id: str) -> Dict[str, Any]:
        """按标签 / 关系类型统计（图模型体检）。"""
        by_label = self.rows(
            "MATCH (e:Entity {dataset: $dataset}) UNWIND labels(e) AS label "
            "WITH label, count(*) AS count WHERE label <> 'Entity' "
            "RETURN label, count ORDER BY count DESC", {"dataset": dataset_id})
        by_type = self.rows(
            "MATCH (:Entity {dataset: $dataset})-[r]->(:Entity) "
            "WHERE type(r) <> 'IN_DATASET' "
            "RETURN type(r) AS type, count(r) AS count ORDER BY count DESC",
            {"dataset": dataset_id})
        return {"by_label": by_label, "by_relationship_type": by_type}


def _rows(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    data = result.get("data") or {}
    fields = data.get("fields") or []
    out: List[Dict[str, Any]] = []
    for values in data.get("values") or []:
        out.append({field: value for field, value in zip(fields, values)})
    return out


def _first(rows: List[Dict[str, Any]], field: str, default: Any = None) -> Any:
    if rows and field in rows[0]:
        return rows[0][field]
    return default
