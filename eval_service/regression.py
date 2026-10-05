"""回归评测模块：升级前后对比黄金测试集上的表现。

流程：
    1. 读取 golden_set.jsonl（question / reference_answer / tags）；
    2. 逐条调用问答应用 POST /v1/chat-messages 拿到答案与检索片段；
    3. 自动调用评价工作流（mock 模式用本地规则）对每条打分；
    4. 与 baseline JSON 对比，输出改进 / 退步 / 不变明细与显著性说明。

显著性说明：对「改进 vs 退步」做符号检验（双侧精确二项分布），
样本量 n<10 时不宣称统计显著，只给方向性结论。
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import metrics
from collector import parse_receipt, resolve_user_score, strip_receipt
from config import Config
from dify_client import DifyClient, DifyError
from judge import Judge
from store import Store, dumps, join_contexts, loads, now_ts

#: 判定为「有变化」的最小 CSI 差值
DELTA_THRESHOLD = 0.1

STATUS_IMPROVED = "改进"
STATUS_REGRESSED = "退步"
STATUS_UNCHANGED = "不变"
STATUS_NEW = "新增"


@dataclass
class GoldenItem:
    """黄金测试集条目。"""

    question: str
    reference_answer: str = ""
    tags: List[str] = field(default_factory=list)
    item_id: str = ""

    def as_dict(self) -> Dict[str, Any]:
        """序列化。"""
        return {"id": self.item_id, "question": self.question, "reference_answer": self.reference_answer, "tags": self.tags}


def load_golden_set(path: Path) -> List[GoldenItem]:
    """读取 JSONL 黄金测试集（每行一个 JSON 对象）。

    :param path: golden_set.jsonl 路径
    :return: GoldenItem 列表
    :raises SystemExit: 文件不存在或格式错误
    """
    path = Path(path)
    if not path.exists():
        print(f"[regression] 黄金测试集不存在：{path}")
        raise SystemExit(2)
    items: List[GoldenItem] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                obj = json.loads(line)
            except ValueError as exc:
                print(f"[regression] 第 {lineno} 行不是合法 JSON：{exc}")
                continue
            question = str(obj.get("question") or "").strip()
            if not question:
                print(f"[regression] 第 {lineno} 行缺少 question，已跳过")
                continue
            tags = obj.get("tags") or []
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]
            items.append(
                GoldenItem(
                    question=question,
                    reference_answer=str(obj.get("reference_answer") or ""),
                    tags=[str(t) for t in tags],
                    item_id=str(obj.get("id") or f"g{len(items) + 1:02d}"),
                )
            )
    if not items:
        print(f"[regression] 黄金测试集为空：{path}")
        raise SystemExit(2)
    return items


def sign_test_p_value(improved: int, regressed: int) -> Optional[float]:
    """符号检验的双侧精确 p 值（二项分布，p=0.5）。

    :param improved: 改进条数
    :param regressed: 退步条数
    :return: p 值；有效样本过少（<5）时返回 None
    """
    n = improved + regressed
    if n < 5:
        return None
    k = min(improved, regressed)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / (2 ** n)
    return round(min(1.0, 2 * tail), 4)


class RegressionRunner:
    """回归评测执行器。"""

    def __init__(
        self,
        cfg: Config,
        store: Store,
        out_dir: Path,
        concurrency: int = 4,
        mock_variant: str = "current",
        verbose: bool = True,
    ):
        """
        :param cfg: 配置
        :param store: Store（仅用于登记 runs）
        :param out_dir: 输出目录
        :param concurrency: 并发数
        :param mock_variant: mock 模式下取用的答案版本（current/baseline）
        """
        self.cfg = cfg
        self.store = store
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.concurrency = concurrency
        self.mock_variant = mock_variant
        self.verbose = verbose
        self.chat_client = DifyClient.from_config(cfg, "app")
        self.eval_client = DifyClient.from_config(cfg, "eval")
        self.judge = Judge(self.eval_client, store, cfg, concurrency=concurrency, verbose=False)

    def _log(self, message: str) -> None:
        """打印进度。"""
        if self.verbose:
            print(f"[regression] {message}")

    def ask(self, item: GoldenItem, index: int) -> Dict[str, Any]:
        """调用问答应用获取答案与检索片段。"""
        inputs: Dict[str, Any] = {}
        if self.chat_client.is_mock:
            inputs["mock_variant"] = self.mock_variant
        resp = self.chat_client.chat_messages(item.question, inputs=inputs, user="regression")
        raw_answer = resp.get("answer") or ""
        # 与采集链路保持一致：解析并剥离对话内回执，回执分数参与 user_score
        receipt_score, receipt_suggestion = parse_receipt(raw_answer)
        user_score_raw = resolve_user_score(None, None, receipt_score)
        return {
            "id": f"reg-{item.item_id}",
            "conversation_id": resp.get("conversation_id") or "regression",
            "query": item.question,
            "answer": strip_receipt(raw_answer),
            "retriever_resources_json": dumps(resp.get("retriever_resources") or []),
            "receipt_score": receipt_score,
            "receipt_suggestion": receipt_suggestion,
            "feedback_content": "",
            "user_score_raw": user_score_raw,
        }

    def run(self, golden_path: Path, baseline_path: Path, save_baseline: bool = False) -> Dict[str, Any]:
        """执行回归评测。

        :param golden_path: 黄金集路径
        :param baseline_path: baseline JSON 路径
        :param save_baseline: 是否把本次结果写为新的 baseline
        :return: 结果字典（含路径与统计）
        """
        items = load_golden_set(golden_path)
        run_id = self.store.start_run(
            "regression",
            {"golden": str(golden_path), "baseline": str(baseline_path), "mock": bool(self.cfg.mock)},
        )
        self._log(f"黄金集 {len(items)} 条，模式 {'mock' if self.cfg.mock else 'live'}")

        current: Dict[str, Dict[str, Any]] = {}
        errors: List[str] = []
        for idx, item in enumerate(items, 1):
            try:
                message = self.ask(item, idx)
                scored = self.judge.score_one(message, reference_answer=item.reference_answer)
                current[item.question] = {
                    "id": item.item_id,
                    "question": item.question,
                    "tags": item.tags,
                    "reference_answer": item.reference_answer,
                    "answer": message["answer"],
                    "csi": scored.get("csi"),
                    "verdict": scored.get("verdict"),
                    "annotation_label": scored.get("annotation_label"),
                    "error_cause": scored.get("error_cause"),
                    "knowledge_gap": bool(scored.get("knowledge_gap")),
                    "badcase": bool(scored.get("badcase")),
                    "user_score": scored.get("user_score"),
                    "dimensions": {
                        k: scored.get(k)
                        for k in ("factual_accuracy", "knowledge_completeness", "relevance", "clarity", "retrieval_quality")
                    },
                    "contexts": scored.get("contexts") or "",
                    "judge_reason": scored.get("judge_reason"),
                }
                self._log(f"[{idx}/{len(items)}] {item.question[:26]}… CSI={scored.get('csi')} {scored.get('verdict')}")
            except Exception as exc:  # noqa: BLE001 - 单条失败不中断
                errors.append(f"{item.item_id} 失败：{exc}")
                self._log(f"[{idx}/{len(items)}] 失败：{exc}")

        baseline = self.load_baseline(baseline_path)
        comparison = self.compare(baseline, current)

        paths = self.write_outputs(comparison, current, baseline_path, save_baseline)
        stats = {
            "total": len(items),
            "scored": len(current),
            "errors": errors,
            "improved": comparison["summary"]["improved"],
            "regressed": comparison["summary"]["regressed"],
            "unchanged": comparison["summary"]["unchanged"],
            "mean_csi": comparison["summary"]["mean_csi"],
            "mean_delta": comparison["summary"]["mean_delta"],
            "p_value": comparison["summary"]["p_value"],
        }
        self.store.finish_run(run_id, "success" if not errors else "success", stats,
                              notes=";".join(errors[:5]) if errors else None)
        return {"current": current, "comparison": comparison, "paths": paths, "stats": stats, "run_id": run_id}

    # ------------------------------------------------ baseline
    @staticmethod
    def load_baseline(path: Path) -> Optional[Dict[str, Any]]:
        """读取 baseline JSON；不存在返回 None。"""
        path = Path(path)
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError) as exc:
            print(f"[regression] baseline 读取失败（将按首次运行处理）：{exc}")
            return None

    @staticmethod
    def build_baseline(current: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """把当前结果整理成 baseline JSON 结构。"""
        items = {}
        for question, row in current.items():
            items[question] = {
                "id": row.get("id"),
                "csi": row.get("csi"),
                "verdict": row.get("verdict"),
                "annotation_label": row.get("annotation_label"),
                "error_cause": row.get("error_cause"),
                "dimensions": row.get("dimensions"),
                "tags": row.get("tags"),
            }
        csis = [r["csi"] for r in current.values() if r.get("csi") is not None]
        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "count": len(current),
            "mean_csi": metrics.mean(csis),
            "items": items,
        }

    # ------------------------------------------------ 对比
    def compare(self, baseline: Optional[Dict[str, Any]], current: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """逐条对比 baseline 与当前结果。

        :return: {"rows": [...], "summary": {...}}
        """
        rows: List[Dict[str, Any]] = []
        improved = regressed = unchanged = 0
        deltas: List[float] = []
        base_items = (baseline or {}).get("items", {})

        for question, cur in current.items():
            base = base_items.get(question)
            cur_csi = cur.get("csi")
            if base is None:
                status = STATUS_NEW
                base_csi = None
                delta = None
            else:
                base_csi = base.get("csi")
                if base_csi is None or cur_csi is None:
                    status, delta = STATUS_UNCHANGED, None
                else:
                    delta = round(float(cur_csi) - float(base_csi), 2)
                    if delta >= DELTA_THRESHOLD:
                        status = STATUS_IMPROVED
                        improved += 1
                        deltas.append(delta)
                    elif delta <= -DELTA_THRESHOLD:
                        status = STATUS_REGRESSED
                        regressed += 1
                        deltas.append(delta)
                    else:
                        status = STATUS_UNCHANGED
                        unchanged += 1
                        deltas.append(delta)
            rows.append(
                {
                    "id": cur.get("id"),
                    "question": question,
                    "tags": "、".join(cur.get("tags") or []),
                    "baseline_csi": base_csi,
                    "current_csi": cur_csi,
                    "delta": delta,
                    "status": status,
                    "baseline_verdict": (base or {}).get("verdict"),
                    "current_verdict": cur.get("verdict"),
                    "current_label": cur.get("annotation_label"),
                    "current_cause": cur.get("error_cause"),
                    "dimensions": cur.get("dimensions") or {},
                    "judge_reason": cur.get("judge_reason") or "",
                }
            )

        rows.sort(key=lambda r: (r["delta"] if r["delta"] is not None else 0))
        cur_csis = [r["csi"] for r in current.values() if r.get("csi") is not None]
        base_csis = [i.get("csi") for i in base_items.values() if i.get("csi") is not None]
        mean_delta = None
        if base_csis and cur_csis:
            mean_delta = round(metrics.mean(cur_csis) - metrics.mean(base_csis), 2)

        p_value = sign_test_p_value(improved, regressed)
        if p_value is None:
            significance = "有效变化样本不足 5 条，不做显著性检验，仅作方向性参考。"
        elif p_value < 0.05:
            significance = f"符号检验双侧 p={p_value} < 0.05，改进/退步差异具有统计显著性。"
        else:
            significance = f"符号检验双侧 p={p_value} >= 0.05，尚不能认定差异显著（可能由样本量或波动导致）。"

        summary = {
            "total": len(current),
            "improved": improved,
            "regressed": regressed,
            "unchanged": unchanged,
            "new": sum(1 for r in rows if r["status"] == STATUS_NEW),
            "mean_csi": metrics.mean(cur_csis),
            "baseline_mean_csi": metrics.mean(base_csis) if base_csis else None,
            "mean_delta": mean_delta,
            "p_value": p_value,
            "significance": significance,
            "has_baseline": bool(base_items),
            "delta_threshold": DELTA_THRESHOLD,
        }
        return {"rows": rows, "summary": summary}

    # ------------------------------------------------ 输出
    def write_outputs(
        self,
        comparison: Dict[str, Any],
        current: Dict[str, Dict[str, Any]],
        baseline_path: Path,
        save_baseline: bool,
    ) -> Dict[str, Path]:
        """写出 regression_report.md / regression_detail.csv / baseline.json。"""
        rows, summary = comparison["rows"], comparison["summary"]
        out: Dict[str, Path] = {}

        lines: List[str] = []
        add = lines.append
        add("# 回归评测报告（升级前后对比）")
        add("")
        add(f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        add(f"- 运行模式：{'mock（本地 fixtures）' if self.cfg.mock else 'live（Dify Service API）'}")
        add(f"- 黄金集条数：{summary['total']}，成功评分 {len(current)} 条")
        add(f"- baseline：{'已加载（' + str(baseline_path) + '）' if summary['has_baseline'] else '不存在，本次结果可作为首个 baseline'}")
        add("")
        add("## 一、总体结论")
        add("")
        add("| 指标 | 数值 |")
        add("| --- | --- |")
        add(f"| 当前平均 CSI | {summary['mean_csi']} |")
        add(f"| baseline 平均 CSI | {summary['baseline_mean_csi']} |")
        add(f"| 平均变化 | {summary['mean_delta']} |")
        add(f"| 改进 / 退步 / 不变 | {summary['improved']} / {summary['regressed']} / {summary['unchanged']} |")
        add(f"| 变化判定阈值 | |ΔCSI| >= {summary['delta_threshold']} |")
        add(f"| 显著性 | {summary['significance']} |")
        add("")
        add("## 二、逐条明细（按 ΔCSI 升序，退步在前）")
        add("")
        add("| ID | 问题 | 标签 | baseline CSI | 当前 CSI | Δ | 结论 | 归因 |")
        add("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for r in rows:
            add(
                "| {i} | {q} | {t} | {b} | {c} | {d} | {s} | {ca} |".format(
                    i=r["id"],
                    q=str(r["question"])[:34].replace("|", "\\|"),
                    t=r["tags"] or "-",
                    b=r["baseline_csi"] if r["baseline_csi"] is not None else "-",
                    c=r["current_csi"] if r["current_csi"] is not None else "-",
                    d=(f"{r['delta']:+.2f}" if r["delta"] is not None else "-"),
                    s=r["status"],
                    ca=r["current_cause"] or "-",
                )
            )
        add("")
        add("## 三、退步项聚焦")
        add("")
        bad = [r for r in rows if r["status"] == STATUS_REGRESSED]
        if not bad:
            add("没有出现退步项。")
        else:
            for r in bad:
                add(f"### {r['id']}　Δ={r['delta']:+.2f}")
                add("")
                add(f"- 问题：{r['question']}")
                add(f"- 当前回答：{current[r['question']]['answer'][:200]}")
                add(f"- 评分理由：{r['judge_reason']}")
                add(f"- 建议：检查该问题所属知识块是否被改动、检索 top_k / rerank 是否退化。")
                add("")
        add("## 四、维度变化")
        add("")
        add("| 维度 | 当前均值 |")
        add("| --- | --- |")
        for key, label in (
            ("factual_accuracy", "事实准确性 FA"),
            ("knowledge_completeness", "知识完备性 KC"),
            ("relevance", "相关性 RE"),
            ("clarity", "表达清晰度 CL"),
            ("retrieval_quality", "检索命中质量 RQ"),
        ):
            vals = [r["dimensions"].get(key) for r in rows if r["dimensions"].get(key) is not None]
            if key == "retrieval_quality":
                vals = [v for v in vals if v and v > 0]
            add(f"| {label} | {metrics.mean(vals)} |")
        add("")
        add("> 显著性说明：对「改进 / 退步」条数做符号检验（双侧精确二项检验，p=0.5）。"
            "样本量不足或 p>=0.05 时，结论仅作方向性参考，建议扩大黄金集后复测。")
        add("")

        md_path = self.out_dir / "regression_report.md"
        md_path.write_text("\n".join(lines), encoding="utf-8")
        out["md"] = md_path

        csv_path = self.out_dir / "regression_detail.csv"
        with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["ID", "问题", "标签", "baseline_CSI", "当前_CSI", "ΔCSI", "结论",
                             "baseline判定", "当前判定", "当前标签", "当前归因", "FA", "KC", "RE", "CL", "RQ", "评分理由", "当前回答"])
            for r in rows:
                d = r["dimensions"]
                writer.writerow([
                    r["id"], r["question"], r["tags"], r["baseline_csi"], r["current_csi"],
                    "" if r["delta"] is None else f"{r['delta']:+.2f}", r["status"],
                    r["baseline_verdict"] or "", r["current_verdict"] or "", r["current_label"] or "",
                    r["current_cause"] or "", d.get("factual_accuracy"), d.get("knowledge_completeness"),
                    d.get("relevance"), d.get("clarity"), d.get("retrieval_quality"),
                    r["judge_reason"], current[r["question"]]["answer"],
                ])
        out["csv"] = csv_path

        if save_baseline:
            payload = self.build_baseline(current)
            baseline_path = Path(baseline_path)
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            with baseline_path.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            out["baseline"] = baseline_path

        self._log(f"报告：{md_path}")
        self._log(f"明细：{csv_path}")
        if save_baseline:
            self._log(f"baseline：{baseline_path}")
        return out


def run_regression(
    cfg: Config,
    store: Store,
    golden_path: Path,
    baseline_path: Path,
    out_dir: Path,
    save_baseline: bool = False,
    concurrency: int = 4,
    mock_variant: str = "current",
) -> Dict[str, Any]:
    """便捷入口：执行一次回归评测。"""
    runner = RegressionRunner(cfg, store, out_dir, concurrency=concurrency, mock_variant=mock_variant)
    return runner.run(golden_path, baseline_path, save_baseline=save_baseline)
