"""报表模块：生成 Markdown / HTML / CSV（可选 xlsx）评测报表。

  * Markdown：适合进 Git、贴工单；
  * HTML：内联 CSS，横向条形图用纯 CSS 实现，不依赖 JS 与图表库；
  * CSV：逐条明细，Excel 可直接打开；
  * xlsx：安装 openpyxl 时额外产出，否则回退 CSV 并打印提示。

支持 --period day/week/month/all 过滤（依据消息 created_at）。
"""

from __future__ import annotations

import csv
import html
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import metrics
from store import Store

#: 报表明细列（key, 中文表头）
DETAIL_COLUMNS = [
    ("message_id", "消息ID"),
    ("msg_created_iso", "时间"),
    ("question", "问题"),
    ("answer", "回答"),
    ("factual_accuracy", "FA"),
    ("knowledge_completeness", "KC"),
    ("relevance", "RE"),
    ("clarity", "CL"),
    ("retrieval_quality", "RQ"),
    ("safety_compliance", "安全合规性SC"),
    ("version_currency", "版本时效性VC"),
    ("terminology_norm", "术语规范性TN"),
    ("user_score_raw", "用户评分"),
    ("csi", "CSI"),
    ("verdict", "判定"),
    ("annotation_label", "标注标签"),
    ("error_cause", "归因"),
    ("hallucination_risk", "幻觉风险"),
    ("knowledge_gap", "知识缺失"),
    ("badcase", "badcase"),
    ("judge_reason", "评分理由"),
]


def _fmt(value: Any, digits: int = 2) -> str:
    """数值格式化：None 显示为 -，浮点保留指定位数。"""
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _pct(value: Optional[float]) -> str:
    """比例格式化为百分数。"""
    if value is None:
        return "-"
    return f"{value * 100:.1f}%"


def _truncate(text: Any, limit: int = 60) -> str:
    """截断长文本，便于表格展示。"""
    s = "" if text is None else str(text).replace("\\n", " ").strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _bar(ratio: Optional[float], width: int = 24) -> str:
    """生成 Markdown 里的文本条形图。"""
    if ratio is None:
        return ""
    filled = int(round(max(0.0, min(1.0, ratio)) * width))
    return "█" * filled + "░" * (width - filled)


class ReportBuilder:
    """报表生成器。"""

    def __init__(self, store: Store, out_dir: Path, title: str = "装备技术资料智能问答质量评测报告"):
        """
        :param store: Store
        :param out_dir: 输出目录
        :param title: 报表标题
        """
        self.store = store
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.title = title

    # ------------------------------------------------ 数据准备
    def collect_rows(self, period: Optional[str] = None) -> List[Dict[str, Any]]:
        """取评价明细并按周期过滤。"""
        rows = self.store.fetch_evaluations()
        return metrics.filter_by_period(rows, period)

    # ------------------------------------------------ Markdown
    def build_markdown(self, rows: Sequence[Dict[str, Any]], agg: Dict[str, Any], period: str) -> str:
        """生成 Markdown 报表文本。"""
        lines: List[str] = []
        add = lines.append
        add(f"# {self.title}")
        add("")
        add(f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        add(f"- 统计周期：{period or 'all（全部）'}")
        add(f"- 样本量：{agg['count']} 条已评价问答")
        add("- 数据来源：Dify Service API（会话 / 消息 / 反馈）+ 满意度评价工作流")
        add("")
        add("## 一、总体指标")
        add("")
        add("| 指标 | 数值 | 说明 |")
        add("| --- | --- | --- |")
        add(f"| CSI 均值 | {_fmt(agg['csi']['mean'])} | 综合满意度指数（0-5） |")
        add(f"| CSI 中位数 / P90 | {_fmt(agg['csi']['median'])} / {_fmt(agg['csi']['p90'])} | 分布集中度 |")
        add(f"| CSI 标准差 | {_fmt(agg['csi']['std'], 3)} | 波动越小越稳定 |")
        add(f"| 达标率（CSI>=3.5） | {_pct(agg['pass_rate_35'])} | 良好及以上占比 |")
        add(f"| 优秀率（CSI>=4.2） | {_pct(agg['pass_rate_42'])} | 优秀占比 |")
        add(f"| badcase 数 / 占比 | {agg['badcase_count']} / {_pct(agg['badcase_rate'])} | 见文末 badcase 判定规则 |")
        add(f"| 知识缺失占比 | {_pct(agg['knowledge_gap_rate'])} | knowledge_gap=true 的比例 |")
        us = agg["user_score"]
        add(f"| 用户评分覆盖 / 均值 | {_pct(us.get('coverage'))} / {_fmt(us.get('mean'))} | 仅统计有反馈的样本 |")
        add(f"| LLM 质量分均值 | {_fmt(agg['llm_quality_mean'])} | 归一化后的四维加权分 |")
        corr = agg["correlation"]
        add(f"| 相关性（Pearson / Spearman） | {_fmt(corr['pearson'], 3)} / {_fmt(corr['spearman'], 3)} | n={corr['n']}，衡量 LLM 与用户判断的一致性 |")
        add("")

        add("## 二、维度均值")
        add("")
        add("| 维度 | 均值 | 条形 |")
        add("| --- | --- | --- |")
        dim_names = [
            ("factual_accuracy", "事实准确性 FA"),
            ("knowledge_completeness", "知识完备性 KC"),
            ("relevance", "相关性 RE"),
            ("clarity", "表达清晰度 CL"),
            ("retrieval_quality", "检索命中质量 RQ（仅统计有检索的样本）"),
        ]
        for key, label in dim_names:
            value = agg["dimension_means"].get(key)
            add(f"| {label} | {_fmt(value)} | {_bar((value or 0) / 5 if value else None)} |")
        add("")
        add(f"> RQ 覆盖率：{_pct(agg['dimension_means'].get('retrieval_quality_coverage'))}（无检索时 RQ 记为 0，不参与均值）")
        add("")

        add("### 装备域专项维度（不进入 CSI 权重，以专项达标与红线方式参与判定）")
        add("")
        add("| 专项维度 | 均值 | 覆盖 | 触发红线 / 异常条数 |")
        add("| --- | --- | --- | --- |")
        dom = agg.get("domain") or {}
        add(
            f"| 安全合规性 SC（仅统计涉及操作/维修/应急的样本） | {_fmt(dom.get('safety_compliance'))} | "
            f"{_pct(dom.get('safety_compliance_coverage'))} | SC<=2（安全红线，直接不合格）：{dom.get('safety_redline_count', 0)} 条 |"
        )
        add(
            f"| 版本时效性 VC（仅统计涉及版本引用的样本） | {_fmt(dom.get('version_currency'))} | "
            f"{_pct(dom.get('version_currency_coverage'))} | VC=1（引用作废版本）：{dom.get('version_obsolete_count', 0)} 条 |"
        )
        add(
            f"| 术语规范性 TN（全部样本） | {_fmt(dom.get('terminology_norm'))} | - | "
            f"TN<=2（术语不规范）：{dom.get('terminology_low_count', 0)} 条 |"
        )
        add("")
        add("> 装备域规则：safety_compliance<=2 直接判「不合格」；version_currency=1 时最多判「待改进」。")
        add("")

        add("## 三、判定分布")
        add("")
        add("| 判定 | 数量 | 占比 | 条形 |")
        add("| --- | --- | --- | --- |")
        for name in metrics.VERDICT_ORDER:
            cnt = agg["verdict_distribution"].get(name, 0)
            ratio = agg["verdict_ratio"].get(name, 0.0)
            add(f"| {name} | {cnt} | {_pct(ratio)} | {_bar(ratio)} |")
        add("")

        add("## 四、标注标签与错误归因")
        add("")
        for header, data in (
            ("标注标签", agg["annotation_label_distribution"]),
            ("错误归因", agg["error_cause_distribution"]),
            ("幻觉风险", agg["hallucination_risk_distribution"]),
        ):
            add(f"| {header} | 数量 |")
            add("| --- | --- |")
            for k, v in data.items():
                add(f"| {k} | {v} |")
            add("")

        add("## 五、逐条明细")
        add("")
        add("| # | 消息ID | 问题 | FA | KC | RE | CL | RQ | SC | VC | TN | US | CSI | 判定 | 标签 | 归因 |")
        add("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for i, row in enumerate(rows, 1):
            add(
                "| {i} | {mid} | {q} | {fa} | {kc} | {re} | {cl} | {rq} | {sc} | {vc} | {tn} | {us} | {csi} | {v} | {lb} | {ca} |".format(
                    i=i,
                    mid=row.get("message_id"),
                    q=_truncate(row.get("question"), 34).replace("|", "\\|"),
                    fa=_fmt(row.get("factual_accuracy"), 0),
                    kc=_fmt(row.get("knowledge_completeness"), 0),
                    re=_fmt(row.get("relevance"), 0),
                    cl=_fmt(row.get("clarity"), 0),
                    rq=_fmt(row.get("retrieval_quality"), 0),
                    sc=_fmt(row.get("safety_compliance"), 0),
                    vc=_fmt(row.get("version_currency"), 0),
                    tn=_fmt(row.get("terminology_norm"), 0),
                    us=_fmt(row.get("user_score_raw") or "-", 0),
                    csi=_fmt(row.get("csi")),
                    v=row.get("verdict") or "-",
                    lb=row.get("annotation_label") or "-",
                    ca=row.get("error_cause") or "-",
                )
            )
        add("")

        bad_rows = [r for r in rows if r.get("badcase")]
        add("## 六、badcase 摘要（按 CSI 升序）")
        add("")
        if not bad_rows:
            add("本期没有 badcase。")
        else:
            add("| 消息ID | CSI | 严重度 | 问题 | 触发原因 |")
            add("| --- | --- | --- | --- | --- |")
            for row in sorted(bad_rows, key=lambda r: r.get("csi") or 0):
                sev = metrics.severity_of(row) or "-"
                reason = "；".join(metrics.severity_reasons(row)) or row.get("judge_reason") or "-"
                add(
                    f"| {row.get('message_id')} | {_fmt(row.get('csi'))} | {sev} | "
                    f"{_truncate(row.get('question'), 30).replace('|', '\\|')} | "
                    f"{_truncate(reason, 60).replace('|', '\\|')} |"
                )
        add("")
        add("---")
        add("")
        add("判定规则：CSI>=4.2 优秀；>=3.5 良好；>=2.8 待改进；<2.8 不合格。"
            "红线：FA<=2 直接不合格；US=dislike 且 CSI>=3.5 降级为待改进。")
        add("装备域红线：安全合规性 SC<=2 直接不合格（遗漏安全前置条件或禁止事项）；"
            "版本时效性 VC=1（引用作废版本）最多判待改进。")
        add("专项维度取值 0 表示「本条不涉及」（RQ 无检索 / SC 不涉及安全内容 / VC 不涉及版本引用），不参与均值计算。")
        add("badcase 规则：CSI<3.0 或 FA<=2 或 US=dislike 或（knowledge_gap 且 KC<=2）"
            "或 SC<=2 或 VC=1 或 TN<=2 或命中装备域归因。")
        add("")
        return "\n".join(lines)

    # ------------------------------------------------ HTML
    def build_html(self, rows: Sequence[Dict[str, Any]], agg: Dict[str, Any], period: str) -> str:
        """生成自包含 HTML 报表（内联 CSS + 纯 CSS 横向条形图）。"""
        def esc(value: Any) -> str:
            return html.escape("" if value is None else str(value))

        def css_bar(ratio: Optional[float], label: str = "") -> str:
            width = 0.0 if ratio is None else max(0.0, min(1.0, ratio)) * 100
            return '<div class="bar"><div class="bar-fill" style="width:{w:.1f}%">{t}</div></div>'.format(
                w=width, t=esc(label)
            )

        dims = [
            ("事实准确性 FA", agg["dimension_means"].get("factual_accuracy")),
            ("知识完备性 KC", agg["dimension_means"].get("knowledge_completeness")),
            ("相关性 RE", agg["dimension_means"].get("relevance")),
            ("表达清晰度 CL", agg["dimension_means"].get("clarity")),
            ("检索命中质量 RQ", agg["dimension_means"].get("retrieval_quality")),
            ("安全合规性 SC（装备域）", (agg.get("domain") or {}).get("safety_compliance")),
            ("版本时效性 VC（装备域）", (agg.get("domain") or {}).get("version_currency")),
            ("术语规范性 TN（装备域）", (agg.get("domain") or {}).get("terminology_norm")),
        ]
        dim_rows = "".join(
            '<tr><td>{n}</td><td class="n">{v}</td><td>{b}</td></tr>'.format(
                n=esc(name), v=_fmt(value), b=css_bar((value or 0) / 5 if value else 0, _fmt(value))
            )
            for name, value in dims
        )
        verdict_rows = "".join(
            '<tr><td>{n}</td><td class="n">{c}</td><td class="n">{p}</td><td>{b}</td></tr>'.format(
                n=esc(name),
                c=agg["verdict_distribution"].get(name, 0),
                p=_pct(agg["verdict_ratio"].get(name, 0.0)),
                b=css_bar(agg["verdict_ratio"].get(name, 0.0), str(agg["verdict_distribution"].get(name, 0))),
            )
            for name in metrics.VERDICT_ORDER
        )

        def dist_table(data: Dict[str, int]) -> str:
            """把分布字典渲染成小表格 + 条形图。"""
            if not data:
                return '<p class="muted">暂无数据</p>'
            total = sum(data.values()) or 1
            body = "".join(
                '<tr><td>{k}</td><td class="n">{v}</td><td>{b}</td></tr>'.format(
                    k=esc(k), v=v, b=css_bar(v / total, f"{v / total * 100:.1f}%")
                )
                for k, v in data.items()
            )
            return f'<table><thead><tr><th>取值</th><th>数量</th><th>占比</th></tr></thead><tbody>{body}</tbody></table>'

        detail_rows = "".join(
            "<tr class='{cls}'><td>{i}</td><td class='mono'>{mid}</td><td>{q}</td>"
            "<td class='n'>{fa}</td><td class='n'>{kc}</td><td class='n'>{re}</td><td class='n'>{cl}</td>"
            "<td class='n'>{rq}</td><td class='n'>{sc}</td><td class='n'>{vc}</td><td class='n'>{tn}</td>"
            "<td class='n'>{us}</td><td class='n strong'>{csi}</td>"
            "<td>{v}</td><td>{lb}</td><td>{ca}</td><td class='small'>{reason}</td></tr>".format(
                cls="bad" if row.get("badcase") else "",
                i=i,
                mid=esc(row.get("message_id")),
                q=esc(_truncate(row.get("question"), 46)),
                fa=_fmt(row.get("factual_accuracy"), 0),
                kc=_fmt(row.get("knowledge_completeness"), 0),
                re=_fmt(row.get("relevance"), 0),
                cl=_fmt(row.get("clarity"), 0),
                rq=_fmt(row.get("retrieval_quality"), 0),
                sc=_fmt(row.get("safety_compliance"), 0),
                vc=_fmt(row.get("version_currency"), 0),
                tn=_fmt(row.get("terminology_norm"), 0),
                us=esc(row.get("user_score_raw") or "-"),
                csi=_fmt(row.get("csi")),
                v=esc(row.get("verdict")),
                lb=esc(row.get("annotation_label")),
                ca=esc(row.get("error_cause")),
                reason=esc(_truncate(row.get("judge_reason"), 80)),
            )
            for i, row in enumerate(rows, 1)
        )

        us = agg["user_score"]
        corr = agg["correlation"]
        cards = [
            ("样本量", str(agg["count"])),
            ("CSI 均值", _fmt(agg["csi"]["mean"])),
            ("CSI 中位数", _fmt(agg["csi"]["median"])),
            ("CSI P90", _fmt(agg["csi"]["p90"])),
            ("达标率 (>=3.5)", _pct(agg["pass_rate_35"])),
            ("优秀率 (>=4.2)", _pct(agg["pass_rate_42"])),
            ("badcase 占比", _pct(agg["badcase_rate"])),
            ("知识缺失占比", _pct(agg["knowledge_gap_rate"])),
            ("用户评分均值", _fmt(us.get("mean"))),
            ("用户评分覆盖", _pct(us.get("coverage"))),
            ("LLM 质量分均值", _fmt(agg["llm_quality_mean"])),
            ("Pearson 相关", _fmt(corr["pearson"], 3)),
        ]
        card_html = "".join(
            f'<div class="card"><div class="card-k">{esc(k)}</div><div class="card-v">{esc(v)}</div></div>'
            for k, v in cards
        )

        template = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root { --fg:#1f2937; --muted:#6b7280; --line:#e5e7eb; --brand:#2f6fed; --bad:#dc2626; }
  * { box-sizing: border-box; }
  body { margin:0; padding:28px; background:#f7f8fa; color:var(--fg);
         font-family:"Microsoft YaHei","PingFang SC","Segoe UI",Arial,sans-serif; font-size:14px; line-height:1.6; }
  h1 { font-size:22px; margin:0 0 6px; }
  h2 { font-size:17px; margin:28px 0 10px; padding-left:9px; border-left:4px solid var(--brand); }
  h3 { font-size:14px; margin:12px 0 6px; color:var(--muted); }
  .meta { color:var(--muted); margin-bottom:18px; }
  .cards { display:grid; grid-template-columns:repeat(auto-fill,minmax(160px,1fr)); gap:12px; }
  .card { background:#fff; border:1px solid var(--line); border-radius:8px; padding:12px 14px; }
  .card-k { color:var(--muted); font-size:12px; }
  .card-v { font-size:20px; font-weight:600; margin-top:4px; }
  table { width:100%; border-collapse:collapse; background:#fff; border:1px solid var(--line); border-radius:8px; overflow:hidden; }
  th, td { padding:7px 10px; text-align:left; border-bottom:1px solid var(--line); vertical-align:top; }
  th { background:#f1f3f7; font-weight:600; white-space:nowrap; }
  td.n { text-align:right; white-space:nowrap; }
  td.mono { font-family:Consolas,monospace; font-size:12px; white-space:nowrap; }
  td.small { font-size:12px; color:var(--muted); }
  td.strong { font-weight:600; }
  tr.bad td.strong { color:var(--bad); }
  tr.bad { background:#fff7f7; }
  .bar { background:#eef2f7; border-radius:4px; height:18px; min-width:120px; }
  .bar-fill { background:var(--brand); color:#fff; font-size:11px; line-height:18px; text-align:right;
              padding-right:6px; border-radius:4px; min-width:2px; white-space:nowrap; }
  .muted { color:var(--muted); }
  footer { margin-top:26px; color:var(--muted); font-size:12px; }
  .grid2 { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:16px; }
</style>
</head>
<body>
<h1>__TITLE__</h1>
<div class="meta">生成时间：__NOW__ ｜ 统计周期：__PERIOD__ ｜ 样本量：__COUNT__ 条</div>

<h2>一、总体指标</h2>
<div class="cards">__CARDS__</div>

<h2>二、维度均值（满分 5）</h2>
<table>
<thead><tr><th>维度</th><th>均值</th><th>对比</th></tr></thead>
<tbody>__DIM_ROWS__</tbody>
</table>
<p class="muted">RQ 覆盖率 __RQ_COV__：未发生检索的问答 RQ 记为 0，不参与均值计算。</p>

<h2>三、判定分布</h2>
<table>
<thead><tr><th>判定</th><th>数量</th><th>占比</th><th>分布</th></tr></thead>
<tbody>__VERDICT_ROWS__</tbody>
</table>

<h2>四、标签与归因分布</h2>
<div class="grid2">
  <div><h3>标注标签</h3>__LABEL_DIST__</div>
  <div><h3>错误归因</h3>__CAUSE_DIST__</div>
  <div><h3>幻觉风险</h3>__RISK_DIST__</div>
  <div><h3>用户评分分布</h3>__US_DIST__</div>
</div>

<h2>五、逐条明细</h2>
<table>
<thead><tr><th>#</th><th>消息ID</th><th>问题</th><th>FA</th><th>KC</th><th>RE</th><th>CL</th><th>RQ</th><th>SC</th><th>VC</th><th>TN</th><th>US</th><th>CSI</th><th>判定</th><th>标签</th><th>归因</th><th>评分理由</th></tr></thead>
<tbody>__DETAIL_ROWS__</tbody>
</table>

<footer>
判定规则：CSI&gt;=4.2 优秀；&gt;=3.5 良好；&gt;=2.8 待改进；&lt;2.8 不合格。红线：FA&lt;=2 直接不合格；US=dislike 且 CSI&gt;=3.5 降级为待改进。<br>
装备域红线：安全合规性 SC&lt;=2 直接不合格（遗漏安全前置条件或禁止事项）；版本时效性 VC=1（引用作废版本）最多判待改进。<br>
专项维度取值 0 表示「本条不涉及」（RQ 无检索 / SC 不涉及安全内容 / VC 不涉及版本引用），不参与均值计算。<br>
badcase 规则：CSI&lt;3.0 或 FA&lt;=2 或 US=dislike 或（knowledge_gap 且 KC&lt;=2）或 SC&lt;=2 或 VC=1 或 TN&lt;=2 或命中装备域归因。
</footer>
</body>
</html>
"""
        replacements = {
            "__TITLE__": esc(self.title),
            "__NOW__": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "__PERIOD__": esc(period or "all（全部）"),
            "__COUNT__": str(agg["count"]),
            "__CARDS__": card_html,
            "__DIM_ROWS__": dim_rows,
            "__RQ_COV__": _pct(agg["dimension_means"].get("retrieval_quality_coverage")),
            "__VERDICT_ROWS__": verdict_rows,
            "__LABEL_DIST__": dist_table(agg["annotation_label_distribution"]),
            "__CAUSE_DIST__": dist_table(agg["error_cause_distribution"]),
            "__RISK_DIST__": dist_table(agg["hallucination_risk_distribution"]),
            "__US_DIST__": dist_table({str(k): v for k, v in (us.get("distribution") or {}).items()}),
            "__DETAIL_ROWS__": detail_rows,
        }
        for key, value in replacements.items():
            template = template.replace(key, value)
        return template

    # ------------------------------------------------ 导出
    def write_csv(self, rows: Sequence[Dict[str, Any]], path: Path) -> Path:
        """导出 CSV 明细。

        使用 utf-8-sig（UTF-8 with BOM），保证 Windows Excel 双击打开不乱码。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow([label for _, label in DETAIL_COLUMNS])
            for row in rows:
                out: List[Any] = []
                for key, _ in DETAIL_COLUMNS:
                    value = row.get(key)
                    if key in ("knowledge_gap", "badcase"):
                        value = "是" if value else "否"
                    out.append(value if value is not None else "")
                writer.writerow(out)
        return path

    def write_xlsx(self, rows: Sequence[Dict[str, Any]], path: Path) -> Optional[Path]:
        """尝试导出 xlsx；未安装 openpyxl 时返回 None 并打印提示。"""
        try:
            from openpyxl import Workbook  # type: ignore
            from openpyxl.styles import Font  # type: ignore
        except ImportError:
            print("[report] 未安装 openpyxl，已回退为 CSV（如需 xlsx：pip install openpyxl）")
            return None
        wb = Workbook()
        ws = wb.active
        ws.title = "evaluations"
        ws.append([label for _, label in DETAIL_COLUMNS])
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for row in rows:
            ws.append([
                ("是" if row.get(k) else "否") if k in ("knowledge_gap", "badcase") else row.get(k)
                for k, _ in DETAIL_COLUMNS
            ])
        for idx, (key, _label) in enumerate(DETAIL_COLUMNS, start=1):
            width = 34 if key in ("question", "answer", "judge_reason") else 14
            ws.column_dimensions[ws.cell(row=1, column=idx).column_letter].width = width
        path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(str(path))
        return path

    # ------------------------------------------------ 主入口
    def generate(
        self,
        period: Optional[str] = None,
        formats: Sequence[str] = ("md", "html", "csv"),
        prefix: str = "report",
        quiet: bool = False,
    ) -> Dict[str, Path]:
        """生成报表并返回产物路径字典。

        :param period: day/week/month/all
        :param formats: md/html/csv/xlsx 的子集
        :param prefix: 文件名前缀
        :return: {格式: 路径}
        """
        rows = self.collect_rows(period)
        agg = metrics.aggregate(rows)
        outputs: Dict[str, Path] = {}
        formats = [f.strip().lower() for f in formats if f and f.strip()]

        if "md" in formats:
            path = self.out_dir / f"{prefix}.md"
            path.write_text(self.build_markdown(rows, agg, period or "all"), encoding="utf-8")
            outputs["md"] = path
        if "html" in formats:
            path = self.out_dir / f"{prefix}.html"
            path.write_text(self.build_html(rows, agg, period or "all"), encoding="utf-8")
            outputs["html"] = path
        if "csv" in formats:
            outputs["csv"] = self.write_csv(rows, self.out_dir / f"{prefix}.csv")
        if "xlsx" in formats:
            xlsx = self.write_xlsx(rows, self.out_dir / f"{prefix}.xlsx")
            if xlsx:
                outputs["xlsx"] = xlsx
            else:
                outputs["csv"] = self.write_csv(rows, self.out_dir / f"{prefix}.csv")

        if not quiet:
            print(f"[report] 样本 {agg['count']} 条，周期 {period or 'all'}，CSI 均值 {_fmt(agg['csi']['mean'])}")
            for fmt, path in outputs.items():
                print(f"[report] 已生成 {fmt.upper()}：{path}")
        return outputs


def generate_report(
    store: Store,
    out_dir: Path,
    period: Optional[str] = None,
    formats: Sequence[str] = ("md", "html", "csv"),
    prefix: str = "report",
    quiet: bool = False,
) -> Dict[str, Path]:
    """便捷入口：生成报表。"""
    return ReportBuilder(store, out_dir).generate(period=period, formats=formats, prefix=prefix, quiet=quiet)
