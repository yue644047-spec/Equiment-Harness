"""治理看板（Windows 本地版）：单文件、零依赖的文档质量治理界面。

定位（对应 docs/10 的治理度量框架）：
    问答系统是文档质量的**探针**，本看板是它的**读数仪表**——
    看的是「哪份资料有质量问题、哪条缺陷该谁改、改没改」。

设计要点：
1. 只用标准库（http.server），不引入 Flask/Django —— 内网摆布、信创环境同样可用；
2. 数据全部取自 SQLite（store.py）与语料目录（corpus.py），不另立口径；
3. 页面：概览 / 记分卡 / 缺陷整改单 / 归因明细 / 单份资料详情；
4. 动作：一键重跑「归因前置」与「记分卡」，让采集→评价→归因→整改闭环在看板上可点；
5. 唯一的写操作是更新缺陷的整改状态（open / fixing / verifying / closed / rejected）。
"""

from __future__ import annotations

import html
import json
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import db_query
from asset_index import AssetIndex
from attribution import DEFECT_ACTION, attribute_evaluation, build_defect
from config import BASE_DIR, Config
from corpus import load_corpus
from doc_score import MIN_SAMPLES, build_scorecard, defect_label
from metrics import aggregate
from store import Store

# ---------------------------------------------------------------- 演示集成常量

#: 检索契约层服务地址（Dify 外部知识库协议兼容）
BRIDGE_URL = "http://127.0.0.1:9380"
#: 演示用 Bearer 密钥（与 kb_bridge 默认值一致）
BRIDGE_KEY = "bridge-demo-key"
#: 端到端演示的其余目录/数据库
ASSETS_DIR = BASE_DIR.parent / "assets"
DEMO_CORPUS_DIR = BASE_DIR.parent / "demo_corpus"
BUSINESS_DB = BASE_DIR.parent / "demo_data" / "business.db"

_LOCAL: Dict[str, Any] = {}

DEFAULT_PORT = 8848
SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}
STATUS_LABEL = {
    "open": "待整改",
    "fixing": "整改中",
    "verifying": "待验证",
    "closed": "已关闭",
    "rejected": "已驳回",
}
BLAME_LABEL = {
    "doc": "文档缺陷",
    "corpus": "覆盖缺口",
    "system": "系统侧",
    "none": "未见异常",
}

CSS = """
:root {
  --bg: #f4f6f9; --card: #ffffff; --ink: #1c2b36; --muted: #6b7c8c;
  --line: #dde5ec; --accent: #1f5f8b; --accent-soft: #e8f1f8;
  --p0: #c0392b; --p1: #c87f0a; --p2: #4a6fa5; --ok: #2e7d5b;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif; font-size: 14px; line-height: 1.6; }
header { background: var(--accent); color: #fff; padding: 14px 24px; }
header h1 { margin: 0; font-size: 18px; font-weight: 600; letter-spacing: .5px; }
header .sub { font-size: 12px; opacity: .85; margin-top: 4px; }
nav { background: #fff; border-bottom: 1px solid var(--line); padding: 0 24px; }
nav a { display: inline-block; padding: 10px 16px; color: var(--muted); text-decoration: none; border-bottom: 2px solid transparent; }
nav a.active { color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; }
main { padding: 20px 24px 60px; max-width: 1500px; }
.flash { background: var(--accent-soft); border-left: 3px solid var(--accent); padding: 10px 14px; margin-bottom: 16px; border-radius: 3px; }
.kpis { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 20px; }
.kpi { background: var(--card); border: 1px solid var(--line); border-radius: 4px; padding: 12px 16px; min-width: 150px; flex: 1; }
.kpi .label { color: var(--muted); font-size: 12px; }
.kpi .value { font-size: 22px; font-weight: 600; margin-top: 4px; }
.kpi .hint { color: var(--muted); font-size: 11px; margin-top: 2px; }
section { background: var(--card); border: 1px solid var(--line); border-radius: 4px; padding: 16px; margin-bottom: 20px; }
section h2 { margin: 0 0 12px; font-size: 15px; }
section h2 .note { color: var(--muted); font-weight: 400; font-size: 12px; margin-left: 8px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { background: #fafcfe; color: var(--muted); font-weight: 600; white-space: nowrap; }
tr:hover td { background: #fafcfe; }
.badge { display: inline-block; padding: 1px 7px; border-radius: 10px; font-size: 12px; border: 1px solid transparent; }
.b-p0 { color: #fff; background: var(--p0); }
.b-p1 { color: #fff; background: var(--p1); }
.b-p2 { color: #fff; background: var(--p2); }
.b-open { color: var(--p0); border-color: var(--p0); }
.b-fixing { color: var(--p1); border-color: var(--p1); }
.b-verifying { color: var(--p2); border-color: var(--p2); }
.b-closed { color: var(--ok); border-color: var(--ok); }
.b-rejected { color: var(--muted); border-color: var(--muted); }
.muted { color: var(--muted); }
.small { font-size: 12px; }
.toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 12px; }
.btn { display: inline-block; padding: 6px 14px; border-radius: 3px; border: 1px solid var(--accent);
  background: var(--accent); color: #fff; text-decoration: none; cursor: pointer; font-size: 13px; }
.btn.ghost { background: #fff; color: var(--accent); }
select, input[type=text] { padding: 5px 8px; border: 1px solid var(--line); border-radius: 3px; font-size: 13px; }
details { margin-top: 6px; }
summary { cursor: pointer; color: var(--accent); font-size: 12px; }
pre { background: #f8fafc; border: 1px solid var(--line); border-radius: 3px; padding: 10px;
  white-space: pre-wrap; word-break: break-all; font-size: 12px; margin: 6px 0 0; }
.kv { color: var(--muted); font-size: 12px; }
a.doc { color: var(--accent); text-decoration: none; }
.warn { color: var(--p1); }
footer { color: var(--muted); font-size: 12px; padding: 0 24px 24px; }
"""


# ---------------------------------------------------------------- 工具


def esc(value: Any) -> str:
    """HTML 转义。"""
    return html.escape("" if value is None else str(value))


def sev_badge(sev: Optional[str]) -> str:
    """严重度徽标。"""
    s = str(sev or "").upper()
    if s not in SEVERITY_ORDER:
        return '<span class="muted">—</span>'
    return f'<span class="badge b-{s.lower()}">{s}</span>'


def status_badge(status: str) -> str:
    """整改状态徽标。"""
    key = str(status or "open")
    return f'<span class="badge b-{esc(key)}">{esc(STATUS_LABEL.get(key, key))}</span>'


def fmt(value: Any, digits: int = 2, dash: str = "—") -> str:
    """数值格式化。"""
    if value is None or value == "":
        return dash
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


# ---------------------------------------------------------------- 演示集成


def local_index():
    """本地条款级索引（延迟构建并缓存）。

    两个用途：① 检索契约层服务不可用时的降级路径；② 资产挂载关系查询。
    """
    if "index" not in _LOCAL:
        sys.path.insert(0, str(BASE_DIR.parent / "kb_bridge"))
        from chunk_index import ChunkIndex, load_ledger_current

        samples = BASE_DIR.parent / "samples"
        ledger = load_ledger_current(samples)
        index = ChunkIndex()
        for directory in (samples, DEMO_CORPUS_DIR):
            if directory.exists():
                index.chunks.extend(ChunkIndex.build(directory, ledger).chunks)
        index._build_stats()
        _LOCAL["index"] = index
    return _LOCAL["index"]


def fetch_bridge_stats() -> Dict[str, Any]:
    """读取检索契约层的运行状态（不可达时返回 offline）。"""
    try:
        with urllib.request.urlopen(BRIDGE_URL + "/healthz", timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        data["online"] = True
        data["url"] = BRIDGE_URL
        return data
    except Exception as exc:  # 服务未启动不应影响其他页面
        return {"online": False, "url": BRIDGE_URL, "error": str(exc), "stats": local_index().stats()}


def bridge_search(question: str, top_k: int = 6, include_obsolete: bool = False) -> Dict[str, Any]:
    """调用检索契约层（Dify 外部知识库协议）；失败时降级为本地索引直查。"""
    payload = json.dumps({
        "knowledge_id": "hys-corpus",
        "query": question,
        "retrieval_setting": {"top_k": top_k, "score_threshold": 0.0},
        "debug_include_obsolete": bool(include_obsolete),
    }).encode("utf-8")
    request = urllib.request.Request(
        BRIDGE_URL + "/retrieval",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {BRIDGE_KEY}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        data["transport"] = "HTTP POST /retrieval（Dify 外部知识库协议）"
        return data
    except Exception as exc:
        index = local_index()
        results = index.search(question, top_k=top_k, include_obsolete=include_obsolete)
        return {
            "records": [r.chunk.to_unified(r.score) for r in results],
            "debug": {
                "forced_condition": "status = 现行有效" if not include_obsolete else "（演示：已放宽版本过滤）",
                "hits": len(results),
                "engine": "local-clause-index",
                "include_obsolete": include_obsolete,
            },
            "transport": f"降级：本地索引直查（检索契约层未连通：{exc}）",
        }


# ---------------------------------------------------------------- 数据装配


class DashboardData:
    """看板数据装配：一次请求内读库 + 读语料，保证口径与命令行一致。"""

    def __init__(self, store: Store, samples_dir: Path):
        self.store = store
        self.samples_dir = samples_dir

    def load(self) -> Dict[str, Any]:
        """装载全部视图数据。"""
        corpus = load_corpus(self.samples_dir)
        rows = self.store.fetch_evaluations()
        defects = self.store.fetch_defects()
        resources = self.store.fetch_resources_map()

        doc_defects = [d for d in defects if d.get("blame") == "doc"]
        corpus_defects = [d for d in defects if d.get("blame") == "corpus"]
        system_defects = [d for d in defects if d.get("blame") == "system"]
        closed = [d for d in defects if d.get("status") == "closed"]

        card = build_scorecard(rows, defects, corpus, out_dir=None, formats=())
        agg = aggregate(rows)

        # 端到端演示：资产索引与业务库状态
        try:
            assets = AssetIndex.load(ASSETS_DIR)
        except Exception:
            assets = AssetIndex()
        bridge_stats = fetch_bridge_stats()

        traces: List[Dict[str, Any]] = []
        for row in rows:
            mid = str(row.get("message_id"))
            item = dict(row)
            item["resources"] = resources.get(mid) or []
            traces.append(item)

        return {
            "corpus": corpus,
            "rows": rows,
            "traces": traces,
            "defects": defects,
            "doc_defects": doc_defects,
            "corpus_defects": corpus_defects,
            "system_defects": system_defects,
            "scorecard": card["rows"],
            "unattributed": card.get("unattributed", 0),
            "agg": agg,
            "closure_rate": (len(closed) / len(defects)) if defects else None,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "assets": assets,
            "asset_stats": assets.stats(),
            "asset_unregistered": assets.unregistered(ASSETS_DIR) if ASSETS_DIR.exists() else [],
            "asset_index_stats": local_index().stats() if ASSETS_DIR.exists() or True else {},
            "bridge": bridge_stats,
            "business_db": str(BUSINESS_DB),
            "business_db_exists": BUSINESS_DB.exists(),
        }


# ---------------------------------------------------------------- 页面


def layout(title: str, body: str, active: str, data: Dict[str, Any], flash: str = "") -> str:
    """页面骨架（含导航与页头）。"""
    nav_items = [
        ("/", "概览"),
        ("/defects", "缺陷整改单"),
        ("/traces", "归因明细"),
        ("/docs", "资料清单"),
        ("/bridge", "检索契约层"),
        ("/assets", "资产库"),
        ("/dbquery", "台账查询"),
    ]
    nav = "".join(
        f'<a class="{"active" if path == active else ""}" href="{path}">{label}</a>'
        for path, label in nav_items
    )
    flash_html = f'<div class="flash">{esc(flash)}</div>' if flash else ""
    corpus = data.get("corpus")
    docs = len(corpus.docs) if corpus else 0
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · 文档质量治理看板</title>
<style>{CSS}</style>
</head>
<body>
<header>
  <h1>装备技术资料 · 文档质量治理看板</h1>
  <div class="sub">问答是探针，资料变好才是目的　|　生成时间 {esc(data.get('generated_at'))}　|　语料 {docs} 份　|　评价 {len(data.get('rows') or [])} 条</div>
</header>
<nav>{nav}</nav>
<main>
{flash_html}
{body}
</main>
<footer>本看板为 Windows 本地/内网离线版本：仅标准库实现，数据来自 eval_service 的 SQLite 库与 samples/ 语料，口径与命令行一致（docs/10）。</footer>
</body>
</html>"""


def render_overview(data: Dict[str, Any]) -> str:
    """概览页：KPI + 记分卡 + 归因分布 + 动作按钮。"""
    agg = data["agg"]
    csi_mean = fmt((agg.get("csi") or {}).get("mean"))
    pass_rate = agg.get("pass_rate_35")
    parts: List[str] = []

    parts.append('<div class="toolbar">')
    parts.append('<a class="btn" href="/actions/attribute">重跑归因前置</a>')
    parts.append('<a class="btn ghost" href="/actions/scorecard">重建记分卡</a>')
    parts.append('<a class="btn ghost" href="/api/summary" target="_blank">JSON 摘要</a>')
    parts.append("</div>")

    kpis = [
        ("评价条数", fmt(len(data["rows"]), 0), "问答探针样本量"),
        ("CSI 均值", csi_mean, "综合满意度指数"),
        ("达标率 ≥3.5", f"{pass_rate * 100:.1f}%" if pass_rate is not None else "—", "回答质量达标比例"),
        ("文档缺陷", fmt(len(data["doc_defects"]), 0), "可定位到具体资料的缺陷"),
        ("覆盖缺口", fmt(len(data["corpus_defects"]), 0), "资料整体缺失，无法定位到某一份"),
        ("系统侧问题", fmt(len(data["system_defects"]), 0), "检索/分块/提示词，不计入资料"),
        ("整改闭环率", f"{data['closure_rate'] * 100:.0f}%" if data["closure_rate"] is not None else "—",
         "已关闭缺陷 / 缺陷总数"),
        ("未归因探针", fmt(data["unattributed"], 0), "无检索片段，不计入任何资料"),
    ]
    parts.append('<div class="kpis">')
    for label, value, hint in kpis:
        parts.append(
            f'<div class="kpi"><div class="label">{esc(label)}</div>'
            f'<div class="value">{esc(value)}</div><div class="hint">{esc(hint)}</div></div>'
        )
    parts.append("</div>")

    # 记分卡
    parts.append("<section><h2>文档质量记分卡"
                 f'<span class="note">样本充分门槛 {MIN_SAMPLES} 条探针；不足则不出 DQI、不参与排名</span></h2>')
    if not data["scorecard"]:
        parts.append('<p class="muted">暂无数据。请先运行 demo / collect / judge，或点击上方「重跑归因前置」。</p>')
    else:
        parts.append(
            "<table><tr><th>资料编号</th><th>名称</th><th>版本</th><th>探针数</th><th>CSI 均值</th>"
            "<th>条款总数</th><th>P0</th><th>P1</th><th>P2</th><th>缺陷密度/100</th><th>DQI</th>"
            "<th>95% 区间</th><th>样本</th><th>Top 缺陷</th></tr>"
        )
        for row in data["scorecard"]:
            ci = f"{row['ci_low']}~{row['ci_high']}" if row["ci_low"] is not None else "—"
            sufficient = "充分" if row["sufficient"] else '<span class="warn">不足</span>'
            doc_link = (
                f'<a class="doc" href="/docs/{urllib.parse.quote(str(row["doc_no"]))}">{esc(row["doc_no"])}</a>'
            )
            parts.append(
                f"<tr><td>{doc_link}</td><td>{esc(row['title'] or '—')}</td>"
                f"<td>{esc(row['doc_version'] or row['current_version'] or '—')}</td>"
                f"<td>{row['probes']}</td><td>{fmt(row['csi_mean'])}</td>"
                f"<td>{fmt(row['clauses_total'], 0)}</td>"
                f"<td>{row['p0']}</td><td>{row['p1']}</td><td>{row['p2']}</td>"
                f"<td>{fmt(row['defect_density'], 1)}</td>"
                f"<td><strong>{fmt(row['dqi'], 1)}</strong></td>"
                f"<td class='small'>{esc(ci)}</td><td>{sufficient}</td>"
                f"<td class='small'>{esc(row['top_defects'] or '—')}</td></tr>"
            )
        parts.append("</table>")
    parts.append("</section>")

    # 归因分布
    parts.append("<section><h2>缺陷类型分布<span class='note'>只统计文档侧与覆盖缺口；系统侧问题另计</span></h2>")
    counter: Dict[str, int] = {}
    for d in data["doc_defects"] + data["corpus_defects"]:
        key = str(d.get("defect_label") or defect_label(str(d.get("defect_type"))))
        counter[key] = counter.get(key, 0) + 1
    if not counter:
        parts.append('<p class="muted">本期未发现文档侧缺陷。</p>')
    else:
        parts.append("<table><tr><th>缺陷类型</th><th>条数</th><th>整改动作</th></tr>")
        for label, cnt in sorted(counter.items(), key=lambda kv: -kv[1]):
            action = ""
            for d in data["doc_defects"] + data["corpus_defects"]:
                if (d.get("defect_label") or defect_label(str(d.get("defect_type")))) == label:
                    action = d.get("action") or ""
                    break
            parts.append(f"<tr><td>{esc(label)}</td><td>{cnt}</td><td class='small'>{esc(action or '—')}</td></tr>")
        parts.append("</table>")
    parts.append("</section>")

    # 端到端演示（对应设计报告 3.5 / 3.4 / 3.7）
    asset_stats = data.get("asset_stats") or {}
    bridge = data.get("bridge") or {}
    bridge_stats = bridge.get("stats") or {}
    parts.append(
        "<section><h2>端到端演示<span class='note'>对应设计报告 3.5 检索契约层 / 3.4 资产索引 / 3.7 数据库交互查询</span></h2>"
        "<div class='kpis'>"
        f"<div class='kpi'><div class='label'>检索契约层</div>"
        f"<div class='value'>{'在线' if bridge.get('online') else '未连通'}</div>"
        f"<div class='hint'>条款片段 {esc(bridge_stats.get('chunks'))} 条 · 作废 {esc(bridge_stats.get('obsolete_chunks'))} 条</div>"
        f"<div class='hint'><a class='doc' href='/bridge'>进入检索测试与版本过滤对比 →</a></div></div>"
        f"<div class='kpi'><div class='label'>资产库</div>"
        f"<div class='value'>{esc(asset_stats.get('total'))}</div>"
        f"<div class='hint'>图片 {esc(asset_stats.get('images'))} · 视频 {esc(asset_stats.get('videos'))} · 标注覆盖 {esc(asset_stats.get('coverage'))}</div>"
        f"<div class='hint'><a class='doc' href='/assets'>进入资产检索 →</a></div></div>"
        f"<div class='kpi'><div class='label'>业务库（台账/履历）</div>"
        f"<div class='value'>{'已就绪' if data.get('business_db_exists') else '未生成'}</div>"
        f"<div class='hint'>只读访问 · 白名单查询模板</div>"
        f"<div class='hint'><a class='doc' href='/dbquery'>进入台账查询 →</a></div></div>"
        "</div></section>"
    )

    # 待办
    pending = [d for d in data["defects"] if d.get("status") in ("open", "fixing")]
    parts.append("<section><h2>待办缺陷（Top 10）</h2>")
    if not pending:
        parts.append('<p class="muted">没有待办缺陷。</p>')
    else:
        parts.append("<table><tr><th>严重度</th><th>资料</th><th>缺陷类型</th><th>归因</th><th>状态</th><th>证据问题</th></tr>")
        for d in sorted(pending, key=lambda x: SEVERITY_ORDER.get(str(x.get("severity")), 9))[:10]:
            parts.append(
                f"<tr><td>{sev_badge(d.get('severity'))}</td><td>{esc(d.get('doc_no'))}</td>"
                f"<td>{esc(d.get('defect_label') or '')}</td><td>{esc(BLAME_LABEL.get(str(d.get('blame')), ''))}</td>"
                f"<td>{status_badge(str(d.get('status')))}</td>"
                f"<td class='small'>{esc(str(d.get('question') or '')[:60])}</td></tr>"
            )
        parts.append("</table>")
    parts.append("</section>")
    return "".join(parts)


def render_defects(data: Dict[str, Any], query: Dict[str, List[str]], flash: str = "") -> str:
    """缺陷整改单：可过滤 + 可改状态。"""
    blame_f = (query.get("blame") or [""])[0]
    sev_f = (query.get("severity") or [""])[0]
    status_f = (query.get("status") or [""])[0]

    items = list(data["defects"])
    if blame_f:
        items = [d for d in items if str(d.get("blame")) == blame_f]
    if sev_f:
        items = [d for d in items if str(d.get("severity")).upper() == sev_f.upper()]
    if status_f:
        items = [d for d in items if str(d.get("status")) == status_f]
    items.sort(key=lambda x: (SEVERITY_ORDER.get(str(x.get("severity")), 9), str(x.get("doc_no"))))

    def option(value: str, label: str, current: str) -> str:
        sel = " selected" if value == current else ""
        return f'<option value="{esc(value)}"{sel}>{esc(label)}</option>'

    parts: List[str] = []
    parts.append('<form class="toolbar" method="get" action="/defects">')
    parts.append('<span class="small muted">归因对象</span><select name="blame">')
    parts.append(option("", "全部", blame_f))
    for key, label in BLAME_LABEL.items():
        if key != "none":
            parts.append(option(key, label, blame_f))
    parts.append("</select>")
    parts.append('<span class="small muted">严重度</span><select name="severity">')
    parts.append(option("", "全部", sev_f))
    for key in ("P0", "P1", "P2"):
        parts.append(option(key, key, sev_f))
    parts.append("</select>")
    parts.append('<span class="small muted">状态</span><select name="status">')
    parts.append(option("", "全部", status_f))
    for key, label in STATUS_LABEL.items():
        parts.append(option(key, label, status_f))
    parts.append("</select>")
    parts.append('<button class="btn" type="submit">筛选</button>')
    parts.append(f'<span class="small muted">共 {len(items)} 条</span>')
    parts.append("</form>")

    if not items:
        parts.append('<section><p class="muted">没有符合条件的缺陷。若从未归因，请先回到概览点击「重跑归因前置」。</p></section>')
        return "".join(parts)

    parts.append("<section><table>")
    parts.append(
        "<tr><th>ID</th><th>严重度</th><th>资料</th><th>缺陷类型</th><th>维度</th><th>归因</th>"
        "<th>次数</th><th>证据问题</th><th>建议动作</th><th>状态</th><th>更新</th></tr>"
    )
    for d in items:
        did = str(d.get("defect_id"))
        status_select = "".join(
            f'<option value="{esc(k)}"{" selected" if k == d.get("status") else ""}>{esc(v)}</option>'
            for k, v in STATUS_LABEL.items()
        )
        evidence = d.get("evidence") or {}
        retrieved = evidence.get("retrieved") or []
        hits = "；".join(
            f"{esc(r.get('doc_no') or '?')} {esc(r.get('version') or '')}（分 {fmt(r.get('score'))}）"
            for r in retrieved[:4]
        ) or "无命中片段"
        reasons = d.get("reasons") or []
        def_owner = esc(d.get("owner") or "")
        def_due = esc(d.get("due_date") or "")
        parts.append(
            f'<tr><td class="small">{esc(did)}</td><td>{sev_badge(d.get("severity"))}</td>'
            f'<td>{esc(d.get("doc_no"))}<div class="kv">{esc(d.get("doc_version") or "")}</div></td>'
            f'<td>{esc(d.get("defect_label") or defect_label(str(d.get("defect_type"))))}</td>'
            f'<td>{esc(d.get("dimension") or "—")}</td>'
            f'<td>{esc(BLAME_LABEL.get(str(d.get("blame")), ""))}</td>'
            f'<td>{esc(d.get("occurrences", 1))}</td>'
            f'<td class="small">{esc(str(d.get("question") or ""))}'
            f'<details><summary>证据与判定理由</summary><div class="kv">命中片段：{hits}</div>'
            f'<pre>{esc("；".join(str(r) for r in reasons) or "—")}</pre></details></td>'
            f'<td class="small">{esc(d.get("action") or "—")}</td>'
            f'<td>{status_badge(str(d.get("status")))}</td>'
            f'<td><form method="post" action="/defects/update" class="small">'
            f'<input type="hidden" name="defect_id" value="{esc(did)}">'
            f'<input type="hidden" name="back" value="{esc(query.get("_raw", ["/defects"])[0])}">'
            f'<select name="status">{status_select}</select>'
            f'<input type="text" name="owner" placeholder="责任单位" value="{def_owner}" size="8">'
            f'<input type="text" name="due_date" placeholder="期限" value="{def_due}" size="8">'
            f'<button class="btn" type="submit">保存</button></form></td></tr>'
        )
    parts.append("</table></section>")
    parts.append(
        '<section><h2>整改纪律</h2><ol class="small">'
        "<li>P0 / P1 类缺陷必须人工确认后再派单（误判会直接损害资料员对系统的信任，见 docs/10 §4.2）。</li>"
        "<li>状态流转：待整改 → 整改中 → 待验证 → 已关闭；只有经单缺陷回归验证后才能关闭。</li>"
        "<li>驳回的缺陷要记录理由，用于校准 LLM 判定口径。</li></ol></section>"
    )
    return "".join(parts)


def render_traces(data: Dict[str, Any], limit: int = 60) -> str:
    """归因明细：逐条问答的证据链。"""
    rows = list(reversed(data["traces"]))[:limit]
    parts: List[str] = [
        f'<section><h2>归因明细<span class="note">最近 {len(rows)} 条问答；命中资料来自 Dify 的 retriever_resources</span></h2>'
    ]
    if not rows:
        parts.append('<p class="muted">暂无评价数据。</p></section>')
        return "".join(parts)

    parts.append(
        "<table><tr><th>问题</th><th>CSI</th><th>命中资料</th><th>归因</th><th>缺陷类型</th><th>说明</th></tr>"
    )
    for row in rows:
        refs = row.get("resources") or []
        doc_list = "；".join(
            f"{esc(r.get('document_name') or '')}（{fmt(r.get('score'))}）" for r in refs[:3]
        ) or '<span class="muted">无命中片段</span>'
        blame = str(row.get("blame") or "")
        defect_type = str(row.get("defect_type") or "")
        label = defect_label(defect_type) if defect_type and not defect_type.startswith("S_") else defect_type
        reason = row.get("judge_reason") or ""
        parts.append(
            f'<tr><td class="small">{esc(str(row.get("question") or "")[:70])}'
            f'<details><summary>答案与片段</summary><div class="kv">答案：</div><pre>{esc(str(row.get("answer") or "")[:800])}</pre>'
            + "".join(
                f'<div class="kv">片段 {idx}（{esc(r.get("document_name") or "")}）：</div><pre>{esc(str(r.get("content") or "")[:500])}</pre>'
                for idx, r in enumerate(refs[:3], 1)
            )
            + "</details></td>"
            f'<td>{fmt(row.get("csi"))}</td><td class="small">{doc_list}</td>'
            f'<td>{esc(BLAME_LABEL.get(blame, blame or "—"))}</td>'
            f'<td class="small">{esc(label or "—")}</td>'
            f'<td class="small">{esc(str(reason)[:120])}</td></tr>'
        )
    parts.append("</table></section>")
    return "".join(parts)


def _rows_table(columns: Sequence[str], rows: Sequence[Dict[str, Any]], limit: int = 50) -> str:
    """把查询结果渲染成表格。"""
    if not columns:
        return '<p class="muted">没有返回列。</p>'
    head = "".join(f"<th>{esc(c)}</th>" for c in columns)
    body: List[str] = []
    for row in rows[:limit]:
        cells = "".join(f"<td class='small'>{esc(row.get(c))}</td>" for c in columns)
        body.append(f"<tr>{cells}</tr>")
    more = f'<p class="muted small">仅显示前 {limit} 行，共 {len(rows)} 行。</p>' if len(rows) > limit else ""
    return f"<table><tr>{head}</tr>{''.join(body)}</table>{more}"


def render_bridge(data: Dict[str, Any], query: Dict[str, List[str]]) -> str:
    """检索契约层页面：协议调用、统一片段、引用契约、版本硬过滤对比。"""
    q = (query.get("q") or [""])[0].strip()
    top_k = int((query.get("top_k") or ["6"])[0] or 6)
    obsolete = (query.get("obsolete") or ["0"])[0] in ("1", "true", "on")
    compare = (query.get("compare") or ["0"])[0] in ("1", "true", "on")
    bridge = data.get("bridge") or {}
    assets: AssetIndex = data.get("assets") or AssetIndex()

    parts: List[str] = []
    stats = (bridge.get("stats") or {})
    status = (
        f'<span class="badge b-closed">在线</span> {esc(bridge.get("url"))}'
        if bridge.get("online")
        else f'<span class="badge b-p1">未连通</span> {esc(bridge.get("url"))}'
             '<div class="kv">本地降级索引仍可用；启动服务：'
             '<code>python kb_bridge/bridge_server.py --port 9380</code></div>'
    )
    parts.append(
        "<section><h2>检索契约层服务<span class='note'>实现 Dify 外部知识库协议；后端可切换</span></h2>"
        f"<div class='kv'>状态：{status}</div>"
        f"<div class='kv'>索引：条款片段 {esc(stats.get('chunks'))} 条 · 资料 {esc(stats.get('documents'))} 份 · "
        f"作废片段 {esc(stats.get('obsolete_chunks'))} 条</div>"
        f"<div class='kv'>版本硬过滤：<strong>status = 现行有效</strong>（服务端强制注入，调用方无法放宽）</div>"
        "<div class='kv'>协议：POST /retrieval，Bearer 鉴权，返回 records["
        "{content, score, title, metadata}]</div></section>"
    )

    parts.append('<form class="toolbar" method="get" action="/bridge">')
    parts.append(f'<input type="text" name="q" value="{esc(q)}" size="46" placeholder="输入问题，例如：滤芯每 300 工作小时更换一次吗">')
    parts.append(f'<select name="top_k">{_options(["3", "6", "10"], str(top_k))}</select>')
    parts.append(
        '<label class="small"><input type="checkbox" name="compare" value="1"'
        + (" checked" if compare else "") + "> 版本过滤对比</label>"
    )
    parts.append(
        '<label class="small"><input type="checkbox" name="obsolete" value="1"'
        + (" checked" if obsolete else "") + "> 放宽版本过滤（演示）</label>"
    )
    parts.append('<button class="btn" type="submit">检索</button>')
    parts.append('</form>')

    if not q:
        parts.append(
            "<section><h2>说明</h2><p class='muted small'>输入问题后，本页会以 Dify 的协议向检索契约层发起一次检索，"
            "并展示统一片段结构（content / score / title / metadata）与引用契约。"
            "勾选「版本过滤对比」可看到同一问题在「硬过滤」与「放宽过滤」两种条件下的差异——"
            "放宽后排在首位的往往是已作废版本条款，正是本设计要防住的失败模式。</p></section>"
        )
        return "".join(parts)

    def record_table(result: Dict[str, Any], title: str, note: str = "") -> str:
        records = result.get("records") or []
        debug = result.get("debug") or {}
        html_parts = [f"<section><h2>{esc(title)}<span class='note'>{esc(note)}</span></h2>"]
        html_parts.append(
            f"<div class='kv'>通道：{esc(result.get('transport') or '')} · 引擎：{esc(debug.get('engine'))} · "
            f"强制条件：{esc(debug.get('forced_condition'))} · 命中 {len(records)} 条</div>"
        )
        if not records:
            html_parts.append('<p class="muted">没有检索到片段。</p></section>')
            return "".join(html_parts)
        html_parts.append(
            "<table><tr><th>#</th><th>引用契约</th><th>状态</th><th>分数</th><th>片段内容</th><th>引用附件</th></tr>"
        )
        for idx, rec in enumerate(records, 1):
            meta = rec.get("metadata") or {}
            status_cls = "b-closed" if meta.get("status") == "现行有效" else "b-p0"
            attached = assets.by_chunk(str(meta.get("chunk_id") or ""))
            attach_html = ""
            for asset in attached[:2]:
                thumb = f"/asset-file?path={urllib.parse.quote(asset.thumb)}" if asset.thumb else ""
                img = f'<img src="{thumb}" alt="{esc(asset.name)}" style="width:64px;border:1px solid var(--line);border-radius:3px">' if thumb else ""
                attach_html += f'<div class="small">{img}<div>{esc(asset.name)}</div><div class="kv">{esc(asset.figure_no)} · {esc(asset.caption)}</div></div>'
            html_parts.append(
                f"<tr><td>{idx}</td><td class='small'>{esc(meta.get('citation'))}<div class='kv'>{esc(meta.get('chunk_id'))}</div></td>"
                f"<td><span class='badge {status_cls}'>{esc(meta.get('status'))}</span></td>"
                f"<td>{fmt(rec.get('score'))}</td>"
                f"<td class='small'>{esc(str(rec.get('content'))[:220])}<div class='kv'>章节：{esc(meta.get('section'))}</div></td>"
                f"<td>{attach_html or '<span class=\"muted small\">无</span>'}</td></tr>"
            )
        html_parts.append("</table></section>")
        return "".join(html_parts)

    if compare:
        strict = bridge_search(q, top_k=top_k, include_obsolete=False)
        loose = bridge_search(q, top_k=top_k, include_obsolete=True)
        strict_ids = {str((r.get("metadata") or {}).get("chunk_id")) for r in strict.get("records") or []}
        removed = [
            r for r in (loose.get("records") or [])
            if str((r.get("metadata") or {}).get("chunk_id")) not in strict_ids
        ]
        parts.append(
            "<section><h2>版本硬过滤效果<span class='note'>同一问题，两次检索</span></h2>"
            f"<div class='kv'>被硬过滤挡下的片段：<strong>{len(removed)}</strong> 条"
            + ("（若无差异，说明该问题没有命中作废版本）" if not removed else "")
            + "</div>"
        )
        if removed:
            parts.append("<table><tr><th>被挡下的片段</th><th>状态</th><th>分数</th><th>内容</th></tr>")
            for rec in removed:
                meta = rec.get("metadata") or {}
                parts.append(
                    f"<tr><td class='small'>{esc(meta.get('citation'))}</td>"
                    f"<td><span class='badge b-p0'>{esc(meta.get('status'))}</span></td>"
                    f"<td>{fmt(rec.get('score'))}</td><td class='small'>{esc(str(rec.get('content'))[:200])}</td></tr>"
                )
            parts.append("</table>")
        parts.append("</section>")
        parts.append(record_table(strict, "① 硬过滤后的检索结果", "status = 现行有效"))
        parts.append(record_table(loose, "② 放宽过滤后的检索结果", "演示：包含作废版本"))
    else:
        result = bridge_search(q, top_k=top_k, include_obsolete=obsolete)
        parts.append(record_table(result, "检索结果", "统一片段 + 引用契约"))
    return "".join(parts)


def render_assets(data: Dict[str, Any], query: Dict[str, List[str]]) -> str:
    """资产库页面：图片与视频的登记、检索与引用附件展示（只检索、不理解）。"""
    keyword = (query.get("keyword") or [""])[0].strip()
    asset_type = (query.get("type") or [""])[0]
    doc_no = (query.get("doc_no") or [""])[0]
    tag = (query.get("tag") or [""])[0].strip()
    assets: AssetIndex = data.get("assets") or AssetIndex()
    stats = data.get("asset_stats") or {}

    results = assets.search(keyword=keyword, asset_type=asset_type, doc_no=doc_no, tags=[tag] if tag else None)

    parts: List[str] = []
    parts.append(
        "<section><h2>资产库<span class='note'>只登记、只检索、不理解：不 OCR、不抽帧、不转写</span></h2>"
        f"<div class='kv'>资产 {esc(stats.get('total'))} 个（图片 {esc(stats.get('images'))} · 视频 {esc(stats.get('videos'))}）· "
        f"有图注 {esc(stats.get('with_caption'))} · 有标签 {esc(stats.get('with_tags'))} · "
        f"标注覆盖率 {esc(stats.get('coverage'))}</div>"
        "<div class='kv'>可检索维度：所属资料、所属章节、页码、图注标题、文件名、人工标签（对应指标 T4 / T6）</div></section>"
    )

    docs = sorted({a.doc_no for a in assets.assets})
    parts.append('<form class="toolbar" method="get" action="/assets">')
    parts.append(f'<input type="text" name="keyword" value="{esc(keyword)}" size="26" placeholder="关键词：图注 / 名称 / 标签 / 文件名">')
    parts.append(f'<select name="type">{_options(["", "image", "video"], asset_type, {"": "全部类型", "image": "图片", "video": "视频"})}</select>')
    parts.append(f'<select name="doc_no">{_options([""] + docs, doc_no, {"": "全部资料"})}</select>')
    parts.append(f'<input type="text" name="tag" value="{esc(tag)}" size="12" placeholder="标签">')
    parts.append('<button class="btn" type="submit">检索</button>')
    parts.append(f'<span class="small muted">命中 {len(results)} 个</span>')
    parts.append("</form>")

    if data.get("asset_unregistered"):
        items = "、".join(esc(x) for x in (data.get("asset_unregistered") or [])[:8])
        parts.append(f"<section><h2>待登记资产<span class='note'>目录中存在但清单未登记，无法被检索</span></h2><p class='small warn'>{items}</p></section>")

    parts.append("<section><h2>资产清单</h2>")
    if not results:
        parts.append('<p class="muted">没有符合条件的资产。</p></section>')
        return "".join(parts)
    parts.append('<div class="kpis">')
    for asset in results:
        thumb = f"/asset-file?path={urllib.parse.quote(asset.thumb)}" if asset.thumb else ""
        img = (
            f'<img src="{thumb}" alt="{esc(asset.name)}" style="width:100%;border-radius:3px;border:1px solid var(--line)">'
            if thumb else '<div class="muted small" style="padding:24px 0;text-align:center">视频资产（演示占位文件，无缩略图）</div>'
        )
        parts.append(
            f'<div class="kpi" style="min-width:280px;max-width:320px">'
            f"{img}"
            f"<div style='margin-top:8px'><strong>{esc(asset.name)}</strong></div>"
            f"<div class='kv'>{esc(asset.figure_no)} · {esc(asset.caption)}</div>"
            f"<div class='kv'>所属：{esc(asset.doc_no)} {esc(asset.doc_version)} · {esc(asset.section)}"
            + (f" · P{esc(asset.page)}" if asset.page else "") + "</div>"
            f"<div class='kv'>挂载片段：{esc(asset.chunk_id) or '（未挂载）'}</div>"
            f"<div class='kv'>标签：{'、'.join(esc(t) for t in asset.tags) or '（无）'}</div>"
            f"<div class='kv small'>{esc(asset.asset_id)} · {esc(asset.suffix)} · {esc(asset.size)} 字节 · 指纹 {esc(asset.fingerprint)}</div>"
            "</div>"
        )
    parts.append("</div></section>")
    return "".join(parts)


def render_dbquery(data: Dict[str, Any], query: Dict[str, List[str]]) -> str:
    """数据库交互查询页面：自然语言 → 白名单模板 → 只读执行。"""
    q = (query.get("q") or [""])[0].strip()
    parts: List[str] = []
    exists = bool(data.get("business_db_exists"))
    parts.append(
        "<section><h2>台账 / 履历查询<span class='note'>确定性模板 + 参数绑定 + 只读账号</span></h2>"
        f"<div class='kv'>业务库：{esc(data.get('business_db'))}"
        + ("（已就绪）" if exists else "（不存在，先运行 <code>python tools/make_demo_business_db.py</code>）")
        + "</div>"
        "<div class='kv'>安全约束：只读连接（mode=ro + PRAGMA query_only）、仅执行白名单模板、参数化绑定、不接受任意 SQL</div></section>"
    )
    parts.append('<form class="toolbar" method="get" action="/dbquery">')
    parts.append(f'<input type="text" name="q" value="{esc(q)}" size="46" placeholder="例如：EQ-1002 的履历 / 近三个月检修记录 / 哪些装备保养要到期了">')
    parts.append('<button class="btn" type="submit">查询</button>')
    for item in db_query.available_intents():
        example = item["example"]
        parts.append(f'<a class="btn ghost small" href="/dbquery?q={urllib.parse.quote(example)}">{esc(example)}</a>')
    parts.append("</form>")

    if not q:
        parts.append("<section><p class='muted'>输入问题或点击上方示例。查询结果会同时给出命中的模板、参数与解释，便于核对。</p></section>")
        return "".join(parts)

    result = db_query.query(q, BUSINESS_DB)
    parts.append("<section><h2>查询结果</h2>")
    parts.append(
        f"<div class='kv'>命中模板：<strong>{esc(result.label)}</strong>（{esc(result.intent)}）· 结果 {result.count} 行</div>"
        f"<div class='kv'>解释：{esc(result.explain)}</div>"
    )
    if result.refused:
        parts.append(f"<p class='warn small'>已拒绝执行：{esc(result.message)}</p>")
    elif result.message:
        parts.append(f"<p class='muted small'>{esc(result.message)}</p>")
    if result.rows:
        parts.append(_rows_table(result.columns, result.rows))
    parts.append(
        "<details><summary>查看实际执行的 SQL 与参数</summary>"
        f"<pre>{esc(result.sql)}</pre><pre>params = {esc(list(result.params))}</pre></details>"
    )
    parts.append("</section>")
    return "".join(parts)


def _options(values: Sequence[str], current: str, labels: Optional[Dict[str, str]] = None) -> str:
    """生成 select 选项。"""
    labels = labels or {}
    out = []
    for value in values:
        selected = " selected" if value == current else ""
        out.append(f'<option value="{esc(value)}"{selected}>{esc(labels.get(value, value) or "全部")}</option>')
    return "".join(out)


def render_docs(data: Dict[str, Any]) -> str:
    """资料清单：条款数、探针数、缺陷数。"""
    corpus = data["corpus"]
    probes: Dict[str, int] = {}
    defects: Dict[str, int] = {}
    for row in data["rows"]:
        doc_no = str(row.get("doc_no") or "").strip()
        if doc_no:
            probes[doc_no] = probes.get(doc_no, 0) + 1
    for d in data["doc_defects"]:
        doc_no = str(d.get("doc_no") or "")
        defects[doc_no] = defects.get(doc_no, 0) + 1

    parts = ["<section><h2>资料清单<span class='note'>条款数来自 samples/ 语料，版本以台账为准</span></h2>"]
    parts.append("<table><tr><th>资料编号</th><th>名称</th><th>版本</th><th>状态</th><th>条款数</th>"
                 "<th>探针数</th><th>文档缺陷</th><th>台账现行版本</th></tr>")
    for doc_no in sorted(corpus.docs):
        info = corpus.docs[doc_no]
        current = corpus.current_version(doc_no) or "（未登记）"
        flag = "" if current == info.version or current == "（未登记）" else ' <span class="warn">版本不一致</span>'
        parts.append(
            f'<tr><td><a class="doc" href="/docs/{urllib.parse.quote(doc_no)}">{esc(doc_no)}</a></td>'
            f"<td>{esc(info.title)}</td><td>{esc(info.version)}{flag}</td><td>{esc(info.status)}</td>"
            f"<td>{info.clauses}</td><td>{probes.get(doc_no, 0)}</td><td>{defects.get(doc_no, 0)}</td>"
            f"<td>{esc(current)}</td></tr>"
        )
    parts.append("</table></section>")
    return "".join(parts)


def render_doc_detail(data: Dict[str, Any], doc_no: str) -> str:
    """单份资料详情。"""
    corpus = data["corpus"]
    info = corpus.docs.get(doc_no)
    parts: List[str] = [f'<div class="toolbar"><a class="btn ghost" href="/docs">← 返回资料清单</a></div>']
    if not info:
        parts.append(f'<section><p class="muted">语料目录中没有找到 {esc(doc_no)}。</p></section>')
        return "".join(parts)

    rows = [r for r in data["rows"] if str(r.get("doc_no") or "") == doc_no]
    defects = [d for d in data["doc_defects"] if str(d.get("doc_no")) == doc_no]
    current = corpus.current_version(doc_no) or "（未登记）"

    parts.append("<section><h2>资料概况</h2><table>")
    parts.append(f"<tr><th>资料编号</th><td>{esc(doc_no)}</td><th>名称</th><td>{esc(info.title)}</td></tr>")
    parts.append(f"<tr><th>版本</th><td>{esc(info.version)}</td><th>台账现行版本</th><td>{esc(current)}</td></tr>")
    parts.append(f"<tr><th>状态</th><td>{esc(info.status)}</td><th>条款数</th><td>{info.clauses}</td></tr>")
    parts.append(f"<tr><th>探针数</th><td>{len(rows)}</td><th>文档缺陷</th><td>{len(defects)}</td></tr>")
    parts.append("</table></section>")

    parts.append("<section><h2>该资料的缺陷</h2>")
    if not defects:
        parts.append('<p class="muted">未发现文档侧缺陷（也可能样本不足）。</p>')
    else:
        parts.append("<table><tr><th>严重度</th><th>类型</th><th>次数</th><th>证据问题</th><th>整改动作</th><th>状态</th></tr>")
        for d in defects:
            parts.append(
                f"<tr><td>{sev_badge(d.get('severity'))}</td>"
                f"<td>{esc(d.get('defect_label') or '')}</td><td>{esc(d.get('occurrences', 1))}</td>"
                f"<td class='small'>{esc(str(d.get('question') or '')[:80])}</td>"
                f"<td class='small'>{esc(d.get('action') or '—')}</td><td>{status_badge(str(d.get('status')))}</td></tr>"
            )
        parts.append("</table>")
    parts.append("</section>")

    parts.append("<section><h2>相关问答</h2>")
    if not rows:
        parts.append('<p class="muted">没有命中该资料的问答记录。</p>')
    else:
        parts.append("<table><tr><th>问题</th><th>CSI</th><th>FA</th><th>KC</th><th>归因</th></tr>")
        for r in rows:
            parts.append(
                f'<tr><td class="small">{esc(str(r.get("question") or "")[:80])}</td>'
                f'<td>{fmt(r.get("csi"))}</td><td>{fmt(r.get("factual_accuracy"), 0)}</td>'
                f'<td>{fmt(r.get("knowledge_completeness"), 0)}</td>'
                f'<td class="small">{esc(BLAME_LABEL.get(str(r.get("blame")), "—"))}</td></tr>'
            )
        parts.append("</table>")
    parts.append("</section>")
    return "".join(parts)


# ---------------------------------------------------------------- 动作


def action_attribute(store: Store, samples_dir: Path) -> str:
    """重跑归因前置并刷新缺陷表。"""
    corpus = load_corpus(samples_dir)
    rows = store.fetch_evaluations()
    if not rows:
        return "没有可归因的评价记录，请先运行 judge。"
    resources = store.fetch_resources_map()
    results = [
        attribute_evaluation(row, resources.get(str(row.get("message_id"))), corpus)
        for row in rows
    ]
    store.update_attribution(results)
    items = [
        build_defect(item, row, DEFECT_ACTION)
        for item, row in zip(results, rows)
        if item.get("blame") in ("doc", "corpus")
    ]
    n = store.replace_defects(items)
    doc_n = sum(1 for i in items if i.get("blame") == "doc")
    return f"归因完成：评价 {len(rows)} 条；文档缺陷 {doc_n} 条、覆盖缺口 {len(items) - doc_n} 条（去重后共 {n} 条）"


def action_scorecard(store: Store, samples_dir: Path, out_dir: Path) -> str:
    """重建记分卡产物（Markdown / CSV）。"""
    corpus = load_corpus(samples_dir)
    rows = store.fetch_evaluations()
    defects = store.fetch_defects()
    result = build_scorecard(rows, defects, corpus, out_dir, formats=("md", "csv"))
    paths = "、".join(str(p) for p in result["paths"].values())
    return f"记分卡已重建：{len(result['rows'])} 份资料；产物 {paths}"


# ---------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    """治理看板 HTTP 处理器。"""

    server_version = "DifyGovDashboard/1.0"
    data: Dict[str, Any] = {}
    #: SQLite 连接不能跨线程复用（sqlite3 默认 check_same_thread=True），
    #: 因此这里只保存库路径，每个请求各自开一个短连接，读完即关。
    db_path: Optional[Path] = None
    samples_dir: Path = BASE_DIR.parent / "samples"
    out_dir: Path = BASE_DIR / "out"
    lock = threading.Lock()

    # ------------------------------------------------ 基础
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        """精简访问日志。"""
        print(f"[dashboard] {self.address_string()} {fmt % args}")

    def _send(self, body: str, status: int = 200) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, obj: Any, status: int = 200) -> None:
        payload = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _serve_asset(self, rel_path: str) -> None:
        """提供资产文件（缩略图/原图）下载；限制在资产目录内，防止目录穿越。"""
        import mimetypes

        target = (ASSETS_DIR / rel_path).resolve()
        root = ASSETS_DIR.resolve()
        if not str(target).startswith(str(root)) or not target.is_file():
            self._send(layout("未找到", "<section><p class='muted'>资产文件不存在。</p></section>", "", self._reload()), 404)
            return
        payload = target.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _redirect(self, path: str) -> None:
        self.send_response(303)
        self.send_header("Location", path)
        self.end_headers()

    def _with_store(self) -> Store:
        """打开一个短连接（调用方负责关闭）。"""
        store = Store(self.db_path)
        store.init_schema()
        return store

    def _reload(self) -> Dict[str, Any]:
        """重新读库（带锁 + 短连接）。"""
        with self.lock:
            store = self._with_store()
            try:
                self.data = DashboardData(store, self.samples_dir).load()
            finally:
                store.close()
        return self.data

    # ------------------------------------------------ 路由
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)
        flash = (query.get("flash") or [""])[0]
        data = self._reload()

        if path == "/":
            self._send(layout("概览", render_overview(data), "/", data, flash))
        elif path == "/defects":
            query = dict(query)
            query["_raw"] = [self.path]
            self._send(layout("缺陷整改单", render_defects(data, query), "/defects", data, flash))
        elif path == "/traces":
            limit = int((query.get("limit") or ["60"])[0] or 60)
            self._send(layout("归因明细", render_traces(data, limit), "/traces", data, flash))
        elif path == "/docs":
            self._send(layout("资料清单", render_docs(data), "/docs", data, flash))
        elif path.startswith("/docs/"):
            doc_no = urllib.parse.unquote(path[len("/docs/"):])
            self._send(layout(f"资料 {doc_no}", render_doc_detail(data, doc_no), "/docs", data, flash))
        elif path == "/bridge":
            query = dict(query)
            query["_raw"] = [self.path]
            self._send(layout("检索契约层", render_bridge(data, query), "/bridge", data, flash))
        elif path == "/assets":
            self._send(layout("资产库", render_assets(data, query), "/assets", data, flash))
        elif path == "/dbquery":
            self._send(layout("台账查询", render_dbquery(data, query), "/dbquery", data, flash))
        elif path == "/asset-file":
            self._serve_asset((query.get("path") or [""])[0])
        elif path == "/api/bridge":
            q = (query.get("q") or [""])[0]
            top_k = int((query.get("top_k") or ["6"])[0] or 6)
            include_obsolete = (query.get("include_obsolete") or ["0"])[0] in ("1", "true", "yes")
            self._send_json(bridge_search(q, top_k=top_k, include_obsolete=include_obsolete) if q else
                            {"error": "缺少 q 参数"})
        elif path == "/api/assets":
            assets: AssetIndex = data.get("assets") or AssetIndex()
            self._send_json({
                "stats": data.get("asset_stats"),
                "assets": [a.to_dict() for a in assets.search(
                    keyword=(query.get("keyword") or [""])[0],
                    asset_type=(query.get("type") or [""])[0],
                    doc_no=(query.get("doc_no") or [""])[0],
                )],
            })
        elif path == "/api/dbquery":
            q = (query.get("q") or [""])[0]
            self._send_json(db_query.query(q, BUSINESS_DB).to_dict() if q else {"error": "缺少 q 参数"})
        elif path == "/api/summary":
            self._send_json(self._summary(data))
        elif path == "/api/scorecard":
            self._send_json(data["scorecard"])
        elif path == "/api/defects":
            self._send_json(data["defects"])
        elif path == "/api/traces":
            self._send_json([
                {
                    "message_id": r.get("message_id"),
                    "question": r.get("question"),
                    "csi": r.get("csi"),
                    "doc_no": r.get("doc_no"),
                    "blame": r.get("blame"),
                    "defect_type": r.get("defect_type"),
                }
                for r in data["traces"]
            ])
        elif path == "/actions/attribute":
            store = self._with_store()
            try:
                msg = action_attribute(store, self.samples_dir)
            finally:
                store.close()
            self._redirect("/?flash=" + urllib.parse.quote(msg))
        elif path == "/actions/scorecard":
            store = self._with_store()
            try:
                msg = action_scorecard(store, self.samples_dir, self.out_dir)
            finally:
                store.close()
            self._redirect("/?flash=" + urllib.parse.quote(msg))
        else:
            self._send(layout("未找到", '<section><p class="muted">页面不存在。</p></section>', "", data), 404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8")
        form = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

        if parsed.path.rstrip("/") == "/defects/update":
            defect_id = form.get("defect_id") or ""
            status = form.get("status") or "open"
            store = self._with_store()
            try:
                ok = store.update_defect_status(
                    defect_id,
                    status,
                    owner=form.get("owner") or "",
                    due_date=form.get("due_date") or "",
                )
            finally:
                store.close()
            msg = f"缺陷 {defect_id} 状态已更新为「{STATUS_LABEL.get(status, status)}」" if ok else f"未找到缺陷 {defect_id}"
            back = form.get("back") or "/defects"
            sep = "&" if "?" in back else "?"
            self._redirect(f"{back}{sep}flash=" + urllib.parse.quote(msg))
        else:
            self._send(layout("未找到", '<section><p class="muted">页面不存在。</p></section>', "", self._reload()), 404)

    # ------------------------------------------------ 摘要
    @staticmethod
    def _summary(data: Dict[str, Any]) -> Dict[str, Any]:
        """JSON 摘要（对接外部报表/监控）。"""
        agg = data["agg"]
        return {
            "generated_at": data["generated_at"],
            "evaluations": len(data["rows"]),
            "csi_mean": (agg.get("csi") or {}).get("mean"),
            "pass_rate_35": agg.get("pass_rate_35"),
            "badcase_count": agg.get("badcase_count"),
            "defects": {
                "doc": len(data["doc_defects"]),
                "corpus": len(data["corpus_defects"]),
                "system": len(data["system_defects"]),
            },
            "closure_rate": data["closure_rate"],
            "unattributed_probes": data["unattributed"],
            "scorecard": data["scorecard"],
        }


def run_dashboard(
    cfg: Config,
    db_path: Path,
    samples_dir: Path,
    out_dir: Path,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    open_browser: bool = False,
) -> int:
    """启动治理看板。

    :param cfg: 配置（用于打印摘要）
    :param db_path: SQLite 数据库路径（每个请求开短连接读取）
    :param samples_dir: 语料目录
    :param out_dir: 产物目录
    :param host: 监听地址（默认仅本机）
    :param port: 监听端口
    :param open_browser: 是否自动打开浏览器
    :return: 进程退出码
    """
    Handler.db_path = Path(db_path)
    Handler.samples_dir = Path(samples_dir)
    Handler.out_dir = Path(out_dir)
    warm = Store(Handler.db_path)
    warm.init_schema()
    try:
        Handler.data = DashboardData(warm, Path(samples_dir)).load()
    finally:
        warm.close()

    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}/"
    print("=" * 68)
    print("文档质量治理看板（Windows 本地版）已启动")
    print(f"  访问地址 : {url}")
    print(f"  数据库   : {cfg.db_path}")
    print(f"  语料目录 : {samples_dir}")
    print(f"  产物目录 : {out_dir}")
    print("  提示     : 按 Ctrl+C 停止；仅监听本机时局域网不可访问")
    print("=" * 68)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[dashboard] 已停止")
    finally:
        httpd.server_close()
    return 0
