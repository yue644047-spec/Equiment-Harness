"""文档质量记分卡（docs/10 §6）：按资料编号聚合探针与缺陷，输出 DQI 与整改单。

统计纪律（写进代码，避免治理被小样本误伤）：
1. 探针数 < MIN_SAMPLES 的资料不出 DQI、不参与排名，只列证据；
2. 所有比例给 Wilson 区间，不给裸值；
3. 缺陷必须先过归因前置（attribution.py），系统问题不得计入文档。

DQI 定义：
    DQI = 100 × (1 − min(1, Σ(严重度权重 × 缺陷数) / 探针数))
    严重度权重：P0 = 5，P1 = 2，P2 = 1
    —— 即「每条探针平均承担的加权缺陷数」直接作为扣分比例，
       不用 CSI 均值，避免奖励「写得简单、只答容易题」的资料。
"""

from __future__ import annotations

import csv
import math
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from attribution import DEFECT_TYPES
from corpus import CorpusIndex

#: 严重度权重（用于 DQI）
SEV_WEIGHT = {"P0": 5, "P1": 2, "P2": 1}

#: 最小样本量：低于此值不出结论
MIN_SAMPLES = 8

UNATTRIBUTED = "（未归因）"


def wilson_interval(k: int, n: int, z: float = 1.96) -> Tuple[Optional[float], Optional[float]]:
    """二项比例的 Wilson 区间（小样本下比正态近似稳健）。

    :param k: 缺陷数
    :param n: 探针数
    :return: (下界, 上界)，均为 0~1；n=0 时返回 (None, None)
    """
    if n <= 0:
        return None, None
    phat = k / n
    denom = 1 + z * z / n
    centre = phat + z * z / (2 * n)
    margin = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / denom), min(1.0, (centre + margin) / denom)


def defect_label(code: str) -> str:
    """缺陷类型的中文名。"""
    info = DEFECT_TYPES.get(code)
    return info[1] if info else code


def build_rows(
    eval_rows: Sequence[Dict[str, Any]],
    defect_items: Sequence[Dict[str, Any]],
    corpus: Optional[CorpusIndex] = None,
) -> List[Dict[str, Any]]:
    """按资料编号聚合出记分卡行。

    :param eval_rows: evaluations 表（需含 doc_no / csi 等列）
    :param defect_items: defects 表（仅 blame == doc 的条目计入文档质量）
    :param corpus: 语料索引（提供条款总数与现行版本）
    :return: 每份资料一行的列表，按探针数降序
    """
    probes: Counter = Counter()
    csi_values: Dict[str, List[float]] = defaultdict(list)
    sev_counts: Dict[str, Counter] = defaultdict(Counter)
    type_counts: Dict[str, Counter] = defaultdict(Counter)
    clause_seen: Dict[str, set] = defaultdict(set)

    for row in eval_rows:
        doc_no = str(row.get("doc_no") or "").strip()
        if not doc_no:
            # 未命中任何资料的回答不构成某份资料的「探针」，另行统计（见 build_scorecard）
            continue
        probes[doc_no] += 1
        csi = row.get("csi")
        if isinstance(csi, (int, float)):
            csi_values[doc_no].append(float(csi))

    for item in defect_items:
        if str(item.get("blame") or "") != "doc":
            continue
        doc_no = str(item.get("doc_no") or "").strip() or UNATTRIBUTED
        severity = str(item.get("severity") or "P2").upper()
        sev_counts[doc_no][severity] += 1
        type_counts[doc_no][str(item.get("defect_type") or "")] += 1
        clause = str(item.get("clause_no") or "").strip()
        if clause:
            clause_seen[doc_no].add(clause)

    rows: List[Dict[str, Any]] = []
    for doc_no, n in probes.items():
        counts = sev_counts.get(doc_no, Counter())
        weighted = sum(SEV_WEIGHT.get(sev, 1) * counts.get(sev, 0) for sev in ("P0", "P1", "P2"))
        defect_total = sum(counts.values())
        sufficient = n >= MIN_SAMPLES
        density = round(weighted / n * 100, 1) if n else None
        score = None
        if sufficient:
            score = round(100 * (1 - min(1.0, weighted / n)), 1)

        info = corpus.docs.get(doc_no) if corpus else None
        clauses_total = corpus.clauses_total(doc_no) if corpus else None
        current_version = corpus.current_version(doc_no) if corpus else None
        clauses_probed = len(clause_seen.get(doc_no, set())) or None
        coverage_rate = None
        if clauses_total and clauses_probed:
            coverage_rate = round(clauses_probed / clauses_total, 3)

        lo, hi = wilson_interval(defect_total, n)
        mean_csi = round(sum(csi_values[doc_no]) / len(csi_values[doc_no]), 2) if csi_values.get(doc_no) else None
        top = [f"{defect_label(code)}×{cnt}" for code, cnt in type_counts.get(doc_no, Counter()).most_common(3)]

        rows.append(
            {
                "doc_no": doc_no,
                "title": info.title if info else "",
                "doc_version": info.version if info else "",
                "current_version": current_version or "",
                "probes": n,
                "csi_mean": mean_csi,
                "clauses_total": clauses_total,
                "clauses_probed": clauses_probed,
                "coverage_rate": coverage_rate,
                "p0": counts.get("P0", 0),
                "p1": counts.get("P1", 0),
                "p2": counts.get("P2", 0),
                "defect_total": defect_total,
                "defect_density": density,
                "dqi": score,
                "ci_low": round(lo, 3) if lo is not None else None,
                "ci_high": round(hi, 3) if hi is not None else None,
                "sufficient": sufficient,
                "top_defects": " / ".join(top),
            }
        )

    rows.sort(key=lambda r: (-r["probes"], r["doc_no"]))
    return rows


def render_markdown(
    rows: Sequence[Dict[str, Any]],
    defect_items: Sequence[Dict[str, Any]],
    unattributed: int = 0,
) -> str:
    """生成记分卡 Markdown 文本。"""
    lines: List[str] = []
    add = lines.append
    add("# 文档质量记分卡")
    add("")
    add(f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    add(f"- 资料数量：{len(rows)}")
    add(f"- 样本充分门槛：{MIN_SAMPLES} 条探针（低于此值不出 DQI、不参与排名）")
    add(f"- 未归因探针：{unattributed} 条（没有任何检索片段，未计入任何资料，属系统侧检索问题）")
    add("")
    add("> 统计纪律：本表所有比例均为抽样估计，请连同探针数与区间一起阅读；")
    add("> 跨文档排名仅在探针口径一致且样本充分时有效。缺陷已过归因前置，系统问题不计入文档。")
    add("")
    add("| 资料编号 | 名称 | 版本 | 探针数 | CSI 均值 | 条款总数 | 已探针条款 | 覆盖命中率 | P0 | P1 | P2 | 缺陷密度/100 | DQI | 95% 区间 | 样本充分 | Top 缺陷 |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in rows:
        ci = f"{r['ci_low']}~{r['ci_high']}" if r["ci_low"] is not None else "—"
        add(
            "| {doc} | {title} | {ver} | {n} | {csi} | {ct} | {cp} | {cov} | {p0} | {p1} | {p2} | {den} | {dqi} | {ci} | {ok} | {top} |".format(
                doc=r["doc_no"],
                title=(r["title"] or "—")[:18],
                ver=r["doc_version"] or r["current_version"] or "—",
                n=r["probes"],
                csi=r["csi_mean"] if r["csi_mean"] is not None else "—",
                ct=r["clauses_total"] if r["clauses_total"] is not None else "—",
                cp=r["clauses_probed"] if r["clauses_probed"] is not None else "—",
                cov=f"{r['coverage_rate'] * 100:.1f}%" if r["coverage_rate"] is not None else "—",
                p0=r["p0"],
                p1=r["p1"],
                p2=r["p2"],
                den=r["defect_density"] if r["defect_density"] is not None else "—",
                dqi=r["dqi"] if r["dqi"] is not None else "—",
                ci=ci,
                ok="✅" if r["sufficient"] else "❌ 样本不足",
                top=r["top_defects"] or "—",
            )
        )
    add("")
    add("## 缺陷整改单（按严重度排序）")
    add("")
    doc_defects = [d for d in defect_items if str(d.get("blame") or "") == "doc"]
    if not doc_defects:
        add("本期未发现文档侧缺陷。")
    else:
        add("| 缺陷ID | 资料 | 版本 | 缺陷类型 | 维度 | 严重度 | 次数 | 证据问题 | 建议动作 | 状态 |")
        add("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        order = {"P0": 0, "P1": 1, "P2": 2}
        for d in sorted(doc_defects, key=lambda x: (order.get(str(x.get("severity")), 9), str(x.get("doc_no")))):
            add(
                "| {did} | {doc} | {ver} | {label} | {dim} | {sev} | {occ} | {q} | {act} | {st} |".format(
                    did=d.get("defect_id"),
                    doc=d.get("doc_no"),
                    ver=d.get("doc_version") or "—",
                    label=d.get("defect_label") or defect_label(str(d.get("defect_type"))),
                    dim=d.get("dimension") or "—",
                    sev=d.get("severity"),
                    occ=d.get("occurrences", 1),
                    q=str(d.get("question") or "")[:28].replace("|", "/"),
                    act=(d.get("action") or "待定性")[:40],
                    st=d.get("status") or "open",
                )
            )
    add("")
    add("## 下一步")
    add("")
    add("1. 对 P0 / P1 类文档缺陷逐条人工确认后派单（误判会直接损害资料员对系统的信任）；")
    add("2. 整改完成后执行单缺陷回归（G3 阶段的 verify-defect 子命令），通过才可关闭；")
    add("3. 样本不足的资料先补探针（G1 阶段的 probe 子命令），再谈结论。")
    add("")
    return "\n".join(lines)


def write_csv(rows: Sequence[Dict[str, Any]], path: Path) -> Path:
    """输出记分卡 CSV。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "doc_no", "title", "doc_version", "current_version", "probes", "csi_mean",
        "clauses_total", "clauses_probed", "coverage_rate", "p0", "p1", "p2",
        "defect_total", "defect_density", "dqi", "ci_low", "ci_high", "sufficient", "top_defects",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) for k in fields})
    return path


def build_scorecard(
    eval_rows: Sequence[Dict[str, Any]],
    defect_items: Sequence[Dict[str, Any]],
    corpus: Optional[CorpusIndex] = None,
    out_dir: Optional[Path] = None,
    formats: Sequence[str] = ("md", "csv"),
) -> Dict[str, Any]:
    """构建记分卡并落盘。

    :return: {"rows": [...], "paths": {...}}
    """
    rows = build_rows(eval_rows, defect_items, corpus)
    unattributed = sum(1 for r in eval_rows if not str(r.get("doc_no") or "").strip())
    paths: Dict[str, Path] = {}
    if out_dir is not None:
        out_path = Path(out_dir)
        if "md" in formats:
            md = out_path / "doc_scorecard.md"
            md.parent.mkdir(parents=True, exist_ok=True)
            md.write_text(render_markdown(rows, defect_items, unattributed), encoding="utf-8")
            paths["md"] = md
        if "csv" in formats:
            paths["csv"] = write_csv(rows, out_path / "doc_scorecard.csv")
    return {"rows": rows, "paths": paths, "unattributed": unattributed}
