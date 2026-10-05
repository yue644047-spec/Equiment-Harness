"""装备数据治理 · 产品包。

自研产品：知识内核套用 RAGFlow 源码，编排内核套用 Dify 的 graphon 引擎，
本包提供数据面、模型网关、入库/检索流水线、编排与 HTTP 服务。
"""

__all__ = ["store", "embedder", "kernels", "ingest", "retriever", "orchestrator", "server"]
