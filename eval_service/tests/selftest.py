#!/usr/bin/env python
"""自检脚本：校验契约公式、解析逻辑与 mock 全链路。

运行::

    python tests/selftest.py

覆盖内容：
    1. user_score 归一化
    2. CSI 计算公式（有 US / 无 US）
    3. verdict 阈值与两条红线
    4. badcase 判定与严重度分级
    5. 对话内回执 [[EVAL_RECEIPT]] 解析（键值对 / JSON / 文本回退三格式）
    6. mock 全链路（collect -> judge -> report -> badcase）产物是否生成
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import metrics  # noqa: E402
from badcase import mine_badcases  # noqa: E402
from collector import (  # noqa: E402
    is_receipt_only,
    parse_eval_receipt,
    parse_receipt,
    resolve_user_score,
    strip_receipt,
)
from config import load_config  # noqa: E402
from dify_client import DifyClient  # noqa: E402
from collector import Collector  # noqa: E402
from grounding import check_grounding  # noqa: E402
from judge import Judge, mock_score_row  # noqa: E402
from report import ReportBuilder  # noqa: E402
from store import Store  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, actual: object, expected: object) -> None:
    """断言相等并累加统计。"""
    global PASSED, FAILED
    if actual == expected:
        PASSED += 1
        print(f"  [OK]   {name}: {actual}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name}: 实际 {actual!r} != 期望 {expected!r}")


def section(title: str) -> None:
    """打印分节标题。"""
    print(f"\n=== {title} ===")


def test_user_score() -> None:
    """user_score 归一化。"""
    section("1. user_score 归一化")
    check("like -> 5", metrics.normalize_user_score("like"), 5)
    check("dislike -> 1", metrics.normalize_user_score("dislike"), 1)
    check("'4' -> 4", metrics.normalize_user_score("4"), 4)
    check("'' -> None", metrics.normalize_user_score(""), None)
    check("None -> None", metrics.normalize_user_score(None), None)


def test_csi() -> None:
    """CSI 公式。"""
    section("2. CSI 计算公式")
    # 有 US：0.30*5 + 0.25*4 + 0.20*4 + 0.15*4 + 0.10*4 = 1.5+1+0.8+0.6+0.4 = 4.30
    check("有 US 的 CSI", metrics.csi_from_scores(4, 4, 4, 4, 5), 4.30)
    # 无 US：(0.25*4 + 0.20*3 + 0.15*5 + 0.10*4)/0.70 = (1+0.6+0.75+0.4)/0.7 = 3.9285.. -> 3.93
    check("无 US 的 CSI（权重归一化）", metrics.csi_from_scores(4, 3, 5, 4, None), 3.93)
    check("维度缺失返回 None", metrics.csi_from_scores(None, 3, 5, 4, 5), None)


def test_verdict() -> None:
    """verdict 阈值与红线。"""
    section("3. verdict 判定与红线")
    check("CSI 4.2 -> 优秀", metrics.decide_verdict(4.2, 5, "like"), "优秀")
    check("CSI 3.5 -> 良好", metrics.decide_verdict(3.5, 5, "like"), "良好")
    check("CSI 2.8 -> 待改进", metrics.decide_verdict(2.8, 5, "like"), "待改进")
    check("CSI 2.79 -> 不合格", metrics.decide_verdict(2.79, 5, "like"), "不合格")
    check("红线 FA=2 -> 不合格", metrics.decide_verdict(4.8, 2, "like"), "不合格")
    check("红线 dislike 且 CSI>=3.5 -> 降级", metrics.decide_verdict(4.5, 5, "dislike"), "待改进")
    check("dislike 但 CSI<3.5 不触发降级", metrics.decide_verdict(3.0, 5, "dislike"), "待改进")

    # --- 装备域专项红线 ---
    check("装备域安全红线 SC=2 -> 不合格", metrics.decide_verdict(5.0, 5, "like", safety_compliance=2), "不合格")
    check("安全合规性 SC=1 -> 不合格", metrics.decide_verdict(5.0, 5, "like", safety_compliance=1), "不合格")
    check("安全合规性 SC=0（不涉及）不触发红线", metrics.decide_verdict(5.0, 5, "like", safety_compliance=0), "优秀")
    check("安全合规性 SC=3 不触发红线", metrics.decide_verdict(5.0, 5, "like", safety_compliance=3), "优秀")
    check("引用作废版本 VC=1 -> 最多待改进", metrics.decide_verdict(5.0, 5, "like", version_currency=1), "待改进")
    check("安全红线强于版本降级", metrics.decide_verdict(5.0, 5, "like", safety_compliance=1, version_currency=1), "不合格")
    check("FA 红线强于安全红线（同为不合格）", metrics.decide_verdict(5.0, 2, "like", safety_compliance=1), "不合格")


def test_badcase() -> None:
    """badcase 判定与分级。"""
    section("4. badcase 判定与分级")
    check("CSI<3 -> badcase", metrics.is_badcase(2.9, 5, 5, False, "like"), True)
    check("FA<=2 -> badcase", metrics.is_badcase(4.9, 2, 5, False, "like"), True)
    check("dislike -> badcase", metrics.is_badcase(4.9, 5, 5, False, "dislike"), True)
    check("gap 且 KC<=2 -> badcase", metrics.is_badcase(4.9, 5, 2, True, "like"), True)
    check("正常 -> 非 badcase", metrics.is_badcase(4.5, 5, 5, False, "like"), False)
    check("P0（FA=2）", metrics.severity_of({"factual_accuracy": 2, "csi": 4.9}), "P0")
    check("P1（CSI<3）", metrics.severity_of({"factual_accuracy": 4, "csi": 2.9}), "P1")
    check("P2（gap）", metrics.severity_of({"factual_accuracy": 4, "csi": 3.2, "knowledge_gap": True}), "P2")

    # --- 装备域专项维度 ---
    check("SC<=2 -> badcase", metrics.is_badcase(4.9, 5, 5, False, "like", safety_compliance=2), True)
    check("VC=1 -> badcase", metrics.is_badcase(4.9, 5, 5, False, "like", version_currency=1), True)
    check("TN<=2 -> badcase", metrics.is_badcase(4.9, 5, 5, False, "like", terminology_norm=2), True)
    check("装备域归因 -> badcase", metrics.is_badcase(4.9, 5, 5, False, "like", error_cause="跨文档冲突"), True)
    check("装备域归因以外的归因不误伤", metrics.is_badcase(4.9, 5, 5, False, "like", error_cause="检索未命中"), False)
    check("SC=0/VC=0/TN=5 -> 非 badcase", metrics.is_badcase(4.9, 5, 5, False, "like"), False)
    check("P0（安全红线 SC=2）", metrics.severity_of({"factual_accuracy": 5, "csi": 4.9, "safety_compliance": 2}), "P0")
    check("P1（引用作废版本 VC=1）", metrics.severity_of({"factual_accuracy": 5, "csi": 4.9, "version_currency": 1}), "P1")
    check("P2（术语不规范 TN=2）", metrics.severity_of({"factual_accuracy": 5, "csi": 4.9, "terminology_norm": 2}), "P2")
    check(
        "不涉及任何专项维度 -> 无分级",
        metrics.severity_of({"factual_accuracy": 5, "csi": 4.9, "safety_compliance": 0,
                             "version_currency": 0, "terminology_norm": 5}),
        None,
    )
    check("derive 透传安全红线", metrics.derive(5, 5, 5, 5, False, "", safety_compliance=2)["verdict"], "不合格")
    check("derive 透传版本降级", metrics.derive(5, 5, 5, 5, False, "", version_currency=1)["verdict"], "待改进")
    check(
        "旧签名调用结果不变（向后兼容）：like -> US=5，CSI=0.30*5+2.8=4.3",
        metrics.derive(4, 4, 4, 4, False, "like"),
        {"user_score": 5, "csi": 4.3, "verdict": "优秀", "badcase": False},
    )
    check(
        "旧签名调用结果不变（向后兼容）：无用户评分",
        metrics.derive(4, 4, 4, 4, False, ""),
        {"user_score": None, "csi": 4.0, "verdict": "良好", "badcase": False},
    )


def test_receipt() -> None:
    """回执解析：规范键值对 / 历史 JSON / 人类可读回退 三种格式与防误判。"""
    section("5. 对话内回执解析")

    # --- (a) 规范格式：dsl/01-智能问答-Chatflow.yml 的实际输出 ---
    canonical = (
        "✅ 已记录你的评价，感谢反馈！\n\n"
        "- 满意度评分：4 / 5\n"
        '- 改进建议：希望补充"低温用油要求"说明，\n以及 score=9 的口径\n\n'
        "我们会据此补充知识库内容并优化回答质量。\n"
        '[[EVAL_RECEIPT]]score=4,suggestion=希望补充"低温用油要求"说明，\n以及 score=9 的口径[[/EVAL_RECEIPT]]'
    )
    score, suggestion = parse_receipt(canonical)
    check("规范回执-分数", score, 4)
    check(
        "规范回执-建议保留逗号/换行/引号与 score= 干扰字样",
        suggestion,
        '希望补充"低温用油要求"说明，\n以及 score=9 的口径',
    )
    check("规范回执-剥离后不含标记", "EVAL_RECEIPT" in strip_receipt(canonical), False)

    empty_suggestion = "感谢反馈！\n[[EVAL_RECEIPT]]score=5,suggestion=[[/EVAL_RECEIPT]]"
    check("规范回执-空建议的分数", parse_receipt(empty_suggestion)[0], 5)
    check("规范回执-空建议的文本", parse_receipt(empty_suggestion)[1], "")
    check(
        "规范回执-越界分数不采纳",
        parse_receipt("[[EVAL_RECEIPT]]score=9,suggestion=x[[/EVAL_RECEIPT]]")[0],
        None,
    )

    # --- (b) 历史 JSON 格式（向后兼容）---
    legacy = '正常回答内容。\n\n[[EVAL_RECEIPT]]{"score":4,"suggestion":"补充示例"}'
    score, suggestion = parse_receipt(legacy)
    check("历史 JSON-分数", score, 4)
    check("历史 JSON-建议", suggestion, "补充示例")
    check("历史 JSON-剥离回执", strip_receipt(legacy), "正常回答内容。")

    # --- (c) 人类可读回退（机器标记被误删时的兜底）---
    text_only = "✅ 已记录你的评价！\n\n- 满意度评分：2 / 5\n- 改进建议：答案太长\n"
    check("文本回退-分数", parse_receipt(text_only)[0], 2)
    check("文本回退-建议", parse_receipt(text_only)[1], "答案太长")

    # --- 防误判：普通回答不能被抓成评价 ---
    check(
        "普通回答不误判",
        parse_receipt("滤芯每 500 工作小时或 12 个月更换一次，依据 HYS-WX-003 V2.0 第 3.1.9 条。"),
        (None, ""),
    )
    check("仅含改进建议字样不误判", parse_receipt("改进建议：可以直接联系 HR。"), (None, ""))

    # --- 与 Dify 原生 feedback 的优先级 ---
    check("dislike 优先于回执", resolve_user_score("dislike", "", 5), "dislike")
    check("回执优先于 like", resolve_user_score("like", "", 3), "3")
    check("无信号 -> 空", resolve_user_score(None, None, None), "")


def test_mock_rules() -> None:
    """mock 评分规则与装备域专项维度的基本行为。"""
    section("6. mock 规则评分与装备域专项维度")
    correct = mock_score_row(
        "滤芯多久更换一次？",
        "滤芯更换周期为每 500 工作小时或 12 个月，以先到为准。",
        "HYS-WX-003 维修规程 3.1.9：滤芯更换周期为每 500 工作小时或 12 个月，以先到为准。",
        resources=[{"score": 0.95}],
    )
    check("正确回答 FA>=4", correct["factual_accuracy"] >= 4, True)
    check("正确回答 RQ（0.95*5=4.75 -> 5）", correct["retrieval_quality"], 5)
    check("周期类提问不触发安全维度", correct["safety_compliance"], 0)
    check("周期类提问不涉及版本引用", correct["version_currency"], 0)

    gap = mock_score_row("这套系统能在 -30 ℃ 环境下使用吗？", "知识库中未收录相关信息，我无法确认。", "", resources=[])
    check("知识缺失 knowledge_gap", gap["knowledge_gap"], True)
    check("知识缺失 RQ=0", gap["retrieval_quality"], 0)

    fake = mock_score_row(
        "滤芯多久更换一次？",
        "滤芯每 800 工作小时更换一次。",
        "HYS-WX-003 维修规程：滤芯更换周期为每 500 工作小时或 12 个月，以先到为准。",
        resources=[{"score": 0.8}],
    )
    check("数值冲突 -> 幻觉编造", fake["error_cause"], "幻觉编造")
    check("数值冲突 -> FA<=2", fake["factual_accuracy"] <= 2, True)

    # --- 装备域专项维度 ---
    obsolete = mock_score_row(
        "滤芯多久更换一次？",
        "滤芯每 300 工作小时更换一次。",
        "HYS-ZL-005 版本台账：滤芯更换周期由 300 工作小时修订为 500 工作小时，V3.0 已作废。",
        resources=[{"score": 0.8}],
    )
    check("引用作废版本 -> VC=1", obsolete["version_currency"], 1)
    check("引用作废版本 -> 归因版本失效", obsolete["error_cause"], "版本失效")

    unsafe = mock_score_row(
        "如何更换滤芯？",
        "打开滤芯壳体，取出旧滤芯，装入新滤芯后拧紧即可。",
        "HYS-WX-003 维修规程：作业前必须停机、切断电源挂牌上锁、泄压至 0 MPa、机械支撑稳固。",
        resources=[{"score": 0.8}],
    )
    check("操作类提问遗漏安全前置 -> SC<=2", unsafe["safety_compliance"] <= 2, True)
    check("操作类提问遗漏安全前置 -> 归因安全警示缺失", unsafe["error_cause"], "安全警示缺失")

    safe = mock_score_row(
        "维修前要做哪些安全准备？",
        "必须先停机，切断电源并挂牌上锁，将系统泄压至 0 MPa，并用机械支撑稳固，待油温降至 45 ℃ 以下。",
        "HYS-WX-003 维修规程：作业前必须停机、切断电源挂牌上锁、泄压至 0 MPa、机械支撑稳固、待油温降至 45 ℃ 以下。",
        resources=[{"score": 0.9}],
    )
    check("完整覆盖安全前置 -> SC>=4", safe["safety_compliance"] >= 4, True)
    check("完整覆盖安全前置 -> 不归因安全警示缺失", safe["error_cause"] != "安全警示缺失", True)

    bad_term = mock_score_row(
        "滤芯多久更换一次？",
        "当油压不足或液压缸动作慢时就要更换滤芯。",
        "HYS-WX-003 维修规程：滤芯更换周期为每 500 工作小时或 12 个月，以先到为准。",
        resources=[{"score": 0.8}],
    )
    check("非规范术语 -> TN<=2", bad_term["terminology_norm"] <= 2, True)

    version_ok = mock_score_row(
        "滤芯更换周期依据哪份资料？",
        "依据 HYS-WX-003 V2.0 维修规程 3.1.9 条，每 500 工作小时或 12 个月。",
        "HYS-WX-003 V2.0：滤芯更换周期为每 500 工作小时或 12 个月，以先到为准。",
        resources=[{"score": 0.9}],
    )
    check("版本类提问且正确引用 -> VC>=4", version_ok["version_currency"] >= 4, True)


def test_eval_receipt_forms() -> None:
    """parse_eval_receipt：三种形式的优先级与 form 字段契约（回执格式修正后新增）。"""
    section("5b. parse_eval_receipt 三形式与 form 字段")

    # 用例 1：(a) 键值对；建议里含逗号、换行、引号，以及 "score=9" 干扰字样
    kv = (
        "✅ 已记录你的评价，感谢反馈！\n\n"
        "- 满意度评分：4 / 5\n"
        "- 改进建议：（见机器标记）\n\n"
        "[[EVAL_RECEIPT]]score=4,suggestion=\"低温用油要求\"说明，需含逗号\n"
        "以及 score=9 的干扰字样[[/EVAL_RECEIPT]]"
    )
    r1 = parse_eval_receipt(kv)
    check("用例1 form", r1["form"], "kv")
    check("用例1 score", r1["score"], 4)
    check(
        "用例1 suggestion（逗号/换行/引号/score= 干扰全部原样保留）",
        r1["suggestion"],
        '"低温用油要求"说明，需含逗号\n以及 score=9 的干扰字样',
    )

    # 用例 2：(a) 键值对，建议为空
    r2 = parse_eval_receipt("✅ 已记录你的评价。\n[[EVAL_RECEIPT]]score=5,suggestion=[[/EVAL_RECEIPT]]")
    check("用例2 form/score/suggestion", (r2["form"], r2["score"], r2["suggestion"]), ("kv", 5, ""))

    # 用例 3：(b) 历史 JSON 格式
    r3 = parse_eval_receipt('[[EVAL_RECEIPT]]{"score":3,"suggestion":"补充示例"}')
    check("用例3 form/score/suggestion", (r3["form"], r3["score"], r3["suggestion"]), ("json", 3, "补充示例"))

    # 用例 4：(c) 只有人类可读正文，没有机器标记
    r4 = parse_eval_receipt("✅ 已记录你的评价，感谢反馈！\n\n- 满意度评分：2 / 5\n- 改进建议：答案太长\n")
    check("用例4 form/score/suggestion", (r4["form"], r4["score"], r4["suggestion"]), ("text", 2, "答案太长"))
    r4b = parse_eval_receipt("✅ 已记录你的评价。\n- 满意度评分：5 / 5\n- 改进建议：\n")
    check("用例4b 建议行为空 -> 空建议", (r4b["form"], r4b["score"], r4b["suggestion"]), ("text", 5, ""))

    # 用例 5：完全无关的普通回答
    r5 = parse_eval_receipt("滤芯每 500 工作小时更换一次，依据 HYS-WX-003 V2.0 第 3.1.9 条。")
    check("用例5 form/score/suggestion", (r5["form"], r5["score"], r5["suggestion"]), ("none", None, ""))
    check(
        "空输入",
        parse_eval_receipt(""),
        {"score": None, "satisfaction": None, "usefulness": None, "experience": None,
         "suggestion": "", "form": "none"},
    )

    # 用例 6：(a) 键值对 v2 —— 三项主观指标（当前 Dify 回执模板的实际输出）
    kv3 = (
        "✅ 已记录你的评价，感谢反馈！\n\n"
        "- 主观指标1 满意度：4 / 5\n"
        "- 主观指标2 有用性：5 / 5\n"
        "- 主观指标3 体验感：3 / 5\n"
        "- 改进建议：请补充低温用油要求\n\n"
        "[[EVAL_RECEIPT]]satisfaction=4,usefulness=5,experience=3,suggestion=请补充低温用油要求[[/EVAL_RECEIPT]]"
    )
    r6 = parse_eval_receipt(kv3)
    check("用例6 form（三项主观回执）", r6["form"], "kv3")
    check("用例6 满意度 -> score", (r6["score"], r6["satisfaction"]), (4, 4))
    check("用例6 有用性/体验感", (r6["usefulness"], r6["experience"]), (5, 3))
    check("用例6 建议", r6["suggestion"], "请补充低温用油要求")
    check("用例6 规范回执 -> is_receipt_only", is_receipt_only(kv3), True)
    # 三项回执缺记号时的人类可读兜底
    r6b = parse_eval_receipt("- 主观指标1 满意度：4 / 5\n- 主观指标2 有用性：2 / 5\n"
                             "- 主观指标3 体验感：5 / 5\n- 改进建议：\n")
    check("用例6b 文本回退三项", (r6b["form"], r6b["score"], r6b["usefulness"], r6b["experience"]),
          ("text", 4, 2, 5))
    # v1 单分回执仍然可用，且满意度按 score 回填
    check("v1 回执 -> satisfaction 回填", parse_eval_receipt(kv)["satisfaction"], 4)

    # 优先级：键值对优先于正文回退；「仅回执」识别
    mixed = kv + "\n[[EVAL_RECEIPT]]{\"score\":1,\"suggestion\":\"legacy\"}"
    check("kv 优先于 json/text", parse_eval_receipt(mixed)["form"], "kv")
    check("规范回执 -> is_receipt_only", is_receipt_only(kv), True)
    check("普通回答 -> 非 is_receipt_only", is_receipt_only("滤芯每 500 工作小时更换一次。"), False)


def test_pipeline() -> None:
    """mock 全链路。"""
    section("7. mock 全链路（collect -> judge -> report -> badcase）")
    tmp = Path(tempfile.mkdtemp(prefix="dify-eval-selftest-"))
    try:
        cfg = load_config(mock=True)
        cfg.db_path = tmp / "selftest.db"
        cfg.out_dir = tmp / "out"
        cfg.ensure_dirs()
        store = Store(cfg.db_path)
        store.init_schema()
        cstats = Collector(DifyClient.from_config(cfg, "app"), store, cfg, verbose=False).collect()
        check("采集消息数 >= 12", cstats.messages >= 12, True)
        jstats = Judge(DifyClient.from_config(cfg, "eval"), store, cfg, verbose=False).run()
        check("评分成功数 + 跳过回执数 == 采集数", jstats.succeeded + jstats.skipped, cstats.messages)
        check("至少跳过 1 条「评价回执」消息", jstats.skipped >= 1, True)
        row = store.conn.execute(
            "SELECT receipt_score, receipt_suggestion, user_score, answer FROM messages WHERE id='msg-2016'"
        ).fetchone()
        check("端到端：回执 score=4", row["receipt_score"], 4)
        check("端到端：回执建议完整", row["receipt_suggestion"], "希望补充低温用油要求的说明")
        check("端到端：user_score=4", row["user_score"], 4)
        check("端到端：回执正文中的换行被保留", "\n" in (row["answer"] or ""), True)
        judged = store.conn.execute(
            "SELECT COUNT(*) AS c FROM evaluations WHERE message_id='msg-2016'"
        ).fetchone()["c"]
        check("回执消息未被当作问答评分", judged, 0)
        outputs = ReportBuilder(store, cfg.out_dir).generate(period="all", formats=("md", "html", "csv"), quiet=True)
        check("生成 md/html/csv", sorted(outputs.keys()), ["csv", "html", "md"])
        bc = mine_badcases(store, cfg.out_dir, quiet=True)
        check("badcase 报告存在", bc["paths"]["md"].exists(), True)
        check("badcases.csv 存在", bc["paths"]["csv"].exists(), True)
        labels = {i["annotation_label"] for i in bc["all_items"]}
        check("badcase 覆盖多种标签", len(labels) >= 2, True)

        # --- 装备域专项维度的端到端贯通（落库 -> 报表 -> badcase 报告）---
        md_path = Path(outputs["md"])
        check("周报文件存在", md_path.exists(), True)
        md_text = md_path.read_text(encoding="utf-8")
        check("周报含装备域专项维度区块", "装备域专项维度" in md_text, True)
        check("周报表头含 SC/VC/TN 列", all(f"| {c} |" in md_text for c in ("SC", "VC", "TN")), True)
        bc_md = bc["paths"]["md"].read_text(encoding="utf-8")
        check("badcase 报告含装备域专项维度分布", "装备域专项维度分布" in bc_md, True)
        check("badcase 报告含归因整改动作", "整改动作" in bc_md, True)
        dom_rows = store.conn.execute(
            "SELECT COUNT(*) AS c FROM evaluations WHERE terminology_norm IS NOT NULL"
        ).fetchone()["c"]
        judged_rows = store.conn.execute("SELECT COUNT(*) AS c FROM evaluations").fetchone()["c"]
        check("装备域专项维度已落库", dom_rows, judged_rows)
        check("专项维度列可查询", store.conn.execute(
            "SELECT COUNT(*) AS c FROM evaluations WHERE safety_compliance IS NOT NULL "
            "AND version_currency IS NOT NULL"
        ).fetchone()["c"], judged_rows)
        store.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_grounding() -> None:
    """生成后可核验性：答案里的数值/编号/版本/条款是否被命中片段支撑。"""
    section("9. 生成后可核验性（grounding，确定性比对）")
    ctx = ("HYS-CZ-002 V4.0 §2.5.2：滤芯更换周期为每 500 工作小时或 12 个月，以先到为准。"
           "液压油为 L-HM46，系统额定工作压力 16 MPa。")

    r1 = check_grounding("滤芯每 500 工作小时或 12 个月更换一次（HYS-CZ-002 V4.0 §2.5.2）。", ctx)
    check("有据可依的回答 → ok", r1["level"], "ok")
    check("有据可依的回答 → 覆盖率 1.0", r1["rate"], 1.0)

    r2 = check_grounding("滤芯每 300 工作小时更换一次（HYS-CZ-002 V3.0）。", ctx)
    check("引用作废数值+作废版本 → fail", r2["level"], "fail")
    check("未支撑项被逐条列出", sorted(u["kind"] for u in r2["ungrounded"]), ["number", "version"])

    r3 = check_grounding("系统额定工作压力为 20 MPa，请以现场为准。", ctx)
    check("单条未支撑数值 → warn", r3["level"], "warn")

    r4 = check_grounding("你好，我是装备资料助手。", ctx)
    check("无事实性 token → n/a", r4["level"], "n/a")

    r5 = check_grounding("依据资料，压力为 16 MPa。", "")
    check("无命中片段时不误判跑偏 → n/a", r5["level"], "n/a")

    r6 = check_grounding("详见第 9.9.9 条。", ctx)
    check("未支撑条款号 → fail", r6["level"], "fail")
    check("未支撑条款号被识别", r6["ungrounded"][0]["kind"], "clause")


def main() -> int:
    """运行全部自检。"""
    print("Dify 评测服务自检开始")
    test_user_score()
    test_csi()
    test_verdict()
    test_badcase()
    test_receipt()
    test_eval_receipt_forms()
    test_mock_rules()
    test_pipeline()
    test_grounding()
    print(f"\n自检结束：通过 {PASSED} 项，失败 {FAILED} 项")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
