"""编排：产品内置问答链 + 用户自定义工作流（跑在 Dify graphon 引擎上）。

  · 智能问答链：检索（产品自研混合检索）→ 组装提示词 → 对话模型 → 带引用答案；
  · 自定义工作流：门户里编辑 DSL，交给编排内核（graphon = Dify 的引擎）执行并回放事件。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from . import kernels
from .embedder import ModelGateway
from .retriever import retrieve
from .store import Store
from .textutil import normalize_for_prompt

SYSTEM_PROMPT = (
    "你是装备技术资料问答助手，服务于装备保障与管理人员。\n"
    "硬性要求：\n"
    "1. 只依据【检索片段】回答，不得编造；片段不足以回答时明确说明「资料中未收录」。\n"
    "2. 每个关键结论后标注引用，格式：《资料名》或 资料编号 版本 §条款。\n"
    "3. 安全相关（操作、维修、拆装、应急处置）必须完整带出前置条件与禁止事项。\n"
    "4. 用简洁的条目作答，不输出思考过程。\n"
)


def build_prompt(question: str, hits: List[Dict[str, Any]],
                 memories: Optional[List[Dict[str, Any]]] = None) -> str:
    blocks: List[str] = []
    for index, hit in enumerate(hits, 1):
        metadata = hit.get("metadata") or {}
        citation = " ".join(
            part
            for part in [
                str(metadata.get("doc_no") or ""),
                str(metadata.get("version") or ""),
                ("§" + str(metadata["clause_no"])) if metadata.get("clause_no") else "",
            ]
            if part
        ) or hit.get("document_name") or ""
        # 提示词里用整形后的文本：HTML 表格还原成竖线文本，模型读得准、也省 token；
        # 前端引用面板仍显示原始 HTML（可渲染成真表格），两边口径不冲突。
        body = normalize_for_prompt(str(hit.get("content") or ""))
        blocks.append(
            "[片段" + str(index) + "]（" + citation + "，相似度 " + str(hit.get("similarity")) + "）\n" + body
        )
    joined = "\n\n".join(blocks) if blocks else "（没有检索到相关片段）"
    memory_block = ""
    if memories:
        lines = ["- " + str(item.get("title") or "") + "：" + str(item.get("content") or "") for item in memories]
        memory_block = ("【长期记忆】（用户已确认的长期背景与口径，可参考，"
                        "与【检索片段】冲突时一律以检索片段为准）\n" + "\n".join(lines) + "\n\n")
    return memory_block + "【检索片段】\n" + joined + "\n\n【用户问题】\n" + question


def answer(store: Store, gateway: ModelGateway, question: str, dataset_ids: List[str],
           top_k: int = 10, threshold: float = 0.2, vector_weight: float = 0.3,
           use_kg: bool = False, stream: bool = False,
           memories: Optional[List[Dict[str, Any]]] = None,
           project_names: Optional[List[str]] = None):
    """内置问答链。stream=True 时返回 (hits, retrieval, generator)。"""
    result = retrieve(store, gateway, question, dataset_ids, top_k=top_k, threshold=threshold,
                      vector_weight=vector_weight, use_kg=use_kg)
    hits = result.get("chunks") or []
    if memories is None:
        memories = store.memories_for_chat(project_names or [])
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_prompt(question, hits, memories)},
    ]
    if stream:
        return hits, result, gateway.chat_stream(messages)
    return hits, result, gateway.chat(messages)


def run_workflow(store: Store, workflow: Dict[str, Any], inputs: Dict[str, Any]) -> Dict[str, Any]:
    """在编排内核（graphon）上跑一张 DSL 图，并把运行记录落库。"""
    dsl = workflow.get("dsl") or "{}"
    response = kernels.orchestration("run", timeout=1800.0, dsl=dsl, inputs=inputs)
    if not response.get("ok"):
        data: Dict[str, Any] = {"status": "failed", "outputs": {}, "events": [], "elapsed_ms": 0,
                                "error": response.get("error") or ""}
    else:
        data = response.get("data") or {}
    return store.add_run(workflow["id"], data.get("status") or "unknown", inputs,
                         data.get("outputs") or {}, data.get("events") or [],
                         data.get("error") or "", int(data.get("elapsed_ms") or 0))


def inspect_workflow(dsl: Any) -> Dict[str, Any]:
    response = kernels.orchestration("inspect", timeout=300.0, dsl=dsl)
    if not response.get("ok"):
        return {"load_status": "FAILED", "reason": response.get("error")}
    return response.get("data") or {}


def parse_dsl(dsl: Any) -> Dict[str, Any]:
    """把外部 DSL 文本（.yml/.yaml/.json）解析成结构化对象。

    YAML 解析交给编排内核代劳（其解释器带 PyYAML），产品服务侧不引入 yaml 依赖。
    解析失败抛 ValueError，由调用方转成 400 拒收（不落库）。
    """
    response = kernels.orchestration("parse", timeout=120.0, dsl=dsl)
    if not response.get("ok"):
        raise ValueError(str(response.get("error") or "DSL 解析失败"))
    return response.get("data") or {}


def skeleton_dsl(mode: str, name: str) -> Dict[str, Any]:
    """按应用类型给出一张最小可运行的骨架 DSL（新建应用时直接落库即可用）。

    注意：graphon 只执行 workflow 与 advanced-chat 两种 Dify 应用模式，
    ``chat`` 属于 inspect-only（可查看不可运行），因此「对话流」统一落 ``advanced-chat``。
    """
    if mode in ("chat", "advanced-chat"):
        return {
            "kind": "app",
            "app": {"mode": "advanced-chat", "name": name},
            "workflow": {
                "graph": {
                    "nodes": [
                        {"id": "start", "type": "custom", "position": {"x": 120, "y": 180},
                         "data": {"type": "start", "title": "开始", "variables": []}},
                        {"id": "answer", "type": "custom", "position": {"x": 400, "y": 180},
                         "data": {"type": "answer", "title": "回答",
                                  "answer": "你好，请在编排工作台完善这条对话流的回答内容。"}},
                    ],
                    "edges": [{"id": "e1", "source": "start", "target": "answer"}],
                },
            },
        }
    return {
        "kind": "app",
        "app": {"mode": "workflow", "name": name},
        "workflow": {
            "graph": {
                "nodes": [
                    {"id": "start", "type": "custom", "position": {"x": 120, "y": 180},
                     "data": {"type": "start", "title": "开始",
                              "variables": [{"variable": "question", "label": "问题", "type": "text-input",
                                             "required": True}]}},
                    {"id": "tpl", "type": "custom", "position": {"x": 400, "y": 180},
                     "data": {"type": "template-transform", "title": "提示词组装",
                              "template": "请依据资料回答问题：{{ question }}",
                              "variables": [{"variable": "question", "value_selector": ["start", "question"]}]}},
                    {"id": "end", "type": "custom", "position": {"x": 680, "y": 180},
                     "data": {"type": "end", "title": "结束",
                              "outputs": [{"variable": "answer", "value_selector": ["tpl", "output"]}]}},
                ],
                "edges": [
                    {"id": "e1", "source": "start", "target": "tpl"},
                    {"id": "e2", "source": "tpl", "target": "end"},
                ],
            },
        },
    }


DEFAULT_DSL = skeleton_dsl("workflow", "示例：资料问答链")


def ensure_default_workflow(store: Store) -> None:
    if store.list_workflows():
        return
    store.save_workflow("示例：资料问答链", json.dumps(DEFAULT_DSL, ensure_ascii=False),
                        note="产品自带示例，可直接运行或改造成你自己的链")
