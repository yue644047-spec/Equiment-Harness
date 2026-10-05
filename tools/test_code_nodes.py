# -*- coding: utf-8 -*-
"""抽取 DSL 中 code 节点的 Python 代码并实测其行为。

用法：python tools/test_code_nodes.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_code_node(dsl_path: Path, node_type: str = "code") -> tuple[str, list[str], set[str]]:
    app = yaml.safe_load(dsl_path.read_text(encoding="utf-8"))
    for node in app["workflow"]["graph"]["nodes"]:
        data = node.get("data") or {}
        if data.get("type") == node_type:
            return data["code"], [v["variable"] for v in data["variables"]], set((data.get("outputs") or {}).keys())
    raise SystemExit(f"{dsl_path} 中未找到 {node_type} 节点")


def load_module(code: str, name: str) -> dict:
    ns: dict = {"__name__": name}
    exec(compile(code, name, "exec"), ns)  # noqa: S102
    return ns


def approx(a: float, b: float, tol: float = 0.011) -> bool:
    return abs(float(a) - float(b)) <= tol


failures: list[str] = []
checks = 0


def expect(label: str, actual, expected) -> None:
    global checks
    checks += 1
    ok = approx(actual, expected) if isinstance(expected, float) else actual == expected
    if not ok:
        failures.append(f"{label}: 期望 {expected!r}，实际 {actual!r}")
    print(f"  {'OK  ' if ok else 'FAIL'} {label}: {actual!r}")


print("=" * 72)
print("测试 1：满意度评价工作流的评分与判定逻辑（dsl/02）")
print("=" * 72)
code, args, outs = load_code_node(ROOT / "dsl" / "02-满意度评价-Workflow.yml")
print(f"main 参数: {args}")
print(f"输出键: {sorted(outs)}")
ns = load_module(code, "eval_node")
main = ns["main"]

judge_ok = json.dumps({
    "factual_accuracy": 5, "knowledge_completeness": 5, "relevance": 5, "clarity": 5,
    "retrieval_quality": 5,
    "safety_compliance": 0, "version_currency": 0, "terminology_norm": 5,   # 0 = 本条不涉及
    "knowledge_gap": False, "hallucination_risk": "无",
    "annotation_label": "正确", "error_cause": "无", "judge_reason": "完全正确",
    "missing_knowledge": [], "improvement_suggestions": [],
}, ensure_ascii=False)

# 主观三项（docs/01：满意度 / 有用性 / 体验感）——缺失项传空串，不参与 SSI
r = main(judge_ok, "5", "", "")
expect("满分 + 主观满分 → CSI", r["csi"], 5.0)
expect("满分 + 主观满分 → verdict", r["verdict"], "优秀")
expect("满分 + 主观满分 → badcase", r["badcase"], False)
# 满分回答 + 无数值主观分（只有客观维度）→ CSI 走 0.70 归一化
r_nous = main(judge_ok, "", "", "")
expect("无主观分 → CSI 仍为 5.0（0.70 归一化）", r_nous["csi"], 5.0)
expect("无主观分 → SSI 输出 0", r_nous["ssi"], 0.0)

# 无 Markdown 围栏、带尾随逗号的脏输出
dirty = '\n\n\u0060\u0060\u0060json\n{"factual_accuracy": 4, "knowledge_completeness": 4, "relevance": 5, "clarity": 4, "retrieval_quality": 4, "safety_compliance": 0, "version_currency": 0, "terminology_norm": 5, "knowledge_gap": false, "annotation_label": "正确", "error_cause": "无", "judge_reason": "ok", "missing_knowledge": [], "improvement_suggestions": [],}\n\u0060\u0060\u0060\n'
r = main(dirty, "", "", "")
expect("脏 JSON + 无用户分 → CSI（0.70 归一化）", r["csi"], round((0.25 * 4 + 0.20 * 4 + 0.15 * 5 + 0.10 * 4) / 0.7, 2))
expect("脏 JSON 解析成功标记", json.loads(r["result_json"])["parse_ok"], True)

hal = json.dumps({"factual_accuracy": 2, "knowledge_completeness": 3, "relevance": 4, "clarity": 4,
                  "retrieval_quality": 3, "knowledge_gap": False, "hallucination_risk": "高",
                  "annotation_label": "错误", "error_cause": "幻觉编造", "judge_reason": "编造条款",
                  "missing_knowledge": ["油缸缸盖螺栓力矩"], "improvement_suggestions": ["补充 HYS-WX-003 力矩条款"]}, ensure_ascii=False)
r = main(hal, "5", "", "")
expect("事实性错误 → verdict 强制不合格", r["verdict"], "不合格")
expect("事实性错误 → badcase", r["badcase"], True)

# 主观满意度只有 1 分：0.30 权重给到 US=1，客观四项满分贡献 3.5 → 0.3 + 3.5 = 3.8
# 注意：赞踩（dislike）信号不在本工作流的输入里，其「最多判待改进」的降级由评测服务
# （metrics.decide_verdict）在落库时施加，故此处只断言 CSI 与阈值判定。
r = main(judge_ok, "1", "", "")
expect("主观满意度=1 → CSI", r["csi"], 3.8)
expect("主观满意度=1 → verdict 良好", r["verdict"], "良好")
expect("主观满意度=1 → 非 badcase", r["badcase"], False)

gap = json.dumps({"factual_accuracy": 4, "knowledge_completeness": 2, "relevance": 4, "clarity": 4,
                  "retrieval_quality": 0, "knowledge_gap": True, "hallucination_risk": "无",
                  "annotation_label": "知识缺失", "error_cause": "知识库缺失", "judge_reason": "未收录",
                  "missing_knowledge": ["低温环境下的用油要求"], "improvement_suggestions": ["补充 HYS-GY-001 低温使用条款"]}, ensure_ascii=False)
r = main(gap, "", "", "")
expect("知识缺失（KC<=2）→ badcase", r["badcase"], True)
expect("知识缺失 → 标签", r["annotation_label"], "知识缺失")
expect("知识缺失 → CSI 按权重计算", r["csi"], round((0.25 * 4 + 0.20 * 2 + 0.15 * 4 + 0.10 * 4) / 0.7, 2))

r = main("模型没有按格式输出，这是一段自由文本", "", "", "")
expect("完全无法解析 → parse_ok", json.loads(r["result_json"])["parse_ok"], False)
expect("完全无法解析 → 默认分", json.loads(r["result_json"])["factual_accuracy"], 3)

# ---------------------------------------------------------------- 装备域专项维度
print()
print("-" * 72)
print("测试 1b：装备域专项维度与红线（safety_compliance / version_currency / terminology_norm）")
print("-" * 72)


def domain_case(**overrides):
    """以满分回答为底，覆盖指定专项维度，返回 main 的结果。"""
    payload = {
        "factual_accuracy": 5, "knowledge_completeness": 5, "relevance": 5, "clarity": 5,
        "retrieval_quality": 5, "safety_compliance": 0, "version_currency": 0, "terminology_norm": 5,
        "knowledge_gap": False, "hallucination_risk": "无",
        "annotation_label": "正确", "error_cause": "无", "judge_reason": "ok",
        "missing_knowledge": [], "improvement_suggestions": [],
    }
    payload.update(overrides)
    return main(json.dumps(payload, ensure_ascii=False), "", "", "")


r = domain_case(safety_compliance=0)
expect("不涉及安全内容 → 不触发安全红线", r["verdict"], "优秀")
expect("不涉及安全内容 → 非 badcase", r["badcase"], False)

r = domain_case(safety_compliance=2, error_cause="安全警示缺失")
expect("安全合规性=2 → CSI 不受影响（专项维度不进权重）", r["csi"], 5.0)
expect("安全合规性=2 → verdict 强制不合格", r["verdict"], "不合格")
expect("安全合规性=2 → badcase", r["badcase"], True)
expect("安全合规性 → 透传输出", r["safety_compliance"], 2)

r = domain_case(version_currency=1, error_cause="版本失效")
expect("引用作废版本 → CSI 不受影响", r["csi"], 5.0)
expect("引用作废版本 → verdict 降级为待改进", r["verdict"], "待改进")
expect("引用作废版本 → badcase", r["badcase"], True)
expect("版本时效性 → 透传输出", r["version_currency"], 1)

r = domain_case(terminology_norm=2, error_cause="术语不统一")
expect("术语不规范 → badcase", r["badcase"], True)
expect("术语规范性 → 透传输出", r["terminology_norm"], 2)

r = domain_case(error_cause="型号适配错误")
expect("型号适配错误 → badcase", r["badcase"], True)

r = domain_case(safety_compliance=9)
expect("安全合规性越界（9）→ 被夹到 0，不触发红线", r["verdict"], "优秀")
expect("安全合规性越界（9）→ 透传输出为 0", r["safety_compliance"], 0)

r = main("模型没有按格式输出", "", "", "")
expect("专项维度缺失 → 安全合规性默认 0（不涉及）", r["safety_compliance"], 0)
expect("专项维度缺失 → 版本时效性默认 0（不涉及）", r["version_currency"], 0)
expect("专项维度缺失 → 术语规范性默认 3", r["terminology_norm"], 3)

print()
print("=" * 72)
print("测试 2：语料标注工作流的解析与质检逻辑（dsl/03）")
print("=" * 72)
code2, args2, outs2 = load_code_node(ROOT / "dsl" / "03-多模态语料标注-Workflow.yml")
print(f"main 参数: {args2}")
print(f"输出键: {sorted(outs2)}")
ns2 = load_module(code2, "anno_node")
main2 = ns2["main"]

pairs = {"qa_pairs": [
    {"question": "滤芯多久更换一次？", "answer": "滤芯每 500 工作小时或 12 个月更换一次，以先到为准。",
     "question_type": "维修型", "material_type": "维修规程", "difficulty": "简单",
     "keywords": ["滤芯", "更换周期"], "source_quote": "滤芯更换周期为每 500 工作小时或 12 个月，以先到为准。",
     "param_values": ["滤芯更换周期 500 工作小时"], "safety_related": False,
     "doc_no": "HYS-WX-003", "doc_version": "V2.0", "quality_score": 5,
     "need_human_review": False, "review_reason": ""},
    {"question": "滤芯多久更换一次？", "answer": "重复条目应被去重。", "quality_score": 5},
    {"question": "更换滤芯前要做哪些安全准备？", "answer": "停机、切断电源并挂牌上锁、泄压至 0 MPa。",
     "question_type": "操作型", "material_type": "操作规程", "difficulty": "中等",
     "keywords": ["安全", "泄压"], "source_quote": "系统泄压至 0 MPa。", "param_values": [],
     "safety_related": True, "doc_no": "HYS-WX-003", "doc_version": "V2.0", "quality_score": 3,
     "need_human_review": True, "review_reason": "涉及安全条款，需技术专家确认"},
    {"question": "", "answer": "空问题应被丢弃", "quality_score": 4},
], "doc_summary": "某型液压举升系统维修规程", "coverage_gaps": ["未说明低温环境下的用油要求"]}
r2 = main2(json.dumps(pairs, ensure_ascii=False), "某型液压举升系统维修规程", "维修规程", "10")
expect("有效问答对条数（去重+丢弃空问题）", r2["qa_count"], 2)
expect("需人工复核条数（含参数/安全条目强制复核）", json.loads(r2["stats_json"])["need_review"], 2)
expect("质量分均值", json.loads(r2["stats_json"])["avg_quality"], 4.0)
expect("排序后第一条为高质量", r2["qa_pairs"][0]["quality_score"], 5)
expect("JSONL 行数", len(r2["jsonl"].strip().split(chr(10))), 2)
expect("保留文档标题", r2["qa_pairs"][0]["doc_title"], "某型液压举升系统维修规程")
expect("覆盖空白点", len(r2["coverage_gaps"]), 1)
expect("资料类型被保留", r2["qa_pairs"][0]["material_type"], "维修规程")
expect("参数值被保留", r2["qa_pairs"][0]["param_values"], ["滤芯更换周期 500 工作小时"])
expect("资料编号与版本被保留", r2["qa_pairs"][0]["doc_no"] + " " + r2["qa_pairs"][0]["doc_version"], "HYS-WX-003 V2.0")
expect("涉及参数条目纳入统计", json.loads(r2["stats_json"])["param_related"], 1)
expect("涉及安全条目纳入统计", json.loads(r2["stats_json"])["safety_related"], 1)
expect("正常语料无作废版本告警", len(r2["obsolete_warnings"]), 0)

# 回归：推理型模型把思考段写在 JSON 之前（思考里还会复述 schema 的花括号），
# 解析器必须先剥思考段再找 JSON，否则会锁到思考段里的 { 而整批丢弃。
wrapped = '<think>\n复述 schema：{"text":3}, by_label {"优秀":2,"良好":1}\n</think>\n' + json.dumps(pairs, ensure_ascii=False)
r2d = main2(wrapped, "某型液压举升系统维修规程", "维修规程", "10")
expect("思考段含花括号时仍能解析出问答对", r2d["qa_count"], 2)
expect("思考段存在时质量分均值仍正确", json.loads(r2d["stats_json"])["avg_quality"], 4.0)

# 作废版本数值必须被检出并并入覆盖空白点
obsolete_pairs = {"qa_pairs": [
    {"question": "滤芯多久更换一次？", "answer": "滤芯每 300 工作小时更换一次。",
     "question_type": "维修型", "material_type": "维修规程", "difficulty": "简单",
     "keywords": ["滤芯"], "source_quote": "300 工作小时", "param_values": ["滤芯更换周期 300 工作小时"],
     "safety_related": False, "doc_no": "HYS-CZ-002", "doc_version": "V3.0",
     "quality_score": 5, "need_human_review": False, "review_reason": ""},
], "doc_summary": "操作规程（含作废版本数值）", "coverage_gaps": []}
r2c = main2(json.dumps(obsolete_pairs, ensure_ascii=False), "某型液压举升系统操作规程", "操作规程", "10")
expect("检出作废版本数值", len(r2c["obsolete_warnings"]), 1)
expect("作废版本告警并入覆盖空白点", len(r2c["coverage_gaps"]), 1)
expect("统计中作废版本计数", json.loads(r2c["stats_json"])["obsolete_warnings"], 1)

r2b = main2(json.dumps({"qa_pairs": []}, ensure_ascii=False), "空文档", "", "abc")
expect("空输入不报错", r2b["qa_count"], 0)
expect("非法 max_pairs 回退默认值", r2b["qa_pairs"], [])

print()
print("=" * 72)
print(f"共 {checks} 项断言，失败 {len(failures)} 项")
for f in failures:
    print("  [FAIL] " + f)
print("=" * 72)
sys.exit(1 if failures else 0)
