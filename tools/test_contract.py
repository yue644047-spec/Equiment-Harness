# -*- coding: utf-8 -*-
"""跨文件契约测试：确保 Dify DSL 与本地评测服务的接口严格对齐。

这类不一致不会报错，只会**静默丢数据**——例如 DSL 输出 score=4,suggestion=...
而解析器只认 JSON 时，用户评分会被全部丢弃。因此必须由测试守住。

用法：
    python tools/test_contract.py

校验内容：
1. dsl/01 的评价回执模板渲染后，能被 eval_service.collector.parse_receipt 正确解析
2. dsl/02 的 start 输入变量，与 eval_service.judge 实际发送的 inputs 键完全一致
3. dsl/02 的 code 节点输出键，覆盖 eval_service.judge 读取的所有 outputs 键
4. dsl/02 的 end 输出，包含评测服务依赖的全部字段
5. 指标权重在 DSL 与 metrics.py 中一致
6. 工作流自报的 csi/verdict 不被采信
7. 装备域专项维度的常量与枚举在 DSL 与 metrics.py 中一致
8. 装备域判定（安全红线 / 版本降级 / badcase）在 DSL 内嵌逻辑与 metrics.py 中逐例一致
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("[FATAL] 需要 PyYAML")
    sys.exit(2)

try:
    from jinja2 import Environment
except ImportError:
    print("[FATAL] 需要 jinja2 才能渲染 DSL 模板")
    sys.exit(2)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval_service"))

PASSED = 0
FAILED = 0


def check(name: str, actual: object, expected: object) -> None:
    global PASSED, FAILED
    if actual == expected:
        PASSED += 1
        print(f"  [OK]   {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name}\n         实际 {actual!r}\n         期望 {expected!r}")


def load_dsl(name: str) -> dict:
    return yaml.safe_load((ROOT / "dsl" / name).read_text(encoding="utf-8"))


def nodes_of(app: dict, node_type: str) -> list[dict]:
    return [n["data"] for n in app["workflow"]["graph"]["nodes"] if n["data"].get("type") == node_type]


def test_receipt_roundtrip() -> None:
    """DSL 出什么，Python 就必须能解什么。"""
    print("\n=== 1. 评价回执模板 ↔ parse_receipt 往返一致性 ===")
    from collector import parse_eval_receipt  # noqa: PLC0415

    templates = [d["template"] for d in nodes_of(load_dsl("01-多模态智能问答-Chatflow.yml"), "template-transform")]
    receipt_templates = [t for t in templates if "EVAL_RECEIPT" in t]
    check("dsl/01 存在回执模板", len(receipt_templates), 1)
    if not receipt_templates:
        return
    template = receipt_templates[0]
    missing_dims = [k for k in ("satisfaction", "usefulness", "experience") if f"{k}=" not in template]
    check("回执模板声明三项主观指标（docs/01 T9）", missing_dims, [])

    env = Environment()
    suggestion = '希望补充"低温环境用油要求"说明，\n以及 score=9 的口径'
    rendered = env.from_string(template).render(
        satisfaction=4, usefulness=5, experience=3, suggestion=suggestion,
    )
    parsed = parse_eval_receipt(rendered)
    check("规范回执可被解析出满意度", parsed["score"], 4)
    check("规范回执可被解析出有用性/体验感", (parsed["usefulness"], parsed["experience"]), (5, 3))
    check("规范回执的建议原文完整保留（含逗号/换行/引号/score= 干扰）", parsed["suggestion"], suggestion)

    rendered_empty = env.from_string(template).render(
        satisfaction=5, usefulness=5, experience=5, suggestion="",
    )
    check("空建议回执可解析出分数", parse_eval_receipt(rendered_empty)["score"], 5)
    check("空建议回执的建议为空串", parse_eval_receipt(rendered_empty)["suggestion"], "")

    check("回执文本含有机器标记", "[[EVAL_RECEIPT]]" in rendered, True)

    # 历史 v1 单分回执（score=）必须继续可解析，否则老会话的评价会全部丢失
    legacy = "✅ 已记录。[[EVAL_RECEIPT]]score=4,suggestion=旧的单分回执[[/EVAL_RECEIPT]]"
    legacy_parsed = parse_eval_receipt(legacy)
    check("v1 单分回执仍可解析", (legacy_parsed["score"], legacy_parsed["satisfaction"]), (4, 4))


def judge_input_keys() -> set[str]:
    """从 judge.py 的 build_inputs 里取出实际发送给工作流的键名。"""
    tree = ast.parse((ROOT / "eval_service" / "judge.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "build_inputs":
            # 只取 return 语句里的字典，避免误抓到函数内部构造的 meta 子字典
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Dict):
                    return {
                        k.value
                        for k in sub.value.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)
                    }
    raise SystemExit("未能在 judge.py 中定位 build_inputs 的返回字典")


def judge_output_keys() -> set[str]:
    """从 judge.py 里取出它读取的 outputs 键名。"""
    source = (ROOT / "eval_service" / "judge.py").read_text(encoding="utf-8")
    keys = set(re.findall(r'outputs\.get\(\s*"([^"]+)"', source))
    # 形如：for key in ("annotation_label", "error_cause", "knowledge_gap"): ... key in outputs
    # 只认循环体里出现 "in outputs" 的那个循环，避免把遍历 payload 的循环也算进来
    for group, tail in re.findall(r"for key in \(([^)]*)\):(.{0,240})", source, re.S):
        if "in outputs" in tail:
            keys |= set(re.findall(r'"([^"]+)"', group))
    # 也覆盖 '"xxx" in outputs' 这种存在性判断的读法
    keys |= set(re.findall(r'"([a-z_]+)"\s+in\s+outputs', source))
    return keys


def test_workflow_io_contract() -> None:
    print("\n=== 2. dsl/02 输入变量 ↔ judge.build_inputs 发送的键 ===")
    app = load_dsl("02-满意度评价-Workflow.yml")
    start = nodes_of(app, "start")[0]
    dsl_inputs = {v["variable"] for v in start["variables"]}
    sent = judge_input_keys()
    check("DSL 声明的输入变量集合", sorted(dsl_inputs), sorted(sent))
    check("两端都不为空", bool(dsl_inputs) and bool(sent), True)

    print("\n=== 3. dsl/02 code 节点输出 ↔ judge 读取的 outputs 键 ===")
    code_node = nodes_of(app, "code")[0]
    declared = set(code_node["outputs"].keys())
    read_keys = judge_output_keys()
    missing = read_keys - declared
    check("judge 读取的键全部在 DSL 的 code 节点中声明", sorted(missing), [])
    check(
        "judge 读取的键集合",
        sorted(read_keys),
        sorted({"result_json", "csi", "verdict", "annotation_label", "error_cause", "knowledge_gap"}),
    )

    print("\n=== 4. dsl/02 end 输出 ↔ 评测服务依赖字段 ===")
    end = nodes_of(app, "end")[0]
    end_outputs = {o["variable"] for o in end["outputs"]}
    needed = {"result_json", "csi", "verdict", "annotation_label", "error_cause", "knowledge_gap", "badcase"}
    check("end 覆盖评测所需字段", sorted(needed - end_outputs), [])
    check("judge 读取的 outputs 键都在 end 中暴露", sorted(read_keys - end_outputs), [])

    print("\n=== 5. 指标权重在 DSL 与 metrics.py 中一致 ===")
    from metrics import WEIGHTS_WITH_US, WEIGHTS_NO_US  # noqa: PLC0415

    code_src = code_node["code"]
    for key, weight in {**WEIGHTS_WITH_US}.items():
        if key == "user_score":
            continue
        check(f"DSL 中 {key} 权重 = {weight}", f'"{key}": {weight}' in code_src.replace(".20", ".20"), True)
    check("无 US 归一化分母 0.70 一致", "0.70" in code_src and abs(sum(WEIGHTS_NO_US.values()) - 0.70) < 1e-9, True)


def test_workflow_csi_not_trusted() -> None:
    """行为验证：工作流自报的 csi/verdict 不得覆盖本地按维度分重算的结果。"""
    print("\n=== 6. 工作流自报 csi 不被采信（行为验证） ===")
    import judge as judge_mod  # noqa: PLC0415
    from metrics import derive  # noqa: PLC0415

    class FakeClient:
        """只回放一份固定的工作流响应，不发网络请求。"""

        is_mock = False

        def run_workflow(self, inputs):  # noqa: ARG002
            return {
                "data": {
                    "status": "succeeded",
                    "outputs": {
                        "result_json": json.dumps(
                            {
                                "factual_accuracy": 5,
                                "knowledge_completeness": 5,
                                "relevance": 5,
                                "clarity": 5,
                                "retrieval_quality": 5,
                                "safety_compliance": 0,
                                "version_currency": 0,
                                "terminology_norm": 5,
                                "knowledge_gap": False,
                                "hallucination_risk": "无",
                                "annotation_label": "正确",
                                "error_cause": "无",
                                "judge_reason": "ok",
                                "missing_knowledge": [],
                                "improvement_suggestions": [],
                            },
                            ensure_ascii=False,
                        ),
                        # 故意自报一个错误值，检验本地是否会被带偏
                        "csi": 1.0,
                        "verdict": "不合格",
                    },
                }
            }

    judge = judge_mod.Judge.__new__(judge_mod.Judge)
    judge.client = FakeClient()
    message = {
        "id": "m1",
        "conversation_id": "c1",
        "query": "滤芯多久需要更换一次？",
        "answer": "滤芯每 500 工作小时或 12 个月更换一次，以先到为准。",
        "user_score_raw": "",
        "retriever_resources_json": "[]",
    }
    row = judge.score_one(message)
    expected = derive(5, 5, 5, 5, False, "")
    check("落库 csi 采用本地重算值而非工作流自报值", row["csi"], expected["csi"])
    check("落库 verdict 采用本地重算值", row["verdict"], expected["verdict"])
    check(
        "工作流自报值被单独留存以供交叉校验",
        (row["workflow_csi"], row["workflow_verdict"]),
        (1.0, "不合格"),
    )


def test_domain_constants() -> None:
    """装备域专项维度的常量与枚举必须在 DSL 与 metrics.py 中一致。

    这类不一致不会报错：DSL 判「不合格」而本地判「优秀」时，看板与工单会各说各话。
    """
    print("\n=== 7. 装备域专项维度常量 ↔ metrics.py 一致性 ===")
    from metrics import (  # noqa: PLC0415
        DOMAIN_BADCASE_CAUSES,
        DOMAIN_DIMENSION_KEYS,
        SAFETY_REDLINE,
        VALID_ERROR_CAUSES,
        VALID_HALLUCINATION_RISK,
        VALID_LABELS,
        VERSION_OBSOLETE,
    )

    app = load_dsl("02-满意度评价-Workflow.yml")
    code_node = nodes_of(app, "code")[0]
    code_src = code_node["code"]

    check("DSL 中 SAFETY_REDLINE 与 metrics 一致", f"SAFETY_REDLINE = {SAFETY_REDLINE}" in code_src, True)
    check("DSL 中 VERSION_OBSOLETE 与 metrics 一致", f"VERSION_OBSOLETE = {VERSION_OBSOLETE}" in code_src, True)
    for cause in DOMAIN_BADCASE_CAUSES:
        check(f"DSL 中装备域归因「{cause}」已列入 DOMAIN_BADCASE_CAUSES", f'"{cause}"' in code_src, True)

    llm_text = "\n".join(
        block.get("text") or ""
        for node in nodes_of(app, "llm")
        for block in (node.get("prompt_template") or [])
    )
    check("LLM 提示词覆盖全部 error_cause 取值", [c for c in VALID_ERROR_CAUSES if c not in llm_text], [])
    check("LLM 提示词覆盖全部标注标签", [l for l in VALID_LABELS if l not in llm_text], [])
    check("LLM 提示词覆盖全部幻觉风险取值", [r for r in VALID_HALLUCINATION_RISK if r not in llm_text], [])

    declared = set(code_node["outputs"].keys())
    end_outputs = {o["variable"] for o in nodes_of(app, "end")[0]["outputs"]}
    check("code 节点声明全部装备域专项维度", sorted(set(DOMAIN_DIMENSION_KEYS) - declared), [])
    check("end 输出暴露全部装备域专项维度", sorted(set(DOMAIN_DIMENSION_KEYS) - end_outputs), [])


def test_domain_behavior_parity() -> None:
    """DSL 内嵌评分逻辑与 metrics.py 在装备域用例上必须逐例一致。"""
    print("\n=== 8. 装备域判定：DSL code 节点 ↔ metrics.py 行为一致 ===")
    import judge as judge_mod  # noqa: PLC0415
    from metrics import derive  # noqa: PLC0415

    code_node = nodes_of(load_dsl("02-满意度评价-Workflow.yml"), "code")[0]
    namespace: dict = {}
    exec(compile(code_node["code"], "<dsl-02-code-node>", "exec"), namespace)  # noqa: S102
    main = namespace["main"]

    base = {
        "factual_accuracy": 5, "knowledge_completeness": 5, "relevance": 5, "clarity": 5,
        "retrieval_quality": 5, "safety_compliance": 0, "version_currency": 0, "terminology_norm": 5,
        "knowledge_gap": False, "hallucination_risk": "无", "annotation_label": "正确",
        "error_cause": "无", "judge_reason": "ok", "missing_knowledge": [], "improvement_suggestions": [],
    }
    cases = [
        ("不涉及安全内容的满分回答", {}, ""),
        ("安全合规性红线 SC=2", {"safety_compliance": 2, "error_cause": "安全警示缺失"}, ""),
        ("安全合规性 SC=1 且主观满意度 1 分", {"safety_compliance": 1, "error_cause": "安全警示缺失"}, "1"),
        ("引用作废版本 VC=1", {"version_currency": 1, "error_cause": "版本失效"}, ""),
        ("术语不规范 TN=2", {"terminology_norm": 2, "error_cause": "术语不统一"}, ""),
        ("型号适配错误", {"error_cause": "型号适配错误"}, ""),
        ("跨文档冲突", {"error_cause": "跨文档冲突"}, "4"),
        ("安全合规性越界 9 → 回落为不涉及", {"safety_compliance": 9}, ""),
        ("版本时效性越界 -1 → 回落为不涉及", {"version_currency": -1}, ""),
        ("知识缺失 + 安全红线", {"knowledge_completeness": 2, "knowledge_gap": True, "safety_compliance": 1}, ""),
        ("事实性错误 + 作废版本", {"factual_accuracy": 2, "version_currency": 1}, ""),
    ]
    for idx, (name, override, user_score) in enumerate(cases, 1):
        payload = {**base, **override}
        # DSL 侧主观输入为三项（满意度/有用性/体验感），此处把单一分映射到「满意度」，
        # 与 metrics.derive 的 user_score 口径对齐；like/dislike 只影响评测侧的降级。
        got = main(json.dumps(payload, ensure_ascii=False), user_score, "", "")
        parsed = json.loads(got["result_json"])
        mine = derive(
            parsed["factual_accuracy"], parsed["knowledge_completeness"], parsed["relevance"],
            parsed["clarity"], parsed["knowledge_gap"], user_score,
            safety_compliance=parsed["safety_compliance"],
            version_currency=parsed["version_currency"],
            terminology_norm=parsed["terminology_norm"],
            error_cause=parsed["error_cause"],
        )
        check(f"用例{idx} {name} · CSI", got["csi"], mine["csi"])
        check(f"用例{idx} {name} · verdict", got["verdict"], mine["verdict"])
        check(f"用例{idx} {name} · badcase", got["badcase"], mine["badcase"])

    # 归因枚举必须能被评测服务的 sanitize 原样接受（否则会被静默改写为「无」）
    sanitizer = judge_mod.Judge.__new__(judge_mod.Judge)
    sanitized, fixed = sanitizer.sanitize(
        {**base, "safety_compliance": 2, "version_currency": 1, "terminology_norm": 2,
         "error_cause": "安全警示缺失"}
    )
    check("装备域归因未被 sanitize 改写", sanitized["error_cause"], "安全警示缺失")
    check("装备域专项维度未被 sanitize 改写",
          (sanitized["safety_compliance"], sanitized["version_currency"], sanitized["terminology_norm"]),
          (2, 1, 2))
    check("合法取值不产生修正计数", fixed, 0)

    # 越界与非法归因必须被收敛到安全默认值，而不是「完全合规」
    sanitized, _ = sanitizer.sanitize({**base, "safety_compliance": 9, "version_currency": -3,
                                       "terminology_norm": 0, "error_cause": "查无此因"})
    check("安全合规性越界收敛为 0（不涉及）而非 5", sanitized["safety_compliance"], 0)
    check("版本时效性越界收敛为 0（不涉及）", sanitized["version_currency"], 0)
    check("术语规范性越界收敛为默认 3", sanitized["terminology_norm"], 3)
    check("非法归因收敛为「无」", sanitized["error_cause"], "无")


def main() -> int:
    print("Dify DSL ↔ 评测服务 契约测试")
    test_receipt_roundtrip()
    test_workflow_io_contract()
    test_workflow_csi_not_trusted()
    test_domain_constants()
    test_domain_behavior_parity()
    print(f"\n契约测试结束：通过 {PASSED} 项，失败 {FAILED} 项")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
