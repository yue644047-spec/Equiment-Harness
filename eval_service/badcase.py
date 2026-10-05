"""badcase 挖掘模块：分级、知识补全清单与提示词优化建议。

分级规则：
    P0：FA<=2 或 幻觉风险中/高（或 annotation_label=错误 / error_cause=幻觉编造）
        或 safety_compliance<=2（装备域安全红线）
    P1：CSI<3.0 或 US=dislike 或 version_currency=1（引用了已作废版本）
    P2：KC<=3 或 knowledge_gap 或 terminology_norm<=2 或命中装备域归因

产物：
    badcase_report.md  —— 分级清单 + 知识补全清单 + 提示词优化建议
    badcases.csv       —— 供人工逐条处理的工单表
"""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import metrics
from store import Store

SEVERITY_LABEL = {
    "P0": "P0 严重（事实错误 / 幻觉 / 安全警示缺失）",
    "P1": "P1 重要（低分 / 用户不满 / 引用作废版本）",
    "P2": "P2 一般（覆盖不足 / 术语不规范 / 知识缺失）",
}

SEVERITY_ACTION = {
    "P0": "24 小时内修复：立即停止沿用该回答，核对资料原文、版本与安全条款，必要时下线相关知识片段",
    "P1": "本周内处理：补充或更正资料片段、清理作废版本、调整检索参数后复测",
    "P2": "排期补充：纳入资料补全与术语统一清单，随版本更新",
}

#: 装备域错误归因 → 整改动作。供闭环 SOP、工单表与整改单直接引用。
CAUSE_ACTION: Dict[str, str] = {
    "知识库缺失": "补充对应资料的正文段落并重建索引（kb-build），必要时新增 FAQ 条目",
    "检索未命中": "提高 top_k、开启 rerank，检查该问题的同义问法与关键词覆盖",
    "分块不当": "调整分段规则：按条款编号切分、保留资料编号与版本号、表格不跨段",
    "提示词缺陷": "在系统提示词中补强约束（数值原样引用、术语规范、仅用现行有效版本）",
    "模型能力不足": "更换更强的模型或提高该节点的推理配置后复测",
    "幻觉编造": "强化「仅依据检索片段作答」约束，参数与编号类回答强制附引用来源",
    "知识过期": "更新资料正文并作废旧段落，重建索引",
    "版本失效": "清除知识库中残留的作废版本段落，并在提示词中要求标注资料编号与版本号",
    "问题歧义": "补充同义问法与消歧示例（明确型号、部位、工况）",
    "型号适配错误": "在资料与提示词中明确适用型号，必要时启用元数据过滤按型号检索",
    "术语不统一": "以《术语与缩略语对照表》为准统一术语，并要求缩略语首次出现时展开",
    "安全警示缺失": "把安全警示与禁止事项作为独立段落入库，提示词中禁止为简洁而删减安全条款",
    "跨文档冲突": "组织技术部门核定权威值，统一各资料表述并标注版本",
    "无": "无需整改：本条为误判或已被其他规则覆盖",
}


class BadcaseMiner:
    """badcase 挖掘器。"""

    def __init__(self, store: Store, out_dir: Path, min_severity: str = "P2"):
        """
        :param store: Store
        :param out_dir: 输出目录
        :param min_severity: 最低严重度（P0 只输出 P0，P2 输出全部）
        """
        self.store = store
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.min_severity = (min_severity or "P2").upper()

    # ------------------------------------------------ 挖掘
    def mine(self) -> List[Dict[str, Any]]:
        """从 evaluations 表挖掘全部 badcase 并写回 badcases 表。"""
        rows = self.store.fetch_evaluations()
        items: List[Dict[str, Any]] = []
        for row in rows:
            if not row.get("badcase"):
                continue
            severity = metrics.severity_of(row) or "P2"
            items.append(
                {
                    "message_id": row.get("message_id"),
                    "conversation_id": row.get("conversation_id"),
                    "severity": severity,
                    "question": row.get("question"),
                    "answer": row.get("answer"),
                    "csi": row.get("csi"),
                    "factual_accuracy": row.get("factual_accuracy"),
                    "knowledge_completeness": row.get("knowledge_completeness"),
                    "relevance": row.get("relevance"),
                    "clarity": row.get("clarity"),
                    "retrieval_quality": row.get("retrieval_quality"),
                    "safety_compliance": row.get("safety_compliance"),
                    "version_currency": row.get("version_currency"),
                    "terminology_norm": row.get("terminology_norm"),
                    "annotation_label": row.get("annotation_label"),
                    "error_cause": row.get("error_cause"),
                    "hallucination_risk": row.get("hallucination_risk"),
                    "knowledge_gap": bool(row.get("knowledge_gap")),
                    "user_score": row.get("user_score"),
                    "user_score_raw": row.get("user_score_raw"),
                    "judge_reason": row.get("judge_reason"),
                    "missing_knowledge": row.get("missing_knowledge") or [],
                    "improvement_suggestions": row.get("improvement_suggestions") or [],
                    "reasons": metrics.severity_reasons(row),
                }
            )
        items.sort(key=lambda x: (metrics.SEVERITY_ORDER.get(x["severity"], 9), x.get("csi") or 0))
        self.store.replace_badcases(items)
        return items

    def filtered(self, items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """按 min_severity 过滤（P0 最严重）。"""
        threshold = metrics.SEVERITY_ORDER.get(self.min_severity, 2)
        return [i for i in items if metrics.SEVERITY_ORDER.get(i["severity"], 9) <= threshold]

    # ------------------------------------------------ 聚合清单
    @staticmethod
    def knowledge_gaps(items: Sequence[Dict[str, Any]]) -> List[Tuple[str, int, List[str]]]:
        """聚合「知识补全清单」：去重、按出现次数降序。

        :return: [(知识点, 次数, 关联问题列表)]
        """
        counter: Counter = Counter()
        related: Dict[str, List[str]] = defaultdict(list)
        for item in items:
            for kw in item.get("missing_knowledge") or []:
                key = str(kw).strip()
                if not key:
                    continue
                counter[key] += 1
                if len(related[key]) < 3:
                    related[key].append(str(item.get("question") or "")[:40])
        return [(k, c, related[k]) for k, c in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))]

    @staticmethod
    def prompt_suggestions(items: Sequence[Dict[str, Any]]) -> List[Tuple[str, int, List[str]]]:
        """聚合「提示词优化建议」。"""
        counter: Counter = Counter()
        related: Dict[str, List[str]] = defaultdict(list)
        for item in items:
            for tip in item.get("improvement_suggestions") or []:
                key = str(tip).strip()
                if not key:
                    continue
                counter[key] += 1
                if len(related[key]) < 3:
                    related[key].append(str(item.get("message_id")))
        return [(k, c, related[k]) for k, c in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))]

    @staticmethod
    def cause_summary(items: Sequence[Dict[str, Any]]) -> List[Tuple[str, int]]:
        """按错误归因汇总。"""
        counter: Counter = Counter(str(i.get("error_cause") or "无") for i in items)
        return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))

    # ------------------------------------------------ 产物
    def build_markdown(self, items: Sequence[Dict[str, Any]]) -> str:
        """生成 badcase_report.md 文本。"""
        lines: List[str] = []
        add = lines.append
        add("# badcase 挖掘报告")
        add("")
        add(f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        add(f"- 输出范围：{SEVERITY_LABEL.get(self.min_severity, self.min_severity)} 及以上")
        add(f"- badcase 总数：{len(items)}")
        by_sev = Counter(i["severity"] for i in items)
        add("- 分级构成：" + "，".join(f"{k} {by_sev.get(k, 0)} 条" for k in ("P0", "P1", "P2")))
        add("")

        add("## 一、分级清单")
        add("")
        for sev in ("P0", "P1", "P2"):
            bucket = [i for i in items if i["severity"] == sev]
            if not bucket:
                continue
            add(f"### {SEVERITY_LABEL[sev]}（{len(bucket)} 条）")
            add("")
            add(f"> 处理时限：{SEVERITY_ACTION[sev]}")
            add("")
            add("| 消息ID | CSI | FA | KC | 标签 | 归因 | 触发原因 | 问题 |")
            add("| --- | --- | --- | --- | --- | --- | --- | --- |")
            for it in bucket:
                add(
                    "| {mid} | {csi} | {fa} | {kc} | {lb} | {ca} | {rs} | {q} |".format(
                        mid=it["message_id"],
                        csi=it.get("csi"),
                        fa=it.get("factual_accuracy"),
                        kc=it.get("knowledge_completeness"),
                        lb=it.get("annotation_label") or "-",
                        ca=it.get("error_cause") or "-",
                        rs="；".join(it.get("reasons") or []) or "-",
                        q=(str(it.get("question") or "")[:36]).replace("|", "\\|"),
                    )
                )
            add("")

        add("## 二、知识补全清单（按出现频次排序）")
        add("")
        gaps = self.knowledge_gaps(items)
        if not gaps:
            add("本期没有知识缺失类问题。")
        else:
            add("| # | 待补全知识点 | 出现次数 | 关联问题 | 建议动作 |")
            add("| --- | --- | --- | --- | --- |")
            for idx, (kw, cnt, rel) in enumerate(gaps, 1):
                add(
                    f"| {idx} | {kw} | {cnt} | {'；'.join(r for r in rel if r)} | "
                    "整理为 Markdown 后执行 kb-build 导入并重建索引 |"
                )
        add("")

        add("## 三、提示词优化建议（按出现频次排序）")
        add("")
        tips = self.prompt_suggestions(items)
        if not tips:
            add("暂无建议。")
        else:
            add("| # | 建议 | 命中 badcase 数 | 示例消息 |")
            add("| --- | --- | --- | --- |")
            for idx, (tip, cnt, rel) in enumerate(tips, 1):
                add(f"| {idx} | {tip} | {cnt} | {'、'.join(rel)} |")
        add("")

        add("## 四、错误归因分布与整改动作")
        add("")
        add("| 归因 | 数量 | 整改动作 |")
        add("| --- | --- | --- |")
        for cause, cnt in self.cause_summary(items):
            add(f"| {cause} | {cnt} | {CAUSE_ACTION.get(cause, '按整改单逐条定性后处理')} |")
        add("")

        add("## 五、装备域专项维度分布")
        add("")
        add("| 专项维度 | 命中规则 | 条数 | 说明 |")
        add("| --- | --- | --- | --- |")
        safety_hit = [i for i in items if self._int(i.get("safety_compliance")) and 0 < self._int(i.get("safety_compliance")) <= 2]
        version_hit = [i for i in items if self._int(i.get("version_currency")) == 1]
        term_hit = [i for i in items if self._int(i.get("terminology_norm"), 5) <= 2]
        add(f"| 安全合规性 SC | SC<=2 | {len(safety_hit)} | 遗漏安全前置条件或禁止事项，装备域安全红线，直接判不合格 |")
        add(f"| 版本时效性 VC | VC=1 | {len(version_hit)} | 引用了已作废版本资料，需清理知识库中的作废段落 |")
        add(f"| 术语规范性 TN | TN<=2 | {len(term_hit)} | 术语或缩略语不规范，需按术语对照表统一 |")
        add("")

        add("## 六、改进闭环建议")
        add("")
        add("1. **知识补全**：把第二节清单交给资料管理员，按周导入并复跑 judge + badcase 验证效果。")
        add("2. **提示词加固**：对幻觉类 badcase，在系统提示词中加入「仅依据检索片段作答、参数与编号必须原样引用」的强约束。")
        add("3. **安全条款加固**：对安全警示缺失类 badcase，把安全前置条件与禁止事项拆为独立段落入库，并在提示词中禁止删减。")
        add("4. **版本治理**：对版本失效类 badcase，按《装备技术资料清单与版本台账》清理知识库中的作废版本段落。")
        add("5. **术语统一**：对术语不统一类 badcase，以《术语与缩略语对照表》为准回填资料与提示词。")
        add("6. **检索调优**：对检索未命中类 badcase，提升 top_k、开启 rerank，并检查分块是否切断了表格、步骤与条款编号。")
        add("7. **回归验证**：修复后用 regression 子命令跑黄金集，确认改进而非引入新退化。")
        add("")
        return "\n".join(lines)

    @staticmethod
    def _int(value: Any, default: int = 0) -> int:
        """把可能为 None/字符串的维度值安全转成整数。"""
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def write_csv(self, items: Sequence[Dict[str, Any]], path: Path) -> Path:
        """导出可人工处理的 badcases.csv（utf-8-sig）。"""
        columns = [
            ("message_id", "消息ID"), ("severity", "严重度"), ("csi", "CSI"),
            ("factual_accuracy", "FA"), ("knowledge_completeness", "KC"),
            ("safety_compliance", "安全合规性SC"), ("version_currency", "版本时效性VC"),
            ("terminology_norm", "术语规范性TN"),
            ("annotation_label", "标注标签"), ("error_cause", "归因"),
            ("hallucination_risk", "幻觉风险"), ("knowledge_gap", "知识缺失"),
            ("user_score_raw", "用户评分"), ("reasons", "触发原因"),
            ("question", "问题"), ("answer", "回答"), ("judge_reason", "评分理由"),
            ("missing_knowledge", "待补全知识点"), ("suggestions", "改进建议"),
            ("owner", "处理人"), ("status", "处理状态"),
        ]
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow([label for _, label in columns])
            for it in items:
                row = dict(it)
                row["reasons"] = "；".join(it.get("reasons") or [])
                row["missing_knowledge"] = "；".join(it.get("missing_knowledge") or [])
                row["suggestions"] = "；".join(it.get("improvement_suggestions") or [])
                row["knowledge_gap"] = "是" if it.get("knowledge_gap") else "否"
                row["owner"] = ""
                row["status"] = "待处理"
                writer.writerow([row.get(k, "") for k, _ in columns])
        return path

    def generate(self, quiet: bool = False) -> Dict[str, Any]:
        """挖掘 + 生成报告与 CSV。

        :return: {"items": [...], "paths": {...}, "gaps": [...], "suggestions": [...]}
        """
        all_items = self.mine()
        items = self.filtered(all_items)
        report_path = self.out_dir / "badcase_report.md"
        report_path.write_text(self.build_markdown(items), encoding="utf-8")
        csv_path = self.write_csv(items, self.out_dir / "badcases.csv")
        result = {
            "items": items,
            "all_items": all_items,
            "paths": {"md": report_path, "csv": csv_path},
            "gaps": self.knowledge_gaps(items),
            "suggestions": self.prompt_suggestions(items),
        }
        if not quiet:
            by_sev = Counter(i["severity"] for i in items)
            print(
                f"[badcase] 共 {len(items)} 条（P0 {by_sev.get('P0', 0)}，"
                f"P1 {by_sev.get('P1', 0)}，P2 {by_sev.get('P2', 0)}）"
            )
            print(f"[badcase] 报告：{report_path}")
            print(f"[badcase] 工单表：{csv_path}")
        return result


def mine_badcases(store: Store, out_dir: Path, min_severity: str = "P2", quiet: bool = False) -> Dict[str, Any]:
    """便捷入口：挖掘 badcase 并产出报表。"""
    return BadcaseMiner(store, out_dir, min_severity=min_severity).generate(quiet=quiet)
